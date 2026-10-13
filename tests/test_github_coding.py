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
    ('github_capabilities', {}), ('github_publication_status', {'request_key': 'not-started'}),
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
    ('github_close_pull_request', {}, 'PATCH', '/pulls/100'),
    ('github_reopen_pull_request', {}, 'PATCH', '/pulls/100'),
    ('github_retarget_pull_request', {'base_branch': 'release/candidate'}, 'PATCH', '/pulls/100'),
    ('github_convert_pull_request_to_draft', {'intent': 'user_requested_draft_for_this_pr'}, 'POST', '/graphql'),
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
    *[('github_create_review', {'body': 'Review summary', 'event': event}, 'POST', '/pulls/100/reviews')
      for event in ['COMMENT', 'REQUEST_CHANGES', 'PENDING']],
    *[('github_submit_review', {'body': 'Review summary', 'event': event, 'review_id': 40, 'creation_request_key': 'pending-origin'},
       'POST', '/pulls/100/reviews/40/events') for event in ['COMMENT', 'REQUEST_CHANGES']],
    ('github_edit_pending_review', {'body': 'Updated summary', 'review_id': 40, 'creation_request_key': 'pending-origin'}, 'PUT', '/pulls/100/reviews/40'),
    ('github_delete_pending_review', {'review_id': 40, 'creation_request_key': 'pending-origin'}, 'DELETE', '/pulls/100/reviews/40'),
    *[('github_workflow_action', {'run_id': 7, 'run_attempt': 2, 'action': action, **extra}, 'POST', path)
      for action, extra, path in [('rerun_failed', {}, '/actions/runs/7/rerun-failed-jobs'),
          ('rerun_all', {}, '/actions/runs/7/rerun'), ('cancel', {}, '/actions/runs/7/cancel'),
          ('rerun_job', {'job_id': 8}, '/actions/jobs/8/rerun')]],
]


def write_args(name, args):
    issue = name in {'github_create_issue', 'github_update_issue', 'github_comment_issue', 'github_edit_issue_comment'}
    return {'request_key': 'coding-test-write', **({} if issue else {'number': 100, 'head_sha': COMMIT}), **args}


def prepare_write(coding, name):
    *_, provider, call = coding
    if name == 'github_reopen_pull_request':
        provider.pr['state'] = 'closed'
    if name == 'github_convert_pull_request_to_draft':
        provider.pr['draft'] = False
    if name in {'github_submit_review', 'github_edit_pending_review', 'github_delete_pending_review'}:
        result = call('github_create_review', **write_args('pr', {
            'request_key': 'pending-origin', 'event': 'PENDING', 'body': 'Pending review',
        })).json()
        assert result.get('confirmed'), result
        provider.dispatches.clear()


@pytest.mark.parametrize('name,args,method,path', WRITE_CASES)
def test_writes_replay_saved_confirmation_without_repeating_dispatch(coding, name, args, method, path):
    _, _, _, _, _, provider, call = coding
    prepare_write(coding, name)
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
    if name == 'github_create_review':
        assert body.get('event') == (None if args['event'] == 'PENDING' else args['event'])
        assert body['commit_id'] == COMMIT
    if name == 'github_retarget_pull_request':
        assert body == {'base': 'release/candidate'}
        assert provider.pr['base']['ref'] == 'release/candidate'
        assert provider.pr['head']['sha'] == COMMIT
    if name == 'github_reopen_pull_request':
        assert body == {'state': 'open'}
    if name == 'github_convert_pull_request_to_draft':
        assert body == {'query': 'mutation($id:ID!){convertPullRequestToDraft(input:{pullRequestId:$id}){pullRequest{number isDraft url}}}',
                        'variables': {'id': 'PR_100'}}


@pytest.mark.parametrize('name,args,method,path', WRITE_CASES)
def test_uncertain_write_is_never_redispatched(coding, monkeypatch, name, args, method, path):
    from app.github import GitHub
    app, _, _, _, github, provider, call = coding
    prepare_write(coding, name)
    provider.lose_response = True
    args = write_args(name, args)
    assert 'error' in call(name, **args).json()
    provider.lose_response = False
    restored = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(restored, 'app_jwt', lambda config=None: 'fixture-jwt')
    monkeypatch.setattr(app.state.connectors, 'github', restored)
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


