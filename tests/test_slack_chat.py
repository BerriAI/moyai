import asyncio
import json
import sqlite3
import time

import pytest
from concurrent.futures import ThreadPoolExecutor

from app.db import Store, now
from app.slack_chat import slack_text, split_reply
from storage_fixture import MemoryObjects
from test_slack import slack_app, signed, event, wait_for

ROOT = '1790719000.123456'


def send(client, index, text, **kw):
    payload = event(f'EvChat{index}', type='message', ts=f'17907190{index:02d}.123456', thread_ts=ROOT, text=text, **kw)
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    return payload


def start(slack_app):
    app, client, runs, _ = slack_app
    assert client.post('/hooks/slack/events', **signed(event())).status_code == 200
    return app, client, runs[0]['id']


def finish(app, run_id, answer):
    message = app.state.store.claim_message(run_id)
    assert message
    app.state.store.finish_message(run_id, message['id'], answer)
    app.state.store.update_run(run_id, status='idle', summary=answer)
    app.state.slack.chat.collect()


def receive(app, run_id, answer):
    message = app.state.store.claim_message(run_id)
    assert message
    app.state.store.update_run(run_id, status='running')
    app.state.manager.receive_result(run_id, {'message_id': message['id'], 'completed': True, 'message': answer})
    app.state.slack.chat.collect()
    return message


def test_web_reply_attribution_uses_current_sender_profile_without_rewriting_history(slack_app):
    from test_spend import sign_in

    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Ready.')
    prompt = 'can you make a pr ?\nKeep the literal ID U12345678 in this example.'
    send(client, 1, '<@U99999999> ' + prompt)
    sign_in(app, client)

    def reply():
        response = client.get(f'/api/runs/{run_id}')
        assert response.status_code == 200
        return response.json()['messages'][-1]

    original = f'Slack reply from U12345678:\n{prompt}'
    assert reply()['display_content'] == f'Slack reply from Slack teammate:\n{prompt}'
    actor = 'slack:T12345678:U12345678'
    app.state.store.execute('UPDATE users SET name=?,email=? WHERE id=?', ('Ryan', 'ryan@berri.ai', actor))
    assert reply()['display_content'] == f'Slack reply from Ryan:\n{prompt}'
    assert reply()['content'] == original
    assert reply()['user_id'] == actor
    claimed = app.state.store.claim_message(run_id)
    assert claimed['content'] == original
    app.state.store.finish_message(run_id, claimed['id'], original)
    # Assistant text quoting a Slack label must not be rewritten.
    assert 'display_content' not in reply()
    # A profile refresh also fixes old, completed history on the next read.
    app.state.store.execute('UPDATE users SET name=? WHERE id=?', ('Ryan Updated', actor))
    history = client.get(f'/api/runs/{run_id}').json()['messages']
    assert history[-2]['display_content'] == f'Slack reply from Ryan Updated:\n{prompt}'
    assert app.state.store.rows('SELECT content FROM messages WHERE id=?', (claimed['id'],))[0]['content'] == original


def test_web_reply_attribution_does_not_rename_initial_or_web_text(slack_app):
    from test_spend import sign_in

    app, client, _, _ = slack_app
    literal = 'Slack reply from U12345678:\nQuoted example'
    client.post('/hooks/slack/events', **signed(event(text='<@U99999999> ' + literal)))
    run_id = app.state.store.rows('SELECT id FROM runs')[0]['id']
    first = app.state.store.messages(run_id)[0]
    assert first['content'] == literal and 'display_content' not in first
    user = sign_in(app, client)
    app.state.store.enqueue_message(run_id, literal, 'web-example', user_id=user)
    assert 'display_content' not in app.state.store.messages(run_id)[-1]


def test_web_reply_attribution_uses_reply_sender_and_email_fallback(slack_app):
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Ready.')
    app.state.store.execute("UPDATE users SET name='Original sender' WHERE kind='slack'")
    send(client, 1, '<@U99999999> Another teammate replies.', user='U87654321')
    app.state.store.execute("UPDATE users SET email='ryan@berri.ai' WHERE id='slack:T12345678:U87654321'")
    reply = app.state.store.messages(run_id)[-1]
    assert reply['display_content'] == 'Slack reply from ryan@berri.ai:\nAnother teammate replies.'
    assert reply['content'] == 'Slack reply from U87654321:\nAnother teammate replies.'


def publication(app, run_id, number=100, title='Show the completed demo'):
    result = {'number': number, 'url': f'https://github.com/BerriAI/moyai/pull/{number}',
              'repository': 'BerriAI/moyai', 'title': title, 'draft': False}
    app.state.store.execute('''INSERT INTO github_publications
        (id,run_id,message_id,arguments_hash,branch,result,connection_version,created_at)
        VALUES(?,?,0,'test','test',?,'test',?)''', (f'{run_id}-{number}', run_id, json.dumps(result), now()))
    return result['url']


def test_pr_completion_uses_confirmed_receipt_and_preserves_plain_replies(slack_app):
    app, client, run_id = start(slack_app)
    url = publication(app, run_id, title='Fix <@U88888888> & preview')
    finish(app, run_id, f'Working demo verified. [View PR]({url})')
    wait_for(lambda: any(message.get('attachments') for message in slack_app[3]))
    posted = next(message for message in slack_app[3] if message.get('attachments'))
    assert posted['channel'] == 'C12345678' and posted['thread_ts'] == ROOT
    card = posted['attachments'][0]
    assert card['color'] == '#5B3FD1'
    assert posted['blocks'][0]['expand'] is True
    assert all(block['expand'] is True for block in card['blocks'] if block['type'] == 'section')
    assert '<@U88888888>' not in json.dumps(card)
    actions = next(block['elements'] for block in card['blocks'] if block['type'] == 'actions')
    assert [item['url'] for item in actions] == [url, url + '/files', f'https://workspace.example/#run={run_id}']
    send(client, 1, '<@U99999999> An unrelated follow-up')
    finish(app, run_id, 'No new pull request.')
    wait_for(lambda: any('No new pull request.' in message.get('text', '') for message in slack_app[3]))
    assert len([message for message in slack_app[3] if message.get('attachments')]) == 1


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('delivery', ['completed', 'saving', 'deferred'])
def test_capture_handoff_selects_only_referenced_saved_files_and_handles_old_grant(slack_app, remote, delivery):
    app, client, run_id = start(slack_app)
    client.portal.call(app.state.slack.chat.shutdown)
    store = app.state.store
    if remote:
        store.objects = MemoryObjects()
    url = publication(app, run_id)
    files = [('demo.webm', b'\x1aE\xdf\xa3webm-recording-fixture'),
             ('result.png', b'\x89PNG\r\n\x1a\nscreenshot-fixture'),
             ('unrelated.png', b'\x89PNG\r\n\x1a\nunrelated-private-content')]
    if delivery != 'deferred':
        for name, raw in files:
            store.artifacts.save(run_id + '-captures/' + name, raw)
    answer = (f'[PR]({url})\n[Video](/workspace/moyai-captures/demo.webm)\n'
              '![Screenshot](/workspace/moyai-captures/result.png)\n'
              '`moyai-captures/result.png` and "/workspace/moyai-captures/demo.webm"')
    message = (finish if delivery == 'completed' else receive)(app, run_id, answer)
    rows = store.rows("SELECT * FROM slack_outbox WHERE kind='answer' ORDER BY id")
    if delivery == 'deferred':
        assert f'<https://workspace.example/#run={run_id}|Video>' in rows[0]['text']
        assert not any('/computer/captures/' in row['text'] for row in rows)
    else:
        assert any(f'https://workspace.example/api/runs/{run_id}/computer/captures/demo.webm' in row['text'] for row in rows)
    assert not any('moyai-captures/' in row['text'] for row in rows)
    if delivery != 'completed':
        assert not any('captures' in json.loads(row['metadata']) for row in rows)
        if remote:
            assert store.objects.reads == 0
        drain_answers(app)
        if delivery == 'deferred':
            for name, raw in files:
                store.artifacts.save(run_id + '-captures/' + name, raw)
        store.finish_message(run_id, message['id'], answer)
        app.state.slack.chat.collect()
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' ORDER BY id")
    media = next(json.loads(row['metadata']) for row in rows if 'captures' in json.loads(row['metadata']))
    assert [capture['name'] for capture in media['captures']] == ['demo.webm', 'result.png']
    assert not any('/workspace/moyai-captures/' in row['text'] for row in rows)
    drain_answers(app)
    assert sum(f'<{url}|PR>' in message.get('text', '') for message in slack_app[3]) == 1
    assert any('Reconnect Slack' in message.get('text', '') for message in slack_app[3])
    assert not any('unrelated.png' in json.dumps(message) for message in slack_app[3])
    app.state.slack.chat.collect()
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer'")) == len(rows)


def test_thread_followups_and_both_slack_event_types_share_one_session(slack_app):
    app, client, run_id = start(slack_app)
    mention = event('EvPaired', type='message')
    client.post('/hooks/slack/events', **signed(mention))
    finish(app, run_id, 'Remembering **blue lantern**. [Docs](https://example.com).')
    followup = send(client, 1, '<@U99999999> What was the phrase?')
    followup['event_id'] = 'EvSecondType'
    followup['event'].update(type='app_mention', text='<@U99999999> What was the phrase?')
    client.post('/hooks/slack/events', **signed(followup))
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1
    messages = app.state.store.messages(run_id)
    assert len(messages) == 3
    assert 'What was the phrase?' in messages[-1]['content']
    assert messages[-1]['status'] == 'queued'
    finish(app, run_id, 'It was blue lantern.')
    app.state.slack.chat.collect()
    answers = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer'")
    assert len(answers) == 2
    assert '*blue lantern*' in answers[0]['text']
    assert '<https://example.com|Docs>' in answers[0]['text']
    assert 'It was blue lantern.' in answers[1]['text']
    assert len(app.state.store.rows('SELECT * FROM slack_receipts')) == 2


def test_existing_slack_session_keeps_modal_after_switch_to_unconfigured_substrate(slack_app):
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Ready.')
    app.state.settings.sandbox_provider = 'substrate'
    assert app.state.settings.missing_sandbox('substrate')
    send(client, 1, '<@U99999999> Continue on the original provider.')
    assert app.state.store.run(run_id)['sandbox_provider'] == 'modal'
    assert app.state.store.messages(run_id)[-1]['status'] == 'queued'


def test_parallel_duplicate_delivery_cannot_enqueue_twice(slack_app):
    app, client, run_id = start(slack_app)
    payload = event('EvParallelFollowup', type='message', ts='1790719001.111111', thread_ts=ROOT, text='<@U99999999> Continue please')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: client.post('/hooks/slack/events', **signed(payload)).status_code, range(4)))
    assert results == [200] * 4
    assert len(app.state.store.messages(run_id)) == 2
    assert len(app.state.store.rows('SELECT * FROM slack_receipts')) == 2


