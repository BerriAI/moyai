"""Exercise session ownership, concurrency and response recovery through the broker."""
import asyncio
import copy
import json

import pytest
from pydantic import ValidationError

from app.connector_errors import ConnectorError
from app.github import Change, Comment, Publish, Update, MAX_FILE, MAX_TOTAL
from sandbox.broker_transport import CONTENT_TYPE, seal, unseal, MAX_BODY
from test_github import BASE, COMMIT, TREE, PAYLOAD, GitHubAPI, connected
from test_workspace import workspace

NEXT = '1' * 40


class FollowupAPI(GitHubAPI):
    def __init__(self, *args):
        super().__init__(*args)
        self.comments = []
        self.next_commit = NEXT

    async def request(self, method, path, **kwargs):
        prefix = '/repos/' + self.repository
        if path in {prefix + '/pulls/100', prefix + '/git/commits/' + COMMIT} or (
                path.endswith('/comments') or path.endswith('/reviews') or method == 'PATCH'
                or (method == 'POST' and path.endswith('/git/commits') and self.pr)):
            self.calls.append((method, path, kwargs))
            if self.on_call:
                self.on_call(method, path)
            if path.endswith('/pulls/100'):
                return {**self.pr, 'head': {**self.pr['head'], 'sha': self.branch}}
            if path.endswith('/git/commits/' + COMMIT):
                return {'tree': {'sha': TREE}}
            if path.endswith('/git/commits'):
                return {'sha': self.next_commit}
            if method == 'PATCH':
                assert '/git/refs/heads/moyai/' in path and kwargs['json']['force'] is False
                self.branch = kwargs['json']['sha']
                if self.lose == 'update':
                    self.lose = ''
                    raise ConnectorError('Lost response')
                return {}
            if method == 'GET':
                return copy.deepcopy(self.comments)
            comment = {'id': 321, 'html_url': self.pr['html_url'] + '#issuecomment-321', **kwargs['json']}
            self.comments.append(comment)
            if self.lose == 'comment':
                self.lose = ''
                raise ConnectorError('Lost response')
            return comment
        return await super().request(method, path, **kwargs)


