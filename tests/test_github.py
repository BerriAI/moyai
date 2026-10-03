import asyncio
import copy
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError

from app.connector_errors import ConnectorError
from app.github import Change, Publish, PERMISSIONS
from app.security import digest
from test_workspace import workspace, cloud_capability
from test_spend import sign_in

BASE = 'a' * 40
CURRENT = 'b' * 40
TREE = 'c' * 40
COMMIT = 'd' * 40
PAYLOAD = {'repository': 'BerriAI/litellm', 'base_sha': BASE, 'request_key': 'fix-test-123',
           'title': 'Fix the tested behavior', 'body': 'A reviewed change. Validation: tests passed.',
           'files': [{'path': 'fix.py', 'content': 'answer = 42\n', 'executable': False}]}


def connected(app):
    run_id, headers = cloud_capability(app, ['github'])
    app.state.connectors.github.save_app({'id': 123, 'slug': 'moyai-test', 'pem': 'private-signing-key'})
    app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 10, 'repository': 'BerriAI/litellm'}, 'BerriAI/litellm')
    return run_id, headers


class GitHubAPI:
    def __init__(self, github, monkeypatch, repository="BerriAI/litellm"):
        self.github, self.calls, self.repository = github, [], repository
        self.branch = None
        self.pr = None
        self.tree = []
        self.lose = ''
        self.status = 'ahead'
        self.on_call = None
        monkeypatch.setattr(github, 'request', self.request)
        async def token(*args, **kwargs):
            return 'installation-secret-never-in-sandbox'
        monkeypatch.setattr(github, 'installation_token', token)

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.on_call:
            self.on_call(method, path)
        prefix = '/repos/' + self.repository
        if path == prefix:
            return {'full_name': self.repository, 'default_branch': 'main', 'html_url': 'https://github.com/' + self.repository, 'private': True}
        if path == prefix + '/git/ref/heads/main':
            return {'object': {'sha': CURRENT}}
        if '/compare/' in path:
            return {'status': self.status}
        if path == prefix + '/git/commits/' + BASE:
            return {'tree': {'sha': TREE}}
        if method == 'GET' and '/git/trees/' in path:
            return {'tree': self.tree}
        if method == 'POST' and path.endswith('/git/trees'):
            return {'sha': 'e' * 40}
        if method == 'POST' and path.endswith('/git/commits'):
            return {'sha': COMMIT}
        if method == 'GET' and '/git/ref/heads/moyai/' in path:
            return {'object': {'sha': self.branch}} if self.branch else None
        if method == 'POST' and path.endswith('/git/refs'):
            self.branch = kwargs['json']['sha']
            if self.lose == 'branch':
                self.lose = ''
                raise ConnectorError('Lost response')
            return {}
        if path.endswith('/pulls'):
            if method == 'GET':
                return [self.pr] if self.pr else []
            assert kwargs['json']['draft'] is False
            self.pr = {'number': 100, 'html_url': 'https://github.com/' + self.repository + '/pull/100', 'draft': False, 'state': 'open'}
            if self.lose == 'pr':
                self.lose = ''
                raise ConnectorError('Lost response')
            return self.pr
        raise AssertionError((method, path))


@pytest.mark.parametrize('path', ['../a', '/a', 'a//b', 'a/./b', 'a\\b', '.git/config', '.GIT/config',
    '.github', '.github/workflows', '.github/workflows/check.yml', '.github/actions', '.ssh', '.ssh/config',
    'CODEOWNERS', 'nested/codeowners', '.env.local', 'a/private.pem', 'a/key.key', 'a\nb'])
def test_protected_paths(path):
    with pytest.raises(ValidationError):
        Change(path=path, content='unsafe')


def test_bounds_and_ambiguous_changes():
    for files in [[{'path': 'a', 'content': 'x'}, {'path': 'A', 'content': 'x'}],
                  [{'path': 'a', 'content': 'x'}, {'path': 'a/b', 'content': 'x'}],
                  [{'path': 'a', 'content': '\0'}], [{'path': 'a', 'content': 'a' * (1024 * 1024 + 1)}]]:
        with pytest.raises(ValidationError):
            Publish.model_validate({**PAYLOAD, 'files': files})