SEARCH_KINDS = [
    ('code', 'code', '', 'contents'),
    ('issues', 'issues', ' is:issue', 'issues'),
    ('pull_requests', 'issues', ' is:pr', 'pull_requests'),
    ('commits', 'commits', '', 'contents'),
]


@pytest.mark.parametrize('kind,endpoint,suffix,permission', SEARCH_KINDS)
@pytest.mark.parametrize('query,terms', [
    ("don't", '"don\'t"'),
    ("it's", '"it\'s"'),
    ("user's", '"user\'s"'),
    ("don't it's", '"don\'t" "it\'s"'),
    ('café 中文 🚀', '"café" "中文" "🚀"'),
    ('cafe\u0301', '"cafe\u0301"'),
    ('user’s “café”', '"user’s" "“café”"'),
    ('bug "connection timeout"', '"bug" "connection timeout"'),
    ('"user\'s café" 中文', '"user\'s café" "中文"'),
    ('  bug\u00a0  timeout  ', '"bug" "timeout"'),
    ('"a  b"', '"a  b"'),
    ('bug#42 C++ foo/bar (-flag) /regex/', '"bug#42" "C++" "foo/bar" "(-flag)" "/regex/"'),
    ('a ' * 249 + 'aa', '"a" ' * 249 + '"aa"'),
])
def test_search_preserves_text_and_scope_in_provider_request(coding, kind, endpoint, suffix, permission, query, terms):
    _, _, _, _, _, provider, call = coding
    response = call('github_search', kind=kind, query=query, page=2)
    assert response.status_code == 200, response.text
    assert 'error' not in response.json(), response.text
    assert provider.searches == [(f'/search/{endpoint}', {
        'q': terms + ' repo:BerriAI/litellm' + suffix, 'per_page': '25', 'page': '2',
    })]
    assert provider.minted == [{'metadata': 'read'}, {permission: 'read'}]
    assert not provider.dispatches


@pytest.mark.parametrize('kind', [case[0] for case in SEARCH_KINDS])
@pytest.mark.parametrize('query', [
    '', '   ', '""', '"   "', 'bug ""', '"unterminated', 'trailing"', 'a' * 501,
    'bug"phrase"', '"phrase"bug', '"first""second"',
    'bug repo:other/repo', 'bug OR repo:other/repo', '"repo:other/repo"',
    'bug is:pr', 'bug is:issue', 'bug OR anything', 'bug NOT anything', 'bug AND anything',
    'bug \\" OR anything', 'bug\\thing', 'bug\nanything', 'bug\rany', 'bug\tany',
    'bug\x00any', 'bug\x1fany', 'bug\x7fany', 'bug\x85any', 'bug\u2028any', 'bug\u2029any',
])
def test_invalid_search_is_rejected_before_provider_access(coding, kind, query):
    _, _, _, _, _, provider, call = coding
    response = call('github_search', kind=kind, query=query)
    assert response.json().get('code') == 'invalid_arguments', response.text
    assert response.json()['dispatched'] is False
    assert not provider.calls and not provider.minted and not provider.searches


def test_no_merge_or_approval_surface(coding):
    *_, provider, call = coding
    for name, args in [('github_merge_pull_request', {}), ('github_enable_auto_merge', {}),
                       ('github_approve_pull_request', {}), ('github_comment_review', {'event': 'APPROVE'}),
                       ('github_create_review', {'event': 'APPROVE'}),
                       ('github_submit_review', {'event': 'APPROVE', 'review_id': 40, 'creation_request_key': 'pending-origin'})]:
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


@pytest.mark.parametrize('name,args', [
    ('github_update_pull_request_details', {'title': 'Concurrent title'}),
    ('github_create_review', {'body': 'Concurrent review', 'event': 'PENDING'}),
    ('github_reopen_pull_request', {}),
    ('github_retarget_pull_request', {'base_branch': 'release/candidate'}),
    ('github_convert_pull_request_to_draft', {'intent': 'user_requested_draft_for_this_pr'}),
    ('github_submit_review', {'body': 'Submit', 'event': 'REQUEST_CHANGES', 'review_id': 40, 'creation_request_key': 'pending-origin'}),
    ('github_edit_pending_review', {'body': 'Edit', 'review_id': 40, 'creation_request_key': 'pending-origin'}),
    ('github_delete_pending_review', {'review_id': 40, 'creation_request_key': 'pending-origin'}),
])
def test_two_brokers_cannot_dispatch_the_same_receipt(coding, monkeypatch, name, args):
    import asyncio
    from app.github import GitHub
    from app.connector_errors import ConnectorError
    app, _, rid, _, first, provider, _ = coding
    prepare_write(coding, name)
    second = GitHub(first.store, first.security, first.settings, first.connectors)
    monkeypatch.setattr(second, 'app_jwt', lambda config=None: 'fixture-jwt')
    args = {'repository_id': 101, **write_args('pr', args)}
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
            broker.call(run, name, args) for broker in (first, second)
        ], return_exceptions=True), timeout=10)
    results = asyncio.run(exercise())
    assert len(provider.dispatches) == 1
    assert sum(isinstance(result, dict) and result.get('confirmed') for result in results) == 1
    assert sum(isinstance(result, ConnectorError) and result.code == 'outcome_unknown' for result in results) == 1


