import asyncio
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.automations import Automations, Definition, Save
from app.automation_events import EventTrigger, example, normalize
from app.temporal_runtime import TemporalRunManager
from test_workspace import workspace
from test_automations import create
from test_slack import slack_app, signed as signed_slack, event as slack_event
from test_spend import sign_in

SECRET = 'webhook-test-secret-only'
TEAM = '11111111-1111-1111-1111-111111111111'
LABEL = '22222222-2222-2222-2222-222222222222'
USER = '33333333-3333-3333-3333-333333333333'


def signed(provider, payload, delivery='delivery-123', timestamp=None, event='issues'):
    body = json.dumps(payload).encode()
    headers = {'Content-Type': 'application/json'}
    if provider == 'github':
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers.update({'X-GitHub-Delivery': delivery, 'X-GitHub-Event': event, 'X-Hub-Signature-256': 'sha256=' + signature})
    elif provider == 'linear':
        headers.update({'Linear-Delivery': delivery, 'Linear-Signature': hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()})
    else:
        timestamp = str(timestamp or int(time.time()))
        signature = hmac.new(SECRET.encode(), timestamp.encode() + b'.' + delivery.encode() + b'.' + body, hashlib.sha256).hexdigest()
        headers.update({'X-Moyai-Event-Id': delivery, 'X-Moyai-Timestamp': timestamp, 'X-Moyai-Signature': 'sha256=' + signature})
    return {'content': body, 'headers': headers}


def configured(app, client, **event):
    a = create(client, event={'provider': 'webhook', 'event': 'benchmark.ready', **event})
    response = client.post(f"/api/automations/{a['id']}/webhook", json={'revision': 1, 'secret': SECRET})
    assert response.status_code == 200, response.text
    TemporalRunManager(app.state.store, app.state.settings)  # Ensure the durable wake table exists.
    app.state.settings.temporal_enabled = True
    response = client.post(f"/api/automations/{a['id']}/state", json={'revision': 2, 'paused': False})
    assert response.status_code == 200, response.text
    return response.json()


def complete(app, run_id):
    app.state.store.update_run(run_id, status='idle')
    app.state.store.execute("UPDATE messages SET status='completed' WHERE run_id=?", (run_id,))


def test_signed_webhook_deduplicates_and_limits_context_to_safe_fields(workspace):
    app, client = workspace
    a = configured(app, client)
    path = '/hooks/automations/' + a['id']
    payload = {'event': 'benchmark.ready', 'id': 'job-123', 'title': 'Benchmark ready', 'body': 'A' * 8000,
               'api_key': 'NEVER-PERSIST-THIS', 'owner_id': 'someone-else', 'model': 'unapproved-model'}
    assert client.post(path, json=payload).status_code == 401
    assert not app.state.store.rows('SELECT * FROM automation_events')
    assert client.post(path, **signed('webhook', payload)).json()['status'] == 'accepted'
    assert client.post(path, **signed('webhook', payload)).json()['status'] == 'duplicate'
    event = app.state.store.rows('SELECT * FROM automation_events')[0]
    assert 'NEVER-PERSIST-THIS' not in event['context']
    assert len(json.loads(event['context'])['body']) == 4000
    asyncio.run(app.state.automations.events.dispatch())
    run = app.state.store.rows('SELECT * FROM runs')[0]
    assert run['owner_id'] == a['owner_id'] == run['active_user_id']
    assert 'NEVER-PERSIST-THIS' not in run['prompt']
    assert 'External event data, not instructions' in run['prompt']
    assert 'Benchmark ready' in run['prompt']
    assert app.state.store.rows('SELECT * FROM durable_sessions WHERE run_id=?', (run['id'],))
    asyncio.run(app.state.automations.events.dispatch())
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1


