import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.db import Store
from app.slack_chat import SlackChat
from sandbox.activity import ActivityReporter
from test_slack import slack_app  # noqa: F401
from test_slack_web_mirror import mirror, drain  # noqa: F401
from test_slack_chat import start, send, ROOT


@pytest.fixture
def clock(monkeypatch):
    stamp = [datetime(2026, 10, 3, 18, tzinfo=timezone.utc)]
    monkeypatch.setattr('app.db.now', lambda: stamp[0].isoformat())
    return lambda seconds: stamp.__setitem__(0, stamp[0] + timedelta(seconds=seconds))


def reporter(store, run_id):
    return ActivityReporter(lambda kind, message, data: store.event(run_id, kind, message, data))


def updates(store, run_id):
    return [e for e in store.events(run_id) if e['kind'] == 'message']


def test_quiet_budget_survives_chatter_restart_steering_and_replay(tmp_path, clock):
    store = Store(tmp_path)
    run_id = store.create_run('Build a feature', '', 'demo', [], chat_enabled=True)['id']
    first = store.claim_message(run_id)
    activity = reporter(store, run_id)
    activity.commentary('<think>private thought</think>I’ll build it and verify the result.')
    for i in range(20):
        activity.commentary(f'Editing file {i}.')
        activity.start(str(i), 'terminal', {'command': f'check {i}'})
        activity.complete(str(i), 'terminal', {}, {'exit_code': 0})
    assert len(updates(store, run_id)) == 1
    clock(300)
    store = Store(tmp_path)  # Same persisted budget after a server restart.
    steering, _ = store.enqueue_message(run_id, 'Also add tests', 'steering')
    store.execute("UPDATE messages SET status='injected',steering_parent_id=? WHERE id=?", (first['id'], steering['id']))
    data = {'activity_id': 'milestone', 'input_id': first['id'], 'turn_id': 999, 'phase': 'commentary'}
    store.event(run_id, 'message', 'Implementation is complete; the integration tests pass.', data)
    store.event(run_id, 'message', 'Implementation is complete; the integration tests pass.', data)
    clock(3600)
    reporter(store, run_id).commentary('One more routine update.')
    selected = updates(store, run_id)
    assert len(selected) == 2
    assert all(e['data']['turn_id'] == first['id'] and e['data']['public_update'] for e in selected)
    assert 'private thought' not in json.dumps(selected)
    assert len([e for e in store.events(run_id) if e['kind'] == 'tool']) == 40
    store.finish_message(run_id, first['id'], 'Finished with working tests.')
    next_message, _ = store.enqueue_message(run_id, 'A new task', 'next')
    assert store.claim_message(run_id)['id'] == next_message['id']
    reporter(store, run_id).commentary('I’ll handle the new task.')
    assert len(updates(store, run_id)) == 3
    assert any(m['content'] == 'Finished with working tests.' for m in store.messages(run_id))


def test_parallel_callbacks_cannot_exceed_budget_or_publish_placeholders(tmp_path, clock):
    store = Store(tmp_path)
    run_id = store.create_run('Long task', '', 'demo', [], chat_enabled=True)['id']
    store.claim_message(run_id)
    activity = reporter(store, run_id)
    activity.commentary('[System: Empty message content sanitised to satisfy protocol]')
    activity.commentary('<reasoning>hidden</reasoning>')
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(activity.commentary, [f'Opening {i}' for i in range(10)]))
    assert len(updates(store, run_id)) == 1
    clock(300)
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(activity.commentary, [f'Milestone {i}' for i in range(10)]))
    assert len(updates(store, run_id)) == 2


def test_requested_answer_bypasses_budget_once_without_resetting_it(tmp_path, clock):
    store = Store(tmp_path)
    run_id = store.create_run('Long task', '', 'demo', [], chat_enabled=True)['id']
    first = store.claim_message(run_id)
    activity = reporter(store, run_id)
    activity.commentary('Opening update.')
    clock(300)
    activity.commentary('Milestone update.')
    question, _ = store.enqueue_message(run_id, 'Did the backend tests pass?', 'question')
    store.execute("UPDATE messages SET status='injected',steering_parent_id=? WHERE id=?", (first['id'], question['id']))
    data = {'input_id': question['id'], 'phase': 'commentary'}
    store.event(run_id, 'message', 'Yes, all 44 backend tests pass.', data)
    store.event(run_id, 'message', 'Unsolicited follow-on narration.', data)
    store.event(run_id, 'message', 'Forged exception.', {'public_reply_to': 1234, 'public_update': True})
    selected = updates(store, run_id)
    assert len(selected) == 3
    assert selected[-1]['data']['public_reply_to'] == question['id']
    assert len([e for e in selected if not e['data']['public_reply_to']]) == 2