PARITY_CASES = [case[:2] for case in WRITE_CASES if case[0] in {
    'github_reopen_pull_request', 'github_retarget_pull_request', 'github_convert_pull_request_to_draft',
    'github_create_review', 'github_submit_review', 'github_edit_pending_review', 'github_delete_pending_review',
}]


@pytest.mark.parametrize('name,args', PARITY_CASES)
@pytest.mark.parametrize('restriction', ['head', 'no_grant', 'readonly', 'wrong_repository', 'scope', 'requester', 'connection'])
def test_parity_writes_keep_current_authority(coding, name, args, restriction):
    app, _, rid, _, github, provider, call = coding
    prepare_write(coding, name)
    args = write_args('pr', args)
    if restriction == 'head':
        args['head_sha'] = 'a' * 40
    elif restriction == 'no_grant':
        app.state.store.execute('DELETE FROM github_publications')
    elif restriction == 'readonly':
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    elif restriction == 'wrong_repository':
        args['repository_id'] = 202
    elif restriction == 'scope':
        provider.permissions['pull_requests'] = 'read'
        github.tokens.clear()
    else:
        def change_authority():
            if restriction == 'requester':
                app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('different-requester', rid))
            else:
                app.state.connectors.save('github', {'installation_id': 11}, 'Changed installation')
        provider.after_pr_read = change_authority
    response = call(name, **args)
    assert response.status_code != 200 or 'error' in response.json(), response.text
    assert provider.dispatches == []


@pytest.mark.parametrize('state,merged', [('open', False), ('closed', True)])
def test_reopen_rejects_open_or_merged_pr(coding, state, merged):
    *_, provider, call = coding
    provider.pr.update(state=state, merged=merged)
    response = call('github_reopen_pull_request', **write_args('pr', {}))
    assert 'error' in response.json() and not provider.dispatches


@pytest.mark.parametrize('name,args', PARITY_CASES)
def test_parity_writes_recheck_head_after_resource_reads(coding, name, args):
    *_, provider, call = coding
    prepare_write(coding, name)
    reads = []
    def change_head():
        reads.append(True)
        if len(reads) == 1:
            provider.pr['head']['sha'] = 'a' * 40
    provider.after_pr_read = change_head
    response = call(name, **write_args('pr', args))
    assert 'error' in response.json() and not provider.dispatches, response.text


@pytest.mark.parametrize('base', ['', 'HEAD', '@', '-option', '../main', 'a..b', 'main~1', 'a.lock', 'a//b', 'a\\b', 'a b', 'a\nb', 'main?x', 'refs/heads/../main'])
def test_retarget_rejects_invalid_refs(coding, base):
    *_, provider, call = coding
    response = call('github_retarget_pull_request', **write_args('pr', {'base_branch': base}))
    assert response.json()['code'] == 'invalid_arguments', response.text
    assert not provider.calls and not provider.minted and not provider.dispatches


def test_retarget_requires_existing_branch_and_rechecks_original_base(coding):
    *_, provider, call = coding
    args = write_args('pr', {'base_branch': 'missing'})
    assert 'error' in call('github_retarget_pull_request', **args).json()
    assert not provider.dispatches
    def retarget_during_read(path):
        if '/branches/' in path:
            provider.pr['base']['ref'] = 'other-base'
    provider.after_read = retarget_during_read
    args.update(base_branch='release/candidate', request_key='base-changed')
    assert 'error' in call('github_retarget_pull_request', **args).json()
    assert not provider.dispatches


