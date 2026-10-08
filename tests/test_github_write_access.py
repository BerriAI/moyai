"""Consent uses real app authentication and broker routes, never real GitHub."""

import pytest
from fastapi.testclient import TestClient

from app.connector_errors import ConnectorError
from app.github import GitHub
from app.security import digest
from test_workspace import workspace
from test_github import COMMIT, BASE, TREE, connected, select
from test_github_followups import FollowupAPI, NEXT, update_args, comment_args

REQUEST = 'github_request_pull_request_write_access'


class ExternalAPI(FollowupAPI):
    def __init__(self, github, monkeypatch):
        super().__init__(github, monkeypatch)
        self.branch = COMMIT
        self.pr = {'number': 100, 'html_url': 'https://github.com/BerriAI/litellm/pull/100',
                   'title': '<untrusted> External fix', 'state': 'open', 'draft': False,
                   'head': {'ref': 'human/fix', 'sha': COMMIT, 'repo': {'id': 101}},
                   'base': {'ref': 'main', 'repo': {'id': 101}}}
        self.tokens = []
        async def token(*args, **kwargs):
            target = github.target(kwargs.get('repository', ''))
            self.tokens.append(target)
            return f'test-token-{target}'
        monkeypatch.setattr(github, 'installation_token', token)

    async def request(self, method, path, **kwargs):
        if '/git/ref/heads/human/' in path or '/git/refs/heads/human/' in path:
            self.calls.append((method, path, kwargs))
            if self.on_call:
                self.on_call(method, path)
            if method == 'PATCH':
                assert kwargs['json']['force'] is False
                self.branch = kwargs['json']['sha']
                if self.lose == 'update':
                    self.lose = ''
                    raise ConnectorError('Lost response')
            return {'object': {'sha': self.branch}}
        if path == '/repositories/202/git/commits/' + COMMIT:
            self.calls.append((method, path, kwargs))
            return {'tree': {'sha': TREE}}
        return await super().request(method, path, **kwargs)