def test_workspace_save_failure_delivers_the_preserved_answer_once(slack_app):
    from app.runner import SAVE_WARNING
    app, client, run_id = start(slack_app)
    message = app.state.store.claim_message(run_id)
    answer = 'Implementation prepared; nine tests still fail.\n\nWorkspace save warning: ' + SAVE_WARNING
    app.state.store.finish_message(run_id, message['id'], answer, 'save_failed')
    app.state.store.update_run(run_id, status='failed', summary=answer, checkpoint_error=SAVE_WARNING)
    app.state.slack.chat.collect()
    app.state.slack.chat.collect()
    answers = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' AND run_id=?", (run_id,))
    assert len(answers) == 1
    assert answers[0]['text'].startswith('Implementation prepared')
    assert 'latest workspace files could not be saved' in answers[0]['text']
    assert 'Response failed:' not in answers[0]['text']


@pytest.mark.parametrize('outcome', ['completed', 'save_failed', 'failed', 'cancelled', 'interrupted'])
def test_received_answer_settles_once_after_restart_without_reposting(slack_app, outcome):
    from app.slack_chat import SlackChat
    app, client, run_id = start(slack_app)
    client.portal.call(app.state.slack.chat.shutdown)
    message = receive(app, run_id, 'The implementation is ready.')
    store = app.state.store
    assert store.messages(run_id)[0]['status'] == 'running'
    store.enqueue_message(run_id, 'Continue later', 'queued-next')
    assert store.claim_message(run_id) is None
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer_settlement'") == [{'status': 'waiting'}]
    drain_answers(app)
    # A replacement collector reads the saved publication and settlement receipts.
    app.state.slack.chat = SlackChat(app.state.slack)
    store.finish_message(run_id, message['id'], 'The implementation is ready.', outcome)
    app.state.slack.chat.collect()
    app.state.slack.chat.collect()
    drain_answers(app)
    rows = store.rows("SELECT text FROM slack_outbox WHERE kind='answer' ORDER BY id")
    assert sum('The implementation is ready.' in row['text'] for row in rows) == 1
    assert len(rows) == (1 if outcome == 'completed' else 2)
    if outcome != 'completed':
        assert ('latest workspace files could not be saved' if outcome == 'save_failed' else
                'Response ' + outcome) in rows[-1]['text']
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer_settlement'") == [{'status': 'settled'}]


@pytest.mark.parametrize('transition', ['sleep', 'wake_before_collect', 'disabled', 'team', 'deletion'])
def test_received_answer_settlement_never_backfills_after_suppression(slack_app, transition):
    app, client, run_id = start(slack_app)
    client.portal.call(app.state.slack.chat.shutdown)
    message = receive(app, run_id, 'An answer before workspace saving.')
    store, chat = app.state.store, app.state.slack.chat
    if transition == 'sleep':
        send(client, 1, 'sleep')
    elif transition == 'wake_before_collect':
        store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
    elif transition == 'disabled':
        app.state.settings.slack_thread_chat_enabled = False
        chat.collect()
    elif transition == 'team':
        app.state.connectors.save('slack', {'bot': {'access_token': 'replacement-bot',
            'team': {'id': 'T87654321'}, 'bot_user_id': 'U99999999'}}, 'Other team')
        chat.collect()
    else:
        app.state.session_lifecycle.request_delete(run_id, '', True)
    store.finish_message(run_id, message['id'], 'An answer before workspace saving.', 'save_failed')
    store.update_run(run_id, status='idle')
    if transition in {'sleep', 'wake_before_collect'}:
        send(client, 2, 'wake')
    elif transition == 'disabled':
        app.state.settings.slack_thread_chat_enabled = True
    elif transition == 'team':
        app.state.connectors.save('slack', {'bot': {'access_token': 'separate-bot-secret',
            'team': {'id': 'T12345678'}, 'bot_user_id': 'U99999999'}}, 'Original team')
    chat.collect()
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer_settlement'") == [{'status': 'skipped'}]
    assert not store.rows("SELECT 1 FROM slack_outbox WHERE dedupe_key LIKE '%:outcome'")


def test_checkpoint_handoffs_do_not_post_synthetic_answers_to_slack(slack_app):
    app, client, run_id = start(slack_app)
    message = app.state.store.claim_message(run_id)
    app.state.store.finish_message(run_id, message['id'], 'Paused and saved', 'steered')
    assert not [m for m in app.state.store.messages(run_id) if m['role'] == 'assistant']
    # Notices saved by an older release must not be delivered after an upgrade.
    app.state.store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant','Old paused notice','steered',?)", (run_id, now()))
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' AND run_id=?", (run_id,))
    send(client, 1, '<@U99999999> Continue the same work')
    finish(app, run_id, 'The actual result')
    answers = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' AND run_id=?", (run_id,))
    assert len(answers) == 1 and 'The actual result' in answers[0]['text']


@pytest.mark.parametrize('state', ['idle', 'handoff', 'cancelled', 'deleted'])
def test_pending_access_forms_and_slack_notices_agree_after_followups(slack_app, state):
    from test_credentials import access_followup, access_request
    from test_spend import active, sign_in

    app, client, root = start(slack_app)
    app.state.settings.slack_thread_chat_enabled = False
    store, vault = app.state.store, app.state.credentials
    store.claim_message(root)
    store.update_run(root, status='running')
    child = active(app, model='test-model')
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root, child['id']))
    requests = {}
    for run_id in (root, child['id']):
        run = store.run(run_id)
        requests[run_id] = access_request(vault, run)['request_id']
        if state == 'handoff':
            access_followup(app, run)
        else:
            store.finish_message(run_id, run['active_message_id'], 'Access is still needed')
            store.update_run(run_id, status='idle' if state == 'deleted' else state)
            if state == 'deleted':
                store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), run_id))
    access_request(vault, active(app, model='test-model'))  # Unrelated requests never enter this Slack thread.
    sign_in(app, client)
    visible = set()
    for run_id, request_id in requests.items():
        response = client.get('/api/runs/' + run_id)
        assert response.status_code == (404 if state == 'deleted' else 200)
        if response.status_code == 200:
            forms = response.json()['credential_requests']
            assert [row['id'] for row in forms] == ([request_id] if state in {'idle', 'handoff'} else [])
            visible.update(row['id'] for row in forms)
    app.state.settings.slack_thread_chat_enabled = True
    app.state.slack.chat.collect()
    app.state.slack.chat.collect()
    notices = store.rows("SELECT dedupe_key,metadata FROM slack_outbox WHERE run_id=? AND dedupe_key LIKE 'credential:%'", (root,))
    assert {row['dedupe_key'] for row in notices} == {'credential:' + request_id for request_id in visible}
    assert len(notices) == len(visible)
    for run_id, request_id in requests.items():
        if request_id in visible:
            card = next(row for row in notices if row['dedupe_key'] == 'credential:' + request_id)
            assert json.loads(card['metadata'])['credential_request_id'] == request_id


@pytest.fixture
def access_delivery(slack_app, monkeypatch):
    from app.credentials import CredentialRequest
    from test_spend import active

    app, client, _, _ = slack_app
    client.portal.call(app.state.slack.chat.shutdown)
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs['headers']['Authorization'] == 'Bearer separate-bot-secret'
        calls.append((url.rsplit('/', 1)[-1], kwargs['json']))
        return {'ok': True, 'ts': '1790719999.123456'}

    monkeypatch.setattr(app.state.connectors, 'request', request)

    def deliver():
        app.state.slack.chat.last_post.clear()
        asyncio.run(app.state.slack.chat.deliver_one())

    def prepare(*, child=False, post=True):
        _, _, root = start(slack_app)
        app.state.store.claim_message(root)
        app.state.store.update_run(root, status='running')
        run = app.state.store.run(root)
        if child:
            run = active(app, user=run['active_user_id'], model='test-model')
            app.state.store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root, run['id']))
        result = app.state.credentials.request(run, CredentialRequest(provider='generic', name='Vercel',
            reason='Deploy the requested site', request_key='deploy', input_fields=[{
                'name': 'VERCEL_TOKEN', 'label': 'Vercel access token', 'secret': True, 'required': True}]))
        app.state.slack.chat.collect()
        if post:
            deliver()
        return root, run['id'], result['request_id']

    return app, client, prepare, calls, deliver


def scope_action(request_id, scope='personal', *, generation=0, clicked='1790720000.123456'):
    return {'type': 'block_actions', 'team': {'id': 'T12345678'}, 'user': {'id': 'U12345678'},
        'channel': {'id': 'C12345678'},
        'container': {'type': 'message', 'channel_id': 'C12345678', 'message_ts': '1790719999.123456'},
        'message': {'ts': '1790719999.123456', 'thread_ts': ROOT},
        'actions': [{'type': 'button', 'action_id': 'credential_scope_' + scope,
            'value': json.dumps({'request_id': request_id, 'generation': generation}), 'action_ts': clicked}],
        'response_url': 'https://untrusted.example/must-never-be-called'}


@pytest.mark.parametrize('child,scope,label', [(False, 'organization', 'Organization'),
    (False, 'session', 'Session only'), (False, 'personal', 'Personal'), (True, 'personal', 'Personal')])
def test_slack_access_scope_updates_one_named_card_and_links_exact_secure_form(access_delivery, child, scope, label):
    app, client, prepare, calls, deliver = access_delivery
    root, run_id, request_id = prepare(child=child)
    first = calls[-1]
    assert first[0] == 'chat.postMessage'
    assert first[1]['text'] == 'Credentials requested: VERCEL_TOKEN'
    assert first[1]['channel'] == 'C12345678' and first[1]['thread_ts'] == ROOT
    buttons = first[1]['blocks'][-1]['elements']
    assert [button['text']['text'] for button in buttons] == ['Organization', 'Personal', 'Session only']
    assert all('url' not in button for button in buttons)
    before = app.state.store.messages(root)
    # A later participant does not become the original access requester.
    app.state.store.execute("UPDATE runs SET active_user_id='slack:T12345678:U87654321' WHERE id=?", (run_id,))
    response = client.post('/hooks/slack/interactions', **signed(scope_action(request_id, scope), form=True))
    assert response.status_code == 200
    deliver()
    update = calls[-1]
    assert update[0] == 'chat.update' and update[1]['ts'] == '1790719999.123456'
    assert 'thread_ts' not in update[1]
    assert 'Selected: ' + label + '.' in update[1]['blocks'][1]['text']['text']
    link = update[1]['blocks'][-1]['elements'][0]
    assert link['text']['text'] == 'Provide Secret'
    assert link['url'] == f'https://workspace.example/#run={run_id}&credential={request_id}&generation=0'
    row = app.state.credentials.row(request_id)
    assert (row['preferred_scope'], row['scope_revision'], row['status']) == (scope, 1, 'pending')
    assert app.state.store.messages(root) == before
    assert not app.state.store.rows('SELECT * FROM provider_secrets')
    app.state.slack.chat.collect()
    deliver()
    assert [method for method, _ in calls] == ['chat.postMessage', 'chat.update']


@pytest.mark.parametrize('invalid', ['signature', 'expired', 'action', 'secret', 'team', 'channel', 'timestamp',
    'thread', 'requester', 'allowlist', 'paused', 'deleted', 'cancelled', 'resolved', 'generation', 'bot'])