@pytest.mark.parametrize('intent', [None, False, True, 'draft', 'pending_ci', 'user_requested_draft'])
def test_draft_requires_exact_explicit_intent(coding, intent):
    *_, provider, call = coding
    args = write_args('pr', {} if intent is None else {'intent': intent})
    response = call('github_convert_pull_request_to_draft', **args)
    assert response.json()['code'] == 'invalid_arguments'
    assert not provider.calls and not provider.minted


@pytest.mark.parametrize('change', ['already_draft', 'closed', 'merged', 'node', 'during_read'])
def test_draft_requires_ready_unmerged_current_pr(coding, change):
    *_, provider, call = coding
    provider.pr['draft'] = False
    if change == 'already_draft':
        provider.pr['draft'] = True
    elif change == 'closed':
        provider.pr['state'] = 'closed'
    elif change == 'merged':
        provider.pr['merged'] = True
    elif change == 'node':
        provider.pr.pop('node_id')
    else:
        provider.after_pr_read = lambda: provider.pr.update(draft=True)
    response = call('github_convert_pull_request_to_draft', **write_args('pr', {'intent': 'user_requested_draft_for_this_pr'}))
    assert 'error' in response.json() and not provider.dispatches


@pytest.mark.parametrize('name,event', [
    ('github_create_review', 'APPROVE'), ('github_create_review', 'DISMISS'),
    ('github_create_review', 'comment'), ('github_create_review', None),
    ('github_submit_review', 'APPROVE'), ('github_submit_review', 'PENDING'),
    ('github_submit_review', 'DISMISS'), ('github_comment_review', 'COMMENT'),
])
def test_nonapproval_review_schemas_are_finite(coding, name, event):
    *_, provider, call = coding
    args = {'body': 'Review', 'event': event}
    if name == 'github_submit_review':
        args.update(review_id=40, creation_request_key='pending-origin')
    response = call(name, **write_args('pr', args))
    assert response.json()['code'] == 'invalid_arguments', response.text
    assert not provider.calls and not provider.minted and not provider.dispatches


@pytest.mark.parametrize('event', ['COMMENT', 'REQUEST_CHANGES'])
@pytest.mark.parametrize('body', [None, '', ' \n '])
def test_submitted_review_creation_requires_summary(coding, event, body):
    *_, provider, call = coding
    args = {'event': event}
    if body is not None:
        args['body'] = body
    response = call('github_create_review', **write_args('pr', args))
    assert response.json()['code'] == 'invalid_arguments'
    assert not provider.calls and not provider.dispatches


def test_pending_review_can_start_without_summary(coding):
    *_, provider, call = coding
    result = call('github_create_review', **write_args('pr', {
        'event': 'PENDING', 'comments': [{'path': 'fix.py', 'line': 1, 'body': 'Please fix'}],
    })).json()
    assert result['confirmed'] and result['data']['state'] == 'PENDING'
    assert 'event' not in provider.dispatches[0][2]


@pytest.mark.parametrize('event', ['COMMENT', 'REQUEST_CHANGES'])
def test_submit_pending_review_preserves_saved_summary_when_omitted(coding, event):
    *_, provider, call = coding
    prepare_write(coding, 'github_submit_review')
    result = call('github_submit_review', **write_args('pr', {
        'event': event, 'review_id': 40, 'creation_request_key': 'pending-origin',
    })).json()
    assert result['confirmed'] and result['data']['body'] == 'Pending review'
    assert provider.dispatches[0][2] == {'event': event}


@pytest.mark.parametrize('name', ['github_submit_review', 'github_edit_pending_review', 'github_delete_pending_review'])
@pytest.mark.parametrize('mismatch', ['no_receipt', 'other_key', 'unconfirmed', 'version', 'repository', 'id',
                                     'author', 'human', 'state', 'commit', 'pr', 'changed_head', 'missing',
                                     'during_read', 'other_chat'])
