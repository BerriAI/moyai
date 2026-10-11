"""Real database contention, bounded wake work and cancellation ownership."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio.testing import ActivityEnvironment

from app.runtime_coordination import lease
from app.temporal_runtime import TemporalRunManager
from scripts.admission_benchmark import populate
from scripts.runtime_startup_probe import admission_probe
from test_runtime_scaling import cluster, target  # noqa: F401


async def test_successful_full_wake_batches_drain_before_idle_poll(cluster, monkeypatch):
    store, settings, _ = cluster
    settings.temporal_dispatch_batch_size = 2
    manager = TemporalRunManager(store, settings)
    runs = [store.create_run('Wake burst', '', 'demo', []) for _ in range(5)]
    for run in runs:
        manager.submit(run)
    client = SimpleNamespace(start_workflow=AsyncMock())
    manager.connect_temporal = AsyncMock(return_value=client)
    async def idle():
        manager.closing = True

    monkeypatch.setattr(manager, 'wait_for_dispatch', idle)
    await asyncio.wait_for(manager.serve(), 5)
    assert client.start_workflow.await_count == 5
    assert not store.rows('SELECT 1 FROM durable_sessions WHERE revision>delivered')


async def test_failed_wake_batch_backs_off_and_retries_only_unacknowledged_rows(cluster, monkeypatch):
    store, settings, _ = cluster
    settings.temporal_dispatch_batch_size = 2
    manager = TemporalRunManager(store, settings)
    runs = [store.create_run('Retry wake', '', 'demo', []) for _ in range(2)]
    for run in runs:
        manager.submit(run)
    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=[ConnectionError('private'), None]))
    manager.connect_temporal = AsyncMock(return_value=client)
    sleep = asyncio.sleep
    idle_count = 0

    async def idle(seconds):
        nonlocal idle_count
        if seconds == 2:
            idle_count += 1
            manager.closing = True
        else:
            await sleep(seconds)

    monkeypatch.setattr(asyncio, 'sleep', idle)
    await asyncio.wait_for(manager.serve(), 5)
    assert idle_count == 1
    assert len(store.rows('SELECT 1 FROM durable_sessions WHERE revision>delivered')) == 1
    client.start_workflow = AsyncMock()
    assert await manager.dispatch() is False
    assert client.start_workflow.await_count == 1
    assert not store.rows('SELECT 1 FROM durable_sessions WHERE revision>delivered')


def test_wake_poll_uses_pending_index_with_large_delivered_history(cluster):
    store, settings, _ = cluster
    manager = TemporalRunManager(store, settings)
    populate(store, history=30000, occupied=0)
    run = store.create_run('One pending wake', '', 'demo', [])
    manager.submit(run)
    store.execute('ANALYZE durable_sessions')
    query = 'SELECT run_id,revision FROM durable_sessions WHERE revision>delivered ORDER BY run_id LIMIT ?'
    plan = store.rows('EXPLAIN (ANALYZE, FORMAT JSON) ' + query, (200,))[0]['QUERY PLAN'][0]['Plan']
    scans = []

    def visit(node):
        if node.get('Relation Name') == 'durable_sessions':
            scans.append(node)
        for child in node.get('Plans', []):
            visit(child)

    visit(plan)
    assert scans and all(n.get('Index Name') == 'idx_durable_sessions_wake' for n in scans)
    assert sum(n['Actual Rows'] + n.get('Rows Removed by Filter', 0) for n in scans) == 1
    assert store.rows(query, (200,)) == [{'run_id': run['id'], 'revision': 1}]


async def test_admission_writer_wait_preserves_worker_responsiveness(cluster):
    store, settings, _ = cluster
    manager = TemporalRunManager(store, settings)
    run = store.create_run('Contended admission', '', 'demo', [], chat_enabled=True)
    manager.submit(run)
    result = await admission_probe(manager, run['id'])
    assert result['admission_ms'] >= 500
    assert result['max_event_loop_lag_ms'] < 300


async def test_activity_initial_read_does_not_block_heartbeats_during_pool_wait(cluster):
    coordinator, _, open_store = cluster
    store, settings = open_store(moyai_database_pool_size=1)
    manager = TemporalRunManager(store, settings)
    run = coordinator.create_run('Contended activity', '', 'demo', [], chat_enabled=True)
    manager.submit(run)
    manager.advance = AsyncMock(return_value=True)
    locked = threading.Event()

    def hold_connection():
        with store.connect():
            locked.set()
            time.sleep(.7)

    holder = asyncio.create_task(asyncio.to_thread(hold_connection))
    await asyncio.to_thread(locked.wait, 5)
    assert locked.is_set()
    task = asyncio.create_task(ActivityEnvironment().run(manager.advance_session, run['id']))
    started = time.monotonic()
    await asyncio.sleep(.1)
    assert time.monotonic() - started < .4
    assert not task.done()
    await asyncio.gather(holder, task)
    manager.advance.assert_awaited_once()


async def test_cancelled_admission_keeps_lease_until_inflight_claim_finishes(cluster, monkeypatch):
    store, settings, open_store = cluster
    other, _ = open_store()
    manager = TemporalRunManager(store, settings)
    run = store.create_run('Cancelled admission', '', 'demo', [], chat_enabled=True)
    manager.submit(run)
    manager.step = AsyncMock(return_value=True)
    entered, release = threading.Event(), threading.Event()
    begin = manager.begin_turn

    def delayed_begin(*args):
        entered.set()
        assert release.wait(5)
        return begin(*args)

    monkeypatch.setattr(manager, 'begin_turn', delayed_begin)
    task = asyncio.create_task(manager.advance(run['id']))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(.05)
        assert not task.done()
        async with lease(other.database, 'sandbox-admission', wait=False) as acquired:
            assert not acquired
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert manager.state(run['id'])['phase'] == 'prepare'
    assert not store.rows('SELECT 1 FROM runtime_leases')
    assert await manager.advance(run['id']) is True
    assert len(store.rows("SELECT id FROM messages WHERE run_id=? AND status='running'", (run['id'],))) == 1