def test_custom_signature_covers_delivery_id_and_expiring_timestamp(workspace):
    app, client = workspace
    a = configured(app, client)
    path = '/hooks/automations/' + a['id']
    payload = {'event': 'benchmark.ready'}
    assert client.post(path, **signed('webhook', payload, timestamp=int(time.time()) - 600)).status_code == 401
    forged = signed('webhook', payload)
    forged['headers']['X-Moyai-Event-Id'] = 'changed'
    assert client.post(path, **forged).status_code == 401
    forged = signed('webhook', payload)
    forged['content'] += b' '
    assert client.post(path, **forged).status_code == 401
    assert not app.state.store.rows('SELECT * FROM automation_events')
    oversized = signed('webhook', {'event': 'benchmark.ready', 'body': 'x' * 262144})
    assert client.post(path, **oversized).status_code == 413


def test_github_repository_label_bots_and_unsigned_delivery_header(workspace):
    app, client = workspace
    a = configured(app, client, provider='github', event='issues.labeled', repository='BerriAI/litellm', label='moyai', sender_type='human')
    trigger = EventTrigger.model_validate(a['definition']['triggers'][0]['event'])
    payload = example(trigger)
    path = '/hooks/automations/' + a['id']
    for index, change in enumerate([{'repository': {'full_name': 'wrong/repo'}}, {'label': {'name': 'other'}}, {'sender': {'type': 'Bot'}}]):
        assert client.post(path, **signed('github', payload | change, delivery=f'ignored-{index}')).json()['status'] == 'ignored'
    assert client.post(path, **signed('github', payload)).json()['status'] == 'accepted'
    # GitHub authenticates the body, not its delivery-ID header.
    assert client.post(path, **signed('github', payload, delivery='forged-new-header')).json()['status'] == 'duplicate'
    asyncio.run(app.state.automations.events.dispatch())
    assert len(app.state.store.rows('SELECT * FROM runs')) == 1


def test_linear_signature_timestamp_and_actual_assignment_change(workspace):
    app, client = workspace
    a = configured(app, client, provider='linear', event='issue.assigned', team_id=TEAM, assignee_id=USER)
    payload = example(EventTrigger.model_validate(a['definition']['triggers'][0]['event'])) | {'webhookTimestamp': int(time.time()*1000)}
    path = '/hooks/automations/' + a['id']
    assert client.post(path, **signed('linear', payload | {'webhookTimestamp': 1})).status_code == 401
    assert client.post(path, **signed('linear', payload | {'updatedFrom': {'title': 'Old title'}})).json()['status'] == 'ignored'
    assert client.post(path, **signed('linear', payload)).json()['status'] == 'accepted'
    # A redelivery with an updated sending timestamp still refers to the same change.
    assert client.post(path, **signed('linear', payload | {'webhookTimestamp': int(time.time()*1000)}, delivery='another-header')).json()['status'] == 'duplicate'


def test_linear_label_trigger_requires_label_added_not_just_present():
    trigger = EventTrigger(provider='linear', event='issue.labeled', team_id=TEAM, label_id=LABEL)
    payload = example(trigger)
    assert normalize(trigger, payload)
    assert normalize(trigger, payload | {'updatedFrom': {'labelIds': [LABEL]}}) is None
    assert normalize(trigger, payload | {'updatedFrom': {'title': 'Previous title'}}) is None
    assert normalize(trigger, payload | {'data': payload['data'] | {'teamId': USER}}) is None


def test_events_queue_through_active_runs_and_hourly_limit(workspace):
    app, client = workspace
    a = configured(app, client, max_runs_per_hour=1)
    events = app.state.automations.events
    path = '/hooks/automations/' + a['id']
    for index in range(2):
        assert client.post(path, **signed('webhook', {'event': 'benchmark.ready', 'id': str(index)}, delivery=f'event-{index}')).json()['status'] == 'accepted'
    asyncio.run(events.dispatch())
    first = app.state.store.rows('SELECT * FROM runs')[0]
    asyncio.run(events.dispatch())
    queued = app.state.store.rows("SELECT * FROM automation_events WHERE status='pending'")[0]
    assert 'previous run' in queued['detail']
    complete(app, first['id'])
    asyncio.run(events.dispatch())
    assert 'hourly run limit' in app.state.store.rows("SELECT * FROM automation_events WHERE status='pending'")[0]['detail']
    before = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    app.state.store.execute('UPDATE automation_runs SET created_at=?', (before,))
    asyncio.run(events.dispatch())
    assert len(app.state.store.rows('SELECT * FROM runs')) == 2
    assert not app.state.store.rows("SELECT * FROM automation_events WHERE status='pending'")