def test_slack_access_actions_reject_changed_or_foreign_context(access_delivery, invalid):
    app, client, prepare, calls, _ = access_delivery
    root, _, request_id = prepare()
    payload = scope_action(request_id)
    store = app.state.store
    if invalid == 'action':
        payload['actions'][0]['action_id'] = 'personal'
    elif invalid == 'secret':
        payload['actions'][0]['value'] = json.dumps({'request_id': request_id, 'generation': 0, 'value': 'private-input'})
    elif invalid == 'team':
        payload['team']['id'] = 'T87654321'
    elif invalid == 'channel':
        payload['channel']['id'] = payload['container']['channel_id'] = 'C87654321'
    elif invalid == 'timestamp':
        payload['message']['ts'] = payload['container']['message_ts'] = '1790719998.123456'
    elif invalid == 'thread':
        payload['message']['thread_ts'] = '1790719998.123456'
    elif invalid == 'requester':
        payload['user']['id'] = 'U87654321'
        store.execute("UPDATE runs SET active_user_id='slack:T12345678:U87654321' WHERE id=?", (root,))
    elif invalid == 'allowlist':
        app.state.settings.slack_session_users = 'U87654321'
    elif invalid == 'paused':
        store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (root,))
    elif invalid == 'deleted':
        store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), root))
    elif invalid == 'cancelled':
        store.update_run(root, status='cancelled')
    elif invalid == 'resolved':
        store.execute("UPDATE credential_requests SET status='declined' WHERE id=?", (request_id,))
    elif invalid == 'generation':
        store.execute('UPDATE credential_requests SET generation=1 WHERE id=?', (request_id,))
    elif invalid == 'bot':
        app.state.connectors.save('slack', {'bot': {'access_token': 'replacement',
            'team': {'id': 'T12345678'}, 'bot_user_id': 'U00000000'}}, 'Changed')
    body = signed(payload, int(time.time()) - 601 if invalid == 'expired' else None, form=True)
    if invalid == 'signature':
        body['content'] += b' '
    response = client.post('/hooks/slack/interactions', **body)
    expected = 401 if invalid in {'signature', 'expired'} else 400 if invalid in {'action', 'secret'} else 403 if invalid in {'team', 'requester', 'allowlist'} else 409
    assert response.status_code == expected
    row = app.state.credentials.row(request_id)
    assert (row['preferred_scope'], row['scope_revision']) == ('', 0)
    assert [method for method, _ in calls] == ['chat.postMessage']


def test_slack_access_choices_are_ordered_and_cas_preserves_changes_during_delivery(access_delivery, monkeypatch):
    app, client, prepare, calls, deliver = access_delivery
    _, _, request_id = prepare()
    original = app.state.connectors.request

    def choose(scope, clicked):
        response = client.post('/hooks/slack/interactions', **signed(scope_action(request_id, scope, clicked=clicked), form=True))
        assert response.status_code == 200

    async def delayed(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        await asyncio.to_thread(choose, 'session', '1790720001.000001')
        return result

    choose('personal', '1790720000.000001')
    monkeypatch.setattr(app.state.connectors, 'request', delayed)
    chat = app.state.slack.chat
    flush = chat.owner.checkpoints.flush

    async def concurrent_delivery():
        checkpoint, release = asyncio.Event(), asyncio.Event()

        async def paused_flush():
            await flush()
            if asyncio.current_task() is sender:
                card = app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key=?", ('credential:' + request_id,))[0]
                if card['status'] == 'pending':
                    checkpoint.set()
                    await release.wait()

        monkeypatch.setattr(chat.owner.checkpoints, 'flush', paused_flush)
        chat.last_post.clear()
        sender = asyncio.create_task(chat.deliver_one())
        try:
            await asyncio.wait_for(checkpoint.wait(), 2)
            card = app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key=?", ('credential:' + request_id,))[0]
            assert card['id'] in chat.delivering
            monkeypatch.setattr(app.state.connectors, 'request', original)
            chat.last_post.clear()
            await chat.deliver_one()
            assert [method for method, _ in calls] == ['chat.postMessage', 'chat.update']
            assert card['id'] in chat.delivering
        finally:
            release.set()
            await sender
            monkeypatch.setattr(chat.owner.checkpoints, 'flush', flush)
        assert not chat.delivering

    client.portal.call(concurrent_delivery)
    card = app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key=?", ('credential:' + request_id,))[0]
    assert card['status'] == 'pending' and card['slack_ts'] == '1790719999.123456'
    monkeypatch.setattr(app.state.connectors, 'request', original)
    # Duplicate and out-of-order retries cannot undo a newer choice.
    choose('organization', '1790720000.000001')
    choose('session', '1790720001.000001')
    choose('session', '1790720002.000001')
    deliver()
    row = app.state.credentials.row(request_id)
    assert (row['preferred_scope'], row['scope_revision']) == ('session', 2)
    assert 'Selected: Session only.' in calls[-1][1]['blocks'][1]['text']['text']
    assert [method for method, _ in calls] == ['chat.postMessage', 'chat.update', 'chat.update']
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key=?", ('credential:' + request_id,))) == 1


@pytest.mark.parametrize('status', ['declined', 'satisfied'])
def test_slack_access_legacy_card_reopens_and_resolves_in_place(access_delivery, status: str) -> None:
    from app.credentials import Resolve

    app, client, prepare, calls, deliver = access_delivery
    _, _, request_id = prepare()
    store, vault = app.state.store, app.state.credentials
    # Older releases saved only a plain-text notice and its confirmed timestamp.
    store.execute("UPDATE slack_outbox SET metadata='{}',text='Access is needed' WHERE dedupe_key=?", ('credential:' + request_id,))
    app.state.slack.chat.collect()
    deliver()
    assert calls[-1][0] == 'chat.update'
    assert client.post('/hooks/slack/interactions', **signed(scope_action(request_id), form=True)).status_code == 200
    deliver()
    # Credentials owns reopening; the card consumes its generation/reset fields.
    store.execute("UPDATE credential_requests SET generation=1,preferred_scope='',scope_revision=0 WHERE id=?", (request_id,))
    app.state.slack.chat.collect()
    deliver()
    buttons = calls[-1][1]['blocks'][-1]['elements']
    assert len(buttons) == 3 and all('url' not in button for button in buttons)
    assert {json.loads(button['value'])['generation'] for button in buttons} == {1}
    assert client.post('/hooks/slack/interactions', **signed(scope_action(request_id), form=True)).status_code == 409
    current = scope_action(request_id, generation=1)
    assert client.post('/hooks/slack/interactions', **signed(current, form=True)).status_code == 200
    if status == 'declined':
        vault.resolve(request_id, Resolve(decision='decline', generation=1), vault.row(request_id)['actor_id'], False)
    else:
        store.update_run(vault.row(request_id)['run_id'], token_hash='active-test-capability-hash')
        asyncio.run(vault.call(store.run(vault.row(request_id)['run_id']), 'credentials_resolve', {
            'request_id': request_id, 'generation': 1, 'source': 'browser_session'}))
    app.state.slack.chat.collect()
    deliver()
    assert all(block['type'] != 'actions' for block in calls[-1][1]['blocks'])
    assert status in calls[-1][1]['blocks'][-1]['text']['text']
    assert client.post('/hooks/slack/interactions', **signed(current, form=True)).status_code == 409
    assert sum(method == 'chat.postMessage' for method, _ in calls) == 1


@pytest.mark.parametrize('change', ['paused', 'channel', 'bot', 'deleted'])
def test_slack_access_rechecks_destination_after_token_refresh(access_delivery, monkeypatch, change):
    app, _, prepare, calls, deliver = access_delivery
    root, _, request_id = prepare(post=False)

    async def refresh():
        if change == 'paused':
            app.state.store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (root,))
        elif change == 'channel':
            app.state.store.execute("UPDATE slack_threads SET channel='C87654321' WHERE run_id=?", (root,))
        elif change == 'deleted':
            app.state.store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), root))
        else:
            app.state.connectors.save('slack', {'bot': {'access_token': 'replacement',
                'team': {'id': 'T12345678'}, 'bot_user_id': 'U00000000'}}, 'Changed')
        return 'separate-bot-secret'

    monkeypatch.setattr(app.state.connectors, 'slack_bot_token', refresh)
    deliver()
    assert not calls
    assert app.state.store.rows('SELECT status FROM slack_outbox WHERE dedupe_key=?', ('credential:' + request_id,))[0]['status'] == 'uncertain'


@pytest.mark.parametrize('known_card', [False, True])
def test_slack_access_recovery_updates_known_cards_without_reposting_uncertain_initial_sends(access_delivery, known_card):
    app, client, prepare, calls, deliver = access_delivery
    _, _, request_id = prepare(post=known_card)
    app.state.store.execute("UPDATE slack_outbox SET status='sending' WHERE dedupe_key=?", ('credential:' + request_id,))

    async def recover():
        app.state.slack.chat.recover()
        await app.state.slack.chat.shutdown()

    client.portal.call(recover)
    deliver()
    assert [method for method, _ in calls] == (['chat.postMessage', 'chat.update'] if known_card else [])
    status = app.state.store.rows('SELECT status FROM slack_outbox WHERE dedupe_key=?', ('credential:' + request_id,))[0]['status']
    assert status == ('sent' if known_card else 'uncertain')


@pytest.mark.parametrize('known_card', [False, True])
def test_slack_access_retries_failed_updates_during_normal_collection_only(access_delivery, monkeypatch, known_card):
    app, client, prepare, calls, deliver = access_delivery
    _, _, request_id = prepare(post=known_card)
    if known_card:
        assert client.post('/hooks/slack/interactions', **signed(scope_action(request_id), form=True)).status_code == 200
    original = app.state.connectors.request

    async def lost_response(method, url, **kwargs):
        await original(method, url, **kwargs)
        raise TimeoutError('Provider response lost after delivery')

    monkeypatch.setattr(app.state.connectors, 'request', lost_response)
    deliver()
    assert app.state.store.rows('SELECT status FROM slack_outbox WHERE dedupe_key=?', ('credential:' + request_id,))[0]['status'] == 'uncertain'
    monkeypatch.setattr(app.state.connectors, 'request', original)
    app.state.slack.chat.collect()
    deliver()
    assert [method for method, _ in calls] == (['chat.postMessage', 'chat.update', 'chat.update'] if known_card else ['chat.postMessage'])
    assert app.state.credentials.row(request_id)['scope_revision'] == (1 if known_card else 0)
    if known_card:
        assert calls[-1][1]['blocks'][-1]['elements'][0]['text']['text'] == 'Provide Secret'


@pytest.mark.parametrize('paused', ['connection', 'thread', 'installation', 'resolved'])
def test_slack_access_rearms_skipped_cards_only_for_same_enabled_destination(access_delivery, paused):
    app, _, prepare, calls, deliver = access_delivery
    root, _, request_id = prepare(post=False)
    store = app.state.store
    if paused == 'thread':
        store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (root,))
    else:
        store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    app.state.slack.chat.collect()
    deliver()
    assert store.rows('SELECT status FROM slack_outbox WHERE dedupe_key=?', ('credential:' + request_id,))[0]['status'] == 'skipped'
    store.execute('UPDATE slack_threads SET paused=0 WHERE run_id=?', (root,))
    store.execute("DELETE FROM connection_policies WHERE provider='slack'")
    if paused == 'installation':
        app.state.connectors.save('slack', {'bot': {'access_token': 'replacement',
            'team': {'id': 'T12345678'}, 'bot_user_id': 'U00000000'}}, 'Changed')
    elif paused == 'resolved':
        store.execute("UPDATE credential_requests SET status='declined' WHERE id=?", (request_id,))
    app.state.slack.chat.collect()
    deliver()
    assert [method for method, _ in calls] == ([] if paused in {'installation', 'resolved'} else ['chat.postMessage'])


