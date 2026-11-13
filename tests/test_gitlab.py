"""GitLab HTTP-boundary tests; actual-app E2E is an independent release gate."""

import asyncio
import base64
import copy
import hashlib
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

import app.gitlab as provider
from app.connector_errors import ConnectorError
from app.gitlab import GitLab, normalize_base_url
from app.gitlab_graphql import REQUEST_CHANGES, REVIEW_CAPABILITY
from app.gitlab_schemas import (
    GitLabCredentials,
    GitLabMergeRequest,
    GitLabRepositoryFile,
)
from app.gitlab_transport import GitLabTransport

# Deliberately synthetic values, never real credentials.
TOKEN = 'unit-test-private-token'
BODY_SECRET = 'unit-test-sensitive-provider-body'


def credentials(**updates):
    return {
        'kind': 'personal', 'access_token': TOKEN, 'base_url': 'https://gitlab.com',
        'repository_ids': [17],
        'repositories': [{'id': 17, 'full_name': 'group/subgroup/repo'}],
        **updates,
    }


@pytest.fixture
def gitlab():
    saved = credentials()
    store = SimpleNamespace(rows=lambda _query, **_kwargs: [{'encrypted': json.dumps(saved)}])
    return GitLab(store, SimpleNamespace(decrypt=lambda text: text), SimpleNamespace(), None)


@pytest.fixture
def boundary(monkeypatch):
    requests = []

    def install(handler):
        def handle(request):
            requests.append(request)
            return handler(request)
        monkeypatch.setattr(provider, 'GitLabTransport', lambda **_kwargs: httpx.MockTransport(handle))
        return requests

    return install


def project(identity=17, **updates):
    return {
        'id': identity, 'path_with_namespace': f'group/subgroup/repo{identity}',
        'default_branch': 'main', 'visibility': 'private', 'archived': False,
        'permissions': {'project_access': None, 'group_access': {'access_level': 40}},
        **updates,
    }


def setup_handler(overrides=None):
    replies = {
        '/api/v4/user': {'id': 9, 'username': 'test-user', 'state': 'active', 'locked': False},
        '/api/v4/personal_access_tokens/self': {
            'id': 3, 'user_id': 9, 'scopes': ['api'], 'active': True, 'revoked': False,
        },
        '/api/v4/projects/17': project(),
        '/api/v4/projects/18': project(18),
        **(overrides or {}),
    }

    def handle(request):
        value = replies[request.url.path]
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    return handle


