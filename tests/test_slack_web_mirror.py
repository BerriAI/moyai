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
    assert [m.role for m in history] == ['user', 'assistant', 'user', 'user', 'assistant']
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
    assert post['thread_ts'] == changes.get('thread_ts')


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