@pytest.fixture
def external(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = connected(app)
    store = app.state.store
    run = store.create_run('External PR work', '', 'modal', ['github'], chat_enabled=True, user_id='shared:local:admin')
    run_id = run['id']
    store.claim_message(run_id)
    store.update_run(run_id, status='running', token_hash=digest('run-capability-only'))
    store.execute("UPDATE runs SET owner_id='someone-else' WHERE id=?", (run_id,))
    github = app.state.connectors.github
    api = ExternalAPI(github, monkeypatch)
    return app, client, run_id, headers, github, api


def call(fixture, name=REQUEST, arguments=None, run_id=None, headers=None):
    app, client, rid, auth, _, _ = fixture
    return client.post(f'/broker/{run_id or rid}/tools/call', headers=headers or auth,
                       json={'name': name, 'arguments': arguments or {'repository_id': 101, 'number': 100}})


def decide(fixture, identity, decision='approve', **kwargs):
    _, client, rid, _, _, _ = fixture
    return client.post(f'/api/runs/{rid}/pr-write-access/{identity}', json={'decision': decision}, **kwargs)


def approved(fixture):
    response = call(fixture)
    assert response.status_code == 200, response.text
    row = response.json()
    assert row['status'] == 'pending', row
    assert decide(fixture, row['id']).json() == {'status': 'approved'}
    return row


@pytest.mark.parametrize('operation', ['update', 'comment'])
@pytest.mark.parametrize('lost', [False, True])
def test_consent_repeated_writes_restart_and_uncertain_response(external, monkeypatch, operation, lost):
    app, client, rid, headers, github, api = external
    args = (update_args() if operation == 'update' else comment_args()).model_dump()
    name = f'github_{operation}_pull_request'
    assert 'error' in call(external, name, args).json()
    row = approved(external)
    assert call(external).json()['status'] == 'approved'
    assert decide(external, row['id']).json()['status'] == 'approved'
    if lost:
        api.lose = operation
    first = call(external, name, args).json()
    assert ('Lost' in first.get('error', '')) if lost else first.get('number') == 100
    restored = GitHub(github.store, github.security, github.settings, github.connectors)
    monkeypatch.setattr(restored, 'request', api.request)
    monkeypatch.setattr(restored, 'installation_token', github.installation_token)
    monkeypatch.setattr(app.state.connectors, 'github', restored)
    app.state.store.execute('UPDATE runs SET active_message_id=42 WHERE id=?', (rid,))
    result = call(external, name, args).json()
    assert result['number'] == 100, result
    assert call(external, name, args).json() == result
    writes = [c for c in api.calls if c[0] == ('PATCH' if operation == 'update' else 'POST') and
              (operation == 'update' or c[1].endswith('/comments'))]
    assert len(writes) == 1
    args['request_key'] = 'another-approved-write'
    if operation == 'update':
        args['base_sha'] = NEXT
        # Controlled new base is now the previous commit.
        old = api.request
        async def next_base(method, path, **kwargs):
            if path.endswith('/git/commits/' + NEXT):
                return {'tree': {'sha': TREE}}
            return await old(method, path, **kwargs)
        monkeypatch.setattr(restored, 'request', next_base)
    assert call(external, name, args).json()['number'] == 100
    assert not app.state.store.rows('SELECT * FROM github_publications')


@pytest.mark.parametrize('decision', ['deny', 'revoke'])
def test_terminal_decisions_never_regrant(external, decision):
    row = approved(external) if decision == 'revoke' else call(external).json()
    expected = 'revoked' if decision == 'revoke' else 'denied'
    assert decide(external, row['id'], decision).json()['status'] == expected
    assert decide(external, row['id'], decision).json()['status'] == expected
    assert decide(external, row['id']).status_code == 409
    assert call(external).json()['status'] == expected
    assert 'error' in call(external, 'github_comment_pull_request', comment_args().model_dump()).json()


@pytest.mark.parametrize('boundary', ['csrf', 'bearer', 'actor', 'session', 'extra', 'broker_grant'])
def test_only_authenticated_requester_can_decide(external, boundary):
    app, client, rid, headers, github, api = external
    row = call(external).json()
    if boundary == 'csrf':
        response = decide(external, row['id'], headers={'X-CSRF-Token': ''})
    elif boundary == 'bearer':
        client.cookies.clear()
        response = decide(external, row['id'], headers=headers)
    elif boundary == 'actor':
        app.state.store.execute("UPDATE runs SET active_user_id='other-user' WHERE id=?", (rid,))
        response = decide(external, row['id'])
    elif boundary == 'session':
        response = client.post(f"/api/runs/another-chat/pr-write-access/{row['id']}", json={'decision': 'approve'})
    elif boundary == 'extra':
        response = client.post(f"/api/runs/{rid}/pr-write-access/{row['id']}", json={'decision': 'approve', 'actor': 'other-user'})
    else:
        response = call(external, arguments={'repository_id': 101, 'number': 100, 'approved': True})
    assert response.status_code >= 400 or 'error' in response.json(), response.text
    assert app.state.store.rows('SELECT status FROM github_write_access')[0]['status'] == 'pending'


@pytest.mark.parametrize('operation', ['update', 'comment'])
@pytest.mark.parametrize('boundary', ['actor', 'session', 'child', 'pr', 'repository', 'connection', 'readonly',
    'disabled', 'closed', 'merged', 'head_branch', 'head_repo', 'base_branch', 'base_repo', 'stop', 'delete'])
def test_scope_and_lifecycle_block_writes(external, operation, boundary):
    app, client, rid, headers, github, api = external
    if boundary == 'repository':
        select(app, (101, 202))
    approved(external)
    args = (update_args() if operation == 'update' else comment_args()).model_dump()
    if boundary == 'actor':
        app.state.store.execute("UPDATE runs SET active_user_id='other-user' WHERE id=?", (rid,))
    elif boundary in {'session', 'child'}:
        other = app.state.store.create_run('Another chat', '', 'modal', ['github'], chat_enabled=True, user_id='shared:local:admin')
        app.state.store.claim_message(other['id'])
        app.state.store.update_run(other['id'], status='running', token_hash=digest('other-cap'))
        if boundary == 'child':
            app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (rid, other['id']))
        rid, headers = other['id'], {'Authorization': 'Bearer other-cap'}
    elif boundary == 'pr': args['number'] = 101
    elif boundary == 'repository': args.update(repository='', repository_id=202)
    elif boundary == 'connection': github.save_app({'id': 999, 'pem': 'different'})
    elif boundary in {'readonly', 'disabled'}:
        app.state.store.execute('INSERT INTO connection_policies(provider,read_only,enabled) VALUES(?,?,?)',
                                 ('github', int(boundary == 'readonly'), int(boundary != 'disabled')))
    elif boundary == 'closed': api.pr['state'] = 'closed'
    elif boundary == 'merged': api.pr['merged'] = True
    elif boundary == 'head_branch': api.pr['head']['ref'] = 'other'
    elif boundary == 'head_repo': api.pr['head']['repo']['id'] = 202
    elif boundary == 'base_branch': api.pr['base']['ref'] = 'other'
    elif boundary == 'base_repo': api.pr['base']['repo']['id'] = 202
    elif boundary == 'stop': app.state.store.update_run(rid, status='cancelled')
    elif boundary == 'delete': app.state.store.execute("UPDATE runs SET deleted_at='now' WHERE id=?", (rid,))
    api.calls.clear()
    response = call(external, f'github_{operation}_pull_request', args, run_id=rid, headers=headers)
    assert response.status_code >= 400 or 'error' in response.json(), response.text
    assert not [c for c in api.calls if c[0] != 'GET']


