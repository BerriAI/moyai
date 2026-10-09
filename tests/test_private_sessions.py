"""Authenticated ASGI regressions for immutable owner-only sessions."""
import sqlite3

import pytest
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app.security import digest
from app.main import NewRun
from test_user_roles import users_app, sign_as


@pytest.fixture
def private_app(users_app, monkeypatch):
    app, client = users_app
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    monkeypatch.setattr(app.state.session_titles, 'schedule', lambda run_id: None)
    sign_as(app, client, 'maya@berri.ai')
    response = client.post('/api/runs', json={'prompt': 'Private sentinel orchid', 'private_session': True,
                                            'client_id': 'private-initial'})
    assert response.status_code == 201, response.text
    run = response.json()
    app.state.store.execute("UPDATE runs SET status='idle' WHERE id=?", (run['id'],))
    return app, client, run


def test_private_creation_immutable_and_exact_retry(private_app):
    app, client, run = private_app
    assert run['private_owner_id'] == 'google:maya'
    payload = {'prompt': 'Private sentinel orchid', 'private_session': True, 'client_id': 'private-initial'}
    assert client.post('/api/runs', json=payload).json()['id'] == run['id']
    payload['private_session'] = False
    assert client.post('/api/runs', json=payload).status_code == 409
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        app.state.store.execute("UPDATE runs SET private_owner_id='' WHERE id=?", (run['id'],))
    assert client.get('/api/runs/' + run['id']).status_code == 200
    assert NewRun(prompt='Default public').private_session is False
    assert client.post('/api/runs', json={'prompt': 'Invalid string bool', 'private_session': 'true'}).status_code == 422
    assert client.post('/api/runs', json={'prompt': 'Cannot spoof owner', 'private_owner_id': 'google:tin'}).status_code == 422


@pytest.mark.parametrize('method,suffix,body', [
    ('GET', '', None), ('GET', '/events', None), ('GET', '/artifact', None),
    ('GET', '/files', None), ('GET', '/files/content?path=secret.txt', None),
    ('GET', '/computer', None), ('GET', '/computer/captures/secret.png', None),
    ('GET', '/side-chats', None), ('GET', '/pull-request', None),
    ('POST', '/messages', {'content': 'Hijack private turn', 'client_id': 'evil-message'}),
    ('POST', '/cancel', None), ('POST', '/archive', {'archived': True}),
    ('PUT', '/pin', {'pinned': True}), ('PUT', '/folder', {'folder_id': None}),
    ('DELETE', '', None),
])
def test_bob_admin_cannot_read_or_mutate(private_app, method, suffix, body):
    app, client, run = private_app
    sign_as(app, client, 'tin@berri.ai')
    response = client.request(method, '/api/runs/' + run['id'] + suffix, json=body)
    assert response.status_code == 404, response.text
    assert 'orchid' not in response.text


def test_admin_lists_search_focus_pins_and_archives_cannot_reveal(private_app):
    app, client, run = private_app
    store = app.state.store
    sign_as(app, client, 'tin@berri.ai')
    # Adversarial retained memberships do not grant visibility.
    store.execute('INSERT INTO session_pins VALUES(?,?,?)', ('google:tin', run['id'], 'now'))
    store.execute('INSERT INTO session_archives VALUES(?,?,?)', ('google:tin', run['id'], 'now'))
    for query in ('', '?scope=all', '?scope=mine', '?search=orchid', '?archived=true', '?focus=' + run['id']):
        response = client.get('/api/runs' + query)
        assert response.status_code == 200
        assert run['id'] not in response.text and 'orchid' not in response.text
    sign_as(app, client, 'maya@berri.ai')
    assert run['id'] in client.get('/api/runs?search=orchid').text


def test_private_derivation_denied_both_directions(private_app):
    app, client, run = private_app
    public = client.post('/api/runs', json={'prompt': 'Public parent'}).json()
    for parent, private in ((run['id'], False), (run['id'], True), (public['id'], True)):
        response = client.post('/api/runs', json={'prompt': 'Side chat export', 'side_chat_of': parent,
                                                'private_session': private})
        assert response.status_code in {403, 422}
    with pytest.raises(sqlite3.IntegrityError, match='derivation'):
        app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (run['id'], public['id']))
    sign_as(app, client, 'tin@berri.ai')
    assert client.post('/api/runs', json={'prompt': 'Stolen context', 'side_chat_of': run['id']}).status_code == 404
    assert client.get('/api/runs/' + public['id']).status_code == 200


