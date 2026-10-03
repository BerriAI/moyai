import asyncio
import json
import time

import pytest
from concurrent.futures import ThreadPoolExecutor

from app.db import Store, now
from app.slack_chat import slack_text, split_reply
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


def publication(app, run_id, number=100, title='Show the completed demo'):
    result = {'number': number, 'url': f'https://github.com/BerriAI/moyai-devin/pull/{number}',
              'repository': 'BerriAI/moyai-devin', 'title': title, 'draft': False}
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
    assert '<@U88888888>' not in json.dumps(card)
    actions = next(block['elements'] for block in card['blocks'] if block['type'] == 'actions')
    assert [item['url'] for item in actions] == [url, url + '/files', f'https://workspace.example/#run={run_id}']
    send(client, 1, 'An unrelated follow-up')
    finish(app, run_id, 'No new pull request.')
    wait_for(lambda: any('No new pull request.' in message.get('text', '') for message in slack_app[3]))
    assert len([message for message in slack_app[3] if message.get('attachments')]) == 1


def test_capture_handoff_selects_only_referenced_saved_files_and_handles_old_grant(slack_app):
    from app import captures
    app, _, run_id = start(slack_app)
    url = publication(app, run_id)
    root = captures.directory(app.state.settings, run_id)
    root.mkdir(parents=True)
    (root / 'demo.webm').write_bytes(b'\x1aE\xdf\xa3webm-recording-fixture')
    (root / 'result.png').write_bytes(b'\x89PNG\r\n\x1a\nscreenshot-fixture')
    (root / 'unrelated.png').write_bytes(b'\x89PNG\r\n\x1a\nunrelated-private-content')
    finish(app, run_id, f'[PR]({url})\n[Video](/workspace/moyai-captures/demo.webm)\n'
           '![Screenshot](/workspace/moyai-captures/result.png)')
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' ORDER BY id")
    media = next(json.loads(row['metadata']) for row in rows if 'captures' in json.loads(row['metadata']))
    assert [capture['name'] for capture in media['captures']] == ['demo.webm', 'result.png']
    assert any(f'https://workspace.example/api/runs/{run_id}/computer/captures/demo.webm' in row['text'] for row in rows)
    assert not any('/workspace/moyai-captures/' in row['text'] for row in rows)
    wait_for(lambda: any('Reconnect Slack' in message.get('text', '') for message in slack_app[3]))
    assert not any('unrelated.png' in json.dumps(message) for message in slack_app[3])
    app.state.slack.chat.collect()
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer'")) == len(rows)


def test_thread_followups_and_both_slack_event_types_share_one_session(slack_app):
    app, client, run_id = start(slack_app)
    mention = event('EvPaired', type='message')
    client.post('/hooks/slack/events', **signed(mention))
    finish(app, run_id, 'Remembering **blue lantern**. [Docs](https://example.com).')
    followup = send(client, 1, 'What was the phrase?')
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


def test_parallel_duplicate_delivery_cannot_enqueue_twice(slack_app):
    app, client, run_id = start(slack_app)
    payload = event('EvParallelFollowup', type='message', ts='1790719001.111111', thread_ts=ROOT, text='Continue please')
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


def test_checkpoint_handoffs_do_not_post_synthetic_answers_to_slack(slack_app):
    app, client, run_id = start(slack_app)
    message = app.state.store.claim_message(run_id)
    app.state.store.finish_message(run_id, message['id'], 'Paused and saved', 'steered')
    assert not [m for m in app.state.store.messages(run_id) if m['role'] == 'assistant']
    # Notices saved by an older release must not be delivered after an upgrade.
    app.state.store.execute("INSERT INTO messages(run_id,role,content,status,created_at) VALUES(?,'assistant','Old paused notice','steered',?)", (run_id, now()))
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' AND run_id=?", (run_id,))
    send(client, 1, 'Continue the same work')
    finish(app, run_id, 'The actual result')
    answers = app.state.store.rows("SELECT * FROM slack_outbox WHERE kind='answer' AND run_id=?", (run_id,))
    assert len(answers) == 1 and 'The actual result' in answers[0]['text']


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
    send(client, 4, 'Continue from where we left off')
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
    send(client, 1, 'A queued followup')
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
    send(client, 1, 'Use opus instead')
    send(client, 2, 'And skip the docs')
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