@pytest.mark.parametrize('lost', ['', 'branch', 'pr'])
def test_publish_normal_pr_once_across_lost_ack_and_new_turn(workspace, monkeypatch, lost):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.lose = lost
    args = Publish.model_validate(PAYLOAD)
    if lost:
        with pytest.raises(ConnectorError, match='Lost'):
            asyncio.run(github.publish(app.state.store.run(run_id), args))
    app.state.store.execute('UPDATE runs SET active_message_id=2 WHERE id=?', (run_id,))
    run = app.state.store.run(run_id)
    result = asyncio.run(github.publish(run, args))
    assert result['draft'] is False and result['number'] == 100
    assert asyncio.run(github.publish(run, args)) == result
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/git/refs')]) == 1
    writes = [c for c in api.calls if c[0] != 'GET']
    assert all(c[0] == 'POST' and c[1].rsplit('/', 1)[-1] in {'trees', 'commits', 'refs', 'pulls'} for c in writes)
    ref = next(c[2]['json']['ref'] for c in writes if c[1].endswith('/refs'))
    assert ref.startswith('refs/heads/moyai/' + run_id[:12] + '/')
    with pytest.raises(ConnectorError, match='different changes'):
        asyncio.run(github.publish(run, Publish.model_validate({**PAYLOAD, 'title': 'Different payload'})))


@pytest.mark.parametrize('entry', [
    {'path': 'fix.py', 'type': 'tree', 'mode': '040000', 'sha': TREE},
    {'path': 'fix.py', 'type': 'blob', 'mode': '120000', 'sha': TREE},
    {'path': 'fix.py', 'type': 'commit', 'mode': '160000', 'sha': TREE},
])
def test_no_implicit_directory_deletion_or_special_file_changes(workspace, monkeypatch, entry):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.tree = [entry]
    with pytest.raises(ConnectorError, match='regular text'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    assert not any(c[0] == 'POST' for c in api.calls)


def test_non_default_base_and_missing_deletion_are_rejected(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    api.status = 'diverged'
    with pytest.raises(ConnectorError, match='not on the current default'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    api.status = 'ahead'
    with pytest.raises(ConnectorError, match='does not exist'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate({**PAYLOAD, 'request_key': 'delete-test-123', 'files': [{'path': 'absent', 'content': None}]})))
    assert not any(c[0] == 'POST' for c in api.calls)


@pytest.mark.parametrize('change', ['disconnect', 'replace', 'stop', 'rotate', 'read_only', 'allowlist'])
def test_revocation_during_publication_stops_further_mutations(workspace, monkeypatch, change):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    def mutate(method, path):
        if method != 'POST' or not path.endswith('/git/trees'):
            return
        if change == 'disconnect':
            app.state.store.execute("DELETE FROM connections WHERE provider='github'")
        elif change == 'replace':
            app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 20, 'repository': 'BerriAI/litellm'}, 'Changed')
        elif change == 'stop':
            app.state.store.update_run(run_id, status='cancelled')
        elif change == 'rotate':
            app.state.store.update_run(run_id, token_hash=digest('new-token'))
        elif change == 'allowlist':
            app.state.connectors.settings.github_repositories = 'BerriAI/moyai-devin'
        else:
            app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    api.on_call = mutate
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.publish(app.state.store.run(run_id), Publish.model_validate(PAYLOAD)))
    assert [c[1].rsplit('/', 1)[-1] for c in api.calls if c[0] == 'POST'] == ['trees']


@pytest.mark.parametrize('user,role', [('alice', 'admin'), ('bob', 'member')])
def test_direct_pr_creation_without_admin_approval_and_no_merge_review_tools(workspace, monkeypatch, user, role):
    app, client = workspace
    run_id, headers = connected(app)
    owner = sign_in(app, client, user, user + '@berri.ai')
    app.state.store.execute('UPDATE runs SET owner_id=?,active_user_id=? WHERE id=?', (owner, owner, run_id))
    assert client.get('/api/session').json()['role'] == role
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    def still_running(method, path):
        assert app.state.store.run(run_id)['status'] == 'running'
        assert not app.state.store.approvals(run_id)
    api.on_call = still_running
    url = f'/broker/{run_id}/tools/call'
    for name in ['github_merge', 'github_approve', 'github_review', 'github_request', 'github_update_branch']:
        assert client.post(url, headers=headers, json={'name': name, 'arguments': {}}).status_code == 403
    tool = next(t for t in client.get(f'/broker/{run_id}/tools', headers=headers).json() if t['name'] == 'github_create_pull_request')
    assert tool['annotations']['readOnlyHint'] is False
    connection = next(c for c in client.get('/api/connections').json() if c['id'] == 'github')
    policy = next(t for t in connection['tools'] if t['name'] == tool['name'])
    assert policy['write'] is True and policy['requires_approval'] is False
    response = client.post(url, headers=headers, json={'name': tool['name'], 'arguments': PAYLOAD})
    assert response.status_code == 200
    assert response.json()['url'] == 'https://github.com/BerriAI/litellm/pull/100'
    assert response.json()['draft'] is False
    assert not app.state.store.approvals(run_id)
    assert not any(e['kind'] == 'approval' for e in app.state.store.events(run_id))
    assert app.state.store.run(run_id)['status'] == 'running'
    if role == 'member':
        assert client.patch('/api/connections/github/policy', json={'enabled': True, 'read_only': False}).status_code == 403


