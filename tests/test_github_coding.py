"""Coding workflow through the real broker, HTTP client and scoped App tokens."""
import base64
import json

import httpx
import pytest

from app.github import Change, TOOLS
from app.github_coding import READS, WRITES
from test_github import connected, COMMIT, repository_data
from test_github_checkout import checkout
from test_workspace import workspace
from test_github_write_access import external
from test_github_followups import published
from agent.tools import github_tools as local

from scripts.github_coding_fixture import CodingProvider, PR, PREFIX


@pytest.fixture
def coding(workspace, monkeypatch):
    app, client = workspace
    rid, headers = connected(app)
    github = app.state.connectors.github
    provider = CodingProvider()
    monkeypatch.setattr(github, 'app_jwt', lambda config=None: 'fixture-jwt')
    original = httpx.AsyncClient
    monkeypatch.setattr('app.github.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(provider.handle), **kw))
    app.state.store.execute('''INSERT INTO github_publications
        (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at) VALUES(?,?,?,?,?,?,?,?)''',
        ('publication', rid, 0, 'fixture', PR['head']['ref'], json.dumps({'number': 100, 'repository_id': 101}), github.connection_version(), 'fixture'))
    def call(name, **args):
        return client.post(f'/broker/{rid}/tools/call', headers=headers,
                           json={'name': name, 'arguments': {'repository_id': 101, **args}})
    return app, client, rid, headers, github, provider, call


READ_CASES = [
    ('github_search', {'kind': 'issues', 'query': 'bug'}), ('github_issues', {}), ('github_issue', {'number': 10}),
    ('github_issue_comments', {'number': 10}), ('github_pull_requests', {}), ('github_branches', {}),
    ('github_commits', {}), ('github_commit', {'sha': COMMIT}), ('github_compare_commits', {'base_sha': COMMIT, 'head_sha': COMMIT}),
    ('github_file', {'path': 'fix.py', 'ref': COMMIT}), ('github_labels', {}), ('github_milestones', {}),
    ('github_releases', {}), ('github_review_threads', {'number': 100}), ('github_workflow_artifacts', {'run_id': 7}),
    ('github_download_artifact', {'artifact_id': 9}),
]


@pytest.mark.parametrize('name,args', READ_CASES)
def test_read_catalog_dispatch_pagination_and_readonly_scopes(coding, name, args):
    app, client, rid, headers, _, provider, call = coding
    app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    catalog = client.get(f'/broker/{rid}/tools', headers=headers).json()
    names = {item['name'] for item in catalog}
    assert READS.keys() <= names and not WRITES.keys() & names
    response = call(name, **args)
    assert response.status_code == 200, response.text
    result = response.json()
    assert 'error' not in result, result
    assert result['repository_id'] == 101
    assert all('write' not in permissions.values() for permissions in provider.minted)
    if name == 'github_issue':
        assert result['data']['body'] == provider.issue['body']
    if 'items' in result:
        assert result['next_page'] == 2
    if name == 'github_search':
        assert result['provider_incomplete'] and result['next_page'] == 2
    if name in {'github_commit', 'github_compare_commits'}:
        assert result['missing_patches'] == ['binary.png']


WRITE_CASES = [
    ('github_create_issue', {'title': 'Bug'}, 'POST', '/issues'),
    ('github_update_issue', {'number': 10, 'state': 'closed'}, 'PATCH', '/issues/10'),
    ('github_comment_issue', {'number': 10, 'body': 'Fixed'}, 'POST', '/issues/10/comments'),
    ('github_edit_issue_comment', {'number': 10, 'comment_id': 26, 'body': 'Fixed'}, 'PATCH', '/issues/comments/26'),
    ('github_update_pull_request_details', {'title': 'New title'}, 'PATCH', '/pulls/100'),
    ('github_update_pull_request_details', {'labels': ['bug'], 'assignees': ['tin']}, 'PATCH', '/issues/100'),
    ('github_pull_request_reviewers', {'action': 'request', 'reviewers': ['reviewer']}, 'POST', '/pulls/100/requested_reviewers'),
    ('github_pull_request_reviewers', {'action': 'remove', 'team_reviewers': ['reviewers']}, 'DELETE', '/pulls/100/requested_reviewers'),
    ('github_reply_review_comment', {'comment_id': 25, 'body': 'Fixed'}, 'POST', '/pulls/100/comments/25/replies'),
    ('github_edit_pull_request_comment', {'comment_id': 25, 'kind': 'inline', 'body': 'Fixed'}, 'PATCH', '/pulls/comments/25'),
    ('github_set_review_thread_resolved', {'thread_id': 'THREAD_1', 'resolved': True}, 'POST', '/graphql'),
    ('github_mark_pull_request_ready', {}, 'POST', '/graphql'),
    ('github_comment_review', {'body': 'One comment', 'comments': [{'path': 'fix.py', 'line': 1, 'body': 'Please fix'}]}, 'POST', '/pulls/100/reviews'),
    *[('github_workflow_action', {'run_id': 7, 'run_attempt': 2, 'action': action, **extra}, 'POST', path)
      for action, extra, path in [('rerun_failed', {}, '/actions/runs/7/rerun-failed-jobs'),
          ('rerun_all', {}, '/actions/runs/7/rerun'), ('cancel', {}, '/actions/runs/7/cancel'),
          ('rerun_job', {'job_id': 8}, '/actions/jobs/8/rerun')]],
]


def write_args(name, args):
    issue = name in {'github_create_issue', 'github_update_issue', 'github_comment_issue', 'github_edit_issue_comment'}
    return {'request_key': 'coding-test-write', **({} if issue else {'number': 100, 'head_sha': COMMIT}), **args}


@pytest.mark.parametrize('name,args,method,path', WRITE_CASES)
def test_writes_replay_saved_confirmation_without_repeating_dispatch(coding, name, args, method, path):
    _, _, _, _, _, provider, call = coding
    args = write_args(name, args)
    first = call(name, **args)
    assert first.status_code == 200, first.text
    assert first.json().get('confirmed') is True, first.text
    assert call(name, **args).json() == first.json()
    assert len(provider.dispatches) == 1
    sent_method, sent_path, body = provider.dispatches[0]
    assert (sent_method, sent_path) == (method, path if path == '/graphql' else PREFIX + path)
    if name == 'github_comment_review':
        assert body['event'] == 'COMMENT'


@pytest.mark.parametrize('name,args,method,path', WRITE_CASES)
def test_uncertain_write_is_never_redispatched(coding, name, args, method, path):
    _, _, _, _, _, provider, call = coding
    provider.lose_response = True
    args = write_args(name, args)
    assert 'error' in call(name, **args).json()
    provider.lose_response = False
    result = call(name, **args).json()
    assert result['code'] == 'outcome_unknown' and result['outcome_uncertain']
    assert len(provider.dispatches) == 1


@pytest.mark.parametrize('restriction', ['head', 'no_grant', 'readonly', 'wrong_repository', 'wrong_comment', 'wrong_thread', 'wrong_run', 'scope', 'revoked_during_read'])
def test_boundaries_prevent_all_mutations(coding, restriction):
    app, _, _, _, _, provider, call = coding
    name, args = 'github_reply_review_comment', {'comment_id': 25, 'body': 'Fixed'}
    args = write_args(name, args)
    if restriction == 'head':
        args['head_sha'] = 'a' * 40
    elif restriction == 'no_grant':
        app.state.store.execute('DELETE FROM github_publications')
    elif restriction == 'readonly':
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    elif restriction == 'wrong_repository':
        args['repository_id'] = 202
    elif restriction == 'wrong_comment':
        provider.wrong_comment = True
    elif restriction == 'wrong_thread':
        name, args = 'github_set_review_thread_resolved', write_args('pr', {'thread_id': 'THREAD_1', 'resolved': True})
        provider.wrong_thread = True
    elif restriction in {'wrong_run', 'scope'}:
        name, args = 'github_workflow_action', write_args('pr', {'run_id': 7, 'run_attempt': 2, 'action': 'rerun_failed'})
        provider.wrong_run = restriction == 'wrong_run'
        if restriction == 'scope':
            provider.permissions['actions'] = 'read'
    else:
        provider.after_read = lambda path: app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1) ON CONFLICT(provider) DO UPDATE SET read_only=1")
    response = call(name, **args)
    assert response.status_code != 200 or 'error' in response.json(), response.text
    assert provider.dispatches == []


