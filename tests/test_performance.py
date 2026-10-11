"""Real message receipts and bounded admin latency statistics on SQLite/Postgres."""
from datetime import datetime, timedelta, timezone

import pytest

from app import performance
from app.db import Store
from app.message_queue import MessageQueue
from test_spend import sign_in
from test_workspace import workspace  # noqa: F401


CURRENT = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def stamp(seconds):
    return (CURRENT + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path)
    yield value
    value.close()


def sample(store, offset=-60, *, mode='modal', model='openai/gpt-6-astra', status='completed', **timings):
    run = store.create_run('Private prompt must not reach statistics', '', mode, [], chat_enabled=True,
                           model=model, received_at=stamp(offset))
    message = store.messages(run['id'])[0]
    store.execute('UPDATE messages SET status=? WHERE id=?', (status, message['id']))
    for name, value in timings.items():
        assert name in {'picked_up_at', 'first_model_at', 'send_now_at', 'pickup_kind', 'send_now_kind'}
        store.execute(f'UPDATE performance_samples SET {name}=? WHERE message_id=?', (value, message['id']))
    return run, message


def test_distribution_coverage_and_real_milestone_definitions(store):
    sample(store, -60, picked_up_at=stamp(-59), first_model_at=stamp(-58), pickup_kind='turn')
    sample(store, -60, picked_up_at=stamp(-57), first_model_at=stamp(-56), pickup_kind='turn')
    sample(store, -60, status='queued')
    sample(store, -60, status='failed')
    sample(store, -60, status='completed', picked_up_at=stamp(-61), first_model_at=stamp(-61))
    result = performance.report(store, current=CURRENT)
    startup = result['metrics']['first_model']
    assert startup == {'total': 5, 'count': 2, 'average_ms': 3000, 'p50_ms': 3000,
                       'p95_ms': 4000, 'max_ms': 4000, 'pending': 1, 'incomplete': 1, 'invalid': 1}
    assert result['metrics']['after_pickup']['average_ms'] == 1000
    assert result['metrics']['after_pickup']['invalid'] == 1
    assert result['metrics']['queue_pickup']['average_ms'] == 2000
    assert 'Private prompt' not in str(result)
    assert sum(bucket['startup']['count'] for bucket in result['trend']) == 2


def test_old_queued_send_now_starts_at_request_not_submission_and_receipt_is_idempotent(store, monkeypatch):
    run = store.create_run('First', '', 'modal', [], chat_enabled=True, user_id='owner')
    active = store.claim_message(run['id'])
    store.update_run(run['id'], status='running')
    message, _ = store.enqueue_message(run['id'], 'Older queued input', 'follow', user_id='owner')
    store.execute('UPDATE messages SET created_at=? WHERE id=?', (stamp(-86400), message['id']))
    queue = MessageQueue(store)
    queue.change(run['id'], message['id'], 'owner', False, 0, 'steer', requested_at=stamp(-20))
    queue.change(run['id'], message['id'], 'owner', False, 1, 'steer', requested_at=stamp(-18))
    queue.live_control(run['id'], active['id'], [])
    assert performance.report(store, current=CURRENT)['metrics']['send_now']['pending'] == 1
    monkeypatch.setattr('app.message_queue.now', lambda: stamp(-5))
    queue.acknowledge(run['id'], active['id'], [message['id']])
    monkeypatch.setattr('app.message_queue.now', lambda: stamp(-1))
    queue.acknowledge(run['id'], active['id'], [message['id']])
    result = performance.report(store, current=CURRENT)
    assert result['metrics']['send_now']['average_ms'] == 15000
    assert result['metrics']['first_model']['total'] == 0
    assert result['recent'][0]['pickup_kind'] == 'steering'
    assert result['recent'][0]['received_at'] is None  # No invented historical startup data.


