import json

import httpx
import pytest
from fastapi import HTTPException

from app.credentials import CredentialRequest
from app.slack_secrets import TOOLS
from test_spend import active, sign_in
from test_workspace import workspace

TOKEN = 'xoxp-personal-test-token'


@pytest.fixture
def personal(workspace):
    app, client = workspace
    sign_in(app, client)
    from app.security import digest
    run = app.state.store.create_run('Personal token test', '', 'modal', ['slack'],
        chat_enabled=True, user_id='google:alice', private_owner_id='google:alice')
    app.state.store.claim_message(run['id'])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    run = app.state.store.run(run['id'])
    saved = client.post('/api/credentials/secrets', json={
        'provider': 'generic', 'name': 'slack-personal', 'format': 'env',
        'scope': 'personal', 'lifetime': 'persistent', 'label': 'My Slack', 'client_id': 'personal-slack',
        'value': json.dumps({'SLACK_USER_TOKEN': TOKEN})})
    assert saved.status_code == 201, saved.text
    request = app.state.credentials.request(run, CredentialRequest(
        provider='generic', name='slack-personal', reason='Read my Slack messages', request_key='slack'))
    assert request['status'] == 'provided'
    return app, run, request['request_id'], saved.json()['id']


@pytest.mark.parametrize('name,args,endpoint', [
    ('slack_personal_search', {'query': 'in:dm project', 'page': 2}, 'search.messages'),
    ('slack_personal_conversations', {'cursor': 'next'}, 'users.conversations'),
    ('slack_personal_history', {'channel': 'D12345678', 'cursor': 'next'}, 'conversations.history'),
    ('slack_personal_thread', {'channel': 'G12345678', 'thread_ts': '1791520602.639729'}, 'conversations.replies'),
])
async def test_reads_use_only_personal_token_and_paginate(personal, monkeypatch, name, args, endpoint):
    app, run, request_id, _ = personal
    seen = []
    def respond(request):
        seen.append(request)
        assert request.headers['authorization'] == 'Bearer ' + TOKEN
        assert request.url.path == '/api/' + endpoint
        if 'cursor' in args:
            assert request.url.params['cursor'] == 'next'
        if 'page' in args:
            assert request.url.params['page'] == '2'
        return httpx.Response(200, json={'ok': True, 'messages': [{'text': 'private message'}],
                                         'response_metadata': {'next_cursor': 'more'}})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    result = await app.state.credentials.call(run, name, {'request_id': request_id, **args})
    assert len(seen) == 1
    assert result['response_metadata']['next_cursor'] == 'more'
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize('change', ['other_user', 'other_session', 'revoked', 'expired', 'organization', 'wrong_capability', 'disabled', 'not_enabled', 'bot_token'])
async def test_rejects_unauthorized_access_before_network(personal, monkeypatch, change):
    app, run, request_id, secret_id = personal
    if change == 'other_user':
        run = {**run, 'active_user_id': 'google:bob'}
    elif change == 'other_session':
        run = active(app)
        run['plugins'] = ['slack']
    elif change == 'disabled':
        app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    elif change == 'not_enabled':
        run = {**run, 'plugins': []}
    elif change == 'bot_token':
        app.state.store.execute('UPDATE provider_secrets SET encrypted=? WHERE id=?',
            (app.state.security.encrypt(json.dumps({'SLACK_USER_TOKEN': 'xoxb-bot'})), secret_id))
    else:
        field, value = {'revoked': ('revoked_at', '2026-01-01'), 'expired': ('expires_at', '2020-01-01'),
                        'organization': ('scope', 'organization'), 'wrong_capability': ('name', 'other')}[change]
        app.state.store.execute(f'UPDATE provider_secrets SET {field}=? WHERE id=?', (value, secret_id))
    def no_network(**kwargs):
        pytest.fail('Unauthorized request reached Slack')
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', no_network)
    with pytest.raises(HTTPException):
        await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})


@pytest.mark.parametrize('body,status,expected', [
    ({'ok': False, 'error': 'invalid_auth'}, 200, 401),
    ({'ok': False, 'error': 'missing_scope'}, 200, 403),
    ({'ok': False, 'error': TOKEN}, 200, 403),
    ({}, 429, 429),
    ({}, 302, 302),
])
async def test_safe_provider_errors(personal, monkeypatch, body, status, expected):
    app, run, request_id, _ = personal
    original = httpx.AsyncClient
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body)), **kw))
    result = await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})
    assert result['status_code'] == expected
    assert TOKEN not in json.dumps(result)


