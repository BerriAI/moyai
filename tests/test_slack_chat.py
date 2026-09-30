import asyncio
import json
import time
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
    wait_for(lambda: app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='reaction'")[0]['status'] == 'sent')
    finish(app, run_id, 'Hello <!channel> <@U12345678>. The key is model-test-key.\n```python\nprint("hello")\n```')
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one())
    sent = slack_app[3][-1]
    assert sent['channel'] == 'C12345678' and sent['thread_ts'] == ROOT
    assert '<!channel>' not in sent['text'] and '<@U12345678>' not in sent['text']
    assert 'model-test-key' not in sent['text'] and '[redacted]' in sent['text']
    assert sent['parse'] == 'none' and sent['link_names'] is False
    assert sent['blocks'][0]['text']['verbatim'] is True


def test_uncertain_outbox_delivery_survives_restart_without_resending(slack_app, monkeypatch):
    app, client, run_id = start(slack_app)
    wait_for(lambda: app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='reaction'")[0]['status'] == 'sent')
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
    assert set(scope.split(',')) == {'app_mentions:read','chat:write','channels:history','groups:history','im:history','reactions:write','assistant:write','users:read','users:read.email'}


def test_acknowledgment_is_one_reaction_without_periodic_chatter(slack_app):
    app, client, run_id = start(slack_app)
    wait_for(lambda: app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='reaction'")[0]['status'] == 'sent')
    client.post('/hooks/slack/events', **signed(event('EvDuplicateType', type='message')))
    app.state.store.execute('UPDATE slack_threads SET last_progress=0')
    app.state.slack.chat.collect()
    assert len(slack_app[3]) == 1
    assert slack_app[3][0]['name'] == 'eyes'
    assert not app.state.store.rows("SELECT * FROM slack_outbox WHERE kind IN ('ack','progress','control')")
    finish(app, run_id, 'Here is the answer.')
    app.state.slack.chat.last_post.clear()
    asyncio.run(app.state.slack.chat.deliver_one())
    assert slack_app[3][-1]['text'].startswith('Here is the answer.')


def test_reaction_failure_does_not_prevent_the_answer(slack_app, monkeypatch):
    app, client, _, sent = slack_app
    real = app.state.connectors.request
    async def reject_reaction(method, url, **kwargs):
        if url.endswith('reactions.add'):
            raise RuntimeError('missing_scope')
        return await real(method, url, **kwargs)
    monkeypatch.setattr(app.state.connectors, 'request', reject_reaction)
    _, _, run_id = start(slack_app)
    wait_for(lambda: app.state.store.rows("SELECT status FROM slack_outbox WHERE kind='reaction'")[0]['status'] == 'uncertain')
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
