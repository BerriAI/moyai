"""Exercise Slack sender isolation through the real authenticated broker and HTTP adapter."""
import copy
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from test_workspace import cloud_capability, workspace
from app.db import now
from app.security import digest


USER = 'shared-search-user-secret'
BOT = 'workspace-bot-secret'
CREDENTIALS = {'access_token': USER, 'kind': 'oauth',
               'bot': {'access_token': BOT, 'scope': 'chat:write,im:write,im:history',
                       'bot_user_id': 'U99999999', 'team': {'id': 'T12345678'}}}


@pytest.fixture
def sender(workspace, monkeypatch):
    app, client = workspace
    run_id, headers = cloud_capability(app, ['slack'])
    app.state.connectors.save('slack', copy.deepcopy(CREDENTIALS), 'Test team')
    for sub, name in [('alice', 'Moe'), ('bob', 'Tin')]:
        app.state.store.identity({'method': 'google', 'identity': {'sub': sub, 'email': sub + '@berri.ai', 'name': name}})
    app.state.store.execute("INSERT INTO users(id,kind,name,created_at,updated_at) VALUES('slack:T12345678:U12345678','slack','Moe','','')")
    app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:alice', run_id))
    requests = []

    def upstream(request):
        requests.append(request)
        endpoint = request.url.path.rsplit('/', 1)[-1]
        if endpoint == 'conversations.open':
            return httpx.Response(200, json={'ok': True, 'channel': {'id': 'D12345678'}})
        if endpoint == 'oauth.v2.access':
            assert parse_qs(request.content.decode())['refresh_token'] == ['bot-refresh']
            return httpx.Response(200, json={'ok': True, 'access_token': 'rotated-bot-secret',
                                            'refresh_token': 'rotated-refresh', 'expires_in': 3600})
        if endpoint == 'search.messages':
            return httpx.Response(200, json={'ok': True, 'messages': {'matches': []}})
        if endpoint == 'conversations.replies':
            return httpx.Response(200, json={'ok': True, 'messages': []})
        if endpoint in {'users.lookupByEmail', 'users.info'}:
            return httpx.Response(200, json={'ok': True, 'user': {'id': 'U12345678', 'team_id': 'T12345678',
                'profile': {'email': 'alice@berri.ai', 'real_name': 'Moe'}}})
        assert endpoint == 'chat.postMessage'
        return httpx.Response(200, json={'ok': True, 'channel': json.loads(request.content)['channel'],
                                        'ts': '1790719999.123456', 'message': {'user': 'U99999999'}})

    original = httpx.AsyncClient
    monkeypatch.setattr('app.connectors.httpx.AsyncClient',
                        lambda **kwargs: original(transport=httpx.MockTransport(upstream), **kwargs))
    return app, client, run_id, headers, requests


def invoke(sender, name='slack_send', arguments=None):
    _, client, run_id, headers, _ = sender
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': name, 'arguments': arguments if arguments is not None else {'channel': 'C12345678', 'text': 'Requested message'}})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize('channel', ['C12345678', 'G12345678', 'D12345678'])
@pytest.mark.parametrize('actor', ['google:alice', 'google:bob', 'slack:T12345678:U12345678'])
def test_every_requester_sends_as_bot(sender, channel, actor):
    app, client, run_id, _, requests = sender
    app.state.store.execute('UPDATE runs SET owner_id=?,active_user_id=? WHERE id=?', ('google:connection-owner', actor, run_id))
    result = invoke(sender, arguments={'channel': channel, 'text': 'Requested message'})
    assert result['message']['user'] == 'U99999999'
    assert len(requests) == 1 and requests[0].headers['Authorization'] == f'Bearer {BOT}'
    assert json.loads(requests[0].content) == {'channel': channel, 'text': ('Tin' if actor == 'google:bob' else 'Moe') + ': Requested message',
                                              'unfurl_links': False, 'unfurl_media': False}
    assert USER not in client.get(f'/api/runs/{run_id}').text
    assert BOT not in client.get(f'/api/runs/{run_id}').text


