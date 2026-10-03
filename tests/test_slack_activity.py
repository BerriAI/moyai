import asyncio
import time

import pytest

from app.db import Store
from app.slack_activity import SlackActivity
from sandbox.activity import ActivityReporter
from test_slack import event, signed, slack_app
from test_slack_chat import ROOT, dm_event, finish, start


@pytest.fixture
def activity(slack_app):
    app, client, _, _ = slack_app
    client.portal.call(app.state.slack.chat.shutdown)
    return slack_app


def sync(app):
    asyncio.run(app.state.slack.chat.activity.sync())


def test_native_working_refresh_clear_and_restart(activity):
    app, _, run_id = start(activity)
    sync(app)
    assert activity[3] == [{'channel_id': 'C12345678', 'thread_ts': ROOT, 'status': 'is getting ready…'}]
    assert not app.state.store.rows("SELECT 1 FROM slack_outbox WHERE kind='reaction'")
    app.state.store.update_run(run_id, status='running')
    sync(app)
    assert activity[3][-1]['status'] == 'is working…'
    sync(app)
    assert len(activity[3]) == 2
    app.state.store.execute('UPDATE slack_activity SET refreshed_at=?', (time.time() - 61,))
    sync(app)
    assert len(activity[3]) == 3
    # The persisted row survives a process restart and is cleared after recovery.
    reopened = Store(app.state.settings.data_dir)
    assert reopened.rows('SELECT status FROM slack_activity')[0]['status'] == 'is working…'
    app.state.slack.chat.activity = SlackActivity(app.state.slack)
    app.state.store.update_run(run_id, status='idle')
    # Initial user input remains queued; status must reflect pending work.
    sync(app)
    assert activity[3][-1]['status'] == 'is getting ready…'
    finish(app, run_id, 'Done')
    sync(app)
    assert activity[3][-1]['status'] == ''
    count = len(activity[3])
    sync(app)
    assert len(activity[3]) == count


@pytest.mark.parametrize('state', ['idle', 'completed', 'failed', 'cancelled', 'interrupted'])
def test_all_terminal_states_clear_indicator_without_a_reply(activity, state):
    app, _, run_id = start(activity)
    app.state.store.claim_message(run_id)
    sync(app)
    app.state.store.update_run(run_id, status=state)
    sync(app)
    assert activity[3][-1]['status'] == ''


def test_paused_thread_clears_and_changed_workspace_never_receives_status(activity):
    app, _, run_id = start(activity)
    sync(app)
    app.state.store.execute('UPDATE slack_threads SET paused=1')
    sync(app)
    assert activity[3][-1]['status'] == ''
    app.state.store.execute("UPDATE slack_threads SET paused=0,team_id='TOTHER'")
    count = len(activity[3])
    sync(app)
    assert len(activity[3]) == count
    with pytest.raises(RuntimeError, match='destination'):
        asyncio.run(app.state.slack.channel.set_status(app.state.slack.channel.source_for_run(run_id), 'is working…'))


def test_connection_is_rechecked_after_token_refresh(activity, monkeypatch):
    app, _, run_id = start(activity)
    async def refreshed():
        app.state.store.execute("INSERT INTO connection_policies(provider,enabled) VALUES('slack',0)")
        return 'separate-bot-secret'
    monkeypatch.setattr(app.state.connectors, 'slack_bot_token', refreshed)
    sync(app)
    assert not activity[3]


def test_uncertain_status_retries_but_completion_clears_immediately(activity, monkeypatch):
    app, _, run_id = start(activity)
    original = app.state.connectors.request
    attempts = []
    async def uncertain(*args, **kwargs):
        attempts.append(kwargs['json'])
        raise TimeoutError('Slack may already have displayed it')
    monkeypatch.setattr(app.state.connectors, 'request', uncertain)
    sync(app)
    sync(app)
    assert len(attempts) == 1
    app.state.store.execute('UPDATE slack_activity SET retry_at=0')
    sync(app)
    assert len(attempts) == 2  # Idempotent status can retry; agent input cannot.
    assert len(app.state.store.messages(run_id)) == 1
    monkeypatch.setattr(app.state.connectors, 'request', original)
    finish(app, run_id, 'Answer survived')
    sync(app)
    assert activity[3][-1]['status'] == ''
    asyncio.run(app.state.slack.chat.deliver_one())
    assert activity[3][-1]['text'].startswith('Answer survived')
    assert activity[3][-1]['blocks'][-1]['type'] == 'context'


def test_posting_during_work_refreshes_indicator_and_web_followup_starts_it(activity):
    app, _, run_id = start(activity)
    sync(app)
    finish(app, run_id, 'First reply')
    sync(app)
    app.state.slack.chat.enqueue_web(run_id, 'Followup from web', 'web-1', None, 'user')
    sync(app)
    assert activity[3][-1]['status'] == 'is getting ready…'
    before = len(activity[3])
    asyncio.run(app.state.slack.chat.deliver_one())
    sync(app)
    assert len(activity[3]) == before + 2
    assert activity[3][-1]['status'] == 'is getting ready…'