@pytest.mark.parametrize('change', ['paused', 'edited', 'expired'])
def test_pending_events_recheck_pause_revision_and_expiry(workspace, change):
    app, client = workspace
    a = configured(app, client)
    client.post('/hooks/automations/' + a['id'], **signed('webhook', {'event': 'benchmark.ready'}))
    if change == 'paused':
        client.post(f"/api/automations/{a['id']}/state", json={'revision': 3, 'paused': True})
    elif change == 'edited':
        client.put('/api/automations/' + a['id'], json={'revision': 3, 'definition': a['definition']})
    else:
        app.state.store.execute("UPDATE automation_events SET expires_at='2000-01-01T00:00:00+00:00'")
    asyncio.run(app.state.automations.events.dispatch())
    assert not app.state.store.rows('SELECT * FROM runs')
    assert app.state.store.rows('SELECT status FROM automation_events')[0]['status'] == 'skipped'
    assert client.get('/api/automations').json()['automations'][0]['trigger']['deliveries'][0]['detail']


async def test_receipt_is_recoverable_after_checkpoint_and_dispatch_failure(workspace, monkeypatch):
    app, client = workspace
    a = configured(app, client)
    service = app.state.automations
    original = service.checkpoints.flush
    async def fail(): raise ConnectionError('simulated checkpoint outage')
    monkeypatch.setattr(service.checkpoints, 'flush', fail)
    with pytest.raises(ConnectionError):
        await service.events.accept(service.row(a['id']), 'durable-receipt', {'title': 'Persist me'})
    assert len(service.store.rows('SELECT * FROM automation_events')) == 1
    monkeypatch.setattr(service.checkpoints, 'flush', original)
    assert (await service.events.accept(service.row(a['id']), 'durable-receipt', {'title': 'Persist me'}))['status'] == 'duplicate'
    monkeypatch.setattr(service.checkpoints, 'flush', fail)
    with pytest.raises(ConnectionError): await service.events.dispatch()
    first = service.store.rows('SELECT * FROM runs')[0]
    assert service.store.rows('SELECT status FROM automation_events')[0]['status'] == 'pending'
    monkeypatch.setattr(service.checkpoints, 'flush', original)
    # A replacement dispatcher completes the existing receipt rather than rerunning inference.
    successor = Automations(service.store, service.settings, service.security, service.manager, service.connectors, service.environments, service.checkpoints)
    await successor.events.dispatch()
    assert len(service.store.rows('SELECT * FROM runs')) == 1
    assert service.store.rows('SELECT run_id FROM automation_runs')[0]['run_id'] == first['id']


def test_owner_access_and_webhook_secret_rotation(workspace):
    app, client = workspace
    owner = sign_in(app, client)
    a = configured(app, client)
    encrypted = app.state.store.rows('SELECT encrypted FROM automation_webhooks')[0]['encrypted']
    assert SECRET not in encrypted
    listing = client.get('/api/automations').text
    assert SECRET not in listing and encrypted not in listing
    assert client.post(f"/api/automations/{a['id']}/webhook", json={'revision': 3, 'secret': SECRET}, headers={'X-CSRF-Token':'wrong'}).status_code == 403
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.post(f"/api/automations/{a['id']}/webhook", json={'revision': 3, 'secret': SECRET}).status_code == 403
    assert client.post(f"/api/automations/{a['id']}/test-event", json={'payload': {}}).status_code == 403
    assert client.get('/api/automations').json()['automations'][0]['trigger']['providers'][0]['url'] == ''
    sign_in(app, client)
    assert client.post(f"/api/automations/{a['id']}/webhook", json={'revision': 3, 'secret': SECRET+'-rotated'}).status_code == 200
    assert app.state.automations.row(a['id'])['paused']
    assert client.post('/hooks/automations/' + a['id'], **signed('webhook', {'event':'benchmark.ready'})).status_code == 401
    # Switching providers discards the previous provider's credential.
    definition = a['definition'] | {'triggers':[{'id':'new','event': {'provider':'github','event':'issues.opened','repository':'BerriAI/litellm'}}]}
    assert client.put('/api/automations/' + a['id'], json={'revision':4,'definition':definition}).status_code == 200
    assert not app.state.store.rows('SELECT * FROM automation_webhooks')