def test_shared_chat_followup_changes_actor_without_changing_sender(sender):
    app, client, _, headers, requests = sender
    store = app.state.store
    run = store.create_run('Send a requested message', '', 'modal', ['slack'], chat_enabled=True, user_id='google:alice')
    run_id = run['id']
    for actor in ['google:alice', 'google:bob']:
        if actor == 'google:bob':
            store.enqueue_message(run_id, 'Send my message too', 'bob-followup', user_id=actor)
        turn = store.claim_message(run_id)
        store.update_run(run_id, status='running', token_hash=digest('run-capability-only'))
        assert invoke((app, client, run_id, headers, requests))['ok']
        store.finish_message(run_id, turn['id'], 'Sent')
        store.update_run(run_id, status='idle')
    assert store.run(run_id)['owner_id'] == 'google:alice'
    assert [r.headers['Authorization'] for r in requests] == [f'Bearer {BOT}'] * 2
    assert [json.loads(r.content)['text'] for r in requests] == ['Moe: Requested message', 'Tin: Requested message']


@pytest.mark.parametrize('actor', ['', 'google:missing', 'shared:local:admin'])
def test_unknown_sender_never_uses_session_owner(sender, actor):
    app, _, run_id, _, requests = sender
    app.state.store.execute('UPDATE runs SET chat_enabled=1,owner_id=?,active_user_id=? WHERE id=?',
                            ('google:alice', actor, run_id))
    assert 'identified requester' in invoke(sender)['error']
    assert not requests


def test_agent_cannot_supply_a_sender_override(sender):
    _, client, run_id, headers, requests = sender
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'slack_send', 'arguments': {'channel': 'C12345678', 'text': 'Hello', 'sender': 'Tin'}})
    assert response.status_code == 422 and not requests


def test_sender_name_is_one_line_and_cannot_create_slack_mentions(sender):
    app, _, _, _, requests = sender
    app.state.store.execute("UPDATE users SET name=? WHERE id='google:alice'", ('Moe\n<!channel> & team',))
    assert invoke(sender)['ok']
    assert json.loads(requests[0].content)['text'] == 'Moe &lt;!channel&gt; &amp; team: Requested message'


@pytest.mark.parametrize('recipient', ['U12345678', 'W12345678'])
def test_dm_opens_bots_conversation_before_sending(sender, recipient):
    requests = sender[-1]
    assert invoke(sender, arguments={'channel': recipient, 'text': 'Hello from the app'})['ok']
    assert [r.url.path for r in requests] == ['/api/conversations.open', '/api/chat.postMessage']
    assert json.loads(requests[0].content) == {'users': recipient}
    assert json.loads(requests[1].content)['channel'] == 'D12345678'
    assert json.loads(requests[1].content)['text'] == 'Moe: Hello from the app'
    assert all(r.headers['Authorization'] == f'Bearer {BOT}' for r in requests)


def test_expired_shared_user_cannot_break_healthy_bot_send(sender):
    app = sender[0]
    creds = copy.deepcopy(CREDENTIALS)
    creds.update(expires_at=1, refresh_token='must-never-refresh-user')
    app.state.connectors.save('slack', creds, 'Test team')
    assert invoke(sender)['ok']
    assert len(sender[-1]) == 1


def test_bot_refresh_preserves_user_and_uses_rotated_bot(sender):
    app = sender[0]
    creds = copy.deepcopy(CREDENTIALS)
    creds['bot'].update(expires_at=1, refresh_token='bot-refresh')
    app.state.connectors.save('slack', creds, 'Test team')
    assert invoke(sender)['ok']
    assert [r.url.path for r in sender[-1]] == ['/api/oauth.v2.access', '/api/chat.postMessage']
    assert sender[-1][-1].headers['Authorization'] == 'Bearer rotated-bot-secret'
    stored = json.loads(app.state.security.decrypt(app.state.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]['encrypted']))
    assert stored['access_token'] == USER
    assert stored['bot']['scope'] == 'chat:write,im:write,im:history'


@pytest.mark.parametrize('problem', ['missing', 'expired', 'no_chat_scope', 'no_im_scope'])
def test_unavailable_bot_never_falls_back_to_shared_user(sender, problem):
    app = sender[0]
    creds = copy.deepcopy(CREDENTIALS)
    if problem == 'missing':
        creds.pop('bot')
    elif problem == 'expired':
        creds['bot']['expires_at'] = 1
    else:
        creds['bot']['scope'] = 'im:write' if problem == 'no_chat_scope' else 'chat:write'
    app.state.connectors.save('slack', creds, 'Test team')
    result = invoke(sender, arguments={'channel': 'U12345678', 'text': 'Hello'})
    assert 'Reconnect Slack' in result['error']
    assert not sender[-1]