def test_slack_access_duplicate_ack_retries_a_failed_checkpoint(access_delivery, monkeypatch):
    app, client, prepare, _, _ = access_delivery
    _, _, request_id = prepare()
    flushed = []

    async def flush():
        flushed.append(1)
        if len(flushed) == 1:
            raise RuntimeError('Checkpoint temporarily unavailable')

    monkeypatch.setattr(app.state.slack.checkpoints, 'flush', flush)
    payload = signed(scope_action(request_id), form=True)
    with pytest.raises(RuntimeError, match='Checkpoint temporarily unavailable'):
        client.post('/hooks/slack/interactions', **payload)
    assert app.state.credentials.row(request_id)['preferred_scope'] == 'personal'
    assert client.post('/hooks/slack/interactions', **payload).status_code == 200
    assert len(flushed) == 2
    assert app.state.credentials.row(request_id)['scope_revision'] == 1


def test_ignores_unrelated_threads_bots_edits_wrong_team_and_shared_channels(slack_app):
    app, client, run_id = start(slack_app)
    for index, changes in enumerate([
        {'thread_ts': '1790718990.000000'}, {'bot_id': 'B12345678'}, {'user': 'U99999999'},
        {'subtype': 'message_changed'}, {'subtype': 'message_deleted'},
    ], 1):
        payload = event(f'EvIgnore{index}', type='message', text='Please do extra work', ts=f'17907190{index:02d}.111111', thread_ts=ROOT)
        payload['event'].update(changes)
        client.post('/hooks/slack/events', **signed(payload))
    payload = event('EvWrongTeam', type='message', text='not authorized', thread_ts=ROOT, ts='1790719010.111111')
    payload['team_id'] = 'T99999999'
    client.post('/hooks/slack/events', **signed(payload))
    payload['team_id'] = 'T12345678'; payload['is_ext_shared_channel'] = True
    client.post('/hooks/slack/events', **signed(payload))
    assert len(app.state.store.messages(run_id)) == 1
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1


@pytest.mark.parametrize('state', ['fresh', 'paused'])
@pytest.mark.parametrize('user', ['U12345678', 'U87654321'])
def test_unmentioned_replies_do_not_start_or_wake_threads(slack_app, state, user):
    app, client, submitted, _ = slack_app
    store = app.state.store
    if state != 'fresh':
        _, _, run_id = start(slack_app)
        finish(app, run_id, 'Ready for your next request.')
        send(client, 10, 'sleep')
    messages = store.rows('SELECT id,content,status FROM messages ORDER BY id')
    runs = store.rows('SELECT id,status,active_message_id,token_hash FROM runs')
    receipts = store.rows('SELECT event_id FROM slack_receipts ORDER BY event_id')
    controls = store.rows("SELECT id FROM slack_outbox WHERE kind='control'")
    submissions = len(submitted)
    for index, text in enumerate([
        'but if you have other things that are more urgent thats also ok', 'thanks', 'yes',
    ], 11):
        payload = send(client, index, text, user=user)
        assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert store.rows('SELECT id,content,status FROM messages ORDER BY id') == messages
    assert store.rows('SELECT id,status,active_message_id,token_hash FROM runs') == runs
    assert store.rows('SELECT event_id FROM slack_receipts ORDER BY event_id') == receipts
    assert store.rows("SELECT id FROM slack_outbox WHERE kind='control'") == controls
    assert len(submitted) == submissions
    if state == 'paused':
        assert store.rows('SELECT paused FROM slack_threads')[0]['paused'] == 1


def test_shared_thread_clarification_answer_does_not_need_another_mention(slack_app):
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Should I include the API documentation?')
    send(client, 1, 'yes')
    messages = app.state.store.messages(run_id)
    assert len(messages) == 3
    assert messages[-1]['content'] == 'Slack reply from U12345678:\nyes'
    assert messages[-1]['status'] == 'queued'
    assert len(app.state.store.rows('SELECT id FROM runs')) == 1


@pytest.mark.parametrize(('directive', 'feedback', 'valid'), [
    ('model opus', 'New messages in this session', True),
    ('model unavailable', 'Choose an enabled model', False),
    ('harness hermes', 'This session uses', True),
    ('harness unavailable', 'Choose a harness in a new thread', False),
])
def test_unmentioned_directives_accept_tasks_and_reply_to_controls(slack_app, directive, feedback, valid):
    app, client, run_id = start(slack_app)
    send(client, 1, directive + '\nPlease continue the work')
    before = app.state.store.messages(run_id)
    assert len(before) == 1 + int(valid)
    if valid:
        assert before[-1]['content'] == 'Slack reply from U12345678:\nPlease continue the work'
    assert app.state.store.rows("SELECT 1 FROM slack_receipts WHERE event_id='EvChat1'")
    send(client, 2, directive)
    assert app.state.store.messages(run_id) == before
    controls = app.state.store.rows("SELECT text FROM slack_outbox WHERE kind='control'")
    assert len(controls) == (1 if valid else 2)
    assert all(feedback in control['text'] for control in controls)


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('text', [
    '<@U99999999> !aside stop', '!aside <@U99999999> stop',
    '<@U99999999> (aside) sleep', '(ASIDE) <@U99999999> status',
])
def test_aside_with_a_mention_leaves_existing_work_running(slack_app, direct, text):
    app, client, submitted, _ = slack_app
    if direct:
        initial = dm_event(1, 'Keep working on the current task')
        assert client.post('/hooks/slack/events', **signed(initial)).status_code == 200
        run_id = submitted[0]['id']
        payload = dm_event(2, text, thread_ts=initial['event']['ts'])
    else:
        _, _, run_id = start(slack_app)
        payload = event('EvAside', type='message', text=text, ts='1790719001.123456', thread_ts=ROOT)
    store = app.state.store
    active = store.claim_message(run_id)
    store.update_run(run_id, status='running', token_hash='still-working')
    before = store.messages(run_id)
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert store.messages(run_id) == before
    run = store.run(run_id)
    assert run['status'] == 'running' and run['token_hash'] == 'still-working'
    assert run['active_message_id'] == active['id']
    assert not store.rows('SELECT 1 FROM slack_receipts WHERE event_id=?', (payload['event_id'],))
    assert not store.rows("SELECT 1 FROM slack_outbox WHERE kind='control'")
    assert store.rows('SELECT paused FROM slack_threads')[0]['paused'] == 0


def test_sleep_suppresses_replies_and_wake_resumes_same_saved_session(slack_app):
    app, client, run_id = start(slack_app)
    assert app.state.slack.chat.mirroring(run_id) == 'active'
    finish(app, run_id, 'First answer')
    send(client, 1, 'sleep')
    assert app.state.store.rows('SELECT paused FROM slack_threads')[0]['paused'] == 1
    assert app.state.slack.chat.mirroring(run_id) == 'paused'
    send(client, 2, 'This should be ignored while asleep')
    assert len(app.state.store.messages(run_id)) == 2
    # A late web answer must not be backfilled when the Slack thread wakes.
    app.state.store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant','late private answer','completed',?)", (run_id, now()))
    app.state.slack.chat.collect()
    send(client, 3, 'wake')
    send(client, 4, '<@U99999999> Continue from where we left off')
    assert app.state.store.rows('SELECT paused FROM slack_threads')[0]['paused'] == 0
    assert app.state.slack.chat.mirroring(run_id) == 'active'
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1
    assert 'Continue from where we left off' in app.state.store.messages(run_id)[-1]['content']
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE text LIKE '%late private answer%'")


def test_wake_before_collector_does_not_replay_sleeping_answers(slack_app):
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Earlier answer')
    send(client, 1, 'sleep')
    app.state.store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant','answer while asleep','completed',?)", (run_id, now()))
    send(client, 2, 'wake')
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE text LIKE '%answer while asleep%'")


def test_stop_revokes_active_capabilities_and_approvals_before_ack(slack_app):
    app, client, run_id = start(slack_app)
    app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='awaiting_approval', token_hash='capability')
    app.state.store.execute("INSERT INTO approvals VALUES('approval',?,'linear_comment','{}','pending',?,'')", (run_id, now()))
    send(client, 1, '<@U99999999> A queued followup')
    send(client, 2, 'stop')
    run = app.state.store.run(run_id)
    assert run['token_hash'] == '' and run['status'] in {'stopping', 'cancelled'}
    assert app.state.store.approvals(run_id)[0]['status'] == 'expired'
    assert app.state.store.messages(run_id)[-1]['status'] == 'cancelled'
    assert len(app.state.store.messages(run_id)) == 2


def test_slack_replies_during_a_turn_guide_it_in_order_without_send_now(slack_app):
    from app.message_queue import MessageQueue
    app, client, run_id = start(slack_app)
    store, q = app.state.store, MessageQueue(app.state.store)
    first = store.claim_message(run_id)['id']
    store.update_run(run_id, status='running')
    web, _ = store.enqueue_message(run_id, 'Web queue stays queued', 'web-pending', user_id=store.run(run_id)['active_user_id'])
    send(client, 1, '<@U99999999> Use opus instead')
    send(client, 2, '<@U99999999> And skip the docs')
    replies = [m['id'] for m in store.messages(run_id) if m['content'].startswith('Slack reply')]
    for reply in replies:
        packet = q.live_control(run_id, first, [])
        assert packet['input']['id'] == reply
        assert q.live_control(run_id, first, [reply])['steer_message_id'] is None
    assert q.live_control(run_id, first, []) == {'steer_message_id': None}
    status = {m['id']: m['status'] for m in store.messages(run_id)}
    assert [status[r] for r in replies] == ['injected', 'injected'] and status[web['id']] == 'queued'


def test_plain_yes_never_grants_external_write_approval(slack_app):
    app, client, run_id = start(slack_app)
    app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='awaiting_approval')
    app.state.store.execute("INSERT INTO approvals VALUES('approval',?,'linear_comment','{}','pending',?,'')", (run_id, now()))
    app.state.slack.chat.collect()
    send(client, 1, 'yes')
    assert app.state.store.approvals(run_id)[0]['status'] == 'pending'
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='approval'")
    assert len(rows) == 1 and 'web session' in rows[0]['text']
    app.state.slack.chat.collect()
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='approval'")) == 1