@pytest.fixture
def published(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    github = app.state.connectors.github
    api = FollowupAPI(github, monkeypatch)
    run = app.state.store.run(run_id)
    receipt = asyncio.run(github.publish(run, Publish.model_validate(PAYLOAD)))
    api.pr.update(head={'ref': receipt['branch'], 'sha': COMMIT, 'repo': {'full_name': api.repository}})
    api.calls.clear()
    return app, client, run, headers, github, api


def update_args():
    return Update.model_validate({k: v for k, v in {
        **PAYLOAD, 'base_sha': COMMIT, 'number': 100, 'request_key': 'revision-two',
        'files': [{'path': 'fix.py', 'content': 'answer = 43\n'}]}.items() if k != 'body'})


def comment_args():
    return Comment(repository=PAYLOAD['repository'], number=100, body='@review-bot review', request_key='review-second-head')


@pytest.mark.parametrize('operation', ['update', 'comment'])
@pytest.mark.parametrize('lost', [False, True])
def test_followup_recovery_does_not_repeat_write_across_turns(published, operation, lost):
    app, client, run, headers, github, api = published
    args = update_args() if operation == 'update' else comment_args()
    if lost:
        api.lose = operation
        with pytest.raises(ConnectorError, match='Lost'):
            asyncio.run(getattr(github, operation)(run, args))
    app.state.store.execute('UPDATE runs SET active_message_id=42 WHERE id=?', (run['id'],))
    run = app.state.store.run(run['id'])
    result = asyncio.run(getattr(github, operation)(run, args))
    assert asyncio.run(getattr(github, operation)(run, args)) == result
    writes = [c for c in api.calls if c[0] == ('PATCH' if operation == 'update' else 'POST')
              and (operation == 'update' or c[1].endswith('/comments'))]
    assert len(writes) == 1
    assert result.get('commit', NEXT) == NEXT
    assert len(api.comments) == (operation == 'comment')
    with pytest.raises(ConnectorError, match='different changes'):
        changed = args.model_copy(update={'title': 'Changed revision'} if operation == 'update' else {'body': 'Different request'})
        asyncio.run(getattr(github, operation)(run, changed))


@pytest.mark.parametrize('operation', ['update', 'comment'])
@pytest.mark.parametrize('restriction', ['other_session', 'other_pr', 'other_repo', 'closed', 'merged', 'foreign_branch', 'fork', 'read_only', 'revoked', 'installation'])
def test_followup_boundaries_prevent_writes(published, operation, restriction):
    app, client, run, headers, github, api = published
    args = update_args() if operation == 'update' else comment_args()
    if restriction == 'other_session':
        app.state.store.execute("UPDATE github_publications SET run_id='another-session'")
    elif restriction == 'other_pr':
        args = args.model_copy(update={'number': 101})
    elif restriction == 'other_repo':
        args = args.model_copy(update={'repository': 'BerriAI/another'})
    elif restriction == 'closed':
        api.pr['state'] = 'closed'
    elif restriction == 'merged':
        api.pr['merged'] = True
    elif restriction == 'foreign_branch':
        api.pr['head']['ref'] = 'main'
    elif restriction == 'fork':
        api.pr['head']['repo']['full_name'] = 'someone/litellm'
    elif restriction == 'read_only':
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('github',1)")
    elif restriction == 'revoked':
        app.state.store.update_run(run['id'], token_hash='')
    else:
        github.save_app({'id': 999, 'pem': 'different'})
    with pytest.raises(ConnectorError):
        asyncio.run(getattr(github, operation)(run, args))
    assert all(c[0] == 'GET' for c in api.calls)


def test_stale_head_and_race_after_commit_do_not_overwrite(published):
    app, client, run, headers, github, api = published
    api.branch = BASE
    with pytest.raises(ConnectorError, match='head changed'):
        asyncio.run(github.update(run, update_args()))
    assert not any(c[0] != 'GET' for c in api.calls)
    api.branch = COMMIT
    def race(method, path):
        if method == 'POST' and path.endswith('/git/commits'):
            api.branch = BASE
    api.on_call = race
    with pytest.raises(ConnectorError, match='changed externally'):
        asyncio.run(github.update(run, update_args()))
    assert not any(c[0] == 'PATCH' for c in api.calls)


def test_revocation_before_branch_update(published):
    app, client, run, headers, github, api = published
    def revoke(method, path):
        if method == 'POST' and path.endswith('/git/commits'):
            app.state.store.update_run(run['id'], status='cancelled')
    api.on_call = revoke
    with pytest.raises(ConnectorError, match='changed'):
        asyncio.run(github.update(run, update_args()))
    assert not any(c[0] == 'PATCH' for c in api.calls)


def test_uncertain_missing_comment_is_not_reposted(published):
    app, client, run, headers, github, api = published
    api.lose = 'comment'
    with pytest.raises(ConnectorError):
        asyncio.run(github.comment(run, comment_args()))
    api.comments.clear()
    with pytest.raises(ConnectorError, match='not confirmed'):
        asyncio.run(github.comment(run, comment_args()))
    assert len([c for c in api.calls if c[0] == 'POST']) == 1


def test_broker_exposes_followups_without_approval_and_reads_feedback(published):
    app, client, run, headers, github, api = published
    url = f"/broker/{run['id']}/tools/call"
    for operation, args in [('update', update_args()), ('comment', comment_args())]:
        name = 'github_' + operation + '_pull_request'
        response = client.post(url, headers=headers, json={'name': name, 'arguments': args.model_dump()})
        assert response.status_code == 200 and 'url' in response.json()
        assert not app.state.store.approvals(run['id'])
    for kind in ['discussion', 'inline', 'reviews']:
        response = client.post(url, headers=headers, json={'name': 'github_pull_request_comments',
            'arguments': {'repository': api.repository, 'number': 100, 'kind': kind}})
        assert response.json()['items'][0]['body'].startswith('@review-bot review')
        assert response.json()['next_page'] is None
    api.comments *= 30
    result = asyncio.run(github.call(run, 'github_pull_request_comments', {'number': 100, 'page': 2}))
    assert result['next_page'] == 3 and api.calls[-1][2]['params']['page'] == 2


def test_large_schema_passes_server_envelope_and_blob_publication(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    api = GitHubAPI(app.state.connectors.github, monkeypatch)
    # Larger than the reported 2,931,023-byte schema and the former broker cap.
    content = '// generated schema\n' + 'x' * (6 * 1024 * 1024)
    body = {'name': 'github_create_pull_request', 'arguments': {**PAYLOAD, 'files': [{'path': 'schema.d.ts', 'content': content}]}}
    raw = json.dumps(body).encode()
    assert len(raw) > MAX_BODY
    packet = seal('run-capability-only', '/tools/call', raw)
    assert unseal('run-capability-only', '/tools/call', packet) == raw
    response = client.post(f'/broker/{run_id}/tools/call', headers={**headers, 'Content-Type': CONTENT_TYPE}, content=packet)
    assert response.status_code == 200 and response.json()['number'] == 100
    blob = next(c[2]['json'] for c in api.calls if c[1].endswith('/git/blobs'))
    assert blob == {'content': content, 'encoding': 'utf-8'}
    tree = next(c[2]['json']['tree'] for c in api.calls if c[0] == 'POST' and c[1].endswith('/git/trees'))
    assert 'content' not in tree[0] and tree[0]['sha']


def test_utf8_byte_and_total_limits():
    assert Change(path='schema.d.ts', content='x' * 2_931_023)
    assert Change(path='limit.ts', content='x' * MAX_FILE)
    with pytest.raises(ValidationError):
        Change(path='too-large.ts', content='é' * (MAX_FILE // 2 + 1))
    files = [{'path': str(i), 'content': 'x' * MAX_FILE} for i in range(2)]
    assert Publish.model_validate({**PAYLOAD, 'files': files})
    with pytest.raises(ValidationError):
        Publish.model_validate({**PAYLOAD, 'files': files + [{'path': 'extra', 'content': 'x'}]})
    with pytest.raises(ValidationError):
        Comment(number=100, body='   ', request_key='empty-comment')