@pytest.mark.parametrize('endpoint', ['conversations.open', 'chat.postMessage', 'oauth.v2.access'])
def test_provider_failure_never_retries_with_user_token(sender, monkeypatch, endpoint):
    app = sender[0]
    attempts = []
    if endpoint == 'oauth.v2.access':
        creds = copy.deepcopy(CREDENTIALS)
        creds['bot'].update(expires_at=1, refresh_token='bot-refresh')
        app.state.connectors.save('slack', creds, 'Test team')

    async def rejected(method, url, **kwargs):
        from app.connector_errors import ConnectorError
        attempts.append((url, kwargs))
        raise ConnectorError('App rejected the operation.')

    monkeypatch.setattr(app.state.connectors, 'request', rejected)
    target = 'U12345678' if endpoint == 'conversations.open' else 'C12345678'
    assert 'error' in invoke(sender, arguments={'channel': target, 'text': 'Hello'})
    assert len(attempts) == 1 and attempts[0][0].endswith(endpoint)
    assert USER not in str(attempts)


@pytest.mark.parametrize('response', [{}, {'channel': {}}, {'channel': {'id': 'C12345678'}}, {'channel': {'id': 1}}])
def test_invalid_dm_response_does_not_send(sender, monkeypatch, response):
    calls = []
    async def open_dm(method, url, **kwargs):
        calls.append(url)
        return response
    monkeypatch.setattr(sender[0].state.connectors, 'request', open_dm)
    assert 'No message was sent' in invoke(sender, arguments={'channel': 'U12345678', 'text': 'Hello'})['error']
    assert len(calls) == 1 and calls[0].endswith('conversations.open')


def test_policy_paused_during_dm_open_prevents_send(sender, monkeypatch):
    app = sender[0]
    calls = []
    async def open_dm(method, url, **kwargs):
        calls.append(url)
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('slack',1)")
        return {'ok': True, 'channel': {'id': 'D12345678'}}
    monkeypatch.setattr(app.state.connectors, 'request', open_dm)
    assert 'disabled' in invoke(sender, arguments={'channel': 'U12345678', 'text': 'Hello'})['error']
    assert len(calls) == 1


@pytest.mark.parametrize('name,args', [('slack_search', {'query': 'release'}),
    ('slack_thread', {'channel': 'C12345678', 'thread_ts': '1790719999.123456'})])
def test_read_tools_retain_the_existing_read_credential(sender, name, args):
    assert invoke(sender, name, args)['ok']
    assert sender[-1][0].headers['Authorization'] == f'Bearer {USER}'


def test_bot_dm_can_be_verified_without_loading_shared_user(sender):
    app = sender[0]
    creds = copy.deepcopy(CREDENTIALS)
    creds.update(expires_at=1, refresh_token='must-never-refresh-user')
    app.state.connectors.save('slack', creds, 'Test team')
    sent = invoke(sender, arguments={'channel': 'U12345678', 'text': 'Hello'})
    assert invoke(sender, 'slack_thread', {'channel': sent['channel'], 'thread_ts': sent['ts'], 'as_bot': True})['ok']
    read = sender[-1][-1]
    assert read.url.path == '/api/conversations.replies'
    assert read.headers['Authorization'] == f'Bearer {BOT}'
    assert dict(read.url.params) == {'channel': sent['channel'], 'ts': sent['ts'], 'limit': '50'}


@pytest.mark.parametrize('problem', ['missing_bot', 'missing_history'])
def test_bot_read_never_falls_back_to_shared_user(sender, problem):
    app = sender[0]
    creds = copy.deepcopy(CREDENTIALS)
    if problem == 'missing_bot':
        creds.pop('bot')
    else:
        creds['bot']['scope'] = 'chat:write,im:write'
    app.state.connectors.save('slack', creds, 'Test team')
    result = invoke(sender, 'slack_thread', {'channel': 'D12345678', 'thread_ts': '1790719999.123456', 'as_bot': True})
    assert 'Reconnect Slack' in result['error'] and not sender[-1]


def test_oauth_only_requests_bot_write_permissions(sender):
    app = sender[0]
    app.state.settings.slack_bot_enabled = True
    params = parse_qs(urlparse(app.state.connectors.authorization_url('slack', 'state')).query)
    assert {'chat:write', 'im:write', 'im:history'} <= set(params['scope'][0].split(','))
    assert all(not scope.endswith(':write') for scope in params['user_scope'][0].split(','))
    connection = next(c for c in app.state.connectors.list() if c['id'] == 'slack')
    assert connection['identity'] == 'Moyai bot sends · shared user reads'