@pytest.mark.parametrize('restriction', ['read_only', 'disabled', 'disconnect', 'plugin', 'cancelled', 'revoked'])
def test_direct_pr_still_requires_connection_policy_and_live_capability(workspace, monkeypatch, restriction):
    app, client = workspace
    run_id, headers = connected(app)
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    if restriction in {'read_only', 'disabled'}:
        response = client.patch('/api/connections/github/policy', json={'enabled': restriction != 'disabled', 'read_only': restriction == 'read_only'})
        assert response.status_code == 200
    elif restriction == 'disconnect':
        client.delete('/api/connections/github')
    elif restriction == 'plugin':
        app.state.store.execute("UPDATE runs SET plugins='[]' WHERE id=?", (run_id,))
    elif restriction == 'cancelled':
        app.state.store.update_run(run_id, status='cancelled')
    else:
        app.state.store.update_run(run_id, token_hash=digest('rotated'))
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'github_create_pull_request', 'arguments': PAYLOAD})
    assert response.status_code == (401 if restriction in {'cancelled', 'revoked'} else 403)
    assert not api.calls and not app.state.store.approvals(run_id)
    if restriction == 'read_only':
        names = [t['name'] for t in client.get(f'/broker/{run_id}/tools', headers=headers).json()]
        assert 'github_checkout' in names and 'github_create_pull_request' not in names


def test_direct_pr_lost_response_is_uncertain_and_explicit_retry_reuses_pr(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    api.lose = 'pr'
    url = f'/broker/{run_id}/tools/call'
    body = {'name': 'github_create_pull_request', 'arguments': PAYLOAD}
    result = client.post(url, headers=headers, json=body).json()
    assert result['outcome_uncertain'] is True and 'Lost response' in result['error']
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    # A follow-up retry recovers the published PR instead of opening another one.
    app.state.store.execute('UPDATE runs SET active_message_id=2 WHERE id=?', (run_id,))
    retry = client.post(url, headers=headers, json=body).json()
    assert retry['url'] == api.pr['html_url'] and retry['draft'] is False
    assert not app.state.store.approvals(run_id)
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/pulls')]) == 1
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/git/refs')]) == 1


def test_connection_secrets_stay_on_server_and_raw_tokens_refused(workspace):
    app, client = workspace
    run_id, _ = connected(app)
    encrypted = app.state.store.rows('SELECT encrypted FROM github_app')[0]['encrypted']
    assert 'private-signing-key' not in encrypted
    for value in [client.get('/api/connections').text, client.get('/api/config').text,
                  json.dumps(app.state.manager.spec(app.state.store.run(run_id)))]:
        assert 'private-signing-key' not in value and 'installation_id' not in value
    assert client.post('/api/connections/github', json={'token': 'personal-secret'}).status_code == 400


def test_reconnect_after_admission_before_write_lock_cannot_substitute_installation(workspace, monkeypatch):
    app, _ = workspace
    run_id, _ = connected(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch)
    admitted_run = {**app.state.store.run(run_id), 'github_connection_version': github.connection_version()}
    app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 20, 'repository': 'BerriAI/litellm'}, 'Reconnected')
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.publish(admitted_run, Publish.model_validate(PAYLOAD)))
    assert not api.calls