def test_pending_review_is_owned_exact_and_still_pending(coding, name, mismatch):
    app, _, _, _, _, provider, call = coding
    prepare_write(coding, name)
    args = {'review_id': 40, 'creation_request_key': 'pending-origin'}
    if name != 'github_delete_pending_review':
        args['body'] = 'Updated review'
    if name == 'github_submit_review':
        args['event'] = 'REQUEST_CHANGES'
    review = provider.reviews[40]
    if mismatch == 'no_receipt':
        app.state.store.execute('DELETE FROM github_followups')
    elif mismatch in {'unconfirmed', 'version', 'repository'}:
        row = app.state.store.rows("SELECT * FROM github_followups WHERE result!=''")[0]
        if mismatch == 'unconfirmed':
            app.state.store.execute("UPDATE github_followups SET result='' WHERE id=?", (row['id'],))
        elif mismatch == 'version':
            app.state.store.execute("UPDATE github_followups SET connection_version='old' WHERE id=?", (row['id'],))
        else:
            result = json.loads(row['result'])
            result['repository_id'] = 202
            app.state.store.execute('UPDATE github_followups SET result=? WHERE id=?', (json.dumps(result), row['id']))
    elif mismatch == 'other_key':
        args['creation_request_key'] = 'different-origin'
    elif mismatch == 'id':
        review['id'] = 41
    elif mismatch == 'author':
        review['user']['id'] = 88
    elif mismatch == 'human':
        review['user']['type'] = 'User'
    elif mismatch == 'state':
        review['state'] = 'COMMENTED'
    elif mismatch == 'commit':
        review['commit_id'] = 'a' * 40
    elif mismatch == 'pr':
        review['pull_request_url'] = 'https://api.github.com/repos/BerriAI/litellm/pulls/999'
    elif mismatch == 'changed_head':
        provider.pr['head']['sha'] = args['head_sha'] = 'a' * 40
    elif mismatch == 'missing':
        provider.reviews.clear()
    elif mismatch == 'other_chat':
        import hashlib
        other_id = hashlib.sha256(b'other-chat:github_create_review:pending-origin').hexdigest()
        app.state.store.execute('UPDATE github_followups SET id=?', (other_id,))
    else:
        reads = []
        def submit_during_read(path):
            if '/reviews/' in path:
                reads.append(path)
                if len(reads) == 2:
                    review['state'] = 'COMMENTED'
        provider.after_read = submit_during_read
    response = call(name, **write_args('pr', args))
    assert 'error' in response.json() and not provider.dispatches, response.text


def test_pending_review_persists_across_brokers_then_edits_and_submits(coding, monkeypatch):
    import asyncio
    from app.github import GitHub
    app, _, rid, _, first, provider, call = coding
    prepare_write(coding, 'github_edit_pending_review')
    second = GitHub(first.store, first.security, first.settings, first.connectors)
    monkeypatch.setattr(second, 'app_jwt', lambda config=None: 'fixture-jwt')
    args = {'repository_id': 101, **write_args('pr', {
        'body': 'Updated summary', 'review_id': 40, 'creation_request_key': 'pending-origin',
    })}
    result = asyncio.run(second.call(app.state.store.run(rid), 'github_edit_pending_review', args))
    assert result['confirmed'] and provider.reviews[40]['body'] == 'Updated summary'
    result = call('github_submit_review', **write_args('pr', {
        'body': 'Please fix', 'review_id': 40, 'creation_request_key': 'pending-origin',
        'event': 'REQUEST_CHANGES', 'request_key': 'submit-pending',
    })).json()
    assert result['confirmed'] and provider.reviews[40]['state'] == 'CHANGES_REQUESTED'
    assert len(provider.dispatches) == 2
    response = call('github_delete_pending_review', **write_args('pr', {
        'review_id': 40, 'creation_request_key': 'pending-origin', 'request_key': 'delete-submitted',
    }))
    assert 'error' in response.json() and len(provider.dispatches) == 2


@pytest.mark.parametrize('name,args', PARITY_CASES)
def test_wrong_write_confirmation_stays_uncertain_without_replay(coding, name, args):
    *_, provider, call = coding
    prepare_write(coding, name)
    provider.write_response = {'data': {'convertPullRequestToDraft': {'pullRequest': {'number': 999, 'isDraft': True}}}}
    args = write_args('pr', args)
    first = call(name, **args)
    assert first.json()['code'] == 'outcome_unknown', first.text
    assert call(name, **args).json()['code'] == 'outcome_unknown'
    assert len(provider.dispatches) == 1


def test_pending_review_confirmation_failure_cannot_be_used_as_ownership(coding):
    *_, provider, call = coding
    provider.write_response = {}
    response = call('github_create_review', **write_args('pr', {
        'event': 'PENDING', 'body': 'Pending', 'request_key': 'pending-origin',
    }))
    assert response.json()['code'] == 'outcome_unknown'
    provider.write_response = None
    response = call('github_delete_pending_review', **write_args('pr', {
        'review_id': 40, 'creation_request_key': 'pending-origin',
    }))
    assert 'error' in response.json()
    assert len(provider.dispatches) == 1


