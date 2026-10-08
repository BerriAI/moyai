import pytest

from app.main import create_app
from app.message_queue import MessageQueue
from test_agent_sidebar import seeded_group
from test_attachments import upload
from test_spend import sign_in
from test_workspace import workspace  # noqa: F401


ROUTE = '/api/settings/preferences'


@pytest.fixture
def personal(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client, 'bob', 'bob@berri.ai')
    return app, client


def active_chat(app, client):
    response = client.post('/api/runs', json={'prompt': 'Investigate this issue'})
    assert response.status_code == 201
    run_id = response.json()['id']
    turn = app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    return run_id, turn['id']


def send(client, run_id, key, **kwargs):
    response = client.post(f'/api/runs/{run_id}/messages', json={
        'content': key, 'client_id': key, **kwargs})
    assert response.status_code == 202, response.text
    return response.json()


def test_members_can_opt_in_and_preference_survives_sign_in_and_restart(personal):
    app, client = personal
    assert client.get('/api/session').json()['role'] == 'member'
    assert client.get(ROUTE).json() == {'send_immediately': False, 'omit_private_tool_payloads': False}
    assert client.put(ROUTE, json={'send_immediately': True}).json() == {'send_immediately': True, 'omit_private_tool_payloads': False}
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert client.get(ROUTE).json() == {'send_immediately': False, 'omit_private_tool_payloads': False}
    assert client.get('/api/session').json()['preferences'] == {'send_immediately': False, 'omit_private_tool_payloads': False}
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/session').json()['preferences'] == {'send_immediately': True, 'omit_private_tool_payloads': False}
    restarted = create_app(app.state.settings)
    assert restarted.state.user_preferences.get('google:bob') == {'send_immediately': True, 'omit_private_tool_payloads': False}
    assert restarted.state.user_preferences.get('google:alice') == {'send_immediately': False, 'omit_private_tool_payloads': False}


def test_preferences_require_authentication_csrf_and_server_owned_identity(personal):
    _, client = personal
    assert client.put(ROUTE, json={'send_immediately': True, 'user_id': 'google:alice'}).status_code == 422
    client.headers.pop('X-CSRF-Token')
    assert client.put(ROUTE, json={'send_immediately': True}).status_code == 403
    client.cookies.clear()
    assert client.get(ROUTE).status_code == 401
    assert client.put(ROUTE, json={'send_immediately': True}).status_code == 401


@pytest.mark.parametrize('key', ['send_immediately', 'omit_private_tool_payloads'])
@pytest.mark.parametrize('value', ['false', 1, None])
def test_preference_requires_an_explicit_boolean(personal, key, value):
    _, client = personal
    assert client.put(ROUTE, json={key: value}).status_code == 422
    assert client.get(ROUTE).json() == {'send_immediately': False, 'omit_private_tool_payloads': False}


def test_trace_preference_defaults_off_and_saves_independently_per_account(personal):
    app, client = personal
    run_id, _ = active_chat(app, client)
    assert app.state.manager.spec(app.state.store.run(run_id))['omit_private_tool_payloads'] is False
    assert client.put(ROUTE, json={'omit_private_tool_payloads': True}).json() == {
        'send_immediately': False, 'omit_private_tool_payloads': True}
    assert client.put(ROUTE, json={'send_immediately': True}).json() == {
        'send_immediately': True, 'omit_private_tool_payloads': True}
    assert app.state.manager.spec(app.state.store.run(run_id))['omit_private_tool_payloads'] is True
    sign_in(app, client, 'alice', 'alice@berri.ai')
    assert client.get(ROUTE).json()['omit_private_tool_payloads'] is False
    restarted = create_app(app.state.settings)
    assert restarted.state.user_preferences.get('google:bob')['omit_private_tool_payloads'] is True
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.put(ROUTE, json={'omit_private_tool_payloads': False}).json() == {
        'send_immediately': True, 'omit_private_tool_payloads': False}


def test_existing_preference_table_migrates_without_changing_saved_chat_behavior(tmp_path):
    from app.db import Store
    from app.user_preferences import UserPreferences
    store = Store(tmp_path)
    actor = store.identity({'method': 'local', 'role': 'admin'})
    with store.connect() as conn:
        conn.execute('CREATE TABLE user_preferences (user_id TEXT PRIMARY KEY, send_immediately INTEGER NOT NULL DEFAULT 0)')
        conn.execute('INSERT INTO user_preferences VALUES(?,1)', (actor,))
    preferences = UserPreferences(store, None, None)
    assert preferences.get(actor) == {'send_immediately': True, 'omit_private_tool_payloads': False}
    # Startup migration is safe to run again.
    assert UserPreferences(store, None, None).get(actor) == preferences.get(actor)


def test_trace_preference_uses_turn_author_and_linked_slack_account(personal):
    app, client = personal
    store = app.state.store
    run_id, turn = active_chat(app, client)
    client.put(ROUTE, json={'omit_private_tool_payloads': True})
    assert app.state.user_preferences.for_run(store.run(run_id))['omit_private_tool_payloads'] is True
    store.finish_message(run_id, turn, 'Done')
    sign_in(app, client, 'alice', 'alice@berri.ai')
    send(client, run_id, 'alice-owns-this-turn')
    store.claim_message(run_id)
    assert store.run(run_id)['owner_id'] == 'google:bob'
    assert app.state.user_preferences.for_run(store.run(run_id))['omit_private_tool_payloads'] is False
    with store.connect() as conn:
        slack = store.slack_identity_in(conn, 'T12345678', 'U12345678')
        conn.execute('UPDATE users SET linked_user_id=? WHERE id=?', ('google:bob', slack))
    slack_run = store.create_run('Slack task', '', 'modal', [], chat_enabled=True, user_id=slack)
    store.claim_message(slack_run['id'])
    assert app.state.user_preferences.for_run(store.run(slack_run['id']))['omit_private_tool_payloads'] is True
    store.execute('UPDATE users SET linked_user_id=NULL WHERE id=?', (slack,))
    assert app.state.user_preferences.for_run(store.run(slack_run['id']))['omit_private_tool_payloads'] is False