def test_stale_checkout_and_revocation_during_operation(external):
    app, client, rid, headers, github, api = external
    row = approved(external)
    api.branch = BASE
    response = call(external, 'github_update_pull_request', update_args().model_dump()).json()
    assert 'head changed' in response['error']
    api.branch = COMMIT
    def revoke(method, path):
        if method == 'POST' and path.endswith('/git/commits'):
            app.state.store.execute("UPDATE github_write_access SET status='revoked' WHERE id=?", (row['id'],))
    api.on_call = revoke
    response = call(external, 'github_update_pull_request', update_args().model_dump()).json()
    assert 'error' in response
    assert not [c for c in api.calls if c[0] == 'PATCH']


@pytest.mark.parametrize('head_enabled', [False, True])
def test_fork_base_comments_and_independently_enabled_code(external, head_enabled):
    app, client, rid, headers, github, api = external
    if head_enabled:
        select(app, (101, 202))
    api.pr['head']['repo']['id'] = 202
    approved(external)
    comment = call(external, 'github_comment_pull_request', comment_args().model_dump()).json()
    assert comment['number'] == 100
    response = call(external, 'github_update_pull_request', update_args().model_dump()).json()
    if head_enabled:
        assert response['commit'] == NEXT, response
        writes = [c for c in api.calls if c[0] == 'PATCH']
        assert writes[0][1].startswith('/repositories/202/')
        assert writes[0][2]['token'] == 'test-token-202'
    else:
        assert 'error' in response
        assert not [c for c in api.calls if c[0] == 'PATCH']


def test_uncertain_missing_comment_never_reposts(external):
    approved(external)
    api = external[-1]
    api.lose = 'comment'
    args = comment_args().model_dump()
    assert 'error' in call(external, 'github_comment_pull_request', args).json()
    api.comments.clear()
    assert 'not be posted again' in call(external, 'github_comment_pull_request', args).json()['error']
    assert len([c for c in api.calls if c[0] == 'POST' and c[1].endswith('/comments')]) == 1


def test_session_api_and_discovery_schema(external):
    app, client, rid, headers, github, api = external
    row = call(external).json()
    response = client.get(f'/api/runs/{rid}')
    assert response.status_code == 200
    assert response.json()['pr_write_access'][0]['id'] == row['id']
    assert row['url'].endswith('/#run=' + rid)
    from app.github import TOOLS
    from sandbox.github_tools import advertised_tools
    schema = TOOLS[REQUEST][2].model_json_schema()
    exposed = advertised_tools([{'name': REQUEST, 'inputSchema': schema}])[0]
    assert exposed['inputSchema']['additionalProperties'] is False
    assert set(exposed['inputSchema']['properties']) == {'repository', 'repository_id', 'number'}
    app.state.store.execute("UPDATE runs SET active_user_id='other-user' WHERE id=?", (rid,))
    assert client.get(f'/api/runs/{rid}').json()['pr_write_access'] == []