def test_manifest_state_is_bound_expiring_single_use_and_admin_only(workspace, monkeypatch):
    app, client = workspace
    start = client.post('/api/connections/github/oauth').json()['url']
    state = parse_qs(urlparse(start).query)['state'][0]
    page = client.get(start)
    assert page.status_code == 200 and 'BerriAI/litellm' in page.text
    assert 'https://github.com' in page.headers['content-security-policy']
    assert client.get('/auth/github/register?state=unknown').status_code == 400
    async def convert(*args, **kwargs):
        return {'owner': {'login': 'BerriAI'}, 'permissions': PERMISSIONS, 'id': 123, 'slug': 'moyai-test', 'pem': 'secret-key'}
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'request', convert)
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'app-jwt')
    callback = f'/oauth/github/app-callback?state={state}&code=temporary-registration-code'
    redirected = client.get(callback, follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers['location'].startswith('https://github.com/apps/moyai-test/installations/new?state=')
    assert client.get(callback).status_code == 400
    next_url = client.post('/api/connections/github/oauth').json()['url']
    next_state = parse_qs(urlparse(next_url).query)['state'][0]
    app.state.store.execute('UPDATE oauth_states SET expires=?', (time.time() - 1,))
    assert client.get(f'/oauth/github/callback?state={next_state}&installation_id=10&setup_action=install').status_code == 400
    sid = app.state.security.signer.dumps({'sid': 'member-session', 'role': 'member', 'method': 'local'})
    client.cookies.clear(); client.cookies.set('workspace_session', sid)
    assert client.get(start).status_code == 403
    assert client.post('/api/connections/github/oauth').status_code == 403


@pytest.mark.parametrize('bad', ['owner', 'permissions', 'repository', 'suspended'])
def test_installation_scope_validation(workspace, monkeypatch, bad):
    app, _ = workspace
    connected(app)
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'jwt')
    async def response(method, path, **kwargs):
        if path.startswith('/app/installations/') and not path.endswith('/access_tokens'):
            return {'account': {'login': 'wrong' if bad == 'owner' else 'BerriAI', 'type': 'Organization'},
                    'permissions': {**PERMISSIONS, **({'administration': 'write'} if bad == 'permissions' else {})},
                    'suspended_at': '2026-01-01' if bad == 'suspended' else None}
        if path.endswith('/access_tokens'):
            assert kwargs['json'] == {'repositories': ['litellm'], 'permissions': {'contents': 'read', 'pull_requests': 'read'}}
            return {'token': 'token', 'expires_at': '2099-01-01T00:00:00Z'}
        return {'repositories': [{'full_name': 'BerriAI/other' if bad == 'repository' else 'BerriAI/litellm'}]}
    monkeypatch.setattr(github, 'request', response)
    with pytest.raises(ConnectorError):
        asyncio.run(github.verify({'kind': 'github_app', 'installation_id': 10, 'repository': 'BerriAI/litellm'}))


def test_git_read_only_stream_and_no_header_or_redirect_leak(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    async def token(*args, **kwargs):
        return 'installation-secret'
    monkeypatch.setattr(github, 'installation_token', token)
    captured, redirect = [], [False]
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'first'
            yield b'second'
    def upstream(request):
        captured.append(request)
        assert request.url.host == 'github.com'
        assert request.url.path.startswith('/BerriAI/litellm.git/')
        assert request.headers['authorization'].startswith('Basic ')
        assert 'run-capability-only' not in str(request.headers)
        assert 'x-arbitrary' not in request.headers
        if redirect[0]:
            return httpx.Response(302, headers={'Location': 'https://evil.example'})
        kind = 'advertisement' if request.method == 'GET' else 'result'
        return httpx.Response(200, headers={'Content-Type': 'application/x-git-upload-pack-' + kind,
                                           'X-Upstream-Secret': 'do-not-forward'}, stream=Stream())
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github_git.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    base = f'/broker/{run_id}/github.git/'
    for method, route in [('GET', 'info/refs?service=git-receive-pack'), ('POST', 'git-receive-pack'),
                          ('POST', 'git-upload-pack?url=evil'), ('GET', 'config'), ('GET', 'info/refs?service=git-upload-pack&x=1')]:
        assert client.request(method, base + route, headers=headers).status_code == 403
    assert not captured
    read = client.get(base + 'info/refs?service=git-upload-pack', headers={**headers, 'X-Arbitrary': 'secret'})
    assert read.content == b'firstsecond'
    assert 'x-upstream-secret' not in read.headers
    assert client.post(base + 'git-upload-pack', headers={**headers, 'Content-Type': 'application/x-git-upload-pack-request'}, content=b'0000').content == b'firstsecond'
    redirect[0] = True
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).status_code == 502
    assert len(captured) == 3  # No request to the redirect target.
    client.delete('/api/connections/github')
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).status_code == 403
    app.state.store.update_run(run_id, token_hash=digest('rotated'))
    assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).status_code == 401