def test_submission_retry_and_second_model_call_do_not_restart_clocks(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client)
    monkeypatch.setattr('app.main.now', lambda: stamp(-10))
    body = {'prompt': 'First request', 'client_id': 'one-submission'}
    run = client.post('/api/runs', json=body).json()
    monkeypatch.setattr('app.main.now', lambda: stamp(-8))
    assert client.post('/api/runs', json=body).json()['id'] == run['id']
    rows = app.state.store.rows('SELECT * FROM performance_samples WHERE run_id=?', (run['id'],))
    assert len(rows) == 1 and rows[0]['received_at'] == stamp(-10)
    app.state.store.claim_message(run['id'])
    active = app.state.store.run(run['id'])
    # Background learning/compaction must not fill a missing agent-model milestone.
    app.state.spend.begin(active, active['model'])
    assert app.state.store.rows('SELECT first_model_at FROM performance_samples WHERE run_id=?', (run['id'],))[0]['first_model_at'] is None
    monkeypatch.setattr('app.spend.now', lambda: stamp(-6))
    app.state.spend.begin(active, active['model'], startup=True)
    monkeypatch.setattr('app.spend.now', lambda: stamp(-3))
    app.state.spend.begin(active, active['model'], startup=True)
    assert app.state.store.rows('SELECT first_model_at FROM performance_samples WHERE run_id=?', (run['id'],))[0]['first_model_at'] == stamp(-6)


@pytest.mark.parametrize('send_now,immediate,expected', [(True, False, 'explicit'), (False, True, 'automatic')])
def test_web_enqueue_context_records_send_intent_and_does_not_leak(store, send_now, immediate, expected):
    run = store.create_run('Initial', '', 'modal', [], chat_enabled=True)
    with performance.web_request(stamp(-10)):
        message, created = store.enqueue_message(run['id'], 'New follow-up', 'once', send_now=send_now, send_immediately=immediate)
    assert created and performance.request_received.get() is None
    row = store.rows('SELECT * FROM performance_samples WHERE message_id=?', (message['id'],))[0]
    assert row['received_at'] == row['send_now_at'] == stamp(-10)
    assert row['send_now_kind'] == expected
    assert performance.report(store, current=CURRENT, send_kind=expected)['metrics']['send_now']['pending'] == 1
    store.enqueue_message(run['id'], 'Internal input', 'internal')
    assert len(store.rows('SELECT * FROM performance_samples')) == 1


def test_report_filters_root_cloud_web_traffic_and_bounds_materialized_samples(store, monkeypatch):
    for offset in [-100, -90, -80]:
        sample(store, offset, first_model_at=stamp(offset + 1))
    demo, _ = sample(store, -10, mode='demo')
    child, _ = sample(store, -10)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (demo['id'], child['id']))
    deleted, _ = sample(store, -10)
    store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (stamp(-1), deleted['id']))
    injected, _ = sample(store, -10, picked_up_at=stamp(-5), pickup_kind='steering')
    monkeypatch.setattr(performance, 'SAMPLE_LIMIT', 2)
    generation = store.generation
    result = performance.report(store, current=CURRENT)
    assert store.generation == generation
    assert result['truncated'] == {'startup': True, 'send_now': False}
    assert result['metrics']['first_model']['count'] == 2
    assert {row['received_at'] for row in result['recent']} == {stamp(-90), stamp(-80)}
    assert performance.report(store, current=CURRENT, model='no-such-model')['recent'] == []
    assert performance.report(store, current=CURRENT, kind='follow_up')['recent'] == []


def test_report_requires_admin_and_offloads_reads(workspace, monkeypatch):
    app, client = workspace
    sign_in(app, client)
    original = performance.report
    def offloaded(*args, **kwargs):
        import asyncio
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return original(*args, **kwargs)
    monkeypatch.setattr(performance, 'report', offloaded)
    response = client.get('/api/admin/performance')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    for query in ['hours=0', 'hours=169', 'kind=secret', 'send_kind=unknown']:
        assert client.get('/api/admin/performance?' + query).status_code == 422
    sign_in(app, client, 'bob', 'bob@berri.ai')
    assert client.get('/api/admin/performance').status_code == 403
    client.cookies.clear()
    assert client.get('/api/admin/performance').status_code in {401, 403}