@pytest.fixture
def linked_sender(sender):
    app = sender[0]
    credentials = copy.deepcopy(CREDENTIALS)
    credentials['bot']['scope'] += ',users:read,users:read.email'
    app.state.connectors.save('slack', credentials, 'Test team')
    app.state.store.execute("""UPDATE users SET email='alice@berri.ai',linked_user_id='google:alice',
        link_method='email',link_status='linked',profile_eligible=1,profile_checked_at=?,
        profile_next_check=9999999999 WHERE id='slack:T12345678:U12345678'""", (now(),))
    return sender


@pytest.mark.parametrize('person_kind', ['google', 'cloudflare'])
@pytest.mark.parametrize('actor,method', [('google:alice', 'email'), ('google:alice', 'manual'),
                                        ('slack:T12345678:U12345678', 'email')])
def test_requester_lookup_and_dm_share_verified_identity(linked_sender, actor, method, person_kind):
    app, client, run_id, headers, requests = linked_sender
    app.state.store.execute("UPDATE users SET kind=? WHERE id='google:alice'", (person_kind,))
    app.state.store.execute('UPDATE users SET link_method=?,link_status=? WHERE kind=?',
                            (method, 'linked' if method == 'email' else 'manual', 'slack'))
    app.state.store.execute('UPDATE runs SET owner_id=?,active_user_id=? WHERE id=?',
                            ('google:bob', actor, run_id))
    tools = client.get(f'/broker/{run_id}/tools', headers=headers).json()
    assert next(tool for tool in tools if tool['name'] == 'slack_me')['annotations']['readOnlyHint']
    assert invoke(linked_sender, 'slack_me', {}) == {
        'user_id': 'U12345678', 'team_id': 'T12345678', 'email': 'alice@berri.ai', 'name': 'Moe'}
    assert not requests
    assert invoke(linked_sender, arguments={'channel': 'me', 'text': 'Issue LIT-123'})['ok']
    assert [r.url.path for r in requests] == ['/api/conversations.open', '/api/chat.postMessage']
    assert json.loads(requests[0].content) == {'users': 'U12345678'}
    assert json.loads(requests[1].content)['text'] == 'Moe: Issue LIT-123'
    assert all(r.headers['Authorization'] == f'Bearer {BOT}' for r in requests)


def test_dm_me_follows_shared_chat_requester_not_original_owner(linked_sender):
    app, client, _, headers, requests = linked_sender
    store = app.state.store
    store.execute("""INSERT INTO users(id,kind,email,name,linked_user_id,link_method,link_status,
        profile_eligible,profile_checked_at,profile_next_check,created_at,updated_at)
        VALUES('slack:T12345678:U87654321','slack','bob@berri.ai','Tin','google:bob',
        'email','linked',1,?,9999999999,'','')""", (now(),))
    run = store.create_run('DM my issue', '', 'modal', ['slack'], chat_enabled=True, user_id='google:alice')
    for actor in ['google:alice', 'google:bob']:
        if actor == 'google:bob':
            store.enqueue_message(run['id'], 'DM my issue too', 'bob-dm', user_id=actor)
        turn = store.claim_message(run['id'])
        store.update_run(run['id'], status='running', token_hash=digest('run-capability-only'))
        assert invoke((app, client, run['id'], headers, requests),
                      arguments={'channel': 'me', 'text': 'My issue'})['ok']
        store.finish_message(run['id'], turn['id'], 'Sent')
        store.update_run(run['id'], status='idle')
    assert store.run(run['id'])['owner_id'] == 'google:alice'
    assert [json.loads(r.content)['users'] for r in requests if r.url.path.endswith('conversations.open')] == [
        'U12345678', 'U87654321']


def test_scheduled_run_resolves_automation_owner(linked_sender, monkeypatch):
    from test_automations import create
    from test_spend import sign_in

    app, client, _, headers, requests = linked_sender
    owner = sign_in(app, client)
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    automation = create(client, prompt='DM me the issue', plugins=['slack'])
    launched = client.post(f"/api/automations/{automation['id']}/run",
                           json={'revision': 1, 'client_id': 'dm-owner-proof'}).json()
    assert launched['outcome'] == 'started'
    run_id = launched['run_id']
    # The real scheduler created the identities; this fixture supplies its broker capability.
    app.state.store.claim_message(run_id)
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run_id,))
    app.state.store.update_run(run_id, status='running', token_hash=digest('run-capability-only'))
    run = app.state.store.run(run_id)
    assert run['owner_id'] == run['active_user_id'] == owner
    assert invoke((app, client, run_id, headers, requests),
                  arguments={'channel': 'me', 'text': 'Automation issue'})['ok']
    assert json.loads(requests[0].content) == {'users': 'U12345678'}


