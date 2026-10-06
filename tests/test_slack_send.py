"""Outbound Slack identity regression tests; provider responses are test doubles."""
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.connectors import ConnectorError, SlackSend
from test_slack import slack_app


@pytest.mark.asyncio
@pytest.mark.parametrize('destination', ['C12345678', 'G12345678', 'D12345678'])
async def test_sends_only_with_bot_identity(slack_app, monkeypatch, destination):
    app, _, _, _ = slack_app
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {'ok': True, 'channel': destination, 'ts': '1791327287.531699'}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    result = await app.state.connectors.call('slack_send', {'channel': destination, 'text': 'Requested message'})
    assert result['channel'] == destination
    assert calls == [('POST', 'https://slack.com/api/chat.postMessage', {
        'allowed_errors': ('missing_scope', 'not_in_channel', 'channel_not_found'),
        'headers': {'Authorization': 'Bearer separate-bot-secret'},
        'json': {'channel': destination, 'text': 'Requested message', 'unfurl_links': False, 'unfurl_media': False},
    })]


@pytest.mark.asyncio
async def test_recipient_dm_is_opened_and_verified_as_bot(slack_app, monkeypatch):
    app, _, _, _ = slack_app
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs['headers'] == {'Authorization': 'Bearer separate-bot-secret'}
        calls.append((method, url.rsplit('/', 1)[1], kwargs))
        if url.endswith('conversations.open'):
            assert kwargs['json'] == {'users': 'U12345678', 'return_im': True}
            return {'ok': True, 'channel': {'id': 'D87654321'}}
        if url.endswith('chat.postMessage'):
            assert kwargs['json']['channel'] == 'D87654321'
            return {'ok': True, 'channel': 'D87654321', 'ts': '1791327287.531699'}
        assert kwargs['params'] == {'channel': 'D87654321', 'ts': '1791327287.531699', 'limit': 50}
        return {'ok': True, 'messages': [{'text': 'Requested message'}], 'has_more': False}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    result = await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Requested message'})
    read = await app.state.connectors.call('slack_thread', {
        'channel': result['channel'], 'thread_ts': result['ts'], 'as_bot': True})
    assert read['messages'][0]['text'] == 'Requested message'
    assert [call[1] for call in calls] == ['conversations.open', 'chat.postMessage', 'conversations.replies']


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [
    ('slack_search', {'query': 'in:ryan'}),
    ('slack_thread', {'channel': 'D12345678', 'thread_ts': '1791327287.531699'}),
])
async def test_default_reads_keep_shared_read_identity(slack_app, monkeypatch, name, args):
    app, _, _, _ = slack_app

    async def request(method, url, **kwargs):
        assert kwargs['headers'] == {'Authorization': 'Bearer provider-user-secret'}
        assert method == 'GET'
        assert 'as_bot' not in kwargs['params']
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    assert await app.state.connectors.call(name, args) == {'ok': True}


@pytest.mark.asyncio
async def test_missing_bot_fails_closed(slack_app, monkeypatch):
    app, _, _, _ = slack_app
    app.state.connectors.save('slack', {'access_token': 'human-only'}, 'Team')

    async def request(*args, **kwargs):
        pytest.fail('Must not fall back to human credentials')

    monkeypatch.setattr(app.state.connectors, 'request', request)
    with pytest.raises(ConnectorError, match='Reconnect Slack'):
        await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Hello'})


@pytest.mark.asyncio
async def test_bot_refresh_does_not_refresh_expired_user(slack_app, monkeypatch):
    app, _, _, _ = slack_app
    app.state.connectors.save('slack', {
        'access_token': 'expired-user', 'expires_at': 1,
        'bot': {'access_token': 'expired-bot', 'expires_at': 1, 'refresh_token': 'bot-refresh'},
    }, 'Team')
    refreshes = []

    async def exchange(provider, *, refresh_token):
        refreshes.append(refresh_token)
        return {'access_token': 'fresh-bot', 'expires_at': time.time() + 3600}

    async def request(method, url, **kwargs):
        assert kwargs['headers'] == {'Authorization': 'Bearer fresh-bot'}
        return {'ok': True}

    monkeypatch.setattr(app.state.connectors, 'exchange', exchange)
    monkeypatch.setattr(app.state.connectors, 'request', request)
    await app.state.connectors.call('slack_send', {'channel': 'C12345678', 'text': 'Hello'})
    assert refreshes == ['bot-refresh']


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,read_only', [(0, 0), (1, 1)])
async def test_policy_blocks_before_provider_call(slack_app, monkeypatch, enabled, read_only):
    app, _, _, _ = slack_app
    app.state.store.execute('INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?)',
                            ('slack', enabled, read_only))

    async def request(*args, **kwargs):
        pytest.fail('Policy must block before any provider call')

    monkeypatch.setattr(app.state.connectors, 'request', request)
    with pytest.raises(ConnectorError, match='policy'):
        await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Hello'})