def test_failed_message_transaction_does_not_leave_a_timing_record(store):
    run = store.create_run('Initial', '', 'modal', [], chat_enabled=True)
    with pytest.raises(RuntimeError):
        with performance.web_request(stamp(-20)), store.connect() as conn:
            conn.begin_write()
            store.enqueue_message_in(conn, run['id'], 'Rolled back', 'rollback', restore_archived=True)
            raise RuntimeError('rollback')
    assert store.rows('SELECT * FROM performance_samples') == []


def test_follow_up_api_and_explicit_send_now_keep_first_receipt(workspace, monkeypatch):
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client)
    run = client.post('/api/runs', json={'prompt': 'Initial'}).json()
    store = app.state.store
    active = store.claim_message(run['id'])
    store.update_run(run['id'], status='running')
    monkeypatch.setattr('app.main.now', lambda: stamp(-30))
    body = {'content': 'Please check this', 'client_id': 'follow-up'}
    response = client.post(f"/api/runs/{run['id']}/messages", json=body)
    assert response.status_code == 202
    message = response.json()
    monkeypatch.setattr('app.main.now', lambda: stamp(-20))
    assert client.post(f"/api/runs/{run['id']}/messages", json=body).json()['created'] is False
    response = client.patch(f"/api/runs/{run['id']}/messages/{message['id']}", json={'action': 'steer', 'revision': 0})
    assert response.status_code == 200
    store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run['id'],))  # Synthetic cloud executor.
    store.finish_message(run['id'], active['id'], 'Finished')
    monkeypatch.setattr('app.db.now', lambda: stamp(-5))
    assert store.claim_message(run['id'])['id'] == message['id']
    result = performance.report(store, current=CURRENT)
    row = next(row for row in result['recent'] if row['message_id'] == message['id'])
    assert row['received_at'] == stamp(-30) and row['send_now_at'] == stamp(-20)
    assert row['send_now_ms'] == 15000 and row['pickup_kind'] == 'turn'


def test_actual_broker_request_records_agent_startup(workspace, monkeypatch):
    import httpx
    from app.security import digest
    app, client = workspace
    monkeypatch.setattr(app.state.manager, 'submit', lambda run: None)
    sign_in(app, client)
    monkeypatch.setattr('app.main.now', lambda: stamp(-10))
    run = client.post('/api/runs', json={'prompt': 'Synthetic timing check'}).json()
    app.state.store.claim_message(run['id'])
    app.state.store.execute("UPDATE runs SET mode='modal' WHERE id=?", (run['id'],))
    app.state.store.update_run(run['id'], status='running', token_hash=digest('capability'))
    app.state.settings.litellm_api_key = 'local-test'
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    real = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'choices': [{'message': {'role': 'assistant', 'content': 'Fixture reply'}, 'finish_reason': 'stop'}]}))
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=transport, **kw))
    monkeypatch.setattr('app.spend.now', lambda: stamp(-3))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions", headers={'Authorization': 'Bearer capability'}, json={'messages': []})
    assert response.status_code == 200
    assert performance.report(app.state.store, current=CURRENT)['metrics']['first_model']['average_ms'] == 7000


def test_report_reads_committed_data_while_another_transaction_is_writing(store):
    from concurrent.futures import ThreadPoolExecutor
    sample(store, first_model_at=stamp(-58))
    with store.connect() as writer, ThreadPoolExecutor(max_workers=1) as reader:
        writer.begin_write()
        writer.execute("UPDATE performance_samples SET first_model_at=NULL")
        result = reader.submit(performance.report, store, current=CURRENT).result(timeout=3)
        assert result['metrics']['first_model']['average_ms'] == 2000