def test_plain_dm_keeps_top_level_routing_and_threaded_dm_gets_status(activity):
    app, client, submitted, _ = activity
    client.post('/hooks/slack/events', **signed(dm_event(1, 'Plain DM')))
    assert app.state.store.rows("SELECT 1 FROM slack_outbox WHERE kind='reaction'")
    sync(app)
    assert activity[3] == []
    payload = dm_event(2, 'Panel thread')
    payload['event']['thread_ts'] = '1790728000.123456'
    client.post('/hooks/slack/events', **signed(payload))
    sync(app)
    assert activity[3] == [{'channel_id': 'D12345678', 'thread_ts': '1790728000.123456', 'status': 'is getting ready…'}]


def test_historical_idle_sessions_are_not_backfilled_and_refresh_is_bounded(activity):
    app, client, run_id = start(activity)
    finish(app, run_id, 'Old answer')
    sync(app)
    assert activity[3] == []
    for i in range(12):
        payload = event(f'EvBatch{i}', ts=f'179080{i:04d}.123456')
        client.post('/hooks/slack/events', **signed(payload))
    sync(app)
    assert len(activity[3]) == 5
    sync(app)
    assert len(activity[3]) == 10
    sync(app)
    assert len(activity[3]) == 12
    assert len({row['thread_ts'] for row in activity[3]}) == 12


def test_focus_changes_coalesce_in_native_indicator_without_posting_messages(activity):
    app, _, run_id = start(activity)
    turn = app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    sync(app)
    sequence = iter(range(10))
    def emit(kind, text, data):
        app.state.store.event(run_id, kind, text, {**data, 'activity_id': str(next(sequence)), 'input_id': turn['id']})
    reporter = ActivityReporter(emit)
    reporter.commentary('<status>Auditing UI changes</status>')
    sync(app)
    assert activity[3][-1]['status'] == 'Auditing UI changes'
    reporter.commentary('<status>Checking schema changes</status>')
    reporter.commentary('<status>Verifying the fix</status>')
    sync(app)
    assert activity[3][-1]['status'] == 'Auditing UI changes'
    app.state.store.execute('UPDATE slack_activity SET refreshed_at=?', (time.time() - 6,))
    sync(app)
    assert activity[3][-1]['status'] == 'Verifying the fix'
    app.state.slack.chat.collect()
    assert not app.state.store.rows("SELECT 1 FROM slack_outbox WHERE kind='progress'")
    assert not any('text' in item for item in activity[3])
    assert not any(item['kind'] == 'message' for item in app.state.store.events(run_id))
    # Process reconstruction and Slack's TTL refresh reuse the latest focus.
    app.state.slack.chat.activity = SlackActivity(app.state.slack)
    app.state.store.execute('UPDATE slack_activity SET refreshed_at=?', (time.time() - 61,))
    sync(app)
    assert activity[3][-1]['status'] == 'Verifying the fix'
    for state, expected in [('awaiting_approval', 'is waiting for approval in the web session'),
                            ('reconnecting', 'is reconnecting to the workspace…'),
                            ('saving', 'is saving the work…'), ('cancelled', '')]:
        app.state.store.update_run(run_id, status=state)
        sync(app)
        assert activity[3][-1]['status'] == expected


def test_focus_retry_uses_latest_value_and_does_not_delay_clearing(activity, monkeypatch):
    app, _, run_id = start(activity)
    turn = app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    original = app.state.connectors.request
    attempts = []
    async def fail(*args, **kwargs):
        attempts.append(kwargs['json'])
        raise TimeoutError('Uncertain delivery')
    monkeypatch.setattr(app.state.connectors, 'request', fail)
    def focus(text):
        app.state.store.event(run_id, 'status', text, {'phase': 'focus', 'activity_id': text, 'input_id': turn['id']})
    focus('Inspecting the UI')
    sync(app)
    focus('Verifying the fix')
    sync(app)
    assert len(attempts) == 1
    monkeypatch.setattr(app.state.connectors, 'request', original)
    app.state.store.execute('UPDATE slack_activity SET retry_at=0')
    sync(app)
    assert activity[3][-1]['status'] == 'Verifying the fix'
    app.state.store.update_run(run_id, status='cancelled')
    sync(app)
    assert activity[3][-1]['status'] == ''


def test_token_refresh_cannot_send_focus_from_a_finished_turn(activity, monkeypatch):
    app, _, run_id = start(activity)
    turn = app.state.store.claim_message(run_id)
    app.state.store.update_run(run_id, status='running')
    app.state.store.event(run_id, 'status', 'Auditing UI changes',
                          {'phase': 'focus', 'activity_id': 'focus', 'input_id': turn['id']})
    async def finish_during_refresh():
        app.state.store.finish_message(run_id, turn['id'], 'Done')
        app.state.store.update_run(run_id, status='idle')
        return 'separate-bot-secret'
    monkeypatch.setattr(app.state.connectors, 'slack_bot_token', finish_during_refresh)
    sync(app)
    assert activity[3] == []
    sync(app)
    assert activity[3] == [{'channel_id': 'C12345678', 'thread_ts': ROOT, 'status': ''}]