@pytest.mark.asyncio
async def test_policy_rechecked_after_open(slack_app, monkeypatch):
    app, _, _, _ = slack_app
    calls = []

    async def request(method, url, **kwargs):
        calls.append(url)
        app.state.store.execute("INSERT INTO connection_policies(provider,read_only) VALUES('slack',1)")
        return {'ok': True, 'channel': {'id': 'D12345678'}}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    with pytest.raises(ConnectorError, match='policy'):
        await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Hello'})
    assert calls == ['https://slack.com/api/conversations.open']


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['conversations.open', 'chat.postMessage'])
async def test_provider_failure_never_retries_or_switches_sender(slack_app, monkeypatch, endpoint):
    app, _, _, _ = slack_app
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs['headers'] == {'Authorization': 'Bearer separate-bot-secret'}
        calls.append(url.rsplit('/', 1)[1])
        if url.endswith(endpoint):
            raise ConnectorError('Response lost or app access rejected')
        return {'ok': True, 'channel': {'id': 'D12345678'}}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    with pytest.raises(ConnectorError):
        await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Hello'})
    assert calls.count(endpoint) == 1
    assert len(calls) == (1 if endpoint == 'conversations.open' else 2)


@pytest.mark.asyncio
async def test_unconfirmed_dm_cannot_post(slack_app, monkeypatch):
    app, _, _, _ = slack_app

    async def request(method, url, **kwargs):
        assert url.endswith('conversations.open')
        return {'ok': True, 'channel': {'id': 'C12345678'}}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    with pytest.raises(ConnectorError, match='No message was sent'):
        await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Hello'})


@pytest.mark.asyncio
@pytest.mark.parametrize('error,help_text', [
    ('missing_scope', 'Reconnect Slack'),
    ('channel_not_found', "recipient's U-prefixed"),
    ('not_in_channel', 'invite the app'),
])
async def test_access_failures_give_actionable_help(slack_app, monkeypatch, error, help_text):
    app, _, _, _ = slack_app
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs['headers'] == {'Authorization': 'Bearer separate-bot-secret'}
        calls.append(url)
        return {'ok': False, 'error': error}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    with pytest.raises(ConnectorError, match=help_text):
        await app.state.connectors.call('slack_send', {'channel': 'U12345678', 'text': 'Hello'})
    assert len(calls) == 1


def test_broker_accepts_user_destination_and_bot_readback(slack_app, monkeypatch):
    from app.security import digest
    app, client, _, _ = slack_app
    run = app.state.store.create_run('Send a test DM', '', 'modal', ['slack'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('test-capability'))
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs['headers'] == {'Authorization': 'Bearer separate-bot-secret'}
        calls.append(url.rsplit('/', 1)[1])
        if url.endswith('conversations.open'):
            return {'ok': True, 'channel': {'id': 'D87654321'}}
        if url.endswith('chat.postMessage'):
            return {'ok': True, 'channel': 'D87654321', 'ts': '1791327287.531699'}
        return {'ok': True, 'messages': [{'text': 'Hello'}], 'has_more': False}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    for name, arguments in [
        ('slack_send', {'channel': 'U12345678', 'text': 'Hello'}),
        ('slack_thread', {'channel': 'D87654321', 'thread_ts': '1791327287.531699', 'as_bot': True}),
    ]:
        response = client.post(f"/broker/{run['id']}/tools/call", headers={'Authorization': 'Bearer test-capability'},
                               json={'name': name, 'arguments': arguments})
        assert response.status_code == 200, response.text
        assert response.json()['ok'], response.text
    assert calls == ['conversations.open', 'chat.postMessage', 'conversations.replies']


def test_oauth_write_scope_belongs_only_to_bot(slack_app):
    app, _, _, _ = slack_app
    app.state.settings.slack_thread_chat_enabled = False
    app.state.settings.slack_dm_enabled = False
    params = parse_qs(urlparse(app.state.connectors.authorization_url('slack', 'state')).query)
    assert 'chat:write' not in params['user_scope'][0].split(',')
    assert {'chat:write', 'im:write', 'im:history'} <= set(params['scope'][0].split(','))
    assert SlackSend(channel='U12345678', text='Hello').channel == 'U12345678'
    identity = next(c['identity'] for c in app.state.connectors.list() if c['id'] == 'slack')
    assert identity == 'Moyai Devin app writes · shared account reads'