@pytest.mark.parametrize('saving', [False, True])
def test_no_historical_answer_backfill_and_legacy_requires_a_new_mention(slack_app, saving):
    app, client, _, _ = slack_app
    old = app.state.store.create_slack_run('EvOld', 'Old request', [], 'C12345678', ROOT, 'U12345678')
    app.state.store.execute("UPDATE slack_events SET reply_status='sent'")
    message = (receive if saving else finish)(app, old['id'], 'Old answer must stay in the app')
    send(client, 1, 'An unrelated reply in an old thread')
    assert not app.state.store.rows('SELECT * FROM slack_threads')
    send(client, 2, '<@U99999999> Continue the previous conversation')
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1
    if saving:
        app.state.store.finish_message(old['id'], message['id'], 'Old answer must stay in the app')
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE text LIKE '%Old answer%'")
    assert not app.state.store.rows("SELECT 1 FROM slack_outbox WHERE kind='answer_settlement'")
    assert 'Continue the previous conversation' in app.state.store.messages(old['id'])[-1]['content']


def test_queue_limit_has_one_visible_reply_and_no_dropped_duplicate_execution(slack_app):
    app, client, run_id = start(slack_app)
    for index in range(1, 6):
        send(client, index, f'<@U99999999> Followup {index}')
    assert len(app.state.store.messages(run_id)) == 5
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key='rejected:EvChat5'")
    assert len(rows) == 1 and '5 queued messages' in rows[0]['text']
    send(client, 5, '<@U99999999> Followup 5')
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key='rejected:EvChat5'")) == 1


def test_results_are_only_sent_to_original_thread_and_never_ping_users(slack_app):
    app, client, run_id = start(slack_app)
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE refreshed_at>0")))
    finish(app, run_id, 'Hello <!channel> <@U12345678>. The key is model-test-key.\n```python\nprint("hello")\n```')
    app.state.slack.chat.last_post.clear()
    client.portal.call(app.state.slack.chat.deliver_one)
    sent = slack_app[3][-1]
    assert sent['channel'] == 'C12345678' and sent['thread_ts'] == ROOT
    assert '<!channel>' not in sent['text'] and '<@U12345678>' not in sent['text']
    assert 'model-test-key' not in sent['text'] and '[redacted]' in sent['text']
    assert sent['parse'] == 'none' and sent['link_names'] is False
    assert sent['blocks'][0]['text']['verbatim'] is True
    assert sent['blocks'][0]['expand'] is True


def test_answers_render_mentions_only_for_users_already_mentioned_in_the_thread(slack_app):
    app, client, runs, sent = slack_app
    client.post('/hooks/slack/events', **signed(event('EvKudos', text='<@U99999999> big kudos to <@U0B1TDTHL4Q> on it')))
    run_id = runs[0]['id']
    app.state.store.execute('UPDATE slack_events SET context_json=? WHERE run_id=?',
                            (json.dumps({'messages': [{'text': 'thanks <@U0C59A1GHPD|bot-name>'}]}), run_id))
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE refreshed_at>0")))
    finish(app, run_id, 'Credit to <@U0B1TDTHL4Q> and <@U0C59A1GHPD>. Not <@U00000001>, <@U99999999>, <!channel> or `<@U0B1TDTHL4Q>`')
    app.state.slack.chat.last_post.clear()
    client.portal.call(app.state.slack.chat.deliver_one)
    text = sent[-1]['text']
    assert 'Credit to <@U0B1TDTHL4Q> and <@U0C59A1GHPD>.' in text
    assert 'Not &lt;@U00000001&gt;, &lt;@U99999999&gt;, &lt;!channel&gt; or `&lt;@U0B1TDTHL4Q&gt;`' in text
    assert sent[-1]['blocks'][0]['text']['text'].startswith('Credit to <@U0B1TDTHL4Q>')


def test_uncertain_outbox_delivery_survives_restart_without_resending(slack_app, monkeypatch):
    app, client, run_id = start(slack_app)
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE refreshed_at>0")))
    finish(app, run_id, 'An important answer')
    attempts = []
    async def fail(*args, **kwargs):
        attempts.append(1)
        raise TimeoutError('The provider response was lost')
    monkeypatch.setattr(app.state.connectors, 'request', fail)
    app.state.slack.chat.last_post.clear()
    client.portal.call(app.state.slack.chat.deliver_one)
    assert app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='answer'")[0]['status'] == 'uncertain'
    # A fresh database handle observes the same durable cursor and outbox.
    reopened = Store(app.state.settings.data_dir)
    assert len(reopened.rows("SELECT * FROM slack_outbox WHERE kind='answer'")) == 1
    app.state.slack.chat.collect()
    app.state.slack.recover()
    app.state.slack.chat.last_post.clear()
    client.portal.call(app.state.slack.chat.deliver_one)
    assert attempts == [1]


@pytest.mark.parametrize('outcome', ['sent', 'timeout', 'cancelled'])
def test_live_slack_delivery_remains_a_deletion_barrier_during_recovery(slack_app, monkeypatch, outcome):
    app, client, _, _ = slack_app
    chat, lifecycle, store = app.state.slack.chat, app.state.session_lifecycle, app.state.store
    client.portal.call(chat.shutdown)
    _, _, run_id = start(slack_app)
    finish(app, run_id, 'A delivery already in flight')
    original_request = app.state.connectors.request
    calls = []

    async def verify():
        started, release = asyncio.Event(), asyncio.Event()
        active = False

        async def request(method, url, **kwargs):
            nonlocal active
            if not url.endswith('/chat.postMessage'):
                return await original_request(method, url, **kwargs)
            calls.append(kwargs['json'])
            active = True
            started.set()
            try:
                await release.wait()
                if outcome == 'timeout':
                    raise TimeoutError('Slack response was lost')
                return await original_request(method, url, **kwargs)
            finally:
                active = False

        monkeypatch.setattr(app.state.connectors, 'request', request)
        delivery = asyncio.create_task(chat.deliver_one())
        try:
            await started.wait()
            chat.recover()
            await chat.deliver_one()  # A second poll must preserve the live owner.
            assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer'") == [{'status': 'sending'}]
            lifecycle.request_delete(run_id, '', True)
            deletion = lifecycle.schedule_delete(run_id)
            await asyncio.sleep(0.05)
            assert active and not delivery.done()
            assert store.run(run_id)['deleted_at'] == ''
            if outcome == 'cancelled':
                delivery.cancel()
            else:
                release.set()
            await asyncio.gather(delivery, return_exceptions=True)
            assert not active
            assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer'") == [
                {'status': 'sent' if outcome == 'sent' else 'uncertain'}]
            await asyncio.wait_for(asyncio.shield(deletion), timeout=3)
            assert store.run(run_id)['deleted_at']
            assert len(calls) == 1
        finally:
            release.set()
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
            await chat.shutdown()
            await lifecycle.close()

    client.portal.call(verify)


def test_orphaned_slack_receipt_recovers_in_process_and_allows_deletion_without_resending(slack_app, monkeypatch):
    app, client, _, _ = slack_app
    chat, lifecycle, store = app.state.slack.chat, app.state.session_lifecycle, app.state.store
    client.portal.call(chat.shutdown)
    _, _, run_id = start(slack_app)
    finish(app, run_id, 'An answer with an ambiguous delivery')
    original_request, original_execute = app.state.connectors.request, store.execute
    attempts = []

    async def request(method, url, **kwargs):
        if not url.endswith('/chat.postMessage'):
            return await original_request(method, url, **kwargs)
        attempts.append(kwargs['json'])
        raise TimeoutError('Slack response was lost')

    def failed_receipt(sql, params=()):
        if "UPDATE slack_outbox SET status='uncertain' WHERE id=?" in sql:
            raise sqlite3.OperationalError('Temporary receipt write failure')
        return original_execute(sql, params)

    async def verify():
        monkeypatch.setattr(app.state.connectors, 'request', request)
        monkeypatch.setattr(store, 'execute', failed_receipt)
        try:
            with pytest.raises(sqlite3.OperationalError, match='receipt write failure'):
                await chat.deliver_one()
            assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer'") == [{'status': 'sending'}]
            monkeypatch.setattr(store, 'execute', original_execute)
            lifecycle.request_delete(run_id, '', True)
            deletion = lifecycle.schedule_delete(run_id)
            await asyncio.sleep(0.05)
            assert store.run(run_id)['deleted_at'] == ''
            await chat.deliver_one()
            assert store.rows("SELECT status FROM slack_outbox WHERE kind='answer'") == [{'status': 'uncertain'}]
            await asyncio.wait_for(asyncio.shield(deletion), timeout=3)
            assert store.run(run_id)['deleted_at']
            assert len(attempts) == 1
        finally:
            monkeypatch.setattr(store, 'execute', original_execute)
            await lifecycle.close()

    client.portal.call(verify)


def test_pending_deletion_with_sending_receipt_recovers_on_actual_app_restart(tmp_path):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787',
                        litellm_api_key='', modal_token_id='', modal_token_secret='',
                        slack_bot_enabled=False, session_titles_enabled=False)
    original = create_app(settings)
    store = original.state.store
    run_id = store.create_run('Retain an uncertain Slack outcome', '', 'demo', [], chat_enabled=True)['id']
    store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (run_id,))
    store.update_run(run_id, status='idle')
    store.execute("INSERT INTO slack_outbox(run_id,dedupe_key,kind,text,status,created_at) VALUES(?,'restart-proof','answer','Retained answer','sending',?)", (run_id, now()))
    original.state.session_lifecycle.request_delete(run_id, '', True)
    store.close()
    recovered = create_app(settings)
    try:
        with TestClient(recovered, base_url=settings.public_url, client=('127.0.0.1', 50000)):
            wait_for(lambda: recovered.state.store.run(run_id)['deleted_at'])
            assert recovered.state.slack.status()['enabled'] is False
            assert recovered.state.store.rows('SELECT status FROM slack_outbox') == [{'status': 'uncertain'}]
    finally:
        store.objects.close()


def test_paused_connection_discards_pending_replies_and_cannot_send_to_new_team(slack_app):
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Do not leak this')
    app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE status='pending'")
    assert app.state.slack.chat.mirroring(run_id) is None
    send(client, 1, 'No new work')
    assert len(app.state.store.messages(run_id)) == 2


def test_long_markdown_replies_have_bounded_balanced_code_blocks():
    source = '## Example\n' + '```python\n' + 'print("hello")\n' * 900 + '```\n[Docs](https://example.com)'
    parts = split_reply(slack_text(source))
    assert len(parts) > 2
    assert all(len(part) <= 2610 for part in parts)
    assert all(part.count('```') % 2 == 0 for part in parts)
    assert '<https://example.com|Docs>' in parts[-1]


def dm_event(index, text, channel='D12345678', user='U12345678', **overrides):
    return event(f'EvDM{index}', type='message', channel_type='im', channel=channel,
                 user=user, text=text, ts=f'17907290{index:02d}.123456', **overrides)