def test_no_historical_answer_backfill_and_legacy_requires_a_new_mention(slack_app):
    app, client, _, _ = slack_app
    old = app.state.store.create_slack_run('EvOld', 'Old request', [], 'C12345678', ROOT, 'U12345678')
    app.state.store.execute("UPDATE slack_events SET reply_status='sent'")
    finish(app, old['id'], 'Old answer must stay in the app')
    send(client, 1, 'An unrelated reply in an old thread')
    assert not app.state.store.rows('SELECT * FROM slack_threads')
    send(client, 2, '<@U99999999> Continue the previous conversation')
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE text LIKE '%Old answer%'")
    assert 'Continue the previous conversation' in app.state.store.messages(old['id'])[-1]['content']


def test_queue_limit_has_one_visible_reply_and_no_dropped_duplicate_execution(slack_app):
    app, client, run_id = start(slack_app)
    for index in range(1, 6):
        send(client, index, f'Followup {index}')
    assert len(app.state.store.messages(run_id)) == 5
    rows = app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key='rejected:EvChat5'")
    assert len(rows) == 1 and '5 queued messages' in rows[0]['text']
    send(client, 5, 'Followup 5')
    assert len(app.state.store.rows("SELECT * FROM slack_outbox WHERE dedupe_key='rejected:EvChat5'")) == 1


def test_results_are_only_sent_to_original_thread_and_never_ping_users(slack_app):
    app, client, run_id = start(slack_app)
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE refreshed_at>0")))
    finish(app, run_id, 'Hello <!channel> <@U12345678>. The key is model-test-key.\n```python\nprint("hello")\n```')
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one())
    sent = slack_app[3][-1]
    assert sent['channel'] == 'C12345678' and sent['thread_ts'] == ROOT
    assert '<!channel>' not in sent['text'] and '<@U12345678>' not in sent['text']
    assert 'model-test-key' not in sent['text'] and '[redacted]' in sent['text']
    assert sent['parse'] == 'none' and sent['link_names'] is False
    assert sent['blocks'][0]['text']['verbatim'] is True


def test_answers_render_mentions_only_for_users_already_mentioned_in_the_thread(slack_app):
    app, client, runs, sent = slack_app
    client.post('/hooks/slack/events', **signed(event('EvKudos', text='<@U99999999> big kudos to <@U0B1TDTHL4Q> on it')))
    run_id = runs[0]['id']
    app.state.store.execute('UPDATE slack_events SET context_json=? WHERE run_id=?',
                            (json.dumps({'messages': [{'text': 'thanks <@U0C59A1GHPD|bot-name>'}]}), run_id))
    wait_for(lambda: bool(app.state.store.rows("SELECT 1 FROM slack_activity WHERE refreshed_at>0")))
    finish(app, run_id, 'Credit to <@U0B1TDTHL4Q> and <@U0C59A1GHPD>. Not <@U00000001>, <@U99999999>, <!channel> or `<@U0B1TDTHL4Q>`')
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one())
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
    asyncio.run(app.state.slack.chat.deliver_one())
    assert app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='answer'")[0]['status'] == 'uncertain'
    # A fresh database handle observes the same durable cursor and outbox.
    reopened = Store(app.state.settings.data_dir)
    assert len(reopened.rows("SELECT * FROM slack_outbox WHERE kind='answer'")) == 1
    app.state.slack.chat.collect()
    app.state.slack.recover()
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one())
    assert attempts == [1]


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


def dm_event(index, text, channel='D12345678', user='U12345678'):
    return event(f'EvDM{index}', type='message', channel_type='im', channel=channel,
                 user=user, text=text, ts=f'17907290{index:02d}.123456')