async def test_auth_is_header_only_and_mr_creation_is_exact(gitlab, boundary, monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:3210')
    payload = {
        'source_branch': 'moyai/feature', 'target_branch': 'main',
        'title': 'Implement feature', 'description': 'Ready for review.',
        'remove_source_branch': False,
    }
    response = {
        'id': 1800, 'iid': 23, 'project_id': 17, 'source_project_id': 17,
        'target_project_id': 17, 'source_branch': 'moyai/feature', 'target_branch': 'main',
        'title': 'Implement feature', 'description': 'Ready for review.',
        'state': 'opened', 'draft': False,
    }
    calls = boundary(lambda _request: httpx.Response(201, json=response))
    actual = await gitlab.request(
        'POST', '/projects/17/merge_requests', token=TOKEN,
        base_url='https://GitLab.Example.com:8443/', json=payload,
    )
    assert actual == response
    assert len(calls) == 1
    request = calls[0]
    assert request.method == 'POST'
    assert str(request.url) == 'https://gitlab.example.com:8443/api/v4/projects/17/merge_requests'
    assert request.headers['PRIVATE-TOKEN'] == TOKEN
    assert 'authorization' not in request.headers
    assert TOKEN not in str(request.url) and TOKEN.encode() not in request.content
    assert request.headers['accept-encoding'] == 'identity'
    assert json.loads(request.content) == payload
    parsed = GitLabMergeRequest.model_validate(actual)
    parsed.check_identity(project_id=17, iid=23)
    assert parsed.iid != parsed.id
    with pytest.raises(ValueError, match='identity'):
        parsed.check_identity(project_id=17, iid=1800)
    with pytest.raises(ValueError, match='identity'):
        parsed.check_identity(project_id=18)


@pytest.mark.parametrize('path', [
    'https://evil.example/api/v4/user', '//evil.example/user', '/api/v4/projects/17',
    '/projects', '/search', '/groups/3/projects', '/users', '/projects/group%2Frepo',
    '/projects/0', '/projects/-1', '/projects/017', '/projects/17?private_token=x',
    '/projects/17#fragment', '/projects/17/../18', '/projects/17/%2e%2e/18',
    '/projects/17/%252e%252e%252f18', '/projects/17//repository',
    '/projects/17/%2Frepository', '/projects/17/%00', '/projects/17/%0a',
    '/projects/17/%255c..', '/projects/17/\\evil', '/projects/17/%ZZ',
    '/projects/17/%ff', '/projects/17/repository/ file',
])
async def test_unsafe_or_unscoped_paths_never_dispatch(path, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Unsafe path reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', path, token=TOKEN)
    assert exc.value.dispatched is False
    assert calls == []


@pytest.mark.parametrize('origin', [
    'http://gitlab.com', 'https://user:password@gitlab.com', 'https://gitlab.com@evil.example',
    'https://gitlab.com/api/v4', 'https://gitlab.com?x=1', 'https://gitlab.com#frag',
    'https://gitlab.com?', 'https://gitlab.com:bad', 'https://gitlab.com:0',
    'https://127.0.0.1', 'https://10.0.0.1', 'https://169.254.169.254',
    'https://[::1]', 'https://[::ffff:127.0.0.1]', 'https://[ff02::1]',
    'https://localhost', 'http://localhost', 'http://127.0.0.1:8929',
    'https://gitlab.com\\@evil.example', 'https://gitlab.com\n', ' https://gitlab.com',
    'https://gitlab.com.', 'https://%67itlab.com',
])
async def test_invalid_origin_never_sends_token(origin, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Unsafe origin reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/user', token=TOKEN, base_url=origin)
    assert exc.value.code == 'gitlab_invalid_origin'
    assert exc.value.dispatched is False
    assert calls == []


def test_normalized_origin_and_explicit_loopback_option(gitlab):
    assert normalize_base_url('https://GITLAB.COM:443/') == 'https://gitlab.com'
    assert normalize_base_url('http://127.0.0.1:8929/', allow_loopback_http=True) == 'http://127.0.0.1:8929'
    assert normalize_base_url('http://[::1]:8929', allow_loopback_http=True) == 'http://[::1]:8929'
    gitlab.settings.gitlab_allow_loopback_http = True
    assert gitlab.normalize_base_url('http://127.0.0.1:8929/') == 'http://127.0.0.1:8929'
    with pytest.raises(ConnectorError):
        normalize_base_url('http://10.0.0.1', allow_loopback_http=True)
    with pytest.raises(ConnectorError):
        normalize_base_url('http://localhost:8929', allow_loopback_http=True)


@pytest.mark.parametrize('token', ['', '\nsecret', 'contains space', 'invalid\u00e9', 'a' * 4097, None])
async def test_bad_tokens_rejected_without_disclosing_input(token, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid token reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/user', token=token)
    assert exc.value.dispatched is False
    assert calls == []


@pytest.mark.parametrize('arguments', [
    {'params': {'private_token': TOKEN}}, {'params': {'access_token': TOKEN}},
    {'params': {'sudo': 9}}, {'params': {'SUDO[]': [9]}},
    {'params': {'sudo[user]': 9}},
    {'json': {'_method': 'DELETE'}}, {'json': {'authorization': TOKEN}},
    {'json': [1]}, {'params': 'private_token=secret'}, {'json': {'value': float('nan')}},
    {'json': {'value': object()}},
])
async def test_auth_override_and_invalid_body_never_dispatch(arguments, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid arguments reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/projects/17/merge_requests', token=TOKEN, **arguments)
    assert exc.value.dispatched is False
    assert calls == []


@pytest.mark.parametrize('text', [
    '/merge', 'A comment\n/approve', 'A comment\r\n   /merge', '\t/close',
    'A comment\r/label ~secret', '\u2003/approve', '```\n/merge\n```',
])
@pytest.mark.parametrize('field', ['body', 'note', 'description'])
async def test_quick_actions_rejected_before_write(text, field, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Quick action reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request(
            'POST', '/projects/17/merge_requests/1/notes', token=TOKEN,
            json={field: text},
        )
    assert exc.value.code == 'gitlab_quick_action'
    assert exc.value.dispatched is False
    assert calls == []


async def test_quick_actions_in_nested_write_body_and_query_are_rejected(gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Quick action reached transport'))
    for arguments in (
        {'json': {'notes': [{'body': '/merge'}]}},
        {'json': {'notes': ({'body': '/merge'},)}},
        {'params': {'body': '/approve'}},
        {'params': {'body': ['/merge']}},
        {'params': {'body[]': ['/merge']}},
    ):
        with pytest.raises(ConnectorError, match='quick actions'):
            await gitlab.request('POST', '/projects/17/merge_requests/1/notes', token=TOKEN, **arguments)
    assert not calls


async def test_normal_note_body_preserved(gitlab, boundary):
    body = 'Path /src/file is valid.\nSee https://example.test/path for context.'
    calls = boundary(lambda _request: httpx.Response(201, json={'id': 5, 'body': body}))
    await gitlab.request('POST', '/projects/17/merge_requests/1/notes', token=TOKEN, json={'body': body})
    assert json.loads(calls[0].content)['body'] == body


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
async def test_redirects_never_follow_or_replay(status, gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(
        status, headers={'Location': 'https://evil.example/steal?token=' + TOKEN},
        text=BODY_SECRET,
    ))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/projects/17/merge_requests', token=TOKEN, json={'title': 'Title'})
    assert len(calls) == 1
    assert exc.value.code == 'gitlab_redirect_refused'
    assert exc.value.receipt(write=True)['outcome_uncertain'] is True
    assert TOKEN not in str(exc.value) and BODY_SECRET not in str(exc.value)


@pytest.mark.parametrize('status,rejected,uncertain', [
    (401, True, False), (403, True, False), (404, True, False),
    (408, False, True), (409, True, False), (422, True, False),
    (429, True, False), (500, False, True), (503, False, True),
])
async def test_safe_http_errors_preserve_effect_outcomes(status, rejected, uncertain, gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(
        status, json={'message': TOKEN + BODY_SECRET}, headers={'Retry-After': '12'},
    ))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/projects/17/merge_requests', token=TOKEN, json={'title': 'Title'})
    error = exc.value
    assert error.status_code == status and error.rejected is rejected
    assert error.retry_after == 12
    assert error.receipt(write=True)['outcome_uncertain'] is uncertain
    assert TOKEN not in str(error) and BODY_SECRET not in str(error)
    assert len(calls) == 1


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '999999999999999999999999', BODY_SECRET])
async def test_untrusted_retry_after_is_not_exposed(value, gitlab, boundary):
    boundary(lambda _request: httpx.Response(429, headers={'Retry-After': value}))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/projects/17', token=TOKEN)
    assert exc.value.retry_after is None
    assert value not in str(exc.value)


@pytest.mark.parametrize('failure,dispatched', [
    (httpx.ConnectError, False), (httpx.ConnectTimeout, False), (httpx.PoolTimeout, False),
    (httpx.ReadTimeout, True), (httpx.WriteError, True), (httpx.RemoteProtocolError, True),
])
async def test_transport_errors_are_safe_and_never_retried(failure, dispatched, gitlab, boundary):
    def fail(request):
        raise failure(TOKEN + BODY_SECRET, request=request)

    calls = boundary(fail)
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/projects/17/merge_requests', token=TOKEN, json={'title': 'Title'})
    assert exc.value.dispatched is dispatched
    assert exc.value.receipt(write=True)['outcome_uncertain'] is dispatched
    assert TOKEN not in str(exc.value) and BODY_SECRET not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
    assert len(calls) == 1


async def test_missing_is_read_only_and_does_not_hide_auth_errors(gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(404, text=BODY_SECRET))
    assert await gitlab.request('GET', '/projects/17', token=TOKEN, missing=True) is None
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/projects/17/merge_requests', token=TOKEN, missing=True)
    assert exc.value.dispatched is False and len(calls) == 1
    boundary(lambda _request: httpx.Response(403, text=BODY_SECRET))
    with pytest.raises(ConnectorError):
        await gitlab.request('GET', '/projects/17', token=TOKEN, missing=True)


@pytest.mark.parametrize('response', [
    httpx.Response(200, text='<html>' + BODY_SECRET + '</html>'),
    httpx.Response(200, content=b'{"bad":', headers={'Content-Type': 'application/json'}),
    httpx.Response(200, content=b'{"value": NaN}', headers={'Content-Type': 'application/json'}),
    httpx.Response(200, content=b'{"value": 1e999}', headers={'Content-Type': 'application/json'}),
    httpx.Response(200, content=b'', headers={'Content-Type': 'application/json'}),
])
async def test_unexpected_or_invalid_json_is_not_a_success(response, gitlab, boundary):
    boundary(lambda _request: response)
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/projects/17', token=TOKEN)
    assert exc.value.code == 'response_unavailable'
    assert BODY_SECRET not in str(exc.value)


async def test_empty_success_and_raw_binary_are_distinct(gitlab, boundary):
    boundary(lambda _request: httpx.Response(204))
    assert await gitlab.request('DELETE', '/projects/17/merge_requests/1/draft_notes/2', token=TOKEN) == {}
    binary = b'\x00\xff\xfe\n'
    boundary(lambda _request: httpx.Response(200, content=binary, headers={'Content-Type': 'application/octet-stream'}))
    assert await gitlab.request('GET', '/projects/17/repository/files/a.bin/raw', token=TOKEN, raw=True) == binary
    boundary(lambda _request: httpx.Response(200, headers={'X-Gitlab-Size': '100'}))
    assert await gitlab.request('HEAD', '/projects/17/repository/files/a.bin', token=TOKEN) == {}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.read = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read += 1
            yield chunk

    async def aclose(self):
        self.closed = True


async def test_stream_size_limit_stops_early_and_closes(gitlab, boundary, monkeypatch):
    monkeypatch.setattr(provider, 'MAX_RESPONSE_BYTES', 100000)
    stream = Chunks([b'x' * 65536 for _ in range(10)])
    boundary(lambda _request: httpx.Response(200, stream=stream))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/projects/17/jobs/2/trace', token=TOKEN, raw=True)
    assert exc.value.code == 'gitlab_response_too_large'
    assert stream.read == 2 and stream.closed


async def test_large_declared_length_refused_before_read(gitlab, boundary):
    stream = Chunks([b'small'])
    boundary(lambda _request: httpx.Response(
        200, headers={'Content-Length': str(provider.MAX_RESPONSE_BYTES + 1)}, stream=stream,
    ))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/projects/17/jobs/2/trace', token=TOKEN, raw=True)
    assert exc.value.code == 'gitlab_response_too_large'
    assert stream.read == 0 and stream.closed


async def test_unexpected_compression_refused_before_read(gitlab, boundary):
    stream = Chunks([b'compressed'])
    boundary(lambda _request: httpx.Response(200, headers={'Content-Encoding': 'gzip'}, stream=stream))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/projects/17/jobs/2/trace', token=TOKEN, raw=True)
    assert exc.value.code == 'gitlab_invalid_response'
    assert stream.read == 0 and stream.closed


async def test_verify_selected_ids_and_canonical_safe_metadata(gitlab, boundary):
    selected = credentials(base_url='https://GitLab.Example.com:8443/', repository_ids=[17, 18, 17])
    selected['_authority'] = 'existing-authority'
    replies = {
        '/api/v4/projects/17': project(
            path_with_namespace='group/nested/repo',
            runners_token=BODY_SECRET, import_url='https://user:secret@elsewhere.test/repo',
            http_url_to_repo='https://user:secret@elsewhere.test/repo.git',
            web_url='https://elsewhere.test/steal',
        ),
    }
    calls = boundary(setup_handler(replies))
    label = await gitlab.verify(selected)
    assert label == 'group/nested/repo, group/subgroup/repo18'
    assert [request.url.path for request in calls] == [
        '/api/v4/user', '/api/v4/personal_access_tokens/self', '/api/v4/projects/17', '/api/v4/projects/18',
    ]
    assert all(request.url.host == 'gitlab.example.com' for request in calls)
    assert selected['base_url'] == 'https://gitlab.example.com:8443'
    assert selected['repository_ids'] == [17, 18]
    assert selected['user_id'] == 9 and selected['username'] == 'test-user'
    assert selected['_authority'] == 'existing-authority' and selected['access_token'] == TOKEN
    assert selected['repositories'][0] == {
        'id': 17, 'full_name': 'group/nested/repo', 'path_with_namespace': 'group/nested/repo',
        'web_url': 'https://gitlab.example.com:8443/group/nested/repo',
        'default_branch': 'main', 'visibility': 'private', 'archived': False, 'access_level': 40,
    }
    assert BODY_SECRET not in json.dumps(selected['repositories'])
    assert gitlab.target('group/nested/repo', selected) == 17


@pytest.mark.parametrize('replacement', [
    project(identity=18),
    project(permissions={'project_access': {'access_level': 20}, 'group_access': None}),
    project(permissions={'project_access': None, 'group_access': None}, visibility='public'),
    project(permissions={'project_access': {'access_level': True}}),
    project(permissions={}),
    project(archived=True),
    project(repository_access_level='disabled'),
    project(merge_requests_access_level='disabled'),
    project(merge_requests_enabled=False),
    project(path_with_namespace='group/../other'),
    project(path_with_namespace='group/repo\n/merge'),
])
async def test_verify_identity_permissions_fail_atomically(replacement, gitlab, boundary):
    selected = credentials(repository_ids=[18, 17])
    original = copy.deepcopy(selected)
    boundary(setup_handler({'/api/v4/projects/17': replacement}))
    with pytest.raises(ConnectorError):
        await gitlab.verify(selected)
    assert selected == original


@pytest.mark.parametrize('override', [
    {'/api/v4/user': {'id': 9, 'username': 'user', 'state': 'blocked'}},
    {'/api/v4/user': {'id': True, 'username': 'user', 'state': 'active'}},
    {'/api/v4/user': {'id': 9, 'username': 'user', 'state': 'active', 'locked': True}},
    {'/api/v4/personal_access_tokens/self': {'id': 3, 'user_id': 10, 'scopes': ['api'], 'active': True, 'revoked': False}},
    {'/api/v4/personal_access_tokens/self': {'id': 3, 'user_id': 9, 'scopes': ['read_api'], 'active': True, 'revoked': False}},
    {'/api/v4/personal_access_tokens/self': {'id': 3, 'user_id': 9, 'scopes': ['api'], 'active': True, 'revoked': True}},
    {'/api/v4/personal_access_tokens/self': {'id': 3, 'user_id': 9, 'scopes': ['api'], 'active': False, 'revoked': False}},
    {'/api/v4/personal_access_tokens/self': httpx.Response(404, text=BODY_SECRET)},
])
async def test_verify_requires_authenticated_active_write_scoped_identity(override, gitlab, boundary):
    selected = credentials()
    original = copy.deepcopy(selected)
    calls = boundary(setup_handler(override))
    with pytest.raises(ConnectorError):
        await gitlab.verify(selected)
    assert not any(request.url.path.startswith('/api/v4/projects/') for request in calls)
    assert selected == original


@pytest.mark.parametrize('ids', [[], ['17'], [True], [0], [-1], [2**63], list(range(1, 1002))])
async def test_invalid_selected_ids_fail_before_network(ids, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid selection reached transport'))
    with pytest.raises(ConnectorError):
        await gitlab.verify(credentials(repository_ids=ids))
    assert not calls


def test_saved_selected_ids_control_resolution_and_options(gitlab):
    assert gitlab.saved_credentials()['kind'] == 'personal'
    assert gitlab.repository_options() == [{'id': 17, 'full_name': 'group/subgroup/repo'}]
    assert gitlab.targets() == ['group/subgroup/repo']
    for target in (17, '17', 'group/subgroup/repo', 'GROUP/SUBGROUP/REPO'):
        assert gitlab.target(target) == 17
    for target in ('', None, True, 18, '18', 'other/repo', 'https://gitlab.com/group/subgroup/repo'):
        with pytest.raises(ConnectorError):
            gitlab.target(target)
    selected = credentials(repositories=[
        {'id': 17, 'full_name': 'group/subgroup/repo', 'access_token': BODY_SECRET},
        {'id': 18, 'full_name': 'other/repo'},
    ])
    assert gitlab.repository_options(selected) == [{'id': 17, 'full_name': 'group/subgroup/repo'}]
    with pytest.raises(ConnectorError):
        gitlab.target('other/repo', selected)
    gitlab.store.rows = lambda _query, **_kwargs: []
    assert gitlab.saved_credentials() == {} and gitlab.repository_options() == [] and gitlab.targets() == []


def test_ambiguous_names_do_not_choose_a_project(gitlab):
    selected = credentials(repository_ids=[17, 18], repositories=[
        {'id': 17, 'full_name': 'group/repo'}, {'id': 18, 'full_name': 'group/repo'},
    ])
    with pytest.raises(ConnectorError, match='ambiguous'):
        gitlab.target('group/repo', selected)
    assert gitlab.target(17, selected) == 17
    selected['repositories'] = [{'id': 17, 'full_name': 'group/a'}, {'id': 17, 'full_name': 'group/b'}]
    with pytest.raises(ConnectorError):
        gitlab.repository_options(selected)


def test_credential_validation_and_repr_do_not_include_secret():
    parsed = GitLabCredentials.model_validate(credentials())
    assert TOKEN not in repr(parsed)
    with pytest.raises(ValidationError) as exc:
        GitLabCredentials.model_validate(credentials(access_token=TOKEN + '\n'))
    assert TOKEN not in str(exc.value)


async def test_file_and_ref_encoding_reach_http_boundary_intact(gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(200, json={'ok': True}))
    path = gitlab.file_path('group/subgroup/repo', 'src/nested/a #?% ü.py')
    await gitlab.request('GET', path, token=TOKEN, params={'ref': 'feature/a & b'})
    assert calls[0].url.raw_path == (
        b'/api/v4/projects/17/repository/files/src%2Fnested%2Fa%20%23%3F%25%20%C3%BC.py'
        b'?ref=feature%2Fa+%26+b'
    )
    assert calls[0].url.params['ref'] == 'feature/a & b'
    with pytest.raises(ConnectorError):
        gitlab.project_path(17, '8/repository/tree')
    with pytest.raises(ConnectorError):
        gitlab.file_path(17, '../secret')


def file_payload(content):
    return {
        'file_path': 'src/file.txt', 'encoding': 'base64', 'content': base64.b64encode(content).decode(),
        'content_sha256': hashlib.sha256(content).hexdigest(),
        # Documented UTF-8 conversion means raw blob size can differ.
        'size': len(content) + 7, 'blob_id': 'a' * 40, 'commit_id': 'b' * 40,
        'last_commit_id': 'c' * 40, 'ref': 'main', 'execute_filemode': False,
    }


def test_file_base64_content_and_hash_integrity():
    binary = b'\x00\xff\xfe\n'
    record = file_payload(binary)
    assert GitLabRepositoryFile.model_validate(record).decoded_content(expected_path='src/file.txt') == binary
    with pytest.raises(ValueError, match='different file'):
        GitLabRepositoryFile.model_validate(record).decoded_content(expected_path='other.txt')
    for update in ({'content': 'bad!'}, {'content_sha256': '0' * 64}, {'content': 'AB=='}):
        with pytest.raises(ValueError):
            GitLabRepositoryFile.model_validate({**record, **update}).decoded_content()
    for update in ({'encoding': 'text'}, {'commit_id': 'b' * 41}):
        with pytest.raises(ValidationError):
            GitLabRepositoryFile.model_validate({**record, **update})


@pytest.mark.parametrize('headers,count,next_page', [
    ({'X-Next-Page': '2'}, 2, 2),
    ({'X-Next-Page': ''}, 2, None),
    ({}, 2, 2),
    ({}, 1, None),
    ({'Link': '<https://evil.example/search?private_token=bad>; rel="next"'}, 2, 2),
])
async def test_project_pagination_uses_numeric_continuation_only(headers, count, next_page, gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(200, json=[{'id': i} for i in range(count)], headers=headers))
    result = await gitlab.page(
        '/projects/17/search', token=TOKEN, params={'scope': 'blobs', 'search': 'needle'}, per_page=2,
    )
    assert result['next_page'] == next_page and len(result['items']) == count
    assert len(calls) == 1 and calls[0].url.path == '/api/v4/projects/17/search'
    assert dict(calls[0].url.params) == {'scope': 'blobs', 'search': 'needle', 'page': '1', 'per_page': '2'}


@pytest.mark.parametrize('next_page', ['1', '3', '-1', '2?token=secret', 'https://evil.example'])
async def test_invalid_continuation_does_not_claim_complete_coverage(next_page, gitlab, boundary):
    boundary(lambda _request: httpx.Response(200, json=[{}], headers={'X-Next-Page': next_page}))
    with pytest.raises(ConnectorError, match='continuation'):
        await gitlab.page('/projects/17/merge_requests', token=TOKEN, per_page=1)


@pytest.mark.parametrize('arguments', [
    {'page': 0}, {'page': True}, {'per_page': 101}, {'per_page': 0},
    {'params': {'pagination': 'keyset'}}, {'params': {'page': 2}},
])
async def test_invalid_page_input_never_dispatches(arguments, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid page reached transport'))
    with pytest.raises(ConnectorError):
        await gitlab.page('/projects/17/merge_requests', token=TOKEN, **arguments)
    assert not calls


def dns_records(*addresses):
    return [
        (socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))
        for address in addresses
    ]


async def test_dns_is_pinned_with_original_host_and_tls_name(monkeypatch):
    resolutions, requests = [], []

    async def resolve(host, port, **kwargs):
        resolutions.append((host, port))
        return dns_records('93.184.216.34')

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    transport = GitLabTransport()
    await transport.transport.aclose()

    def network(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 17})

    transport.transport = httpx.MockTransport(network)
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get('https://gitlab.example.com:8443/api/v4/projects/17', headers={'PRIVATE-TOKEN': TOKEN})
    assert response.status_code == 200
    assert resolutions == [('gitlab.example.com', 8443)]
    assert requests[0].url.host == '93.184.216.34'
    assert requests[0].url.port == 8443
    assert requests[0].headers['host'] == 'gitlab.example.com:8443'
    assert requests[0].extensions['sni_hostname'] == 'gitlab.example.com'
    assert requests[0].headers['PRIVATE-TOKEN'] == TOKEN


@pytest.mark.parametrize('addresses', [
    ('127.0.0.1',), ('10.0.0.1',), ('169.254.169.254',), ('::1',),
    ('93.184.216.34', '10.0.0.1'), ('::ffff:127.0.0.1',), ('ff02::1',), (),
])
async def test_dns_to_private_or_mixed_addresses_never_sends_token(addresses, monkeypatch):
    async def resolve(*args, **kwargs):
        return dns_records(*addresses)

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    transport = GitLabTransport()
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(lambda _request: pytest.fail('Forbidden address reached network'))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(ConnectorError) as exc:
            await client.get('https://gitlab.example.com/api/v4/user', headers={'PRIVATE-TOKEN': TOKEN})
    assert exc.value.dispatched is False and TOKEN not in str(exc.value)


async def test_local_http_opt_in_still_requires_loopback_dns(monkeypatch):
    async def resolve(*args, **kwargs):
        return dns_records('127.0.0.1')

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    transport = GitLabTransport(allow_loopback_http=True)
    await transport.transport.aclose()
    transport.transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={'id': 9}))
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get('http://127.0.0.1:8929/api/v4/user')
    assert response.status_code == 200


async def test_dns_errors_are_safe_no_dispatch(monkeypatch):
    async def resolve(*args, **kwargs):
        raise socket.gaierror(TOKEN + BODY_SECRET)

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    transport = GitLabTransport()
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        with pytest.raises(ConnectorError) as exc:
            await client.get('https://gitlab.example.com/api/v4/user')
    assert exc.value.dispatched is False and exc.value.retryable
    assert TOKEN not in str(exc.value) and BODY_SECRET not in str(exc.value)


def discovery_params(**updates):
    return {
        'membership': True, 'simple': True, 'page': 1, 'per_page': 100,
        'order_by': 'id', 'sort': 'asc', 'search': '', **updates,
    }


async def test_admin_discovery_contract_is_membership_scoped_and_bounded(gitlab, boundary):
    results = [{'id': 17, 'path_with_namespace': 'group/subgroup/repo'}]
    calls = boundary(lambda _request: httpx.Response(200, json=results))
    assert await gitlab.request(
        'GET', '/projects', token=TOKEN, params=discovery_params(page=3, search='group/repo'),
    ) == results
    assert calls[0].url.path == '/api/v4/projects'
    assert dict(calls[0].url.params) == {
        'membership': 'true', 'simple': 'true', 'page': '3', 'per_page': '100',
        'order_by': 'id', 'sort': 'asc', 'search': 'group/repo',
    }
    assert calls[0].headers['PRIVATE-TOKEN'] == TOKEN


@pytest.mark.parametrize('changes', [
    {'membership': False}, {'membership': 'true'}, {'simple': False}, {'simple': 1},
    {'page': 0}, {'page': 10001}, {'page': True}, {'per_page': 101},
    {'per_page': 0}, {'order_by': 'name'}, {'sort': 'desc'},
    {'search': 's' * 201}, {'search': ['one']}, {'owned': True}, {'sudo': 9},
])
async def test_admin_discovery_rejects_widened_or_invalid_queries(changes, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid discovery reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('GET', '/projects', token=TOKEN, params=discovery_params(**changes))
    assert exc.value.dispatched is False and not calls


@pytest.mark.parametrize('options', [
    {'method': 'POST'}, {'method': 'HEAD'}, {'raw': True}, {'missing': True},
    {'json': {'name': 'new project'}},
])
async def test_admin_discovery_never_accepts_writes_or_raw(options, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid discovery reached transport'))
    method = options.pop('method', 'GET')
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request(method, '/projects', token=TOKEN, params=discovery_params(), **options)
    assert exc.value.dispatched is False and not calls


@pytest.mark.parametrize('result', [{'projects': []}, list(range(101))])
async def test_discovery_rejects_invalid_page_responses(result, gitlab, boundary):
    boundary(lambda _request: httpx.Response(200, json=result))
    with pytest.raises(ConnectorError, match='invalid project discovery'):
        await gitlab.request('GET', '/projects', token=TOKEN, params=discovery_params())


def review_result(**mr_updates):
    return {'data': {'mergeRequestRequestChanges': {'errors': [], 'mergeRequest': {
        'id': 'gid://gitlab/MergeRequest/1800', 'iid': '23', 'projectId': 17,
        'sourceProjectId': 17, 'targetProjectId': 17, 'diffHeadSha': 'a' * 40,
        **mr_updates,
    }}}}


def review_payload(**updates):
    return {
        'query': REQUEST_CHANGES,
        'variables': {'projectPath': 'group/subgroup/repo', 'iid': '23'},
        **updates,
    }


async def test_fixed_graphql_request_changes_is_origin_and_project_bound(gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(200, json=review_result()))
    result = await gitlab.request('POST', '/graphql', token=TOKEN, json=review_payload())
    assert result == review_result()
    assert calls[0].url == 'https://gitlab.com/api/graphql'
    assert calls[0].headers['authorization'] == 'Bearer ' + TOKEN
    assert 'private-token' not in calls[0].headers
    assert TOKEN not in str(calls[0].url) and TOKEN.encode() not in calls[0].content
    assert json.loads(calls[0].content) == review_payload()


async def test_fixed_graphql_capability_is_typed_and_filters_unknown_output(gitlab, boundary):
    boundary(lambda _request: httpx.Response(200, json={
        'data': {'__type': {'fields': [
            {'name': 'mergeRequestRequestChanges', 'internal': BODY_SECRET},
        ]}, 'internal': BODY_SECRET},
        'extensions': {'internal': BODY_SECRET},
    }))
    assert await gitlab.request('POST', '/graphql', token=TOKEN, json={'query': REVIEW_CAPABILITY}) == {
        'data': {'__type': {'fields': [{'name': 'mergeRequestRequestChanges'}]}},
    }


def static_schema():
    return {'data': {'__schema': {
        'mutationType': {'name': 'Mutation'},
        'types': [
            {'name': 'Unrelated', 'kind': 'OBJECT', 'description': BODY_SECRET},
            {'name': 'Mutation', 'kind': 'OBJECT', 'fields': [
                {'name': 'mergeRequestRequestChanges', 'description': BODY_SECRET},
            ]},
        ],
    }}}


async def test_static_graphql_schema_is_projected_with_separate_input_bound(gitlab, boundary, monkeypatch):
    monkeypatch.setattr(provider, 'MAX_RESPONSE_BYTES', 10)
    monkeypatch.setattr(provider, 'MAX_SCHEMA_RESPONSE_BYTES', 2048)
    boundary(lambda _request: httpx.Response(200, json=static_schema()))
    result = await gitlab.request('POST', '/graphql', token=TOKEN, json={'query': REVIEW_CAPABILITY})
    assert result == {'data': {'__type': {'fields': [{'name': 'mergeRequestRequestChanges'}]}}}
    assert BODY_SECRET not in json.dumps(result)
    # The larger allowance never applies to a review write or a normal REST read.
    boundary(lambda _request: httpx.Response(200, json=review_result()))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json=review_payload())
    assert exc.value.code == 'gitlab_response_too_large'


async def test_static_graphql_schema_still_has_a_hard_size_limit(gitlab, boundary, monkeypatch):
    monkeypatch.setattr(provider, 'MAX_SCHEMA_RESPONSE_BYTES', 10)
    boundary(lambda _request: httpx.Response(200, json=static_schema()))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json={'query': REVIEW_CAPABILITY})
    assert exc.value.code == 'gitlab_response_too_large'


@pytest.mark.parametrize('types', [
    [],
    [{'name': 'Mutation', 'kind': 'SCALAR', 'fields': []}],
    [{'name': 'Mutation', 'kind': 'OBJECT', 'fields': []}] * 2,
    [{'name': 'Mutation', 'kind': 'OBJECT', 'fields': None}],
])
async def test_invalid_static_graphql_schema_does_not_report_capability(types, gitlab, boundary):
    result = static_schema()
    result['data']['__schema']['types'] = types
    boundary(lambda _request: httpx.Response(200, json=result))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json={'query': REVIEW_CAPABILITY})
    assert exc.value.code == 'gitlab_invalid_graphql_response'


async def test_null_static_graphql_schema_is_a_safe_failure(gitlab, boundary):
    boundary(lambda _request: httpx.Response(200, json={'data': {'__schema': None}}))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json={'query': REVIEW_CAPABILITY})
    assert exc.value.code == 'gitlab_invalid_graphql_response'


@pytest.mark.parametrize('payload', [
    {'query': 'mutation { mergeRequestAccept(input:{}){errors}}'},
    {'query': REVIEW_CAPABILITY, 'variables': {}},
    review_payload(operationName='another'),
    review_payload(query=REQUEST_CHANGES + ' query Other { currentUser { id } }'),
    review_payload(variables={'projectPath': 'other/repo', 'iid': '23'}),
    review_payload(variables={'projectPath': 'group/subgroup/repo', 'iid': 23}),
    review_payload(variables={'projectPath': 'group/subgroup/repo', 'iid': '023'}),
    review_payload(variables={'projectPath': 'group/subgroup/repo', 'iid': '23', 'sudo': 9}),
    review_payload(variables={'projectPath': 'group/../repo', 'iid': '23'}),
])
async def test_graphql_rejects_arbitrary_queries_and_unselected_targets(payload, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid GraphQL reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json=payload)
    assert exc.value.dispatched is False and not calls


@pytest.mark.parametrize('overrides', [
    {'token': 'different-token'}, {'base_url': 'https://other.gitlab.example'},
    {'params': {}}, {'params': {'sudo': 9}}, {'raw': True}, {'missing': True},
])
async def test_graphql_rejects_changed_credentials_or_transport_overrides(overrides, gitlab, boundary):
    calls = boundary(lambda _request: pytest.fail('Invalid GraphQL reached transport'))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', json=review_payload(), **{'token': TOKEN, **overrides})
    assert exc.value.dispatched is False and not calls


@pytest.mark.parametrize('updates', [
    {'iid': '24'}, {'projectId': 18}, {'sourceProjectId': 18}, {'targetProjectId': 18},
    {'projectId': True}, {'diffHeadSha': None}, {'id': 'gid://gitlab/Issue/1800'},
])
async def test_graphql_identity_mismatch_is_uncertain_not_acknowledged(updates, gitlab, boundary):
    boundary(lambda _request: httpx.Response(200, json=review_result(**updates)))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json=review_payload())
    assert exc.value.receipt(write=True)['outcome_uncertain'] is True


@pytest.mark.parametrize('result', [
    {'errors': [{'message': BODY_SECRET + TOKEN}]},
    {'data': {'mergeRequestRequestChanges': {'errors': [BODY_SECRET + TOKEN], 'mergeRequest': None}}},
    {'data': {'mergeRequestRequestChanges': None}},
    {'data': None},
])
async def test_graphql_errors_are_sanitized_and_never_retried(result, gitlab, boundary):
    calls = boundary(lambda _request: httpx.Response(200, json=result))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json=review_payload())
    assert exc.value.receipt(write=True)['outcome_uncertain'] is True
    assert TOKEN not in str(exc.value) and BODY_SECRET not in str(exc.value)
    assert len(calls) == 1


async def test_graphql_never_acknowledges_empty_http_success(gitlab, boundary):
    boundary(lambda _request: httpx.Response(204))
    with pytest.raises(ConnectorError) as exc:
        await gitlab.request('POST', '/graphql', token=TOKEN, json=review_payload())
    assert exc.value.receipt(write=True)['outcome_uncertain'] is True