@pytest.mark.parametrize('state', ['missing', 'stale'])
def test_google_identity_is_discovered_or_refreshed_with_bot(linked_sender, state):
    app, _, _, _, requests = linked_sender
    if state == 'missing':
        app.state.store.execute("DELETE FROM users WHERE kind='slack'")
    else:
        app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE kind='slack'")
    assert invoke(linked_sender, 'slack_me', {})['user_id'] == 'U12345678'
    expected = ['/api/users.lookupByEmail', '/api/users.info'] if state == 'missing' else ['/api/users.info']
    assert [r.url.path for r in requests] == expected
    assert all(r.headers['Authorization'] == f'Bearer {BOT}' for r in requests)
    if state == 'missing':
        assert dict(requests[0].url.params) == {'email': 'alice@berri.ai'}
    assert dict(requests[-1].url.params) == {'user': 'U12345678'}
    profile = app.state.store.rows("SELECT * FROM users WHERE id='slack:T12345678:U12345678'")[0]
    assert profile['linked_user_id'] == 'google:alice' and profile['link_status'] == 'linked'
    requests.clear()
    assert invoke(linked_sender, arguments={'channel': 'me', 'text': 'After lookup'})['ok']
    assert [r.url.path for r in requests] == ['/api/conversations.open', '/api/chat.postMessage']


@pytest.mark.parametrize('problem', ['manual_email', 'conflict', 'duplicate_google', 'duplicate_slack',
                                    'missing_actor', 'shared_actor', 'other_team', 'disallowed_email'])
def test_dm_me_rejects_untrusted_or_ambiguous_identity(linked_sender, problem):
    app, _, run_id, _, requests = linked_sender
    store = app.state.store
    if problem == 'manual_email':
        store.execute("UPDATE users SET email='bob@berri.ai',link_method='manual',link_status='manual' WHERE kind='slack'")
    elif problem == 'conflict':
        store.execute("UPDATE users SET profile_conflict=1 WHERE kind='slack'")
    elif problem == 'duplicate_google':
        store.identity({'method': 'google', 'identity': {'sub': 'duplicate', 'email': 'alice@berri.ai'}})
    elif problem == 'duplicate_slack':
        store.execute("""INSERT INTO users(id,kind,email,name,linked_user_id,link_status,profile_eligible,
            profile_checked_at,created_at,updated_at) VALUES('slack:T12345678:U11111111','slack',
            'alice@berri.ai','Duplicate','google:alice','linked',1,?,'','')""", (now(),))
    elif problem == 'disallowed_email':
        app.state.settings.google_allowed_domains = 'example.org'
    else:
        actor = {'missing_actor': '', 'shared_actor': 'shared:local:admin',
                 'other_team': 'slack:T87654321:U12345678'}[problem]
        if problem == 'other_team':
            with store.connect() as conn:
                store.slack_identity_in(conn, 'T87654321', 'U12345678')
        store.execute('UPDATE runs SET chat_enabled=1,owner_id=?,active_user_id=? WHERE id=?',
                      ('google:alice', actor, run_id))
    assert 'error' in invoke(linked_sender, arguments={'channel': 'me', 'text': 'Private issue'})
    assert not requests