@pytest.mark.parametrize('name', ['github_update_issue', 'github_comment_issue'])
def test_issue_tools_cannot_bypass_pr_approval(coding, name):
    _, _, _, _, _, provider, call = coding
    provider.issue['pull_request'] = {'url': 'PR'}
    result = call(name, number=10, body='update', request_key='issue-not-pr').json()
    assert 'error' in result and not provider.dispatches


def test_artifact_pages_are_complete_and_revision_pinned(coding):
    _, _, _, _, _, provider, call = coding
    args, chunks = {'artifact_id': 9}, []
    while args:
        result = call('github_download_artifact', **args).json()
        chunks.append(base64.b64decode(result['content']))
        args = result['next_arguments']
    assert b''.join(chunks) == provider.archive
    provider.archive += b'changed'
    assert call('github_download_artifact', artifact_id=9, revision=result['revision']).json()['code'] == 'source_changed'


@pytest.mark.parametrize('query', ['bug repo:other/repo', 'bug OR repo:other/repo', 'bug OR anything', 'bug\nanything'])
def test_search_cannot_expand_repository_scope(coding, query):
    *_, call = coding
    response = call('github_search', kind='code', query=query)
    assert response.status_code == 422 or 'error' in response.json()


def test_no_merge_or_approval_surface(coding):
    *_, provider, call = coding
    for name, args in [('github_merge_pull_request', {}), ('github_enable_auto_merge', {}),
                       ('github_approve_pull_request', {}), ('github_comment_review', {'event': 'APPROVE'})]:
        response = call(name, **write_args(name, {'body': 'review', **args}))
        assert response.status_code in {403, 404, 422} or 'error' in response.json(), response.text
    assert not provider.dispatches
    assert not any('merge' in name or 'approve' in name for name in TOOLS)