def test_agentchat_dm_followups_preserve_session_model_sender_and_top_level_replies(slack_app):
    from agentchat import AgentChat
    app, client, submitted, sent = slack_app
    assert isinstance(app.state.slack.agentchat, AgentChat)
    first = dm_event(1, 'Remember granite and read <@U87654321> as source text')
    assert client.post('/hooks/slack/events', **signed(first)).status_code == 200
    run_id = submitted[0]['id']
    finish(app, run_id, 'Remembering granite.')
    assert client.post('/hooks/slack/events', **signed(dm_event(2, 'model opus'))).status_code == 200
    second = dm_event(3, 'What was the phrase?')
    assert client.post('/hooks/slack/events', **signed(second)).status_code == 200
    second['event_id'] = 'EvDMDuplicate'
    assert client.post('/hooks/slack/events', **signed(second)).status_code == 200
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1
    messages = app.state.store.messages(run_id)
    assert len(messages) == 3
    assert '<@U87654321>' in messages[0]['content']
    assert messages[-1]['model'] == 'anthropic/claude-opus-5-5'
    assert messages[-1]['user_id'] == 'slack:T12345678:U12345678'
    assert app.state.store.run(run_id)['owner_id'] == messages[-1]['user_id']
    wait_for(lambda: any('text' in item for item in sent))
    assert all(msg['channel'] == 'D12345678' and msg.get('thread_ts') is None for msg in sent)
    assert 'signed-in BerriAI teammates can view it' in next(item['text'] for item in sent if 'text' in item)
    history = asyncio.run(app.state.slack.agentchat.state.history('slack:T12345678:D12345678'))
    assert [m.text for m in history] == [m['content'] for m in messages]
    assert asyncio.run(app.state.slack.agentchat.state.history('slack:T12345678:D12345678',limit=0)) == ()


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
    assert set(scope.split(',')) == {'app_mentions:read','chat:write','channels:history','groups:history','im:history','reactions:write','assistant:write','files:write','users:read','users:read.email'}


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
    asyncio.run(app.state.slack.chat.deliver_one())
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
    asyncio.run(app.state.slack.chat.deliver_one())
    assert sent[-1]['text'].startswith('Still answered.')
    assert app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='answer'")[0]['status'] == 'sent'


def test_questions_addressed_to_someone_else_do_not_wake_moyai(slack_app):
    app, client, run_id = start(slack_app)
    for index, text in enumerate(['<@U88888888> what is <@U99999999>?', '<@U88888888> please continue'], 1):
        send(client, index, text)
    assert len(app.state.store.messages(run_id)) == 1
    send(client, 3, '<@U99999999> tell me about <@U88888888>')
    send(client, 4, '<@U88888888> <@U99999999> please both explain')
    assert len(app.state.store.messages(run_id)) == 3
    assert '<@U88888888>' in app.state.store.messages(run_id)[-2]['content']


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
    client.post('/hooks/slack/events', **signed(dm_event(2, 'Again')))
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
    client.post('/hooks/slack/events', **signed(dm_event(5, 'Continue the plain DM')))
    assert len(app.state.store.messages(main_run)) == 2
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
def test_native_media_batch_then_one_card_and_dm_notice(media_delivery, dm):
    app, prepare, calls, uploaded = media_delivery
    run_id, answer = prepare(dm)
    finish(app, run_id, ('Verified behavior. ' * 200) + answer)
    drain_answers(app)
    completions = [kwargs['json'] for method, kwargs in calls if method == 'files.completeUploadExternal']
    assert len(completions) == 1
    assert [f['id'] for f in completions[0]['files']] == ['demo.webm', 'result.png']
    assert completions[0]['channel_id'].startswith('D' if dm else 'C')
    assert completions[0].get('thread_ts') == (None if dm else ROOT)
    assert [raw for _, raw in uploaded] == [b'\x1aE\xdf\xa3webm-demo', b'\x89PNG\r\n\x1a\nresult']
    replies = [kwargs['json'] for method, kwargs in calls if method == 'chat.postMessage']
    assert len(replies) == 2 and not replies[0].get('attachments') and replies[1]['attachments']
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
    asyncio.run(app.state.slack.chat.deliver_one())
    assert not any(method == 'files.completeUploadExternal' for method, _ in calls)
    assert len(uploaded) == (1 if boundary == 'bytes' else 0)
    media = app.state.store.rows("SELECT status FROM slack_outbox WHERE dedupe_key LIKE '%:media'")
    assert media[0]['status'] == 'uncertain'


def test_lost_media_completion_is_not_replayed_and_card_still_delivers(media_delivery, monkeypatch):
    app, prepare, calls, uploaded = media_delivery
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
        await app.state.slack.chat.shutdown()
    asyncio.run(restart())
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


def test_explicit_capture_followup_delivers_without_a_new_pr(media_delivery):
    app, prepare, calls, uploaded = media_delivery
    run_id, _ = prepare()
    finish(app, run_id, '[Video](/workspace/moyai-captures/demo.webm) '
           '![Screenshot](/workspace/moyai-captures/result.png)')
    drain_answers(app)
    assert len(uploaded) == 2
    replies = [kwargs['json'] for method, kwargs in calls if method == 'chat.postMessage']
    assert len(replies) == 1 and not replies[0].get('attachments')