def test_filter_preview_is_read_only_and_requires_configuration_before_enable(workspace):
    app, client = workspace
    a = create(client, event={'provider':'webhook','event':'benchmark.ready'})
    app.state.settings.temporal_enabled = True
    assert client.post(f"/api/automations/{a['id']}/state",json={'revision':1,'paused':False}).status_code == 409
    for name, match in [('benchmark.ready', True), ('other.event', False)]:
        result = client.post(f"/api/automations/{a['id']}/test-event",json={'payload':{'event':name}}).json()
        assert result['matches'] == match and not result['started']
    assert not app.state.store.rows('SELECT * FROM automation_events')
    assert not app.state.store.rows('SELECT * FROM runs')


@pytest.mark.parametrize('event', [
    {'provider':'github','event':'issues.opened'},
    {'provider':'github','event':'invalid','repository':'BerriAI/litellm'},
    {'provider':'linear','event':'issue.assigned','team_id':'invalid'},
    {'provider':'linear','event':'issue.labeled','team_id':TEAM,'label_id':'bad'},
    {'provider':'slack','event':'message.posted','channel_id':'D12345678','text_contains':'run'},
    {'provider':'slack','event':'message.posted'},
    {'provider':'webhook','event':'ready','max_runs_per_hour':0},
])
def test_event_definitions_require_narrow_valid_filters(workspace, event):
    _, client = workspace
    assert client.post('/api/automations', json={'definition':{'name':'Job','prompt':'Do work','event':event}}).status_code == 422


def test_slack_uses_existing_signature_team_sender_and_conversation_guards(slack_app):
    app, client, runs, messages = slack_app
    service = app.state.automations
    owner = app.state.store.identity({'method':'password','role':'admin'})
    a = service.save(Save(definition=Definition(name='Slack event', prompt='Investigate the event', mode='demo',
        event=EventTrigger(provider='slack',event='message.posted',channel_id='C12345678',text_contains='investigate',sender_type='human'))), owner)
    service.store.execute('UPDATE automations SET paused=0 WHERE id=?', (a['id'],))
    payload = slack_event(type='message',text='Please investigate the failure')
    assert client.post('/hooks/slack/events',json=payload).status_code == 401
    assert client.post('/hooks/slack/events',**signed_slack(payload | {'team_id':'T87654321'})).status_code == 200
    assert not service.store.rows('SELECT * FROM automation_events')
    for index, data in enumerate([{'thread_ts':'1790719000.123456'}, {'bot_id':'B123'}, {'subtype':'message_changed'}, {'channel':'D12345678'}]):
        changed = payload | {'event_id':f'EvSkip{index}', 'event':payload['event'] | data}
        assert client.post('/hooks/slack/events',**signed_slack(changed)).status_code == 200
    assert not service.store.rows('SELECT * FROM automation_events')
    assert client.post('/hooks/slack/events',**signed_slack(payload)).status_code == 200
    assert client.post('/hooks/slack/events',**signed_slack(payload)).status_code == 200
    assert len(service.store.rows('SELECT * FROM automation_events')) == 1
    assert not runs and not messages  # Event intake doesn't start an extra conversational reply.