@pytest.mark.parametrize('path', ['.env.example', 'nested/.env.sample', '.env.template'])
def test_environment_templates_pass_both_publication_layers(checkout, path):
    repo, *_ = checkout
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('AGENT_HARNESS=deepagents-temporal\n')
    changes = local.collect(str(repo), 'Update harness', 'Template default only', 'template-fix')
    change = Change.model_validate(changes['files'][0])
    assert change.path == path and change.content == file.read_text()


@pytest.mark.parametrize('path', ['.env', '.env.local', '.env.example.secret', 'nested/.env.production', '.env.example/config', 'nested/.ssh/key', 'nested/CODEOWNERS'])
def test_credentials_still_rejected_by_both_layers(checkout, path):
    repo, *_ = checkout
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text('secret')
    with pytest.raises(ValueError):
        Change(path=path, content='secret')
    with pytest.raises(local.GitHubToolError):
        local.collect(str(repo), 'Should reject', 'Body', 'reject-secrets')


def test_external_pr_grant_is_required_and_revocation_stops_followups(external, monkeypatch):
    from test_github_write_access import approved, call as external_call, decide
    app, _, _, _, github, provider = external
    original = provider.request
    writes = []
    async def request(method, path, **kwargs):
        if method == 'PATCH' and path.endswith('/pulls/100'):
            writes.append(kwargs['json'])
            return {'number': 100, **kwargs['json']}
        return await original(method, path, **kwargs)
    monkeypatch.setattr(github, 'request', request)
    args = {'repository_id': 101, 'number': 100, 'head_sha': COMMIT, 'title': 'Updated title', 'request_key': 'external-title'}
    name = 'github_update_pull_request_details'
    assert 'error' in external_call(external, name, args).json()
    assert writes == []
    grant = approved(external)
    assert external_call(external, name, args).json()['confirmed']
    assert decide(external, grant['id'], 'revoke').status_code == 200
    assert 'error' in external_call(external, name, {**args, 'request_key': 'after-revoke'}).json()
    assert len(writes) == 1


def test_template_update_reaches_the_publication_commit(published):
    import asyncio
    from test_github_followups import update_args
    _, _, run, _, github, provider = published
    args = update_args().model_copy(update={'files': [Change(path='.env.example', content='AGENT_HARNESS=deepagents-temporal\n')]})
    result = asyncio.run(github.update(run, args))
    assert result['commit'] == provider.next_commit
    trees = [kwargs['json']['tree'] for method, path, kwargs in provider.calls if method == 'POST' and path.endswith('/git/trees')]
    assert trees[-1][0]['path'] == '.env.example'


@pytest.mark.parametrize('wrong', [False, True])
def test_push_ci_requires_the_exact_pr_branch(coding, wrong):
    *_, provider, call = coding
    provider.push_run = True
    provider.wrong_run = wrong
    result = call('github_workflow_action', **write_args('pr', {'run_id': 7, 'run_attempt': 2, 'action': 'rerun_failed'})).json()
    assert ('error' in result) == wrong
    assert len(provider.dispatches) == (0 if wrong else 1)


def test_binary_pages_are_not_modified_by_text_redaction():
    from app.github_coding import clean
    # This is valid base64 whose suffix resembles an assignment to a secret.
    payload = {'encoding': 'base64', 'content': 'tokenA==', 'body': 'password=secret'}
    assert clean(payload, 'server-token')['content'] == payload['content']
    assert 'secret' not in clean(payload, 'server-token')['body']


def test_two_brokers_cannot_dispatch_the_same_receipt(coding, monkeypatch):
    import asyncio
    from app.github import GitHub
    from app.connector_errors import ConnectorError
    app, _, rid, _, first, provider, _ = coding
    second = GitHub(first.store, first.security, first.settings, first.connectors)
    monkeypatch.setattr(second, 'app_jwt', lambda config=None: 'fixture-jwt')
    args = {'repository_id': 101, **write_args('pr', {'title': 'Concurrent title'})}
    run = app.state.store.run(rid)
    async def exercise():
        ready = asyncio.Event()
        arrivals = []
        for broker in (first, second):
            original = broker.authorized_pr
            count = [0]
            async def fenced(*values, _original=original, _count=count, **kwargs):
                result = await _original(*values, **kwargs)
                _count[0] += 1
                if _count[0] == 2:
                    arrivals.append(True)
                    if len(arrivals) == 2:
                        ready.set()
                    await ready.wait()
                return result
            monkeypatch.setattr(broker, 'authorized_pr', fenced)
        return await asyncio.wait_for(asyncio.gather(*[
            broker.call(run, 'github_update_pull_request_details', args) for broker in (first, second)
        ], return_exceptions=True), timeout=10)
    results = asyncio.run(exercise())
    assert len(provider.dispatches) == 1
    assert sum(isinstance(result, dict) and result.get('confirmed') for result in results) == 1
    assert sum(isinstance(result, ConnectorError) and result.code == 'outcome_unknown' for result in results) == 1
