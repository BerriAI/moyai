import asyncio
import json

import pytest

from app.db import Store
from app.slack_chat import SlackChat
from test_slack import slack_app, event, signed
from test_slack_chat import start, send, dm_event, ROOT
from test_spend import sign_in


@pytest.fixture
def mirror(slack_app, monkeypatch):
    app, client, submitted, sent = slack_app
    # Deterministic delivery; production uses the same persisted outbox worker.
    client.portal.call(app.state.slack.chat.shutdown)
    original = app.state.connectors.request

    async def request(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        return {**result, 'ts': str(len(sent)) + '.123456'}

    monkeypatch.setattr(app.state.connectors, 'request', request)
    return slack_app


def web(app, client, run_id, text='Continue from web', key='web-message-1', user='alice'):
    sign_in(app, client, user, user+'@berri.ai')
    return client.post(f'/api/runs/{run_id}/messages', json={'content': text, 'client_id': key})


def drain(app):
    chat = app.state.slack.chat
    for _ in range(30):
        if not app.state.store.rows("SELECT 1 FROM slack_outbox WHERE status='pending'"):
            return
        chat.last_post.clear()
        asyncio.run(chat.deliver_one())
    raise AssertionError('Outbox did not drain')


def test_web_attachment_mirrors_a_protected_link_once_without_file_bytes(mirror):
    from test_attachments import upload
    app, client, run_id = start(mirror)
    sign_in(app, client)
    file = upload(client).json()
    body = {'content': 'Read the document', 'client_id': 'web-file-message', 'attachment_ids': [file['id']]}
    assert client.post(f'/api/runs/{run_id}/messages', json=body).json()['created'] is True
    assert client.post(f'/api/runs/{run_id}/messages', json=body).json()['created'] is False
    rows = app.state.store.rows("SELECT text FROM slack_outbox WHERE kind='input'")
    assert len(rows) == 1 and 'SKILL.md' in rows[0]['text']
    assert '/#run=' + run_id in rows[0]['text']
    assert 'User-provided file' not in rows[0]['text']


def test_web_retry_two_sso_senders_and_answer_order(mirror):
    app, client, run_id = start(mirror)
    # A saved answer can precede a web input before the collector runs.
    first = app.state.store.claim_message(run_id)
    app.state.store.finish_message(run_id, first['id'], 'Earlier answer')
    app.state.store.update_run(run_id, status='idle')
    assert web(app, client, run_id).json()['created'] is True
    assert web(app, client, run_id).json()['created'] is False
    assert web(app, client, run_id, key='web-message-2', user='bob').status_code == 202
    next_turn = app.state.store.claim_message(run_id)
    app.state.store.finish_message(run_id, next_turn['id'], 'Answer to web')
    app.state.slack.chat.collect()
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind IN ('input','answer') ORDER BY id")
    assert [r['kind'] for r in rows] == ['answer', 'input', 'input', 'answer']
    assert [json.loads(r['metadata'])['sender_id'] for r in rows if r['kind'] == 'input'] == ['google:alice', 'google:bob']
    drain(app)
    posts = [r for r in mirror[3] if 'text' in r]
    assert len(posts) == 4
    assert 'Alice (alice@berri.ai) · via Moyai web' in posts[1]['text']
    assert 'Bob (bob@berri.ai) · via Moyai web' in posts[2]['text']
    assert all(r['channel'] == 'C12345678' and r['thread_ts'] == ROOT for r in posts)
    source = app.state.slack.channel.source_for_run(run_id)
    history = asyncio.run(app.state.slack.agentchat.state.history(source.conversation_id))
    assert len(history) == 5
    # A still-queued input follows the executed turn and its answer.
    assert [m.role for m in history] == ['user', 'assistant', 'user', 'assistant', 'user']
    assert web(app, client, run_id, user='bob').status_code == 409


@pytest.mark.parametrize('changes', [{}, {'thread_ts': '1790728000.123456'}])
def test_web_input_reuses_the_exact_dm_destination(mirror, changes):
    app, client, submitted, sent = mirror
    payload = dm_event(1, 'Direct session')
    payload['event'].update(changes)
    client.post('/hooks/slack/events', **signed(payload))
    run_id = submitted[0]['id']
    assert web(app, client, run_id).status_code == 202
    drain(app)
    post = next(r for r in sent if 'text' in r)
    assert post['channel'] == 'D12345678'
    assert post['thread_ts'] == changes.get('thread_ts', payload['event']['ts'])


def test_long_input_is_complete_scrubbed_and_cannot_ping_or_echo(mirror):
    app, client, run_id = start(mirror)
    text = 'hello <!channel> <@U87654321> model-test-key\n' + ('code\n' * 2000)
    assert web(app, client, run_id, text=text).status_code == 202
    drain(app)
    posts = [r for r in mirror[3] if 'metadata' in r]
    assert len(posts) > 2
    assert ''.join(r['blocks'][1]['text']['text'] for r in posts) == app.state.store.messages(run_id)[-1]['content'].replace('model-test-key', '[redacted]')
    assert all(len(r['blocks'][1]['text']['text']) <= 2600 for r in posts)
    assert all('<!channel>' not in r['text'] and not r['mrkdwn'] and r['parse'] == 'none' for r in posts)
    for i, post in enumerate(posts):
        payload = event('EvEcho'+str(i), type='message', bot_id='BMOYAI', user='U99999999',
                        text=post['text'], thread_ts=ROOT, ts=f'17907199{i:02d}.123456')
        client.post('/hooks/slack/events', **signed(payload))
    assert len(app.state.store.messages(run_id)) == 2


def test_sleep_and_reconnect_never_backfill_web_inputs(mirror):
    app, client, run_id = start(mirror)
    web(app, client, run_id, text='Queued before sleep')
    send(client, 1, 'sleep')
    app.state.store.update_run(run_id, status='idle')
    web(app, client, run_id, text='Typed while asleep', key='while-asleep')
    send(client, 2, 'wake')
    app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    web(app, client, run_id, text='Typed while disabled', key='while-disabled')
    app.state.store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='slack'")
    app.state.slack.chat.collect()
    drain(app)
    inputs = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='input'")
    assert len(inputs) == 1 and inputs[0]['status'] == 'skipped'
    assert not any('via Moyai web' in r.get('text', '') for r in mirror[3])


def test_new_workspace_cannot_receive_queued_inputs(mirror):
    app, client, run_id = start(mirror)
    web(app, client, run_id)
    app.state.store.execute("UPDATE slack_threads SET team_id='TOTHER123'")
    drain(app)
    assert app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='input'")[0]['status'] == 'skipped'
    assert mirror[3] == []


def test_transaction_rolls_back_input_if_mirror_queue_fails(mirror, monkeypatch):
    app, client, run_id = start(mirror)
    def fail(*args, **kwargs):
        raise RuntimeError('Disk unavailable')
    monkeypatch.setattr(app.state.slack.chat, 'queue', fail)
    with pytest.raises(RuntimeError, match='Disk unavailable'):
        web(app, client, run_id)
    assert len(app.state.store.messages(run_id)) == 1


def test_pending_input_survives_restart_and_ambiguous_send_never_replays(mirror, monkeypatch):
    app, client, run_id = start(mirror)
    web(app, client, run_id)
    reopened = Store(app.state.settings.data_dir)
    assert len(reopened.rows("SELECT * FROM slack_outbox WHERE kind='input' AND status='pending'")) == 1
    attempts = []
    original = app.state.connectors.request
    async def fail(method, url, **kwargs):
        if url.endswith('chat.postMessage'):
            attempts.append(1)
            raise TimeoutError('Response lost after Slack accepted post')
        return await original(method, url, **kwargs)
    monkeypatch.setattr(app.state.connectors, 'request', fail)
    drain(app)
    assert reopened.rows("SELECT status FROM slack_outbox WHERE kind='input'")[0]['status'] == 'uncertain'
    app.state.slack.chat = SlackChat(app.state.slack)
    app.state.slack.chat.collect()
    drain(app)
    assert attempts == [1]
    assert app.state.store.messages(run_id)[-1]['content'] == 'Continue from web'


def test_old_and_unbound_inputs_are_never_backfilled(mirror):
    app, client, run_id = start(mirror)
    user_id = app.state.store.identity({'method': 'password', 'role': 'admin'})
    app.state.store.enqueue_message(run_id, 'Old web text', 'old-web-text', user_id=user_id)
    app.state.slack.chat.collect()
    standalone = app.state.store.create_run('Standalone', '', 'demo', [], chat_enabled=True)
    assert web(app, client, standalone['id']).status_code == 202
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='input'")


def test_selected_progress_follows_previous_answer_and_precedes_web_input(mirror):
    app, client, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    first = store.claim_message(run_id)
    store.finish_message(run_id, first['id'], 'Earlier final')
    store.enqueue_message(run_id, 'Next task', 'next')
    active = store.claim_message(run_id)
    for text in ('Opening', 'Tests pass', 'Unwanted narration'):
        store.event(run_id, 'message', text, {'phase': 'commentary'})
    assert web(app, client, run_id).status_code == 202
    chat.collect()
    rows = store.rows('SELECT kind,text FROM slack_outbox ORDER BY id')
    assert [r['kind'] for r in rows] == ['answer', 'progress', 'progress', 'input']
    drain(app)
    store.finish_message(run_id, active['id'], 'Verification failed', 'failed')
    chat.collect()
    drain(app)
    posts = [r for r in mirror[3] if 'text' in r]
    assert [p['text'].split('\n')[0] for p in posts[:3]] == ['Earlier final', 'Opening', 'Tests pass']
    assert 'Response failed:' in posts[-1]['text']
    assert all(p['channel'] == 'C12345678' and p['thread_ts'] == ROOT for p in posts)


@pytest.mark.parametrize('transition', ['finish', 'next-turn', 'stopping', 'sleep', 'disabled', 'team', 'flush-finish'])
def test_progress_delivery_rechecks_scope_and_binding_without_backfill(mirror, transition, monkeypatch):
    app, client, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    active = store.claim_message(run_id)
    store.event(run_id, 'message', 'Update before transition')
    chat.collect()
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='progress'") == [{'status': 'pending'}]
    if transition in {'finish', 'next-turn'}:
        store.finish_message(run_id, active['id'], 'Final')
        if transition == 'next-turn':
            store.enqueue_message(run_id, 'Next task', 'next')
            store.claim_message(run_id)
    elif transition == 'stopping':
        store.update_run(run_id, status='stopping')
    elif transition == 'sleep':
        store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
        store.event(run_id, 'message', 'Update while sleeping')
        send(client, 1, 'wake')  # Wake before the periodic collector runs.
    elif transition == 'disabled':
        store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
        store.event(run_id, 'message', 'Update while disabled')
        chat.collect()
        store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='slack'")
        chat.collect()
    elif transition == 'flush-finish':
        async def finish_during_flush():
            store.finish_message(run_id, active['id'], 'Final during checkpoint')
        monkeypatch.setattr(app.state.slack.checkpoints, 'flush', finish_during_flush)
    else:
        store.execute("UPDATE slack_threads SET team_id='TOTHER123'")
    drain(app)  # Completion and next-turn cases race after collection.
    assert not any('Update' in p.get('text', '') for p in mirror[3])
    assert all(r['status'] == 'skipped' for r in store.rows("SELECT status FROM slack_outbox WHERE kind='progress'"))


def test_pending_progress_survives_recovery_but_ambiguous_send_never_replays(mirror, monkeypatch):
    app, client, run_id = start(mirror)
    store = app.state.store
    store.claim_message(run_id)
    store.event(run_id, 'message', 'Opening update')
    app.state.slack.chat.collect()
    chat = app.state.slack.chat = SlackChat(app.state.slack)
    async def recover_without_delivery():
        chat.recover()
        await chat.shutdown()
    # Keep managed workers on the same loop as application lifespan teardown.
    client.portal.call(recover_without_delivery)
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='progress'") == [{'status': 'pending'}]
    attempts = []
    async def uncertain(*args, **kwargs):
        attempts.append(kwargs['json'])
        raise TimeoutError('Response lost after Slack accepted progress')
    monkeypatch.setattr(app.state.connectors, 'request', uncertain)
    drain(app)
    chat.collect()
    drain(app)
    assert len(attempts) == 1
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='progress'") == [{'status': 'uncertain'}]


def test_adopting_active_legacy_thread_never_backfills_existing_progress(mirror):
    app, client, _, _ = mirror
    store = app.state.store
    old = store.create_slack_run('EvOld', 'Old task', [], 'C12345678', ROOT, 'U12345678')
    store.claim_message(old['id'])
    store.event(old['id'], 'message', 'Progress before binding')
    send(client, 1, '<@U99999999> Continue')
    app.state.slack.chat.collect()
    drain(app)
    assert not any('Progress before binding' in p.get('text', '') for p in mirror[3])
    store.event(old['id'], 'message', 'Progress after binding')
    app.state.slack.chat.collect()
    drain(app)
    assert any('Progress after binding' in p.get('text', '') for p in mirror[3])


def test_legacy_commentary_consumes_budget_without_backfill_or_suppressing_approval(mirror):
    app, _, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    active = store.claim_message(run_id)
    for text in ('Old opening', 'Old milestone'):
        store.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'message',?,?,'2026-10-03')",
                      (run_id, text, json.dumps({'turn_id': active['id'], 'phase': 'commentary'})))
    store.event(run_id, 'message', 'Unwanted third update')
    store.execute("INSERT INTO approvals VALUES('approval',?,'linear_comment','{}','pending','2026-10-03','')", (run_id,))
    chat.collect()
    drain(app)
    assert len([e for e in store.events(run_id) if e['kind'] == 'message']) == 2
    assert not store.rows("SELECT 1 FROM slack_outbox WHERE kind='progress'")
    posts = [p for p in mirror[3] if 'text' in p]
    assert len(posts) == 1 and 'administrator' in posts[0]['text']