def test_opt_in_injects_rapid_followups_in_order_without_promoting_old_queue(personal):
    app, client = personal
    run_id, turn_id = active_chat(app, client)
    old = send(client, run_id, 'queued-before-opt-in')
    queue = MessageQueue(app.state.store)
    assert queue.live_control(run_id, turn_id, []) == {'steer_message_id': None}
    assert client.put(ROUTE, json={'send_immediately': True}).status_code == 200
    first = send(client, run_id, 'immediate-first', send_now=False)
    second = send(client, run_id, 'immediate-second')
    # The browser must know the delivery mode before a worker consumes it;
    # otherwise it briefly renders automatic follow-ups as queue cards.
    pending = {m['id']: m for m in client.get(f'/api/runs/{run_id}').json()['messages']}
    assert pending[old['id']]['send_immediately'] == 0
    for message in (first, second):
        assert pending[message['id']]['status'] == 'queued'
        assert pending[message['id']]['send_immediately'] == 1
    assert queue.live_control(run_id, turn_id, [])['input']['id'] == first['id']
    # Another message can arrive during the first message's locked handoff.
    third = send(client, run_id, 'immediate-third', send_now=True)
    assert queue.live_control(run_id, turn_id, [first['id']])['input']['id'] == second['id']
    assert queue.live_control(run_id, turn_id, [second['id']])['input']['id'] == third['id']
    assert queue.live_control(run_id, turn_id, [third['id']]) == {'steer_message_id': None}
    messages = {m['id']: m for m in client.get(f'/api/runs/{run_id}').json()['messages']}
    assert messages[old['id']]['status'] == 'queued'
    for message in (first, second, third):
        assert messages[message['id']]['status'] == 'injected'
        assert messages[message['id']]['steering_parent_id'] == turn_id
    assert send(client, run_id, 'immediate-first')['created'] is False
    assert app.state.store.run(run_id)['active_message_id'] == turn_id


def test_opt_out_restores_queueing_and_explicit_send_now_still_works(personal):
    app, client = personal
    run_id, turn_id = active_chat(app, client)
    client.put(ROUTE, json={'send_immediately': True})
    automatic = send(client, run_id, 'sent-before-opt-out')
    assert client.put(ROUTE, json={'send_immediately': False}).json() == {'send_immediately': False, 'omit_private_tool_payloads': False}
    queued = send(client, run_id, 'queued-after-opt-out')
    queue = MessageQueue(app.state.store)
    assert queue.live_control(run_id, turn_id, [])['input']['id'] == automatic['id']
    assert queue.live_control(run_id, turn_id, [automatic['id']]) == {'steer_message_id': None}
    immediate = send(client, run_id, 'explicit-send-now', send_now=True)
    assert queue.live_control(run_id, turn_id, [])['input']['id'] == immediate['id']
    assert queue.live_control(run_id, turn_id, [immediate['id']]) == {'steer_message_id': None}
    assert app.state.store.rows('SELECT status FROM messages WHERE id=?', (queued['id'],))[0]['status'] == 'queued'


def test_preference_follows_sender_and_preserves_other_users_capability_boundary(personal):
    app, client = personal
    run_id, turn_id = active_chat(app, client)
    client.put(ROUTE, json={'send_immediately': True})
    sign_in(app, client, 'alice', 'alice@berri.ai')
    send(client, run_id, 'alice-default-queues')
    queue = MessageQueue(app.state.store)
    assert queue.live_control(run_id, turn_id, []) == {'steer_message_id': None}
    client.put(ROUTE, json={'send_immediately': True})
    message = send(client, run_id, 'alice-request-handoff')
    assert queue.live_control(run_id, turn_id, []) == {'steer_message_id': message['id'], 'handoff': True}
    assert app.state.store.run(run_id)['active_user_id'] == 'google:bob'


def test_automatic_followups_still_take_priority_when_the_active_turn_finishes(personal):
    app, client = personal
    run_id, turn_id = active_chat(app, client)
    old = send(client, run_id, 'older-queued-message')
    client.put(ROUTE, json={'send_immediately': True})
    first = send(client, run_id, 'automatic-message-one')
    second = send(client, run_id, 'automatic-message-two')
    # The current response can finish before its next steering poll.
    app.state.store.finish_message(run_id, turn_id, 'Done')
    for message in (first, second, old):
        assert app.state.store.claim_message(run_id)['id'] == message['id']
        app.state.store.finish_message(run_id, message['id'], 'Done')


def test_automatic_followup_keeps_attachments_in_a_direct_subagent_chat(personal):
    app, client = personal
    _, child, _ = seeded_group(app)
    first = send(client, child, 'start-direct-agent-chat')
    assert app.state.store.claim_message(child)['id'] == first['id']
    app.state.store.update_run(child, status='running')
    client.put(ROUTE, json={'send_immediately': True})
    file = upload(client, 'checks.txt', b'Check that the setting persists.').json()
    followup = send(client, child, 'check-this-attached-file', attachment_ids=[file['id']])
    control = MessageQueue(app.state.store).live_control(child, first['id'], [])
    assert control['input']['id'] == followup['id']
    assert control['input']['attachments'][0]['id'] == file['id']
    assert 'check-this-attached-file' in control['input']['content']
    assert app.state.store.rows('SELECT send_immediately FROM messages WHERE id=?', (followup['id'],))[0]['send_immediately'] == 1