def test_agentchat_dm_roots_are_independent_and_followups_keep_thread_context(slack_app, monkeypatch):
    from app.agentchat_slack import SessionState
    app, client, submitted, sent = slack_app
    conversations = []
    receive = app.state.slack.channel._receiver

    async def observed(channel, message) -> None:
        conversations.append(message.conversation_id)
        await receive(channel, message)

    monkeypatch.setattr(app.state.slack.channel, '_receiver', observed)
    first = dm_event(1, 'Remember granite and read <@U87654321> as source text')
    root = first['event']['ts']
    client.post('/hooks/slack/events', **signed(first))
    run_id = submitted[0]['id']
    finish(app, run_id, 'Remembering granite.')
    client.post('/hooks/slack/events', **signed(dm_event(2, 'model opus', thread_ts=root)))
    second = dm_event(3, 'What was the phrase?', thread_ts=root)
    client.post('/hooks/slack/events', **signed(second))
    second['event_id'] = 'EvDMDuplicate'
    client.post('/hooks/slack/events', **signed(second))
    fresh = dm_event(4, 'A separate task')
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: client.post('/hooks/slack/events', **signed(fresh)).status_code, range(2)))
    assert statuses == [200, 200]
    bindings = app.state.store.rows('SELECT * FROM slack_threads ORDER BY thread_ts')
    assert len(bindings) == 2
    fresh_id = bindings[1]['run_id']
    assert fresh_id != run_id and bindings[1]['thread_ts'] == fresh['event']['ts']
    messages = app.state.store.messages(run_id)
    assert len(messages) == 3 and '<@U87654321>' in messages[0]['content']
    assert messages[-1]['model'] == 'anthropic/claude-opus-5-5'
    assert messages[-1]['user_id'] == 'slack:T12345678:U12345678'
    assert app.state.store.run(run_id)['owner_id'] == messages[-1]['user_id']
    isolated = app.state.store.messages(fresh_id)
    assert len(isolated) == 1 and isolated[0]['content'] == 'A separate task'
    # New threads inherit the person's saved model, without sharing history
    # or rewriting messages that were accepted before the preference changed.
    assert isolated[0]['model'] == 'anthropic/claude-opus-5-5'
    assert messages[0]['model'] == app.state.settings.resolve_model()
    assert isolated[0]['id'] not in {message['id'] for message in messages}
    assert set(conversations) == {f'slack:T12345678:D12345678:{r}' for r in (root, fresh['event']['ts'])}
    # Reconstruct the adapter against the persisted bindings; no channel-wide fallback.
    state = SessionState(Store(app.state.settings.data_dir))
    for binding in bindings:
        source = app.state.slack.channel.source_for_run(binding['run_id'])
        assert source.conversation_id == 'slack:T12345678:D12345678:' + binding['thread_ts']
        history = asyncio.run(state.history(source.conversation_id))
        assert [m.text for m in history] == [m['content'] for m in app.state.store.messages(binding['run_id'])]
        assert asyncio.run(state.history(source.conversation_id, limit=0)) == ()
    assert asyncio.run(state.history('slack:T12345678:D12345678')) == ()
    wait_for(lambda: any('text' in item for item in sent))
    replies = [item for item in sent if 'text' in item]
    assert replies and all(msg['channel'] == 'D12345678' and msg['thread_ts'] == root for msg in replies)
    assert 'signed-in BerriAI teammates can view it' in replies[0]['text']


def test_dm_commands_only_control_their_thread(slack_app):
    app, client, submitted, _ = slack_app
    first = dm_event(1, 'First task')
    client.post('/hooks/slack/events', **signed(first))
    first_id = submitted[-1]['id']
    client.post('/hooks/slack/events', **signed(dm_event(2, 'Second task')))
    second_id = submitted[-1]['id']
    assert first_id != second_id
    for index, command in enumerate(['stop', 'sleep', 'wake', 'status'], 3):
        client.post('/hooks/slack/events', **signed(dm_event(index, command)))
    assert all(app.state.store.run(r)['status'] == 'queued' for r in [first_id, second_id])
    client.post('/hooks/slack/events', **signed(dm_event(7, 'sleep', thread_ts=first['event']['ts'])))
    assert app.state.store.rows('SELECT paused FROM slack_threads WHERE run_id=?', (first_id,))[0]['paused'] == 1
    assert app.state.store.run(second_id)['status'] == 'queued'
    client.post('/hooks/slack/events', **signed(dm_event(8, 'model opus')))
    client.post('/hooks/slack/events', **signed(dm_event(9, 'New default model task')))
    assert submitted[-1]['model'] == 'anthropic/claude-opus-5-5'
    # A model command may set a preference for future sessions, but must not
    # rewrite either existing thread's model.
    assert all(app.state.store.run(r)['model'] == app.state.settings.resolve_model()
               for r in (first_id, second_id))


def test_dm_users_and_channel_threads_cannot_share_a_session(slack_app):
    app, client, submitted, _ = slack_app
    first = dm_event(1, 'First user')
    client.post('/hooks/slack/events', **signed(first))
    client.post('/hooks/slack/events', **signed(dm_event(2, 'Second user',channel='D87654321',user='U87654321')))
    client.post('/hooks/slack/events', **signed(dm_event(3, 'Different sender in original DM',user='U87654321')))
    client.post('/hooks/slack/events', **signed(event()))
    assert len(app.state.store.rows('SELECT * FROM runs')) == 3
    assert len(app.state.store.messages(submitted[0]['id'])) == 1
    assert {r['owner_id'] for r in submitted} == {'slack:T12345678:U12345678','slack:T12345678:U87654321'}


def test_disabled_dm_mode_and_non_dm_events_cannot_start_a_direct_session(slack_app):
    app, client, submitted, _ = slack_app
    app.state.settings.slack_dm_enabled = False
    client.post('/hooks/slack/events', **signed(dm_event(1,'Do something')))
    app.state.settings.slack_dm_enabled = True
    payload = dm_event(2,'<@U99999999> Do something');payload['event']['channel_type']='mpim'
    client.post('/hooks/slack/events', **signed(payload))
    app.state.settings.slack_thread_chat_enabled = False
    client.post('/hooks/slack/events', **signed(dm_event(3,'Do something')))
    assert not submitted


def test_agentchat_temporary_handler_failure_does_not_consume_retry(slack_app):
    app, client, submitted, _ = slack_app
    payload = dm_event(1,'Retry after temporary cloud configuration failure')
    saved = app.state.settings.litellm_api_key
    app.state.settings.litellm_api_key = ''
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 503
    assert not app.state.store.rows('SELECT * FROM slack_receipts')
    app.state.settings.litellm_api_key = saved
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(submitted) == 1
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert len(submitted) == 1


def test_dm_context_starts_from_current_request_without_shared_account_history(slack_app, monkeypatch):
    app, client, submitted, sent = slack_app
    client.post('/hooks/slack/events', **signed(dm_event(1,'My direct request')))
    wait_for(lambda: bool(sent))
    async def unexpected(*args, **kwargs):
        raise AssertionError('A DM must not import history with the shared search credential')
    monkeypatch.setattr(app.state.connectors,'request',unexpected)
    run_id = submitted[0]['id']
    asyncio.run(app.state.slack.prepare(run_id))
    source = app.state.store.slack_source(run_id)
    assert source['context_status'] == 'ready'
    assert source['kind'] == 'dm'
    assert [m['text'] for m in source['messages']] == ['My direct request']


def test_agentchat_history_and_receipts_survive_adapter_restart(slack_app):
    from app.agentchat_slack import SessionState
    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Persistent answer')
    state = SessionState(Store(app.state.settings.data_dir))
    assert asyncio.run(state.claim('slack:T12345678:EvTest1')) is False
    history = asyncio.run(state.history('slack:T12345678:C12345678:'+ROOT))
    assert history[-1].text == 'Persistent answer'
    assert asyncio.run(state.history('slack:TOTHER:C12345678:'+ROOT)) == ()


def test_slack_oauth_requests_bot_dm_and_thread_scopes(slack_app):
    from urllib.parse import urlparse, parse_qs
    app, _, _, _ = slack_app
    app.state.settings.slack_client_id = 'test-client'
    scope = parse_qs(urlparse(app.state.connectors.authorization_url('slack','state')).query)['scope'][0]
    assert set(scope.split(',')) == {'app_mentions:read','chat:write','im:write','channels:history','groups:history','im:history','reactions:write','assistant:write','files:write','files:read','users:read','users:read.email'}


def test_acknowledgment_is_native_status_without_periodic_chatter(slack_app):
    app, client, run_id = start(slack_app)
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE refreshed_at>0")))
    client.post('/hooks/slack/events', **signed(event('EvDuplicateType', type='message')))
    app.state.store.execute('UPDATE slack_threads SET last_progress=0')
    app.state.slack.chat.collect()
    assert len(slack_app[3]) == 1
    assert slack_app[3][0]['status'] == 'is getting ready…'
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE kind IN ('ack','progress','control')")
    finish(app, run_id, 'Here is the answer.')
    app.state.slack.chat.last_post.clear()
    client.portal.call(app.state.slack.chat.deliver_one)
    assert slack_app[3][-1]['text'].startswith('Here is the answer.')


def test_status_failure_does_not_prevent_the_answer(slack_app, monkeypatch):
    app, client, _, sent = slack_app
    real = app.state.connectors.request
    async def reject_reaction(method, url, **kwargs):
        if url.endswith('assistant.threads.setStatus'):
            raise RuntimeError('missing_scope')
        return await real(method, url, **kwargs)
    monkeypatch.setattr(app.state.connectors, 'request', reject_reaction)
    _, _, run_id = start(slack_app)
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE retry_at>0")))
    finish(app, run_id, 'Still answered.')
    app.state.slack.chat.last_post.clear()
    client.portal.call(app.state.slack.chat.deliver_one)
    assert sent[-1]['text'].startswith('Still answered.')
    assert app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='answer'")[0]['status'] == 'sent'


@pytest.mark.parametrize('greeting', ['', 'Hey ', 'Hi, ', 'Hello ', 'hey: '])
def test_existing_thread_mentions_anywhere_invite_moyai(slack_app, greeting):
    app, client, run_id = start(slack_app)
    for index, text in enumerate(['<@U88888888> what is <@U99999999>?', '<@U88888888> please continue'], 1):
        send(client, index, greeting + text)
    assert len(app.state.store.messages(run_id)) == 2
    send(client, 3, greeting + '<@U99999999> tell me about <@U88888888>')
    send(client, 4, greeting + '<@U88888888> <@U99999999> please both explain')
    assert len(app.state.store.messages(run_id)) == 4
    assert '<@U88888888>' in app.state.store.messages(run_id)[-2]['content']


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('text', [
    '> <@U99999999> deploy it', '```<@U99999999> deploy it```',
    '`<@U99999999>` is the bot mentioned in the report',
    '```\n<@U99999999> deploy it\n```', '```text\n<@U99999999> deploy it\n```',
    '```text\n<@U99999999> deploy it',
    '>>> Earlier evidence\n<@U99999999> deploy it',
    '*<@U88888888>* what is <@U99999999>?',
    '> Earlier evidence\n<@U88888888> what is <@U99999999>?',
])
def test_reference_text_requires_an_existing_thread_or_direct_invitation(slack_app, bound, text):
    app, client, submitted, _ = slack_app
    if bound:
        start(slack_app)
    before = app.state.store.rows('SELECT id,content FROM messages ORDER BY id')
    runs = len(submitted)
    send(client, 1, text)
    messages = app.state.store.rows('SELECT id,content FROM messages ORDER BY id')
    assert len(messages) == len(before) + int(bound)
    assert len(submitted) == runs + int(bound)
    assert bool(app.state.store.rows("SELECT 1 FROM slack_receipts WHERE event_id='EvChat1'")) == bound