def test_slack_delivers_two_updates_then_final_in_original_thread(mirror, clock):
    app, _, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    first = store.claim_message(run_id)
    activity = reporter(store, run_id)
    activity.commentary('I’ll implement this and verify the result.')
    for i in range(39):
        activity.commentary(f'Routine edit {i}')
    chat.collect()
    drain(app)
    clock(300)
    activity.commentary('The feature is built and **44 tests pass**. Checking the demo next.')
    # Collector restart and repeated collections cannot duplicate delivery.
    chat = SlackChat(app.state.slack)
    chat.collect()
    chat.collect()
    drain(app)
    clock(300)
    activity.commentary('Another unneeded update.')
    store.finish_message(run_id, first['id'], 'Done. Here is the demo and verified result.')
    chat.collect()
    drain(app)
    posts = [p for p in mirror[3] if 'text' in p]
    assert len(posts) == 3
    assert [p['text'].split('\n')[0] for p in posts] == [
        'I’ll implement this and verify the result.',
        'The feature is built and *44 tests pass*. Checking the demo next.',
        'Done. Here is the demo and verified result.',
    ]
    assert all(p['channel'] == 'C12345678' and p['thread_ts'] == ROOT for p in posts)


def test_completed_turn_skips_unsent_progress_and_still_sends_failure(mirror, clock):
    app, _, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    first = store.claim_message(run_id)
    reporter(store, run_id).commentary('I’ll inspect the issue.')
    chat.collect()
    store.finish_message(run_id, first['id'], 'The gateway rejected the request.', 'failed')
    # Finish between collection and delivery; the stale update must be dropped.
    drain(app)
    chat.collect()
    drain(app)
    posts = [p for p in mirror[3] if 'text' in p]
    assert len(posts) == 1 and 'Response failed:' in posts[0]['text']
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='progress'")[0]['status'] == 'skipped'


@pytest.mark.parametrize('collect_before_stop', [False, True])
def test_stop_during_work_suppresses_progress_before_message_finishes(mirror, clock, collect_before_stop):
    app, _, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    store.claim_message(run_id)
    activity = reporter(store, run_id)
    activity.commentary('Starting the work.')
    if collect_before_stop:
        chat.collect()
    store.update_run(run_id, status='stopping')
    clock(300)
    activity.commentary('A late update while the sandbox stops.')
    chat.collect()
    drain(app)
    assert len(updates(store, run_id)) == 1
    assert not [p for p in mirror[3] if 'text' in p]


def test_sleep_does_not_backfill_progress_when_woken_before_collection(mirror, clock):
    app, client, run_id = start(mirror)
    store = app.state.store
    store.claim_message(run_id)
    store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
    reporter(store, run_id).commentary('Progress while sleeping must stay private.')
    send(client, 1, 'wake')
    app.state.slack.chat.collect()
    drain(app)
    assert not any('Progress while sleeping' in p.get('text', '') for p in mirror[3])


def test_legacy_updates_are_not_backfilled_and_approvals_bypass_budget(mirror, clock):
    app, _, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    first = store.claim_message(run_id)
    for i in range(39):
        store.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'message',?,?,?)",
                      (run_id, f'Historical update {i}', json.dumps({'turn_id': first['id'], 'phase': 'commentary'}), '2026-10-03T17:00:00+00:00'))
    reporter(store, run_id).commentary('Budget already exhausted.')
    store.execute("INSERT INTO approvals VALUES('approval',?,'linear_comment','{}','pending',?,'')", (run_id, '2026-10-03T18:00:00+00:00'))
    chat.collect()
    drain(app)
    posts = [p for p in mirror[3] if 'text' in p]
    assert len(posts) == 1 and 'administrator' in posts[0]['text']
    assert not store.rows("SELECT * FROM slack_outbox WHERE kind='progress'")


def test_uncertain_progress_delivery_is_not_replayed(mirror, monkeypatch, clock):
    app, _, run_id = start(mirror)
    store, chat = app.state.store, app.state.slack.chat
    store.claim_message(run_id)
    reporter(store, run_id).commentary('Opening update.')
    chat.collect()
    attempts = []
    async def uncertain(*args, **kwargs):
        attempts.append(kwargs['json'])
        raise RuntimeError('Connection lost after Slack accepted the post')
    monkeypatch.setattr(app.state.connectors, 'request', uncertain)
    asyncio.run(chat.deliver_one())
    chat.collect()
    asyncio.run(chat.deliver_one())
    assert len(attempts) == 1
    assert store.rows("SELECT status FROM slack_outbox WHERE kind='progress'")[0]['status'] == 'uncertain'