def test_real_signed_other_user_and_admin_cannot_approve_member_request(external):
    from test_spend import sign_in
    app, client, rid, headers, github, api = external
    member = sign_in(app, client, sub='bob', email='bob@berri.ai')
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (member, rid))
    row = call(external).json()
    sign_in(app, client)  # The administrator is not the requester.
    assert decide(external, row['id']).status_code == 403
    sign_in(app, client, sub='bob', email='bob@berri.ai')
    assert decide(external, row['id']).json()['status'] == 'approved'
    assert call(external, 'github_comment_pull_request', comment_args().model_dump()).json()['number'] == 100


def test_request_discovery_and_denied_extra_scope(external):
    app, client, rid, headers, github, api = external
    tools = client.get(f'/broker/{rid}/tools', headers=headers).json()
    item = next(t for t in tools if t['name'] == REQUEST)
    assert item['inputSchema']['additionalProperties'] is False
    assert 'approved' not in item['inputSchema']['properties']
    row = approved(external)
    # Changed connection must not permit a stale browser approval.
    github.save_app({'id': 999, 'pem': 'changed'})
    assert decide(external, row['id']).status_code == 409
    assert decide(external, row['id'], 'revoke').json()['status'] == 'revoked'


def test_cancellation_durable_after_restart(external):
    app, client, rid, headers, github, api = external
    approved(external)
    response = client.post(f'/api/runs/{rid}/cancel')
    assert response.status_code == 200
    app.state.store.update_run(rid, status='running', token_hash=digest('run-capability-only'))
    assert call(external).json()['status'] == 'revoked'


def test_request_metadata_change_and_idempotency(external):
    app, client, rid, headers, github, api = external
    first = call(external).json()
    assert call(external).json() == first
    assert len(app.state.store.rows('SELECT * FROM github_write_access')) == 1
    api.pr['base']['ref'] = 'other'
    assert 'error' in call(external).json()
    assert len(app.state.store.rows('SELECT * FROM github_write_access')) == 1


@pytest.mark.parametrize('profile', ['fresh', 'stale', 'conflict', 'wrong_email', 'linked_only'])
def test_slack_requester_ui_uses_verified_identity_not_accounting_link(external, profile):
    from app.db import now
    from test_spend import sign_in
    app, client, rid, headers, github, api = external
    actor = sign_in(app, client)
    store = app.state.store
    store.execute("""INSERT INTO users(id,kind,email,name,linked_user_id,created_at,updated_at,
        profile_eligible,profile_checked_at,profile_conflict)
        VALUES('slack:requester','slack',?,'Slack requester',?,?,?, ?,?,?)""",
        ('wrong@berri.ai' if profile == 'wrong_email' else 'alice@berri.ai', actor, now(), now(),
         int(profile != 'linked_only'), '2000-01-01T00:00:00+00:00' if profile == 'stale' else now(), int(profile == 'conflict')))
    store.execute("UPDATE runs SET active_user_id='slack:requester' WHERE id=?", (rid,))
    row = call(external).json()
    data = client.get(f'/api/runs/{rid}').json()['pr_write_access']
    if profile != 'fresh':
        assert data == []
        assert decide(external, row['id']).status_code == 403
        return
    assert data[0]['id'] == row['id']
    assert decide(external, row['id']).json()['status'] == 'approved'
    # A web follow-up by that verified person retains consent but not author identity.
    store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (actor, rid))
    assert call(external).json()['id'] == row['id']
    assert call(external, 'github_comment_pull_request', comment_args().model_dump()).json()['number'] == 100
    store.execute("UPDATE users SET profile_conflict=1 WHERE id='slack:requester'")
    assert 'error' in call(external, 'github_comment_pull_request', comment_args().model_dump()).json()


