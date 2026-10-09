"""Independent event sessions, bounded admission, and existing schedule upgrades."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from temporalio.client import ScheduleAlreadyRunningError, ScheduleOverlapPolicy

from app.automations import Automations, Definition, Save
from app.temporal_runtime import TemporalRunManager
from test_automations import create
from test_automation_events import configured, signed, complete
from test_slack import slack_app, signed as slack_signed, event as slack_event
from test_workspace import workspace


def test_twenty_slack_events_start_twenty_sessions_then_one_more_next_hour(slack_app):
    app, client, submitted, messages = slack_app
    service, store = app.state.automations, app.state.store
    owner = store.identity({'method': 'password', 'role': 'admin'})
    automation = service.save(Save(definition=Definition(
        name='Feedback triage', prompt='Investigate this feedback', mode='demo', max_runs_per_hour=20,
        triggers=[{'id': 'feedback', 'event': {'provider': 'slack', 'event': 'message.posted',
                    'channel_id': 'C12345678', 'sender_type': 'human'}}])), owner)
    store.execute('UPDATE automations SET paused=0 WHERE id=?', (automation['id'],))
    TemporalRunManager(store, app.state.settings)
    app.state.settings.temporal_enabled = True

    def deliver(number):
        payload = slack_event(f'Feedback{number}', type='message', text=f'Feedback {number}',
                              ts=f'1790719000.{number:06d}')
        assert client.post('/hooks/slack/events', **slack_signed(payload)).status_code == 200

    for number in range(20):
        deliver(number)
    asyncio.run(service.events.dispatch())
    runs = store.rows('SELECT * FROM runs ORDER BY created_at,id')
    assert len(runs) == 20
    assert all(run['status'] == 'queued' and run['owner_id'] == owner for run in runs)
    assert len(store.rows('SELECT * FROM durable_sessions')) == 20
    for number, run in enumerate(runs):
        assert f'"body": "Feedback {number}"' in run['prompt']
    for number in range(20):
        deliver(number)
    asyncio.run(service.events.dispatch())
    assert len(store.rows('SELECT * FROM runs')) == 20
    # A new hour permits just the new event, even if all prior work is still active.
    earlier = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.execute('UPDATE automation_runs SET created_at=?', (earlier,))
    deliver(20)
    asyncio.run(service.events.dispatch())
    assert len(store.rows('SELECT * FROM runs')) == 21
    assert len(store.rows('SELECT * FROM durable_sessions')) == 21
    assert not store.rows("SELECT 1 FROM automation_events WHERE status='pending'")
    assert not submitted and not messages  # Admission does not call models or send Slack messages.


@pytest.mark.parametrize('limit', ['hourly', 'workspace'])
def test_concurrent_admission_keeps_limits_atomic_and_retries_idempotent(workspace, monkeypatch, limit):
    app, client = workspace
    service, store = app.state.automations, app.state.store
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    if limit == 'workspace':
        store.max_pending_runs = 3
    a = create(client, max_runs_per_hour=3 if limit == 'hourly' else None)
    b = create(client, max_runs_per_hour=None) if limit == 'workspace' else a

    def launch(index):
        target = a if index % 2 == 0 else b
        try:
            return asyncio.run(service.launch(target['id'], 1, f'concurrent-{index}', manual=True))
        except HTTPException as exc:
            assert exc.status_code == 429
            return {'outcome': 'capacity'}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(launch, range(20)))
    started = [i for i, result in enumerate(results) if result['outcome'] == 'started']
    assert len(started) == len(store.rows('SELECT * FROM runs')) == 3
    for index in started:
        assert launch(index) == results[index]
    assert len(store.rows('SELECT * FROM runs')) == 3


def test_full_workspace_preserves_events_then_drains_available_slots(workspace):
    app, client = workspace
    a = configured(app, client)
    store = app.state.store
    store.max_pending_runs = 2
    unrelated = store.create_run('Unrelated work', '', 'demo', [])
    path = '/hooks/automations/' + a['id']
    for number in range(3):
        assert client.post(path, **signed('webhook', {'event': 'benchmark.ready', 'body': f'Feedback {number}'},
                                         delivery=f'capacity-{number}')).json()['status'] == 'accepted'
    asyncio.run(app.state.automations.events.dispatch())
    pending = store.rows("SELECT * FROM automation_events WHERE status='pending' ORDER BY received_at,occurrence")
    assert len(pending) == 2 and 'workspace session capacity' in pending[0]['detail']
    assert all('Feedback' in event['context'] for event in pending)
    assert len(store.rows('SELECT * FROM runs')) == 2
    complete(app, unrelated['id'])
    asyncio.run(app.state.automations.events.dispatch())
    assert len(store.rows('SELECT * FROM runs')) == 3
    assert len(store.rows("SELECT * FROM automation_events WHERE status='pending'")) == 1
    assert len(store.rows("SELECT * FROM runs WHERE status='queued'")) == 2


def test_busy_automation_does_not_starve_other_inboxes(workspace):
    app, client = workspace
    a = configured(app, client, max_runs_per_hour=1)
    b = configured(app, client)
    for automation, count in [(a, 20), (b, 2)]:
        for number in range(count):
            response = client.post('/hooks/automations/' + automation['id'], **signed(
                'webhook', {'event': 'benchmark.ready'}, delivery=f'fair-{number}'))
            assert response.json()['status'] == 'accepted'
    asyncio.run(app.state.automations.events.dispatch())
    counts = {row['automation_id']: row['count'] for row in app.state.store.rows(
        "SELECT automation_id,COUNT(*) AS count FROM automation_runs WHERE outcome='started' GROUP BY automation_id")}
    assert counts == {a['id']: 1, b['id']: 2}


async def test_existing_schedules_retry_policy_upgrade_without_invalidating_events(workspace):
    from app.automations import SCHEDULE_VERSION
    app, client = workspace
    a = configured(app, client)
    service, store = app.state.automations, app.state.store
    # Model a persisted pre-upgrade database and already-synced schedule.
    store.execute('ALTER TABLE automations DROP COLUMN synced_schedule_version')
    store.execute('UPDATE automations SET synced_revision=revision')
    assert client.post('/hooks/automations/' + a['id'], **signed(
        'webhook', {'event': 'benchmark.ready'})).json()['status'] == 'accepted'
    # Include a timer so the remote update must succeed before marking the version.
    import json
    definition = a['definition']
    definition['triggers'].append({'id': 'hourly', 'schedule': {'frequency': 'hourly'}})
    store.execute('UPDATE automations SET definition=? WHERE id=?', (json.dumps(definition), a['id']))
    successor = Automations(store, service.settings, service.security, service.manager,
                            service.connectors, service.environments, service.checkpoints)

    class Remote:
        failed = True
        updates = []

        async def create_schedule(self, identity, schedule, **kwargs):
            raise ScheduleAlreadyRunningError()

        def get_schedule_handle(self, identity):
            return self

        async def update(self, callback, **kwargs):
            if self.failed:
                raise ConnectionError('temporary outage')
            self.updates.append(callback(None).schedule)

    remote = Remote()
    await successor.sync(remote)
    assert successor.row(a['id'])['synced_schedule_version'] == 0
    assert successor.public(successor.row(a['id']), a['owner_id'])['schedule_sync_pending']
    remote.failed = False
    await successor.sync(remote, a['id'])
    assert remote.updates[0].policy.overlap == ScheduleOverlapPolicy.ALLOW_ALL
    assert successor.row(a['id'])['revision'] == a['revision']
    assert successor.row(a['id'])['synced_schedule_version'] == SCHEDULE_VERSION
    assert not successor.public(successor.row(a['id']), a['owner_id'])['schedule_sync_pending']
    await successor.events.dispatch()
    assert len(store.rows('SELECT * FROM runs')) == 1
    restarted = Automations(store, service.settings, service.security, service.manager,
                            service.connectors, service.environments, service.checkpoints)
    await restarted.sync(remote)
    assert len(remote.updates) == 1