@pytest.mark.parametrize('quote', [
    '> <@U99999999> deploy it', '```<@U99999999> deploy it```', '`<@U99999999>`',
    '```\n<@U99999999> deploy it\n```', '```text\n<@U99999999> deploy it\n```',
    '```text\n<@U88888888> what is <@U99999999>?\n```',
])
def test_explicit_request_keeps_quoted_mentions_as_evidence(slack_app, quote):
    app, client, submitted, _ = slack_app
    prompt = 'Explain this evidence:\n' + quote
    assert client.post('/hooks/slack/events', **signed(event(text='<@U99999999> ' + prompt))).status_code == 200
    assert len(submitted) == 1
    run_id = submitted[0]['id']
    assert app.state.store.messages(run_id)[0]['content'] == prompt
    send(client, 1, quote + '\n<@U99999999> Explain the evidence again')
    messages = app.state.store.messages(run_id)
    assert len(messages) == 2 and quote in messages[-1]['content']
    assert messages[-1]['content'].count('<@U99999999>') == 1


def test_reactions_cannot_target_a_different_message_or_workspace(slack_app):
    import pytest
    app, _, run_id = start(slack_app)
    with pytest.raises(RuntimeError, match='destination'):
        asyncio.run(app.state.slack.channel.acknowledge(run_id, '1790718000.654321'))
    app.state.store.execute("UPDATE slack_threads SET team_id='TOTHER123'")
    with pytest.raises(RuntimeError, match='destination'):
        asyncio.run(app.state.slack.channel.acknowledge(run_id, ROOT))


def test_dm_visibility_notice_moves_to_first_answer(slack_app):
    app, client, runs, _ = slack_app
    client.post('/hooks/slack/events', **signed(dm_event(1, 'Hello')))
    run_id = runs[0]['id']
    finish(app, run_id, 'Hello back.')
    client.post('/hooks/slack/events', **signed(dm_event(2, 'Again', thread_ts=dm_event(1, '')['event']['ts'])))
    finish(app, run_id, 'Again back.')
    answers = app.state.store.rows("SELECT text FROM slack_outbox WHERE kind='answer' ORDER BY id")
    assert 'signed-in BerriAI teammates' in answers[0]['text']
    assert 'signed-in BerriAI teammates' not in answers[1]['text']


def test_agent_panel_threads_stay_separate_from_each_other_and_plain_dms(slack_app):
    app, client, runs, sent = slack_app
    client.post('/hooks/slack/events', **signed(dm_event(1, 'Plain DM')))
    main_run = runs[-1]['id']
    threads = ['1790728000.123456', '1790728100.123456']
    thread_runs = []
    for index, root in enumerate(threads, 2):
        payload = dm_event(index, 'Thread '+str(index))
        payload['event']['thread_ts'] = root
        client.post('/hooks/slack/events', **signed(payload))
        thread_runs.append(runs[-1]['id'])
    assert len(set([main_run, *thread_runs])) == 3
    followup = dm_event(4, 'Continue the first agent panel thread')
    followup['event']['thread_ts'] = threads[0]
    client.post('/hooks/slack/events', **signed(followup))
    client.post('/hooks/slack/events', **signed(dm_event(5, 'A new DM session')))
    assert len(app.state.store.messages(main_run)) == 1
    assert len(app.state.store.rows('SELECT * FROM runs')) == 4
    assert len(app.state.store.messages(thread_runs[0])) == 2
    assert len(app.state.store.messages(thread_runs[1])) == 1
    channel = app.state.slack.channel
    for run_id, root in zip(thread_runs, threads):
        source = channel.source_for_run(run_id)
        assert source.conversation_id.endswith(':'+root)
        asyncio.run(channel.reply(source, 'Panel answer'))
        assert sent[-1]['thread_ts'] == root


@pytest.fixture
def media_delivery(slack_app, monkeypatch):
    from app import captures
    from agentchat.channels import slack_media
    app, client, runs, sent = slack_app
    app.state.connectors.save('slack', {'access_token': 'provider-user-secret', 'bot': {
        'access_token': 'separate-bot-secret', 'team': {'id': 'T12345678'},
        'bot_user_id': 'U99999999', 'scope': 'reactions:write,files:write'}}, 'Test organization')
    client.portal.call(app.state.slack.chat.shutdown)
    calls, uploaded = [], []

    async def request(method: str, url: str, **kwargs) -> dict:
        assert kwargs['headers']['Authorization'] == 'Bearer separate-bot-secret'
        calls.append((url.rsplit('/', 1)[-1], kwargs))
        if url.endswith('getUploadURLExternal'):
            assert method == 'POST'
            name = kwargs['data']['filename']
            return {'ok': True, 'file_id': name, 'upload_url': 'https://files.slack.com/upload/' + name}
        if url.endswith('completeUploadExternal'):
            return {'ok': True, 'files': kwargs['json']['files']}
        sent.append(kwargs['json'])
        return {'ok': True, 'ts': '1790719999.123456'}

    async def upload(url: str, raw: bytes) -> None:
        uploaded.append((url, raw))

    monkeypatch.setattr(app.state.connectors, 'request', request)
    monkeypatch.setattr(slack_media, 'upload_bytes', upload)

    def prepare(dm: bool = False) -> tuple:
        payload = dm_event(20, 'Finish a coding PR') if dm else event()
        assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
        run_id = runs[0]['id']
        app.state.store.execute("UPDATE slack_outbox SET status='skipped'")
        root = captures.directory(app.state.settings, run_id)
        root.mkdir(parents=True)
        (root / 'demo.webm').write_bytes(b'\x1aE\xdf\xa3webm-demo')
        (root / 'result.png').write_bytes(b'\x89PNG\r\n\x1a\nresult')
        url = publication(app, run_id)
        answer = f'[PR]({url}) [Video](/workspace/moyai-captures/demo.webm) '
        answer += '![Result](/workspace/moyai-captures/result.png)'
        return run_id, answer

    return app, prepare, calls, uploaded


def drain_answers(app) -> None:
    while app.state.store.rows("SELECT 1 FROM slack_outbox WHERE status='pending'"):
        app.state.slack.chat.last_post.clear()
        asyncio.run(app.state.slack.chat.deliver_one())


@pytest.mark.parametrize('dm', [False, True])
def test_answer_and_card_precede_native_media_batch_and_preserve_dm_notice(media_delivery, dm):
    app, prepare, calls, uploaded = media_delivery
    run_id, answer = prepare(dm)
    finish(app, run_id, ('Verified behavior. ' * 200) + answer)
    drain_answers(app)
    completions = [kwargs['json'] for method, kwargs in calls if method == 'files.completeUploadExternal']
    assert len(completions) == 1
    assert [f['id'] for f in completions[0]['files']] == ['demo.webm', 'result.png']
    assert completions[0]['channel_id'].startswith('D' if dm else 'C')
    assert completions[0].get('thread_ts') == (dm_event(20, '')['event']['ts'] if dm else ROOT)
    assert [raw for _, raw in uploaded] == [b'\x1aE\xdf\xa3webm-demo', b'\x89PNG\r\n\x1a\nresult']
    replies = [kwargs['json'] for method, kwargs in calls if method == 'chat.postMessage']
    assert len(replies) == 2 and not replies[0].get('attachments') and replies[1]['attachments']
    methods = [method for method, _ in calls]
    assert max(i for i, method in enumerate(methods) if method == 'chat.postMessage') < methods.index('files.getUploadURLExternal')
    assert all(reply['thread_ts'] == completions[0]['thread_ts'] for reply in replies)
    for reply in replies:
        sections = [block for block in reply['blocks'] if block['type'] == 'section']
        assert sections and all(block['expand'] is True for block in sections)
        assert all(len(block['text']['text']) <= 3000 for block in sections)
    assert replies[-1]['blocks'][-1]['type'] == 'context'
    assert f'#run={run_id}' in replies[-1]['blocks'][-1]['elements'][0]['text']
    assert ('signed-in BerriAI teammates' in replies[-1]['text']) == dm
    assert all(row['status'] == 'sent' for row in app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='answer'"))
    prior = len(calls)
    app.state.slack.chat.collect()
    drain_answers(app)
    assert len(calls) == prior


@pytest.mark.parametrize('boundary', ['refresh', 'url', 'bytes'])
@pytest.mark.parametrize('change', ['pause', 'team', 'bot'])
def test_media_rechecks_destination_after_awaits(media_delivery, monkeypatch, boundary, change):
    from agentchat.channels import slack_media
    app, prepare, calls, uploaded = media_delivery
    run_id, answer = prepare()
    finish(app, run_id, answer)
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one(media=False))

    def revoke() -> None:
        if change == 'pause':
            app.state.store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
        else:
            app.state.connectors.save('slack', {'bot': {'access_token': 'replacement',
                'team': {'id': 'T87654321' if change == 'team' else 'T12345678'},
                'bot_user_id': 'U00000000' if change == 'bot' else 'U99999999', 'scope': 'files:write'}}, 'Changed')

    original_token = app.state.connectors.slack_bot_token
    original_request = app.state.connectors.request
    original_upload = slack_media.upload_bytes

    async def refresh() -> str:
        token = await original_token()
        if boundary == 'refresh':
            revoke()
        return token

    async def request(method: str, url: str, **kwargs) -> dict:
        result = await original_request(method, url, **kwargs)
        if boundary == 'url' and url.endswith('getUploadURLExternal'):
            revoke()
        return result

    async def upload(url: str, raw: bytes) -> None:
        await original_upload(url, raw)
        if boundary == 'bytes':
            revoke()

    monkeypatch.setattr(app.state.connectors, 'slack_bot_token', refresh)
    monkeypatch.setattr(app.state.connectors, 'request', request)
    monkeypatch.setattr(slack_media, 'upload_bytes', upload)
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one(media=True))
    assert not any(method == 'files.completeUploadExternal' for method, _ in calls)
    assert len(uploaded) == (1 if boundary == 'bytes' else 0)
    media = app.state.store.rows("SELECT status FROM slack_outbox WHERE dedupe_key LIKE '%:media'")
    assert media[0]['status'] == 'uncertain'