def test_shared_login_cannot_create_private(users_app, monkeypatch):
    app, client = users_app
    app.state.settings.password_login_enabled = True
    app.state.settings.workspace_password = 'long-private-test-password'
    client.headers['Origin'] = app.state.settings.public_url
    response = client.post('/api/login', json={'password': app.state.settings.workspace_password})
    assert response.status_code == 200
    client.headers.update({'Origin': app.state.settings.public_url, 'X-CSRF-Token': client.get('/api/session').json()['csrf']})
    assert client.post('/api/runs', json={'prompt': 'No individual', 'private_session': True}).status_code == 403


@pytest.mark.parametrize('name', ['slack_send', 'linear_create_issue', 'skills_save', 'memory_save',
                                  'credentials_request', 'credentials_run', 'automation_create',
                                  'agents_fanout', 'media_share'])
def test_broker_private_exports_denied_before_dispatch(private_app, name):
    app, client, run = private_app
    app.state.store.execute("UPDATE runs SET mode='modal',status='running',token_hash=? WHERE id=?",
                            (digest('private-capability'), run['id']))
    response = client.post('/broker/' + run['id'] + '/tools/call',
                           headers={'Authorization': 'Bearer private-capability'}, json={'name': name, 'arguments': {}})
    assert response.status_code == 403, response.text


def test_attached_resource_and_websocket_guard(private_app):
    app, client, run = private_app
    attachment = 'a' * 32
    store = app.state.store
    store.attachments.save(attachment, 'google:maya', 'secret.txt', b'orchid', ('text/plain', b'', 'orchid'), 1024 * 1024)
    message_id = store.messages(run['id'])[0]['id']
    store.execute('UPDATE attachments SET message_id=? WHERE id=?', (message_id, attachment))

    @app.websocket('/api/runs/{run_id}/test-private-ws')
    async def guarded_socket(websocket: WebSocket, run_id: str):
        # The application's global privacy dependency must guard this too.
        await websocket.accept()
        await websocket.send_text('owner verified')
        await websocket.close()

    with client.websocket_connect('wss://workspace.example/api/runs/' + run['id'] + '/test-private-ws') as ws:
        assert ws.receive_text() == 'owner verified'
    sign_as(app, client, 'tin@berri.ai')
    for suffix in ('/preview', '/audio'):
        assert client.get('/api/attachments/' + attachment + suffix).status_code == 404
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('wss://workspace.example/api/runs/' + run['id'] + '/test-private-ws'):
            pass


def test_search_tool_does_not_export_owner_private_history_into_shared_run(private_app):
    app, client, private = private_app
    public = client.post('/api/runs', json={'prompt': 'Public current chat'}).json()
    store = app.state.store
    for run in (private, public):
        message = store.messages(run['id'])[0]
        store.execute("UPDATE runs SET status='running',active_message_id=? WHERE id=?", (message['id'], run['id']))
    assert app.state.session_lifecycle.search(store.run(public['id']), {'query': 'orchid'})['sessions'] == []
    other = client.post('/api/runs', json={'prompt': 'Another private orchid', 'private_session': True}).json()
    found = app.state.session_lifecycle.search(store.run(private['id']), {'query': 'orchid'})['sessions']
    assert [row['id'] for row in found] == [other['id']]


def test_broker_discovery_and_execution_pass_server_run(private_app, monkeypatch):
    app, client, run = private_app
    seen = []

    def allowed(name, context):
        assert context['private_owner_id'] == 'google:maya'
        seen.append(name)
        return True

    async def call(name, arguments, *, run):
        return {'selected_owner': run['private_owner_id'], 'query': arguments['query']}

    # Contract seam: connector worker supplies the run-aware selection service.
    monkeypatch.setattr(app.state.connectors, 'allowed', allowed)
    monkeypatch.setattr(app.state.connectors, 'call', call)
    app.state.store.execute("UPDATE runs SET mode='modal',status='running',token_hash=?,plugins=? WHERE id=?",
                            (digest('private-capability'), '["slack"]', run['id']))
    headers = {'Authorization': 'Bearer private-capability'}
    listing = client.get('/broker/' + run['id'] + '/tools', headers=headers)
    assert listing.status_code == 200, listing.text
    names = {tool['name'] for tool in listing.json()}
    assert 'slack_search' in names
    assert not names.intersection({'slack_send', 'media_share', 'skills_save', 'memory_save', 'credentials_request', 'agents_fanout'})
    response = client.post('/broker/' + run['id'] + '/tools/call', headers=headers,
                           json={'name': 'slack_search', 'arguments': {'query': 'personal channel'}})
    assert response.status_code == 200
    assert response.json() == {'selected_owner': 'google:maya', 'query': 'personal channel'}
    assert seen.count('slack_search') == 3  # discovery, admission, pre-execution recheck
    denied = client.post('/broker/' + run['id'] + '/credentials/materialize', headers=headers, json={})
    assert denied.status_code == 403


