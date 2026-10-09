"""Real ASGI OAuth and scoped-read requests with synthetic upstream token identities."""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from test_workspace import workspace
from test_spend import sign_in
from test_slack import slack_app

PERSONAL = 'personal-sentinel-never-output'
SHARED = 'organization-sentinel-never-output'


@pytest.fixture
def personal(workspace, monkeypatch):
    app, client = workspace
    service = app.state.connectors.personal_slack
    sign_in(app, client)
    app.state.settings.slack_client_id = 'test-client'
    app.state.settings.slack_client_secret = 'test-secret'
    app.state.connectors.save('slack', {'access_token': SHARED}, 'Org')
    requests = []
    state = {'failure': False, 'hook': None}

    def upstream(request):
        requests.append(request)
        endpoint = request.url.path.rsplit('/', 1)[-1]
        hook = state['hook']
        if hook:
            hook(endpoint)
        if state['failure'] == 'network':
            raise httpx.ConnectError(PERSONAL, request=request)
        if state['failure'] == 'rate_limit':
            return httpx.Response(429, text=PERSONAL)
        if state['failure']:
            return httpx.Response(200, json={'ok': False, 'error': PERSONAL})
        if endpoint == 'oauth.v2.access':
            return httpx.Response(200, json={'ok': True, 'team': {'id': 'T12345678'},
                'authed_user': {'id': 'U12345678', 'token_type': 'user', 'access_token': PERSONAL,
                                'refresh_token': 'refresh-sentinel', 'scope': 'search:read'}})
        if endpoint == 'auth.test':
            return httpx.Response(200, json={'ok': True, 'team_id': 'T12345678', 'user_id': 'U12345678',
                                            'team': 'Test team', 'user': 'alice'})
        assert endpoint == 'search.messages'
        assert request.headers['authorization'] in {'Bearer ' + PERSONAL, 'Bearer ' + SHARED}
        identity = 'personal' if request.headers['authorization'] == 'Bearer ' + PERSONAL else 'organization'
        return httpx.Response(200, json={'ok': True, 'messages': {'matches': [{'text': identity}]}})

    original = httpx.AsyncClient
    monkeypatch.setattr('app.connectors.httpx.AsyncClient',
        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    return app, client, service, requests, state


def start(personal):
    _, client, _, _, _ = personal
    response = client.post('/api/connections/slack/personal/oauth')
    assert response.status_code == 200, response.text
    query = parse_qs(urlparse(response.json()['url']).query)
    assert 'scope' not in query
    assert query['redirect_uri'][0].endswith('/oauth/slack/personal/callback')
    return query['state'][0]


def connect(personal):
    state = start(personal)
    response = personal[1].get('/oauth/slack/personal/callback', params={'state': state, 'code': 'code'}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return state


def run(personal, private=True):
    app = personal[0]
    from app.security import digest
    r = app.state.store.create_run('Personal Slack test', '', 'modal', ['slack'],
        chat_enabled=True, user_id='google:alice', private_owner_id='google:alice' if private else '')
    app.state.store.claim_message(r['id'])
    app.state.store.update_run(r['id'], status='running', token_hash=digest('capability'))
    return app.state.store.run(r['id'])


def search(personal, r):
    return personal[1].post(f"/broker/{r['id']}/tools/call", headers={'Authorization': 'Bearer capability'},
        json={'name': 'slack_search', 'arguments': {'query': 'hello'}})


def test_oauth_owner_csrf_replay_and_encryption(personal):
    app, client, service, requests, _ = personal
    assert client.post('/api/connections/slack/personal/oauth', headers={'X-CSRF-Token': ''}).status_code == 403
    state = start(personal)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/oauth/slack/personal/callback', params={'state': state, 'code': 'code'}).status_code == 400
    assert not requests
    sign_in(app, client)
    # New sign-in has a distinct session: cannot redeem old browser state.
    assert client.get('/oauth/slack/personal/callback', params={'state': state, 'code': 'code'}).status_code == 400
    state = connect(personal)
    assert client.get('/oauth/slack/personal/callback', params={'state': state, 'code': 'code'}).status_code == 400
    status = client.get('/api/connections/slack/personal')
    assert status.json()['effective_source'] == 'personal'
    assert PERSONAL not in status.text
    assert PERSONAL not in service.row('google:alice')['encrypted']
    assert client.post('/api/connections/slack/personal/check').status_code == 200
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert not client.get('/api/connections/slack/personal').json()['connected']
    assert client.delete('/api/connections/slack/personal').status_code == 200
    assert service.configured('google:alice')


def test_selection_preference_disconnect_and_shared_denial(personal):
    app, client, _, requests, _ = personal
    r = run(personal)
    assert 'organization' in search(personal, r).text
    connect(personal)
    response = search(personal, r)
    assert 'personal' in response.text, response.text
    assert PERSONAL not in response.text
    shared = run(personal, False)
    before = len(requests)
    denied = search(personal, shared)
    assert 'private' in denied.text.lower(), denied.text
    assert len(requests) == before
    assert client.delete('/api/connections/slack/personal').status_code == 200
    assert app.state.store.run(r['id'])['private_owner_id'] == 'google:alice'
    assert 'organization' in search(personal, r).text


@pytest.mark.parametrize('failure', ['decrypt', 'expired', 'provider', 'network', 'rate_limit'])
def test_errors_never_fall_back_or_leak(personal, failure):
    app, _, service, requests, state = personal
    connect(personal)
    r = run(personal)
    if failure == 'decrypt':
        app.state.store.execute("UPDATE personal_slack_grants SET encrypted='broken'")
    elif failure == 'expired':
        service.save('google:alice', {'access_token': PERSONAL, 'expires_at': 1}, 'test', service.row('google:alice')['revision'])
    else:
        state['failure'] = failure
    requests.clear()
    response = search(personal, r)
    assert 'personal' in response.text.lower()
    assert PERSONAL not in response.text
    assert service.configured('google:alice')
    assert all(q.headers.get('authorization') != 'Bearer ' + SHARED for q in requests)


@pytest.mark.parametrize('change', ['disconnect', 'turn', 'policy', 'plugin'])
def test_postawait_recheck_discards_content(personal, change):
    app, _, service, _, state = personal
    connect(personal)
    r = run(personal)
    def hook(endpoint):
        if endpoint != 'search.messages':
            return
        if change == 'disconnect':
            service.disconnect('google:alice')
        elif change == 'turn':
            app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', r['id']))
        elif change == 'plugin':
            app.state.store.execute('UPDATE runs SET plugins=? WHERE id=?', ('[]', r['id']))
        else:
            app.state.store.execute("INSERT OR REPLACE INTO connection_policies(provider,enabled) VALUES('slack',0)")
    state['hook'] = hook
    response = search(personal, r)
    assert 'matches' not in response.text, response.text
    assert PERSONAL not in response.text


def test_oauth_disconnect_during_exchange_cannot_resurrect(personal):
    _, client, service, _, state = personal
    token = start(personal)
    state['hook'] = lambda endpoint: service.disconnect('google:alice') if endpoint == 'oauth.v2.access' else None
    response = client.get('/oauth/slack/personal/callback', params={'state': token, 'code': 'code'}, follow_redirects=False)
    assert response.status_code == 409
    assert not service.configured('google:alice')


def test_refresh_disconnect_cas_and_redacted_check(personal):
    _, client, service, _, state = personal
    connect(personal)
    service.save('google:alice', {'access_token': PERSONAL, 'expires_at': 1, 'refresh_token': 'refresh',
                                'team_id': 'T12345678', 'user_id': 'U12345678'}, 'test', service.row('google:alice')['revision'])
    state['hook'] = lambda endpoint: service.disconnect('google:alice') if endpoint == 'oauth.v2.access' else None
    response = client.post('/api/connections/slack/personal/check')
    assert response.status_code == 409
    assert PERSONAL not in response.text
    assert not service.configured('google:alice')


def test_personal_only_policy_and_unsupported_modes(personal):
    app, _, service, requests, _ = personal
    connect(personal)
    r = run(personal)
    app.state.store.execute("DELETE FROM connections WHERE provider='slack'")
    assert app.state.connectors.allowed('slack_search', run=r)
    assert 'matches' in search(personal, r).text
    app.state.store.execute("INSERT OR REPLACE INTO connection_policies(provider,enabled,read_only) VALUES('slack',1,1)")
    assert 'matches' in search(personal, r).text
    app.state.store.execute("UPDATE connection_policies SET enabled=0 WHERE provider='slack'")
    before = len(requests)
    assert 'matches' not in search(personal, r).text
    assert len(requests) == before
    app.state.store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='slack'")
    app.state.store.execute('UPDATE runs SET chat_enabled=0 WHERE id=?', (r['id'],))
    assert 'private' in search(personal, r).text.lower()
    assert service.configured('google:alice')


def test_rejection_and_expired_state_preserve_grant(personal):
    app, client, service, _, _ = personal
    connect(personal)
    revision = service.row('google:alice')['revision']
    state = start(personal)
    rejected = client.get('/oauth/slack/personal/callback', params={'state': state, 'error': 'access_denied'}, follow_redirects=False)
    assert rejected.status_code == 303
    assert service.row('google:alice')['revision'] == revision
    assert client.get('/oauth/slack/personal/callback', params={'state': state, 'code': 'code'}).status_code == 400
    state = start(personal)
    app.state.store.execute('UPDATE personal_slack_states SET expires=0')
    assert client.get('/oauth/slack/personal/callback', params={'state': state, 'code': 'code'}).status_code == 400
    assert service.row('google:alice')['revision'] == revision


def test_automation_http_denies_configured_personal(personal):
    _, client, _, _, _ = personal
    connect(personal)
    response = client.post('/api/automations', json={'definition': {
        'name': 'Slack report', 'prompt': 'Read Slack', 'plugins': ['slack'], 'mode': 'demo',
        'timing': {'frequency': 'daily', 'time': '09:00', 'timezone': 'UTC'}}})
    assert response.status_code == 409, response.text
    assert 'Personal Slack' in response.text


def test_refresh_success_and_shared_login_unavailable(personal):
    from fastapi.responses import JSONResponse
    app, client, service, requests, _ = personal
    connect(personal)
    service.save('google:alice', {'access_token': 'expired', 'expires_at': 1, 'refresh_token': 'refresh',
                                'team_id': 'T12345678', 'user_id': 'U12345678'}, 'test', service.row('google:alice')['revision'])
    requests.clear()
    assert 'matches' in search(personal, run(personal)).text
    assert [q.url.path.rsplit('/', 1)[-1] for q in requests] == ['oauth.v2.access', 'auth.test', 'search.messages']
    response = JSONResponse({})
    app.state.settings.password_login_enabled = True
    sid = app.state.security.new_session(response)
    client.cookies.set('workspace_session', response.headers['set-cookie'].split('workspace_session=')[1].split(';')[0])
    client.headers['X-CSRF-Token'] = app.state.security.csrf(sid)
    assert not client.get('/api/connections/slack/personal').json()['available']
    assert client.post('/api/connections/slack/personal/oauth').status_code == 403


def test_linked_identity_freshness_and_absent_owner(personal):
    from app.db import now
    app, _, _, requests, _ = personal
    connect(personal)
    r = run(personal, private=False)
    actor = 'slack:T12345678:U12345678'
    app.state.store.execute("INSERT INTO users(id,kind,name,created_at,updated_at) VALUES(?,'slack','Alice','','')", (actor,))
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', (actor, r['id']))
    assert 'organization' in search(personal, r).text
    app.state.store.execute("UPDATE users SET email='alice@berri.ai',linked_user_id='google:alice',profile_eligible=1,profile_checked_at=? WHERE id=?", (now(), actor))
    before = len(requests)
    assert 'private' in search(personal, r).text.lower()
    assert len(requests) == before
    app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE id=?", (actor,))
    before = len(requests)
    assert 'matches' not in search(personal, r).text
    assert len(requests) == before


def test_http_private_creation_discovery_and_disconnect_retains_privacy(personal, monkeypatch):
    app, client, service, requests, _ = personal
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    connect(personal)
    created = client.post('/api/runs', json={'prompt': 'Private source context', 'private_session': True,
                                           'plugins': ['slack']})
    assert created.status_code == 201, created.text
    r = created.json()
    assert r['private_owner_id'] == 'google:alice'
    from app.security import digest
    app.state.store.claim_message(r['id'])
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (r['id'],))
    app.state.store.update_run(r['id'], status='running', token_hash=digest('capability'))
    app.state.store.execute("DELETE FROM connections WHERE provider='slack'")
    headers = {'Authorization': 'Bearer capability'}
    listed = client.get(f"/broker/{r['id']}/tools", headers=headers)
    assert listed.status_code == 200, listed.text
    names = {t['name'] for t in listed.json()}
    assert {'slack_search', 'slack_thread'} <= names
    assert 'slack_send' not in names
    assert search(personal, r).json()['messages']['matches'][0]['text'] == 'personal'
    assert client.delete('/api/connections/slack/personal').status_code == 200
    assert client.get('/api/runs/' + r['id']).json()['private_owner_id'] == 'google:alice'
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/runs/' + r['id']).status_code == 404


def test_automation_history_never_copies_private_run(personal, monkeypatch):
    app, client, _, _, _ = personal
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    from test_automations import create
    automation = create(client)
    r = run(personal)
    app.state.store.update_run(r['id'], status='idle', summary='private-history-sentinel')
    app.state.store.execute('''INSERT INTO automation_runs(occurrence,automation_id,run_id,revision,outcome,detail,created_at)
        VALUES(?,?,?,1,'started','private-history-sentinel','2026-10-09')''',
        ('private-injected-history', automation['id'], r['id']))
    history = client.get('/api/automations')
    assert history.status_code == 200
    assert r['id'] not in history.text and 'private-history-sentinel' not in history.text
    launched = client.post('/api/automations/' + automation['id'] + '/run',
                           json={'revision': 1, 'client_id': 'new-history-test'})
    assert launched.status_code == 202, launched.text
    new_run = app.state.store.run(launched.json()['run_id'])
    assert r['id'] not in new_run['prompt'] and 'private-history-sentinel' not in new_run['prompt']


@pytest.mark.parametrize('configured', [False, True])
def test_slack_webhook_source_context_obeys_personal_boundary(slack_app, monkeypatch, configured):
    import asyncio
    from app.db import now
    from test_slack import event, signed
    app, client, runs, _ = slack_app
    sign_in(app, client)
    response = client.post('/hooks/slack/events', **signed(event(thread_ts='1790718000.123456')))
    assert response.status_code == 200
    r = app.state.store.run(runs[0]['id'])
    actor = r['active_user_id'] or r['owner_id']
    app.state.store.execute("UPDATE users SET email='alice@berri.ai',linked_user_id='google:alice',profile_eligible=1,profile_checked_at=? WHERE id=?", (now(), actor))
    service = app.state.connectors.personal_slack
    if configured:
        service.save('google:alice', {'access_token': PERSONAL}, 'Synthetic personal', service.revision('google:alice'))
    reads = []
    async def upstream(method, url, **kwargs):
        if 'conversations.' in url or 'chat.getPermalink' in url:
            reads.append(kwargs['headers']['Authorization'])
            return {'ok': True, 'messages': [{'ts': '1790718000.123456', 'text': 'shared-source-sentinel'}]}
        return {'ok': True}
    monkeypatch.setattr(app.state.connectors, 'request', upstream)
    asyncio.run(app.state.slack.prepare_source(r['id']))
    source = app.state.store.slack_source(r['id'])
    if configured:
        assert source['context_status'] == 'unavailable'
        assert 'private web chat' in source['warning']
        assert not reads and not source['messages']
    else:
        assert source['context_status'] == 'ready'
        assert reads and set(reads) == {'Bearer provider-user-secret'}


def test_missing_owner_record_does_not_fall_back(personal):
    app, _, service, requests, _ = personal
    connect(personal)
    r = run(personal)
    app.state.store.execute("DELETE FROM users WHERE id='google:alice'")
    requests.clear()
    response = search(personal, r)
    assert 'matches' not in response.text
    assert not requests and service.configured('google:alice')


def test_login_disabled_with_grant_never_uses_organization(personal):
    app, _, _, requests, _ = personal
    connect(personal)
    r = run(personal)
    app.state.settings.google_client_id = ''
    app.state.settings.google_client_secret = ''
    requests.clear()
    response = search(personal, r)
    assert 'matches' not in response.text
    assert not requests