@pytest.fixture
def exact_pr_coding(coding):
    from app.security import digest
    app, client, _, headers, github, provider, _ = coding
    app.state.store.execute('DELETE FROM github_publications')
    run = app.state.store.create_run('Exact PR work', '', 'modal', ['github'],
                                    chat_enabled=True, user_id='shared:local:admin')
    rid = run['id']
    app.state.store.claim_message(rid)
    app.state.store.update_run(rid, status='running', token_hash=digest('run-capability-only'))
    def call(name, **args):
        return client.post(f'/broker/{rid}/tools/call', headers=headers,
                           json={'name': name, 'arguments': {'repository_id': 101, **args}})
    return app, client, rid, headers, github, provider, call


@pytest.mark.parametrize('name,args', PARITY_CASES)
def test_parity_uses_existing_exact_pr_grant_and_respects_revocation(exact_pr_coding, name, args):
    from test_github_write_access import approved, decide
    *_, provider, call = exact_pr_coding
    args = write_args('pr', args)
    assert 'error' in call(name, **args).json()
    assert not provider.dispatches
    grant = approved(exact_pr_coding[:6])
    prepare_write(exact_pr_coding, name)
    result = call(name, **args).json()
    assert result.get('confirmed'), result
    assert decide(exact_pr_coding[:6], grant['id'], 'revoke').status_code == 200
    assert 'error' in call(name, **{**args, 'request_key': 'revoked-action'}).json()
    assert len(provider.dispatches) == 1


def test_retarget_does_not_transfer_an_exact_pr_grant(exact_pr_coding):
    from test_github_write_access import approved
    app, _, _, _, _, provider, call = exact_pr_coding
    grant = approved(exact_pr_coding[:6])
    result = call('github_retarget_pull_request', **write_args('pr', {'base_branch': 'release/candidate'})).json()
    assert result.get('confirmed'), result
    assert app.state.store.rows('SELECT status FROM github_write_access WHERE id=?', (grant['id'],))[0]['status'] == 'revoked'
    result = call('github_create_review', **write_args('pr', {'body': 'New base review', 'event': 'COMMENT'})).json()
    assert 'error' in result
    assert len(provider.dispatches) == 1


def test_retarget_same_base_keeps_exact_pr_grant(exact_pr_coding):
    from test_github_write_access import approved
    app, _, _, _, _, provider, call = exact_pr_coding
    grant = approved(exact_pr_coding[:6])
    result = call('github_retarget_pull_request', **write_args('pr', {'base_branch': 'main'})).json()
    assert result.get('confirmed'), result
    assert app.state.store.rows('SELECT status FROM github_write_access WHERE id=?', (grant['id'],))[0]['status'] == 'approved'


def test_unknown_retarget_never_broadens_or_replays_grant(exact_pr_coding):
    from test_github_write_access import approved
    *_, provider, call = exact_pr_coding
    approved(exact_pr_coding[:6])
    provider.lose_response = True
    args = write_args('pr', {'base_branch': 'release/candidate'})
    assert 'error' in call('github_retarget_pull_request', **args).json()
    provider.lose_response = False
    assert provider.pr['base']['ref'] == 'release/candidate'
    assert call('github_retarget_pull_request', **args).json()['code'] == 'outcome_unknown'
    assert 'error' in call('github_create_review', **write_args('pr', {'body': 'New review', 'event': 'COMMENT'})).json()
    assert len(provider.dispatches) == 1