def connect_multiple(app):
    app.state.connectors.settings.github_repositories = 'BerriAI/litellm,BerriAI/moyai-devin'
    app.state.connectors.save('github', {'kind': 'github_app', 'installation_id': 10,
        'repositories': ['BerriAI/litellm', 'BerriAI/moyai-devin']}, 'Both repositories')


def test_self_repository_pr_is_created_directly_without_approval(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    connect_multiple(app)
    github = app.state.connectors.github
    api = GitHubAPI(github, monkeypatch, 'BerriAI/moyai-devin')
    payload = {**PAYLOAD, 'repository': 'BerriAI/moyai-devin'}
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'github_create_pull_request', 'arguments': payload})
    assert response.status_code == 200
    result = response.json()
    assert result['repository'] == 'BerriAI/moyai-devin' and not result['draft']
    assert not app.state.store.approvals(run_id)
    assert all(path.startswith('/repos/BerriAI/moyai-devin') for _, path, _ in api.calls)
    assert all(method == 'GET' or (method == 'POST' and path.rsplit('/', 1)[-1] in {'trees', 'commits', 'refs', 'pulls'}) for method, path, _ in api.calls)


def test_repository_routing_and_no_implicit_access_expansion(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    github.settings.github_repositories = 'BerriAI/litellm,BerriAI/moyai-devin'
    with pytest.raises(ConnectorError, match='Reconnect'):
        asyncio.run(github.selected_target({}, 'BerriAI/moyai-devin'))
    assert asyncio.run(github.selected_target({})) == 'BerriAI/litellm'
    connect_multiple(app)
    api = GitHubAPI(github, monkeypatch, 'BerriAI/moyai-devin')
    run = {**app.state.store.run(run_id), 'repo_url': 'https://github.com/BerriAI/moyai-devin.git'}
    result = asyncio.run(github.call(run, 'github_checkout', {}))
    assert result['repository'] == 'BerriAI/moyai-devin'
    assert result['git_path'] == '/github/BerriAI/moyai-devin.git'
    assert asyncio.run(github.call(run, 'github_repositories', {}))['repositories'] == github.targets()
    api.calls.clear()
    for target in ['BerriAI/other', 'OtherOrg/moyai-devin', '../moyai-devin']:
        with pytest.raises(ConnectorError):
            asyncio.run(github.call(run, 'github_checkout', {'repository': target}))
        response = client.get(f'/broker/{run_id}/github/{target}.git/info/refs?service=git-upload-pack', headers=headers)
        assert response.status_code in {403, 404}
    assert not api.calls
    approved = {**run, 'github_connection_version': github.connection_version()}
    github.settings.github_repositories = 'BerriAI/litellm'
    with pytest.raises(ConnectorError):
        asyncio.run(github.publish(approved, Publish.model_validate({**PAYLOAD, 'repository': 'BerriAI/moyai-devin'})))
    assert not api.calls


def test_installation_tokens_are_scoped_cached_and_verified_per_repository(workspace, monkeypatch):
    app, _ = workspace
    connected(app); connect_multiple(app)
    github = app.state.connectors.github
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'jwt')
    requests = []
    async def request(method, path, **kwargs):
        if path.endswith('/access_tokens'):
            requests.append(kwargs['json'])
            repo = kwargs['json']['repositories'][0]
            return {'token': repo + '-' + kwargs['json']['permissions']['contents'], 'expires_at': '2099-01-01T00:00:00Z'}
        if path == '/installation/repositories':
            repo = kwargs['token'].removesuffix('-read')
            return {'repositories': [{'full_name': 'BerriAI/' + repo}]}
        return {'account': {'login': 'BerriAI', 'type': 'Organization'}, 'permissions': PERMISSIONS, 'suspended_at': None}
    monkeypatch.setattr(github, 'request', request)
    credentials = {'kind': 'github_app', 'installation_id': 10, 'repositories': github.targets()}
    assert asyncio.run(github.verify(credentials)) == 'BerriAI/litellm, BerriAI/moyai-devin'
    assert [r['repositories'] for r in requests] == [['litellm'], ['moyai-devin']]
    assert asyncio.run(github.installation_token(repository='berriai/MOYAI-devin')) == 'moyai-devin-read'
    assert len(requests) == 2
    assert asyncio.run(github.installation_token(repository='BerriAI/moyai-devin', write=True)) == 'moyai-devin-write'
    assert requests[-1] == {'repositories': ['moyai-devin'], 'permissions': {'contents': 'write', 'pull_requests': 'write'}}
    assert asyncio.run(github.installation_token(repository='BerriAI/litellm')) == 'litellm-read'
    with pytest.raises(ConnectorError):
        asyncio.run(github.installation_token(repository='BerriAI/unlisted', write=True))
    assert len(requests) == 3