@pytest.mark.parametrize('branch', ['../issues', 'x/../../pulls', '/main', 'a?b', 'a#b', 'a\\b', 'a\nb', 'a.lock'])
def test_untrusted_branch_metadata_is_rejected_or_encoded(external, branch):
    app, client, rid, headers, github, api = external
    api.pr['head']['ref'] = branch
    response = call(external).json()
    if branch == 'a#b':  # Valid Git branch, encoded before a ref API request.
        assert response['status'] == 'pending'
    else:
        assert 'error' in response
        assert not app.state.store.rows('SELECT * FROM github_write_access')


def test_demo_real_consent_message_resume_and_revoke(tmp_path):
    from scripts.pr_write_access_demo import demo
    from test_workspace import wait_for
    app = demo(tmp_path)
    with TestClient(app, base_url='http://127.0.0.1:8794', client=('127.0.0.1', 50000)) as client:
        session = client.get('/api/session').json()
        client.headers.update({'Origin': 'http://127.0.0.1:8794', 'X-CSRF-Token': session['csrf']})
        start = client.post('/demo/start')
        assert start.status_code == 200, start.text
        rid = start.json()['id']
        run = client.get(f'/api/runs/{rid}').json()
        row = run['pr_write_access'][0]
        assert row['status'] == 'pending'
        endpoint = f"/api/runs/{rid}/pr-write-access/{row['id']}"
        assert client.post(endpoint, json={'decision': 'approve'}).json()['status'] == 'approved'
        message = client.post(f'/api/runs/{rid}/messages', json={'content': 'Continue the approved work', 'client_id': 'local-demo-first-resume'})
        assert message.status_code == 202, message.text
        wait_for(lambda: len(app.state.demo_github.comments) == 1)
        wait_for(lambda: app.state.store.run(rid)['status'] == 'completed')
        assert app.state.demo_github.branch == NEXT
        assert client.post(endpoint, json={'decision': 'revoke'}).json()['status'] == 'revoked'
        assert client.post(f'/api/runs/{rid}/messages', json={'content': 'Continue again', 'client_id': 'local-demo-second-resume'}).status_code == 202
        wait_for(lambda: app.state.store.messages(rid)[-1]['role'] == 'assistant' and 'revoked' in app.state.store.messages(rid)[-1]['content'])
        assert len(app.state.demo_github.comments) == 1
        assert not app.state.store.rows('SELECT * FROM github_publications')


def test_authenticated_revocation_during_inflight_write(external):
    import httpx
    app, client, rid, headers, github, api = external
    row = approved(external)
    original = github.request
    async def transport(method, path, **kwargs):
        result = await original(method, path, **kwargs)
        if method == 'POST' and path.endswith('/git/commits'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=app.state.settings.public_url,
                                         cookies=client.cookies, headers=dict(client.headers)) as browser:
                response = await browser.post(f"/api/runs/{rid}/pr-write-access/{row['id']}", json={'decision': 'revoke'})
                assert response.status_code == 200, response.text
        return result
    github.request = transport
    response = call(external, 'github_update_pull_request', update_args().model_dump()).json()
    assert 'revoked' in response['error']
    assert not [c for c in api.calls if c[0] == 'PATCH']


@pytest.mark.parametrize('changed', ['actor', 'turn', 'connection'])
def test_inflight_requester_turn_and_connection_guards(external, changed):
    app, client, rid, headers, github, api = external
    approved(external)
    def mutate(method, path):
        if method == 'POST' and path.endswith('/git/trees'):
            if changed == 'actor':
                app.state.store.execute("UPDATE runs SET active_user_id='other-user' WHERE id=?", (rid,))
            elif changed == 'turn':
                app.state.store.execute('UPDATE runs SET active_message_id=active_message_id+1 WHERE id=?', (rid,))
            else:
                github.save_app({'id': 999, 'pem': 'changed'})
    api.on_call = mutate
    assert 'error' in call(external, 'github_update_pull_request', update_args().model_dump()).json()
    assert not [c for c in api.calls if c[0] == 'PATCH' or c[0] == 'POST' and c[1].endswith('/git/commits')]