def test_broker_search_is_scoped_to_private_owner(private_app):
    app, client, alice = private_app
    sign_as(app, client, 'tin@berri.ai')
    bob = client.post('/api/runs', json={'prompt': 'Bob private current', 'private_session': True}).json()
    message = app.state.store.messages(bob['id'])[0]
    app.state.store.execute("UPDATE runs SET mode='modal',status='running',token_hash=?,active_message_id=? WHERE id=?",
                            (digest('bob-capability'), message['id'], bob['id']))
    response = client.post('/broker/' + bob['id'] + '/tools/call',
                           headers={'Authorization': 'Bearer bob-capability'},
                           json={'name': 'sessions_search', 'arguments': {'query': 'orchid'}})
    assert response.status_code == 200, response.text
    assert response.json()['sessions'] == []
    assert alice['id'] not in response.text


def test_default_shared_creation_cannot_be_retried_as_private(private_app):
    app, client, _ = private_app
    payload = {'prompt': 'Original shared chat', 'client_id': 'shared-submission'}
    shared = client.post('/api/runs', json=payload).json()
    assert shared['private_owner_id'] == ''
    assert client.post('/api/runs', json={**payload, 'private_session': True}).status_code == 409
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        app.state.store.execute('UPDATE runs SET private_owner_id=? WHERE id=?', ('google:maya', shared['id']))


def test_private_model_http_flow_and_followup_stay_owner_only(private_app, monkeypatch):
    import httpx
    import json
    app, client, run = private_app
    store = app.state.store
    app.state.settings.litellm_api_key = 'synthetic-gateway-key'
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.modal_token_id = 'synthetic-modal-id'
    app.state.settings.modal_token_secret = 'synthetic-modal-secret'
    app.state.settings.agent_model = app.state.settings.resolve_model()
    assert not app.state.settings.missing_cloud('modal')
    # The HTTP creation fixture suppresses the sandbox launcher; establish its
    # normal claimed-turn capability, then exercise the real model broker.
    store.claim_message(run['id'])
    store.execute("UPDATE runs SET mode='modal',status='running',token_hash=? WHERE id=?",
                  (digest('private-model-capability'), run['id']))
    from app.context_budget import ModelContextLimits, counting_input
    async def limits(model):
        return ModelContextLimits(context_window=2_000_000, max_input_tokens=2_000_000,
                                  max_output_tokens=1_000_000)
    async def count(payload, **kwargs):
        return counting_input(payload)[1], 'fixture_bytes'
    monkeypatch.setattr(app.state.context_budget, 'limits', limits)
    monkeypatch.setattr(app.state.context_budget, 'count', count)
    traces = []
    monkeypatch.setattr(app.state.tracing, 'model', lambda *a, **kw: traces.append(a))
    def gateway(request):
        payload = json.loads(request.content)
        assert payload['messages'][-1]['content'] == 'Private inference sentinel'
        return httpx.Response(200, json={'id': 'synthetic-response', 'choices': [
            {'index': 0, 'message': {'role': 'assistant', 'content': 'Private answer'}, 'finish_reason': 'stop'}],
            'usage': {'total_tokens': 9}})
    original = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: original(transport=httpx.MockTransport(gateway), **kw))
    response = client.post('/broker/' + run['id'] + '/v1/chat/completions',
        headers={'Authorization': 'Bearer private-model-capability'},
        json={'messages': [{'role': 'user', 'content': 'Private inference sentinel'}], 'stream': True})
    assert response.status_code == 200, response.text
    assert 'Private answer' in response.text and '[DONE]' in response.text
    assert not traces
    current = store.run(run['id'])
    store.finish_message(run['id'], current['active_message_id'], 'Private answer')
    store.update_run(run['id'], status='idle')
    followup = client.post('/api/runs/' + run['id'] + '/messages',
                           json={'content': 'Continue privately', 'client_id': 'private-followup'})
    assert followup.status_code == 202, followup.text
    assert store.run(run['id'])['private_owner_id'] == 'google:maya'
    sign_as(app, client, 'tin@berri.ai')
    assert client.get('/api/runs/' + run['id']).status_code == 404
