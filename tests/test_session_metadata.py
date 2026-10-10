"""Session-ID questions must never start model, title or sandbox work."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db import Store
from app.session_metadata import is_session_id_request, session_id_response
from test_attachments import upload
from test_session_titles import drain as drain_titles, gateway, completion
from test_slack import slack_app, event, signed
from test_slack_chat import ROOT, start
from test_slack_web_mirror import mirror, web, drain
from test_workspace import workspace


@pytest.mark.parametrize('text', [
    '/session-id', '/session_id', 'session ID', 'What is the session ID?',
    'What’s this session’s ID?', 'what is your session id',
    'Can you give me the current session ID?', 'Please return my session id.',
    'show me the ID for this chat please', 'what is the identifier of this run?',
    '  SESSION   id?!  ',
])
def test_clear_requests(text):
    assert is_session_id_request(text)


@pytest.mark.parametrize('text', [
    'What is a session ID?', 'Find the session ID for yesterday’s task',
    'What is the session ID? Then fix the bug.', '/session-id and deploy',
    'Explain session IDs', 'List all session ids', 'What is the parent session ID?',
    '"what is the session id?"', 'Implement /session-id', 'session id: abc123',
    'Slack reply from U12345678:\n/session-id', 'What is the session cookie?',
])
def test_other_work_is_not_intercepted(text):
    assert not is_session_id_request(text)


def forbid_agent_work(app, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail('Session metadata must not start agent or title work')
    monkeypatch.setattr(app.state.manager, 'submit', fail)
    monkeypatch.setattr(app.state.session_titles, 'schedule', fail)


@pytest.mark.parametrize('mode', ['demo', 'modal'])
def test_new_session_is_instant_durable_and_idempotent(workspace, monkeypatch, mode):
    app, client = workspace
    forbid_agent_work(app, monkeypatch)
    # No model/cloud credentials, and even a full agent queue is irrelevant.
    app.state.store.max_pending_runs = 0
    body = {'prompt': 'What is this session’s ID?', 'mode': mode, 'client_id': 'metadata-create'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post('/api/runs', json=body), range(2)))
    assert [r.status_code for r in results] == [201, 201]
    run = results[0].json()
    assert run['id'] == results[1].json()['id']
    assert run['status'] == 'idle' and run['display_title'] == 'Session ID'
    reopened = Store(app.state.settings.data_dir)
    messages = reopened.messages(run['id'])
    assert [(m['role'], m['status']) for m in messages] == [('user', 'completed'), ('assistant', 'completed')]
    assert messages[1]['content'] == session_id_response(run['id']) and messages[1]['model'] == ''
    assert not reopened.has_queued_messages(run['id'])
    assert not reopened.rows('SELECT * FROM model_requests')
    assert run['sandbox_id'] == ''
    assert client.post('/api/runs', json={**body, 'prompt': '/session-id'}).status_code == 409


@pytest.mark.parametrize('status', ['idle', 'running', 'awaiting_approval', 'stopping', 'cancelled'])
def test_followup_preserves_agent_state_and_queue(workspace, monkeypatch, status):
    app, client = workspace
    store = app.state.store
    run = store.create_run('Original work', '', 'modal', [], chat_enabled=True)
    active = store.claim_message(run['id'])
    queued, _ = store.enqueue_message(run['id'], 'Keep working', 'pending-work')
    store.update_run(run['id'], status=status, summary='Original result', pending_result='original')
    store.execute('UPDATE runs SET steer_message_id=?,turn_model_calls=3 WHERE id=?', (queued['id'], run['id']))
    before = store.run(run['id'])
    original_messages = store.messages(run['id'])
    forbid_agent_work(app, monkeypatch)
    body = {'content': '/session-id', 'client_id': 'metadata-followup', 'send_now': True}
    url = f"/api/runs/{run['id']}/messages"
    response = client.post(url, json=body)
    assert response.status_code == 202 and response.json()['status'] == 'completed'
    assert response.json()['created'] is True
    assert client.post(url, json=body).json()['created'] is False
    assert client.post(url, json={**body, 'content': 'session id'}).status_code == 409
    after = store.run(run['id'])
    assert {k: v for k, v in after.items() if k != 'updated_at'} == {k: v for k, v in before.items() if k != 'updated_at'}
    messages = store.messages(run['id'])
    assert [m for m in messages if m['id'] in {active['id'], queued['id']}] == original_messages
    assert len(messages) == 4
    assert next(m for m in messages if m['role'] == 'assistant')['content'] == session_id_response(run['id'])


def test_auth_deletion_and_attachments_keep_normal_boundaries(workspace, monkeypatch):
    app, client = workspace
    submitted = []
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: submitted.append(run))
    attachment = upload(client).json()['id']
    response = client.post('/api/runs', json={'prompt': '/session-id', 'attachment_ids': [attachment]})
    run_id = response.json()['id']
    assert len(submitted) == 1 and response.json()['status'] == 'queued'
    url = f'/api/runs/{run_id}/messages'
    body = {'content': '/session-id', 'client_id': 'metadata-auth'}
    assert client.post(url, json=body, headers={'X-CSRF-Token': ''}).status_code == 403
    next_attachment = upload(client).json()['id']
    assert client.post(url, json={**body, 'attachment_ids': [next_attachment]}).json()['status'] == 'queued'
    assert len(submitted) == 2
    store = app.state.store
    store.execute("UPDATE runs SET deleted_at='2026-10-07T00:00:00Z' WHERE id=?", (run_id,))
    assert client.post(url, json={**body, 'client_id': 'deleted-metadata'}).status_code == 404
    client.cookies.clear()
    assert client.post('/api/runs', json={'prompt': '/session-id'}).status_code == 401


def test_subagent_metadata_does_not_reopen_parent_work(workspace, monkeypatch):
    app, client = workspace
    run = app.state.store.create_run('Worker assignment', '', 'demo', [], chat_enabled=True)
    app.state.store.execute("UPDATE runs SET parent_run_id='parent',status='waiting_children' WHERE id=?", (run['id'],))
    forbid_agent_work(app, monkeypatch)
    response = client.post(f"/api/runs/{run['id']}/messages", json={'content': 'session id', 'client_id': 'child-metadata'})
    assert response.status_code == 202 and response.json()['status'] == 'completed'
    assert app.state.store.run(run['id'])['status'] == 'waiting_children'


def test_title_backfill_cannot_make_a_model_call(workspace, monkeypatch):
    app, client = workspace
    requests = []
    gateway(monkeypatch, lambda request: requests.append(request) or completion())
    response = client.post('/api/runs', json={'prompt': '/session-id'})
    app.state.settings.litellm_api_key = 'test-key'
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    client.portal.call(app.state.session_titles.start)
    app.state.session_titles.schedule(response.json()['id'])
    client.portal.call(drain_titles, app.state.session_titles)
    assert not requests


@pytest.mark.parametrize('fresh', [True, False])
def test_slack_metadata_uses_durable_control_reply_without_agent_work(mirror, monkeypatch, fresh):
    app, client, submitted, sent = mirror
    if not fresh:
        _, _, run_id = start(mirror)
        app.state.store.update_run(run_id, status='running', summary='Existing work')
        app.state.store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
        before = app.state.store.run(run_id)
    submitted.clear()
    forbid_agent_work(app, monkeypatch)
    app.state.settings.modal_token_id = ''
    app.state.settings.litellm_api_key = ''
    payload = event('EvMetadata', text='<@U99999999> what is this session id?',
                    ts='1790719010.123456', **({} if fresh else {'thread_ts': ROOT}))
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    receipt = app.state.store.rows("SELECT * FROM slack_receipts WHERE event_id='EvMetadata'")[0]
    run_id = receipt['run_id']
    assert receipt['command'] == 'session-id' and receipt['message_id'] is None
    replies = app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key='command:EvMetadata'")
    assert len(replies) == 1 and session_id_response(run_id) in replies[0]['text']
    if fresh:
        assert app.state.store.run(run_id)['status'] == 'idle'
    else:
        assert app.state.store.run(run_id) == before
        assert app.state.store.rows('SELECT paused FROM slack_threads')[0]['paused'] == 1
    drain(app, client)
    assert len([message for message in sent if session_id_response(run_id) in message.get('text', '')]) == 1
    assert not submitted


def test_web_mirror_orders_input_before_instant_answer(mirror, monkeypatch):
    app, client, run_id = start(mirror)
    first = app.state.store.claim_message(run_id)
    app.state.store.finish_message(run_id, first['id'], 'Previous answer')
    app.state.store.update_run(run_id, status='idle')
    forbid_agent_work(app, monkeypatch)
    assert web(app, client, run_id, text='/session-id').json()['status'] == 'completed'
    assert web(app, client, run_id, text='/session-id').json()['created'] is False
    app.state.slack.chat.collect()
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind IN ('input','answer') ORDER BY id")
    assert [r['kind'] for r in rows] == ['answer', 'input', 'answer']
    assert 'Previous answer' in rows[0]['text']
    assert session_id_response(run_id) in rows[2]['text']


@pytest.mark.parametrize('extra', [
    {'text': '<@U99999999> What is the session id? Then fix the bug.'},
    {'text': '<@U99999999> /session-id', 'files': [{'id': 'F12345678'}]},
    {'text': '<@U99999999> model test-model\n/session-id'},
])
def test_slack_extra_work_still_reaches_agent(slack_app, extra):
    app, client, submitted, _ = slack_app
    assert client.post('/hooks/slack/events', **signed(event(**extra))).status_code == 200
    assert len(submitted) == 1
    assert app.state.store.has_queued_messages(submitted[0]['id'])


def test_slack_dm_returns_its_own_session_id(mirror, monkeypatch):
    from test_slack_chat import dm_event
    app, client, submitted, sent = mirror
    forbid_agent_work(app, monkeypatch)
    assert client.post('/hooks/slack/events', **signed(dm_event(1, '/session-id'))).status_code == 200
    run_id = app.state.store.rows('SELECT id FROM runs')[0]['id']
    drain(app, client)
    reply = next(message for message in sent if session_id_response(run_id) in message.get('text', ''))
    assert reply['channel'].startswith('D')
    assert not submitted