def test_late_requester_change_revokes_grant_after_confirmed_retarget(exact_pr_coding, monkeypatch):
    from test_github_write_access import approved
    app, _, rid, _, github, provider, call = exact_pr_coding
    grant = approved(exact_pr_coding[:6])
    actor = app.state.store.run(rid)['active_user_id']
    revoke = github.revoke_retargeted_pr_access
    invalidations = []
    def invalidate(authority):
        invalidations.append((authority['id'], app.state.store.run(rid)['active_user_id']))
        revoke(authority)
    async def forbid_ui_decision(*args, **kwargs):
        raise AssertionError('Confirmed retargets must use the required invalidation owner, not UI consent.')
    monkeypatch.setattr(github, 'revoke_retargeted_pr_access', invalidate)
    monkeypatch.setattr(github, 'decide_write_access', forbid_ui_decision)
    provider.after_write = lambda: app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('different-requester', rid))
    args = write_args('pr', {'base_branch': 'release/candidate'})
    response = call('github_retarget_pull_request', **args)
    assert response.status_code == 200 and response.json().get('confirmed'), response.text
    assert response.json()['data']['base']['ref'] == 'release/candidate'
    assert invalidations == [(grant['id'], 'different-requester')]
    assert app.state.store.rows('SELECT status FROM github_write_access WHERE id=?', (grant['id'],))[0]['status'] == 'revoked'
    receipts = app.state.store.rows('SELECT sent,result FROM github_followups')
    assert len(receipts) == 1 and receipts[0]['sent'] == 1
    assert json.loads(receipts[0]['result']) == response.json()
    assert 'error' in call('github_retarget_pull_request', **args).json()
    provider.after_write = None
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (actor, rid))
    assert 'error' in call('github_retarget_pull_request', **args).json()
    assert 'error' in call('github_create_review', **write_args('pr', {'body': 'New review', 'event': 'COMMENT'})).json()
    assert invalidations == [(grant['id'], 'different-requester')]
    assert len(provider.dispatches) == 1


def test_retarget_invalidation_failure_never_falls_back_or_replays(exact_pr_coding, monkeypatch):
    from test_github_write_access import approved
    app, _, _, _, github, provider, call = exact_pr_coding
    approved(exact_pr_coding[:6])
    invalidations, decisions = [], []
    def unavailable_invalidation(authority):
        invalidations.append(authority['id'])
        raise RuntimeError('Invalidation persistence failed after dispatch')
    async def forbid_ui_decision(*args, **kwargs):
        decisions.append(True)
        raise AssertionError('No parallel revocation fallback is permitted.')
    monkeypatch.setattr(github, 'revoke_retargeted_pr_access', unavailable_invalidation)
    monkeypatch.setattr(github, 'decide_write_access', forbid_ui_decision)
    args = write_args('pr', {'base_branch': 'release/candidate'})
    assert 'error' in call('github_retarget_pull_request', **args).json()
    assert provider.pr['base']['ref'] == 'release/candidate'
    receipts = app.state.store.rows('SELECT sent,result FROM github_followups')
    assert len(receipts) == 1 and receipts[0]['sent'] == 1 and not receipts[0]['result']
    assert call('github_retarget_pull_request', **args).json()['code'] == 'outcome_unknown'
    assert len(invalidations) == 1 and not decisions
    assert len(provider.dispatches) == 1


@pytest.mark.parametrize('name,args', PARITY_CASES)
def test_grant_revocation_during_read_prevents_dispatch(exact_pr_coding, name, args):
    from test_github_write_access import approved
    app, _, _, _, _, provider, call = exact_pr_coding
    grant = approved(exact_pr_coding[:6])
    prepare_write(exact_pr_coding, name)
    provider.after_pr_read = lambda: app.state.store.execute(
        "UPDATE github_write_access SET status='revoked' WHERE id=?", (grant['id'],))
    result = call(name, **write_args('pr', args)).json()
    assert 'error' in result and not provider.dispatches


def test_reopen_rechecks_state_before_dispatch(coding):
    *_, provider, call = coding
    provider.pr['state'] = 'closed'
    provider.after_pr_read = lambda: provider.pr.update(state='open')
    result = call('github_reopen_pull_request', **write_args('pr', {})).json()
    assert 'error' in result and not provider.dispatches


@pytest.mark.parametrize('name,args', PARITY_CASES)
def test_parity_result_persistence_failure_never_replays(coding, monkeypatch, name, args):
    import asyncio
    from app.connector_errors import ConnectorError
    app, _, rid, _, github, provider, _ = coding
    prepare_write(coding, name)
    execute = app.state.store.execute
    def failing_save(sql, params=()):
        if sql.startswith('UPDATE github_followups SET result='):
            raise RuntimeError('Save failed after dispatch')
        return execute(sql, params)
    monkeypatch.setattr(app.state.store, 'execute', failing_save)
    args = {'repository_id': 101, **write_args('pr', args)}
    run = app.state.store.run(rid)
    with pytest.raises(RuntimeError, match='Save failed'):
        asyncio.run(github.call(run, name, args))
    monkeypatch.setattr(app.state.store, 'execute', execute)
    with pytest.raises(ConnectorError) as error:
        asyncio.run(github.call(run, name, args))
    assert error.value.code == 'outcome_unknown'
    assert len(provider.dispatches) == 1