def test_specific_git_route_cannot_change_repo_or_allow_push(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app); connect_multiple(app)
    github = app.state.connectors.github
    tokens, paths = [], []
    async def token(*args, **kwargs):
        assert not kwargs.get('write')
        tokens.append(kwargs['repository'])
        return 'server-only'
    monkeypatch.setattr(github, 'installation_token', token)
    def upstream(request):
        paths.append(request.url.path)
        kind = 'advertisement' if request.method == 'GET' else 'result'
        return httpx.Response(200, content=b'0000', headers={'Content-Type': 'application/x-git-upload-pack-' + kind})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github_git.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    for repo in ['BerriAI/litellm', 'BerriAI/moyai-devin']:
        base = f'/broker/{run_id}/github/{repo}.git/'
        assert client.get(base + 'info/refs?service=git-upload-pack', headers=headers).content == b'0000'
        assert paths[-1] == '/' + repo + '.git/info/refs' and tokens[-1] == repo
        for suffix in ['info/refs?service=git-receive-pack', 'info/refs?service=git-upload-pack&repository=other', 'config']:
            assert client.get(base + suffix, headers=headers).status_code == 403
        assert client.post(base + 'git-receive-pack', headers=headers).status_code == 403
    assert len(paths) == 2
    github.settings.github_repositories = 'BerriAI/litellm'
    assert client.get(f'/broker/{run_id}/github/BerriAI/moyai-devin.git/info/refs?service=git-upload-pack', headers=headers).status_code == 403
    assert len(paths) == 2


@pytest.mark.parametrize('invalid', ['', 'owner', 'type', 'permission', 'id', 'signature'])
def test_existing_app_recovery_verifies_before_saving_and_keeps_key_private(workspace, monkeypatch, invalid):
    app, client = workspace
    github = app.state.connectors.github
    secret = 'private-key-material-' * 12
    def sign(config=None):
        if invalid == 'signature':
            raise ConnectorError('The signing key is invalid.')
        return 'signed-jwt'
    monkeypatch.setattr(github, 'app_jwt', sign)
    async def request(method, path, **kwargs):
        assert method == 'GET' and path == '/app' and kwargs['token'] == 'signed-jwt'
        return {'id': 1 if invalid == 'id' else 123, 'slug': 'moyai-test',
                'owner': {'login': 'Other' if invalid == 'owner' else 'BerriAI', 'type': 'User' if invalid == 'type' else 'Organization'},
                'permissions': {**PERMISSIONS, **({'administration': 'write'} if invalid == 'permission' else {})}}
    monkeypatch.setattr(github, 'request', request)
    result = client.post('/api/connections/github/app', json={'app_id': 123, 'private_key': secret})
    assert secret not in result.text
    if invalid:
        assert result.status_code >= 400 and not github.app_config()
    else:
        assert result.status_code == 200 and result.json()['url'].startswith('https://github.com/apps/moyai-test/installations/new?')
        assert github.app_config()['pem'] == secret
        assert secret not in app.state.store.rows('SELECT encrypted FROM github_app')[0]['encrypted']
        assert secret not in client.get('/api/connections').text
    sid = app.state.security.signer.dumps({'sid': 'member-session', 'role': 'member', 'method': 'local'})
    client.cookies.clear(); client.cookies.set('workspace_session', sid)
    assert client.post('/api/connections/github/app', json={'app_id': 123, 'private_key': secret}).status_code == 403