def test_media_lane_does_not_block_new_answers_and_preserves_live_send_owner(media_delivery, slack_app, monkeypatch):
    from agentchat.channels import slack_media
    app, prepare, calls, uploaded = media_delivery
    client, chat, store = slack_app[1], app.state.slack.chat, app.state.store
    run_id, answer = prepare()
    first = receive(app, run_id, answer)
    assert not store.rows("SELECT 1 FROM slack_outbox WHERE dedupe_key LIKE '%:media'")
    drain_answers(app)
    store.finish_message(run_id, first['id'], answer)
    chat.collect()
    original_upload = slack_media.upload_bytes

    async def verify():
        started, release = asyncio.Event(), asyncio.Event()

        async def upload(url: str, raw: bytes) -> None:
            started.set()
            await release.wait()
            await original_upload(url, raw)

        monkeypatch.setattr(slack_media, 'upload_bytes', upload)
        chat.last_post.clear()
        chat.recover()
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            chat.reconcile_sending()
            assert store.rows("SELECT status FROM slack_outbox WHERE dedupe_key LIKE '%:media'") == [{'status': 'sending'}]
            assert chat.last_post['C12345678'] > time.monotonic() - 1.1
            store.enqueue_message(run_id, 'Another question', 'next-while-uploading')
            receive(app, run_id, 'A new answer while the earlier upload is blocked.')
            chat.wake.set()
            async with asyncio.timeout(4):
                while not any('A new answer while' in kwargs.get('json', {}).get('text', '')
                              for method, kwargs in calls if method == 'chat.postMessage'):
                    await asyncio.sleep(.02)
            assert not release.is_set() and not uploaded
            assert store.rows("SELECT status FROM slack_outbox WHERE dedupe_key LIKE '%:media'") == [{'status': 'sending'}]
            release.set()
            async with asyncio.timeout(3):
                while store.rows("SELECT 1 FROM slack_outbox WHERE dedupe_key LIKE '%:media' AND status='sending'"):
                    await asyncio.sleep(.02)
        finally:
            release.set()
            await chat.shutdown()
        assert chat.watcher.cancelled() and chat.media_watcher.cancelled()

    client.portal.call(verify)
    assert len(uploaded) == 2
    assert sum(method == 'files.completeUploadExternal' for method, _ in calls) == 1


def test_lost_media_completion_is_not_replayed_and_card_still_delivers(media_delivery, slack_app, monkeypatch):
    app, prepare, calls, uploaded = media_delivery
    client = slack_app[1]
    app_loop = app.state.slack.chat.watcher.get_loop()
    run_id, answer = prepare()
    finish(app, run_id, answer)
    original = app.state.connectors.request

    async def request(method: str, url: str, **kwargs) -> dict:
        result = await original(method, url, **kwargs)
        if url.endswith('completeUploadExternal'):
            raise TimeoutError('Provider acknowledgment lost')
        return result

    monkeypatch.setattr(app.state.connectors, 'request', request)
    drain_answers(app)
    reopened = Store(app.state.settings.data_dir)
    assert reopened.rows("SELECT status FROM slack_outbox WHERE dedupe_key LIKE '%:media'")[0]['status'] == 'uncertain'
    assert any(kwargs['json'].get('attachments') for method, kwargs in calls if method == 'chat.postMessage')
    async def restart() -> None:
        app.state.slack.recover()
        watcher = app.state.slack.chat.watcher
        assert watcher.get_loop() is app_loop
        await app.state.slack.chat.shutdown()
        assert watcher.cancelled()
    client.portal.call(restart)
    app.state.slack.chat.collect()
    drain_answers(app)
    assert sum(method == 'files.completeUploadExternal' for method, _ in calls) == 1
    assert len(uploaded) == 2


def test_cancelled_final_does_not_start_media_delivery(media_delivery):
    app, prepare, calls, uploaded = media_delivery
    run_id, answer = prepare()
    message = app.state.store.claim_message(run_id)
    app.state.store.finish_message(run_id, message['id'], answer, status='cancelled')
    app.state.slack.chat.collect()
    drain_answers(app)
    assert not uploaded and not any(method.startswith('files.') for method, _ in calls)


@pytest.mark.parametrize('deferred', [False, True])
def test_explicit_capture_followup_delivers_without_a_new_pr(media_delivery, deferred):
    from app import captures
    app, prepare, calls, uploaded = media_delivery
    run_id, _ = prepare()
    answer = ('[Video](/workspace/moyai-captures/demo.webm) '
              '![Screenshot](/workspace/moyai-captures/result.png)')
    if deferred:
        root = captures.directory(app.state.settings, run_id)
        files = {path.name: path.read_bytes() for path in root.iterdir()}
        for path in root.iterdir():
            path.unlink()
        message = receive(app, run_id, answer)
        drain_answers(app)
        assert not uploaded and not any(method.startswith('files.') for method, _ in calls)
        for name, raw in files.items():
            app.state.store.artifacts.save(run_id + '-captures/' + name, raw)
        app.state.store.finish_message(run_id, message['id'], answer)
        app.state.slack.chat.collect()
        app.state.slack.chat.collect()
    else:
        finish(app, run_id, answer)
    drain_answers(app)
    assert len(uploaded) == 2
    replies = [kwargs['json'] for method, kwargs in calls if method == 'chat.postMessage']
    assert len(replies) == 1 and not replies[0].get('attachments')
    assert 'moyai-captures/' not in replies[0]['text']
    methods = [method for method, _ in calls]
    assert methods.index('chat.postMessage') < methods.index('files.getUploadURLExternal')
    if deferred:
        assert f'<https://workspace.example/#run={run_id}|Video>' in replies[0]['text']


def test_deleted_slack_binding_cannot_continue_or_be_readopted(slack_app):
    from test_spend import sign_in

    app, client, run_id = start(slack_app)
    finish(app, run_id, 'Saved result')
    store = app.state.store
    sign_in(app, client, 'alice', 'alice@berri.ai')
    source = store.slack_source(run_id)
    before = store.messages(run_id)
    assert client.delete('/api/runs/' + run_id).status_code == 200
    assert not store.rows("SELECT 1 FROM slack_outbox WHERE run_id=? AND status='pending'", (run_id,))
    for index, prompt in enumerate(('Follow up normally', '<@U99999999> Explicit new mention', '/wake', '/model openai/gpt-6-astra'), 71):
        payload = event(f'Deleted{index}', type='message', ts=f'17907199{index:02d}.123456', thread_ts=source['thread_ts'], text=prompt)
        assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert store.messages(run_id) == before
    assert len(store.rows('SELECT id FROM runs')) == 1
    # Legacy adoption still finds the retained deleted row, then refuses it.
    store.execute('DELETE FROM slack_threads WHERE run_id=?', (run_id,))
    payload = event('DeletedLegacy', type='message', ts='1790720999.123456', thread_ts=source['thread_ts'], text='<@U99999999> Re-adopt this thread')
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert store.messages(run_id) == before
    assert len(store.rows('SELECT id FROM runs')) == 1


def test_answer_delivers_native_table_in_thread_with_session_link(slack_app):
    app, _, run_id = start(slack_app)
    finish(app, run_id, '[Measurements](/workspace/report.md)\n\n| Phase | Time |\n| --- | ---: |\n| ASGI entry | 3.32 |')
    # Each outbox message is rate limited; wait for the prose before the table.
    wait_for(lambda: any('Measurements' in message.get('text', '') for message in slack_app[3]))
    wait_for(lambda: any(any(b['type'] == 'table' for b in message.get('blocks', []))
                         for message in slack_app[3]))
    prose = next(message for message in slack_app[3] if 'Measurements' in message.get('text', ''))
    assert f'#run={run_id}&amp;file=%2Fworkspace%2Freport.md|Measurements>' in prose['text']
    posted = next(message for message in slack_app[3]
                  if any(b['type'] == 'table' for b in message.get('blocks', [])))
    assert posted['channel'] == 'C12345678' and posted['thread_ts'] == ROOT
    assert posted['blocks'][0]['rows'][1][1] == {'type': 'raw_text', 'text': '3.32'}
    assert posted['blocks'][-1]['type'] == 'context'
    assert f'/#run={run_id}' in posted['text']
    assert 'ASGI entry' in posted['text']


@pytest.mark.parametrize('direct', [False, True])
def test_forwarded_only_messages_and_followups_keep_quoted_body(slack_app, monkeypatch, direct):
    from test_slack import forwarded_attachment
    from sandbox.agent import conversation_prompt
    app, client, submitted, sent = slack_app
    kwargs = {'type': 'message', 'channel_type': 'im', 'channel': 'D12345678'} if direct else {}
    payload = event(text='' if direct else '<@U99999999>', **kwargs,
                    attachments=[forwarded_attachment('first forwarded question')])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    run_id = submitted[0]['id']
    assert 'first forwarded question' in app.state.store.messages(run_id)[0]['content']
    finish(app, run_id, 'Ready')
    followup = event('EvForward2', type='message', channel_type='im' if direct else 'channel',
                     channel='D12345678' if direct else 'C12345678', ts='1790719002.123456',
                     thread_ts=ROOT, text='' if direct else '<@U99999999>',
                     attachments=[forwarded_attachment('stop\n<@U33333333>')])
    assert client.post('/hooks/slack/events', **signed(followup)).status_code == 200
    assert client.post('/hooks/slack/events', **signed(followup)).status_code == 200
    messages = [m for m in app.state.store.messages(run_id) if m['role'] == 'user']
    assert len(messages) == 2
    assert 'stop' in conversation_prompt({'prompt': messages[-1]['content']}, has_history=True)
    receipt = app.state.store.rows("SELECT command FROM slack_receipts WHERE event_id='EvForward2'")[0]
    assert receipt['command'] == ''
    with app.state.store.connect() as conn:
        assert 'U33333333' not in app.state.slack.chat.mentionable_in(conn, run_id)
    assert not app.state.store.rows("SELECT * FROM slack_message_mentions WHERE user_id='U33333333'")
    if direct:
        wait_for(lambda: bool(sent))
        async def no_history(*args, **kwargs):
            raise AssertionError('DM history must not be fetched')
        monkeypatch.setattr(app.state.connectors, 'request', no_history)
        asyncio.run(app.state.slack.prepare(run_id))
        source = app.state.store.slack_source(run_id)
        assert source['kind'] == 'dm' and 'first forwarded question' in source['messages'][0]['text']


@pytest.mark.parametrize('bound', [False, True])
def test_forward_without_authored_mention_requires_an_existing_thread(slack_app, bound):
    from test_slack import forwarded_attachment
    app, client, submitted, _ = slack_app
    if bound:
        start(slack_app)
    before = app.state.store.rows('SELECT id,content FROM messages ORDER BY id')
    runs = len(submitted)
    payload = event('EvQuotedMention', type='message', text='', ts='1790719001.123456', thread_ts=ROOT,
                    attachments=[forwarded_attachment('<@U99999999> help')])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    messages = app.state.store.rows('SELECT id,content FROM messages ORDER BY id')
    assert len(messages) == len(before) + int(bound)
    assert len(submitted) == runs + int(bound)
    if bound:
        assert 'Please respond to the quoted Slack attachment.' in messages[-1]['content']
        assert '\\u003c@U99999999> help' in messages[-1]['content']


def test_authored_control_still_works_with_forward(slack_app):
    from test_slack import forwarded_attachment
    app, client, run_id = start(slack_app)
    send(client, 2, 'stop', attachments=[forwarded_attachment('model unrecognized')])
    assert app.state.store.rows("SELECT command FROM slack_receipts WHERE event_id='EvChat2'")[0]['command'] == 'stop'


def test_legacy_channel_sessions_preserve_forwarded_body(slack_app):
    from test_slack import forwarded_attachment
    app, client, submitted, _ = slack_app
    app.state.settings.slack_thread_chat_enabled = False
    payload = event(text='<@U99999999> explain', attachments=[forwarded_attachment('legacy body')])
    assert client.post('/hooks/slack/events', **signed(payload)).status_code == 200
    assert 'legacy body' in submitted[0]['prompt']