@pytest.mark.parametrize('failure', ['unavailable', 'deleted', 'team', 'email'])
def test_failed_profile_refresh_never_sends(linked_sender, monkeypatch, failure):
    app, _, _, _, requests = linked_sender
    app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE kind='slack'")
    original = app.state.connectors.request

    async def profile(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        if failure == 'unavailable':
            return {'ok': False, 'error': 'ratelimited'}
        if failure == 'email':
            result['user']['profile']['email'] = 'bob@berri.ai'
        else:
            result['user']['deleted' if failure == 'deleted' else 'team_id'] = True if failure == 'deleted' else 'T87654321'
        return result

    monkeypatch.setattr(app.state.connectors, 'request', profile)
    assert 'error' in invoke(linked_sender, arguments={'channel': 'me', 'text': 'Private issue'})
    assert [r.url.path for r in requests] == ['/api/users.info']
    if failure == 'unavailable':
        monkeypatch.setattr(app.state.connectors, 'request', original)
        requests.clear()
        assert invoke(linked_sender, arguments={'channel': 'me', 'text': 'Retry after recovery'})['ok']
        assert [r.url.path for r in requests] == [
            '/api/users.info', '/api/conversations.open', '/api/chat.postMessage']


@pytest.mark.parametrize('endpoint,change', [
    ('users.info', 'requester'), ('users.info', 'team'),
    ('conversations.open', 'requester'), ('conversations.open', 'team'),
    ('conversations.open', 'bot'), ('conversations.open', 'turn'),
    ('conversations.open', 'token'), ('conversations.open', 'link'),
])
def test_dm_me_rechecks_requester_and_workspace_after_await(linked_sender, monkeypatch, endpoint, change):
    app, _, run_id, _, requests = linked_sender
    if endpoint == 'users.info':
        app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE kind='slack'")
    original = app.state.connectors.request

    async def switched(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        if url.endswith(endpoint):
            if change == 'requester':
                app.state.store.execute('UPDATE runs SET active_user_id=? WHERE id=?', ('google:bob', run_id))
            elif change == 'turn':
                app.state.store.execute('UPDATE runs SET active_message_id=123456 WHERE id=?', (run_id,))
            elif change == 'token':
                app.state.store.execute('UPDATE runs SET token_hash=? WHERE id=?', (digest('revoked'), run_id))
            elif change == 'link':
                app.state.store.execute("UPDATE users SET id='slack:T12345678:U87654321' WHERE id='slack:T12345678:U12345678'")
            else:
                saved = app.state.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]
                credentials = json.loads(app.state.security.decrypt(saved['encrypted']))
                if change == 'team':
                    credentials['bot']['team']['id'] = 'T87654321'
                else:
                    credentials['bot']['bot_user_id'] = 'U88888888'
                app.state.connectors.save('slack', credentials, 'Another team')
        return result

    monkeypatch.setattr(app.state.connectors, 'request', switched)
    assert 'error' in invoke(linked_sender, arguments={'channel': 'me', 'text': 'Private issue'})
    assert [r.url.path for r in requests] == ['/api/' + endpoint]


def test_dm_me_rechecks_write_policy_after_final_profile_refresh(linked_sender, monkeypatch):
    app, _, _, _, requests = linked_sender
    original = app.state.connectors.request

    async def paused(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        if url.endswith('conversations.open'):
            app.state.store.execute("UPDATE users SET profile_checked_at='2000-01-01T00:00:00+00:00' WHERE kind='slack'")
        elif url.endswith('users.info'):
            app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('slack',1)")
        return result

    monkeypatch.setattr(app.state.connectors, 'request', paused)
    assert 'disabled' in invoke(linked_sender, arguments={'channel': 'me', 'text': 'Private issue'})['error']
    assert [r.url.path for r in requests] == ['/api/conversations.open', '/api/users.info']


@pytest.mark.parametrize('name', ['slack_me', 'slack_send'])
def test_agent_cannot_override_self_recipient(linked_sender, name):
    _, client, run_id, headers, requests = linked_sender
    arguments = {'email': 'bob@berri.ai'}
    if name == 'slack_send':
        arguments.update(channel='me', text='Private issue')
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': name, 'arguments': arguments})
    assert response.status_code == 422 and not requests


@pytest.mark.parametrize('problem', ['profile_scope', 'linking_disabled', 'read_only', 'disabled'])
def test_dm_me_preserves_identity_and_connection_gates(linked_sender, problem):
    app, client, run_id, headers, requests = linked_sender
    if problem == 'profile_scope':
        app.state.connectors.save('slack', copy.deepcopy(CREDENTIALS), 'Test team')
    elif problem == 'linking_disabled':
        app.state.settings.slack_identity_linking_enabled = False
    else:
        app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                                ('slack', int(problem != 'disabled'), int(problem == 'read_only')))
    response = client.post(f'/broker/{run_id}/tools/call', headers=headers,
                           json={'name': 'slack_send', 'arguments': {'channel': 'me', 'text': 'Private issue'}})
    assert response.status_code == 403 or (response.status_code == 200 and 'error' in response.json())
    assert not requests