async def test_revocation_during_request_does_not_release_messages(personal, monkeypatch):
    app, run, request_id, secret_id = personal
    def respond(request):
        app.state.store.execute("UPDATE provider_secrets SET revoked_at='now' WHERE id=?", (secret_id,))
        return httpx.Response(200, json={'ok': True, 'messages': [{'text': 'private'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(HTTPException):
        await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})


def test_discovery_respects_scope_and_policy(personal):
    app, run, _, _ = personal
    app.state.settings.temporal_enabled = True
    names = lambda r: {t['name'] for t in app.state.credentials.tools(r)}
    assert set(TOOLS) <= names(run)
    assert not set(TOOLS) & names({**run, 'plugins': []})
    app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    assert not set(TOOLS) & names(run)


def test_broker_discovers_and_executes_personal_reads(personal, workspace, monkeypatch):
    app, run, request_id, _ = personal
    _, client = workspace
    app.state.settings.temporal_enabled = True
    original = httpx.AsyncClient
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'ok': True, 'messages': {'matches': []}})), **kw))
    headers = {'Authorization': 'Bearer capability'}
    endpoint = '/broker/' + run['id'] + '/tools'
    listing = client.get(endpoint, headers=headers)
    assert listing.status_code == 200
    assert set(TOOLS) <= {t['name'] for t in listing.json()}
    result = client.post(endpoint + '/call', headers=headers, json={
        'name': 'slack_personal_search', 'arguments': {'request_id': request_id, 'query': 'my private notes'}})
    assert result.status_code == 200, result.text
    assert result.json() == {'ok': True, 'messages': {'matches': []}}


async def test_session_lifetime_is_enforced(personal):
    app, run, request_id, secret_id = personal
    app.state.store.execute("UPDATE provider_secrets SET lifetime='session',root_id='different-root' WHERE id=?", (secret_id,))
    with pytest.raises(HTTPException):
        await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})


@pytest.mark.parametrize('change', ['requester', 'policy', 'rotation'])
async def test_access_change_during_request_does_not_release_messages(personal, monkeypatch, change):
    app, run, request_id, secret_id = personal
    def respond(request):
        if change == 'requester':
            app.state.store.execute("UPDATE runs SET active_user_id='google:bob' WHERE id=?", (run['id'],))
        elif change == 'policy':
            app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
        else:
            app.state.store.execute('UPDATE provider_secrets SET encrypted=? WHERE id=?',
                (app.state.security.encrypt(json.dumps({'SLACK_USER_TOKEN': 'xoxp-replacement'})), secret_id))
        return httpx.Response(200, json={'ok': True, 'messages': [{'text': 'private'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(HTTPException):
        await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})


@pytest.mark.parametrize('context', ['shared', 'delegated', 'slack_mirror', 'automation'])
async def test_private_web_boundary(personal, monkeypatch, context):
    app, run, request_id, _ = personal
    if context == 'shared':
        run = {**run, 'private_owner_id': ''}
    elif context == 'delegated':
        run = {**run, 'parent_run_id': 'parent'}
    elif context == 'slack_mirror':
        monkeypatch.setattr(app.state.store, 'slack_source', lambda _: {'channel': 'D12345678'})
    else:
        original_rows = app.state.store.rows
        monkeypatch.setattr(app.state.store, 'rows', lambda sql, params=():
            [{'run_id': run['id']}] if 'FROM automation_runs WHERE run_id=' in sql else original_rows(sql, params))
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: pytest.fail('Private context leaked'))
    with pytest.raises(HTTPException):
        await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})
    assert not set(TOOLS) & {t['name'] for t in app.state.credentials.tools(run)}


@pytest.mark.parametrize('field,value', [('active_message_id', 999), ('token_hash', 'rotated'),
    ('private_owner_id', ''), ('deleted_at', 'now'), ('parent_run_id', 'parent')])
async def test_context_change_suppresses_result(personal, monkeypatch, field, value):
    app, run, request_id, _ = personal
    def respond(request):
        original_run = app.state.store.run
        monkeypatch.setattr(app.state.store, 'run', lambda rid: {**original_run(rid), field: value})
        return httpx.Response(200, json={'ok': True, 'messages': [{'text': 'private'}]})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.slack_secrets.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(HTTPException):
        await app.state.credentials.call(run, 'slack_personal_search', {'request_id': request_id, 'query': 'private'})


def test_private_request_requires_existing_personal_secret(personal):
    app, run, _, secret_id = personal
    app.state.store.execute("UPDATE provider_secrets SET scope='organization' WHERE id=?", (secret_id,))
    with pytest.raises(HTTPException):
        app.state.credentials.request(run, CredentialRequest(provider='generic', name='slack-personal',
            reason='private', request_key='missing', secret_id=secret_id))
    assert not app.state.store.rows("SELECT 1 FROM credential_requests WHERE status='pending'")


def test_unrelated_private_credential_request_still_blocked(personal):
    app, run, _, _ = personal
    with pytest.raises(HTTPException):
        app.state.credentials.request(run, CredentialRequest(provider='generic', name='other',
            reason='private', request_key='unrelated'))



def test_personal_token_cannot_be_exported_to_command(personal):
    from app.credentials import Materialize
    app, run, request_id, _ = personal
    with pytest.raises(HTTPException):
        app.state.credentials.materialize(run, Materialize(request_ids=[request_id]))
