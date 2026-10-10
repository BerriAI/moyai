"""Actual PostgreSQL ownership and concurrency, without sandbox/model spend."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import Context
import subprocess
import sys
import threading
import time
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.database import DatabaseError
from app.db import Store, database
from app.durable_runner import DurableRunner
from app.main import create_app
from app.model_slots import ModelSlots
from app.runtime_coordination import LeaseLost, lease
from test_postgres_runtime import target  # noqa: F401


@pytest.fixture
def cluster(tmp_path, target):
    url, schema = target
    settings = Settings(_env_file=None, data_dir=tmp_path / 'coordinator',
        moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
        moyai_runtime_role='coordinator', temporal_enabled=True, temporal_tls=False,
        object_storage_bucket='synthetic-cluster-test', session_secret='cluster-test-stable-key',
        encryption_key=Fernet.generate_key().decode(), max_concurrent_runs=3,
        demo_step_seconds=.01, sandbox_idle_seconds=0, session_titles_enabled=False)
    stores = []

    def open_store(role='worker', **overrides):
        options = settings.model_copy(update={'data_dir': tmp_path / str(len(stores)),
                                             'moyai_runtime_role': role, **overrides})
        store = Store(options.data_dir, database_url=url, database_schema=schema,
                      database_initialize=True, application_instance=True, runtime_role=role,
                      database_pool_size=options.moyai_database_pool_size,
                      max_pending_runs=options.max_pending_runs, runtime_settings=options)
        stores.append(store)
        store.database.configure_runtime(options)
        return store, options

    coordinator, _ = open_store('coordinator')
    try:
        yield coordinator, settings, open_store
    finally:
        for store in reversed(stores):
            store.close()


def test_capacity_can_exceed_old_ceilings_without_changing_default_budgets():
    settings = Settings(_env_file=None, max_concurrent_runs=3000,
                        max_pending_runs=20000, max_concurrent_model_requests=3000,
                        moyai_database_pool_size=32, temporal_worker_activities=256)
    assert (settings.max_concurrent_runs, settings.max_pending_runs, settings.max_concurrent_model_requests) == (3000, 20000, 3000)
    defaults = Settings(_env_file=None)
    assert (defaults.max_concurrent_runs, defaults.max_pending_runs, defaults.max_concurrent_model_requests) == (100, 1000, 8)
    with pytest.raises(ValueError, match='PostgreSQL and Temporal'):
        Settings(_env_file=None, moyai_runtime_role='worker')
    with pytest.raises(ValueError, match='shared object storage'):
        Settings(_env_file=None, moyai_runtime_role='worker', temporal_enabled=True,
                 moyai_database_url='postgresql://localhost/test')


def test_multiple_workers_share_one_coordinator_and_reject_configuration_drift(cluster):
    coordinator, settings, open_store = cluster
    first, _ = open_store()
    second, _ = open_store(moyai_database_pool_size=24)
    assert second.database.pool.max_size == 24
    assert first.rows('SELECT 1 AS value') == [{'value': 1}]
    with pytest.raises(DatabaseError, match='Another coordinator'):
        open_store('coordinator')
    with pytest.raises(DatabaseError, match='Another Moyai instance'):
        open_store('standalone')
    with pytest.raises(DatabaseError, match='same shared runtime configuration'):
        open_store(max_concurrent_runs=3000)
    with pytest.raises(DatabaseError, match='Drain and stop'):
        coordinator.database.configure_runtime(settings.model_copy(update={'max_concurrent_runs': 4}))
    assert first.rows('SELECT 1 AS value') == [{'value': 1}]


async def test_execution_lease_is_exclusive_and_renewed_without_holding_pool_connections(cluster):
    coordinator, _, open_store = cluster
    second, _ = open_store(moyai_database_pool_size=1)
    async with lease(coordinator.database, 'test-owner', ttl=.3):
        await asyncio.sleep(.5)
        async def compete():
            async with lease(second.database, 'test-owner', wait=False) as acquired:
                assert not acquired
        await asyncio.create_task(compete())
        # A different execution still proceeds, even with a one-connection pool.
        async with lease(second.database, 'different-owner', wait=False) as acquired:
            assert acquired
            assert second.rows('SELECT 1 AS value')[0]['value'] == 1
    async with lease(second.database, 'test-owner', wait=False) as acquired:
        assert acquired
    assert not coordinator.rows('SELECT * FROM runtime_leases')


async def test_nonwaiting_eviction_skips_owner_write_and_reclaims_another_session(cluster):
    coordinator, settings, open_store = cluster
    second, options = open_store()
    manager = DurableRunner(second, options)
    manager.cleanup = AsyncMock()
    runs = [coordinator.create_run('Warm capacity test', '', 'demo', []) for _ in range(3)]
    for index, run in enumerate(runs):
        manager.submit(run)
        manager.save(run['id'], {'phase': 'warm', 'sandbox_id': 'synthetic', 'idle_until': index + 1})
    busy, reclaimable, _ = [run['id'] for run in runs]
    assert not manager.has_capacity()

    async def evict():
        async with manager.admission('incoming-session') as available:
            assert available

    async with lease(coordinator.database, 'session:' + busy):
        with coordinator.connect(write_scope=busy) as conn:
            # The actual owner write holds FOR SHARE on its lease until commit.
            conn.execute('UPDATE runs SET summary=? WHERE id=?', ('Still writing', busy))
            task = asyncio.create_task(evict(), context=Context())
            await asyncio.wait_for(task, 1)
            assert manager.state(busy)['phase'] == 'warm'
            assert manager.state(reclaimable)['phase'] == 'idle'
            # Eviction has released global admission even while the first
            # owner's write is still open. The next worker can admit work.
            async with lease(second.database, 'sandbox-admission', wait=False) as acquired:
                assert acquired
    manager.cleanup.assert_awaited_once()


def test_lease_creation_contention_returns_without_waiting_for_the_first_commit(cluster, monkeypatch):
    coordinator, _, open_store = cluster
    second, _ = open_store()
    inserted, commit = threading.Event(), threading.Event()
    connect = coordinator.database.connect

    @contextmanager
    def hold_commit(**kwargs):
        with connect(**kwargs) as conn:
            yield conn
            inserted.set()
            assert commit.wait(5)

    monkeypatch.setattr(coordinator.database, 'connect', hold_commit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(coordinator.database.acquire_lease, 'new-lease', 'first', 30)
        try:
            assert inserted.wait(2)
            second_attempt = pool.submit(second.database.acquire_lease, 'new-lease', 'second', 30)
            assert second_attempt.result(timeout=1) is False
        finally:
            commit.set()
        assert first.result(timeout=2)
    assert second.rows("SELECT token FROM runtime_leases WHERE name='new-lease'") == [{'token': 'first'}]


def test_locked_expired_lease_is_skipped_then_reclaimed_after_commit(cluster):
    coordinator, _, open_store = cluster
    second, _ = open_store()
    assert coordinator.database.acquire_lease('expiring-write', 'old', .1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with coordinator.database.connect() as conn:
            conn.raw.execute("SELECT 1 FROM runtime_leases WHERE name='expiring-write' FOR SHARE")
            time.sleep(.15)
            assert second.rows("SELECT 1 FROM runtime_leases WHERE name='expiring-write' AND expires_at<=clock_timestamp()")
            attempt = pool.submit(second.database.acquire_lease, 'expiring-write', 'new', 30)
            assert attempt.result(timeout=1) is False
            assert second.rows("SELECT token FROM runtime_leases WHERE name='expiring-write'") == [{'token': 'old'}]
    assert second.database.acquire_lease('expiring-write', 'new', 30)
    assert second.rows("SELECT token FROM runtime_leases WHERE name='expiring-write'") == [{'token': 'new'}]


async def test_expired_owner_is_fenced_before_any_write_even_before_heartbeat_notices(cluster):
    coordinator, _, open_store = cluster
    second, _ = open_store()
    run = coordinator.create_run('Synthetic task', '', 'demo', [])
    async with lease(coordinator.database, 'session:' + run['id']):
        with second.database.pool.connection() as conn:
            conn.execute("UPDATE runtime_leases SET expires_at=clock_timestamp()-interval '1 second'")
        assert await asyncio.to_thread(second.database.acquire_lease, 'session:' + run['id'], 'successor', 30)
        with pytest.raises(DatabaseError, match='Execution ownership expired'):
            await database(coordinator.update_run, run['id'], summary='stale write')
    assert second.run(run['id'])['summary'] == ''
    assert second.rows('SELECT token FROM runtime_leases')[0]['token'] == 'successor'
    second.database.release_lease('session:' + run['id'], 'successor')


async def test_lease_loss_cancels_ongoing_execution(cluster):
    coordinator, _, open_store = cluster
    second, _ = open_store()
    entered = asyncio.Event()

    async def execute():
        async with lease(coordinator.database, 'lost-execution', ttl=.3):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(execute())
    await entered.wait()
    with second.database.pool.connection() as conn:
        conn.execute("UPDATE runtime_leases SET token='replacement'")
    with pytest.raises(LeaseLost):
        await asyncio.wait_for(task, 2)


def test_killed_process_releases_ownership_after_database_lease_expiry(cluster):
    coordinator, settings, _ = cluster
    script = '''
import sys,time
from app.database import PostgresDatabase
from app.config import Settings
settings=Settings.model_validate_json(sys.argv[3]).model_copy(update={'moyai_runtime_role':'worker'})
db=PostgresDatabase(sys.argv[1],sys.argv[2],application_instance=True,runtime_role='worker',runtime_settings=settings)
assert db.acquire_lease('crash-probe','old-process',.5)
print('owned',flush=True)
time.sleep(30)
'''
    process = subprocess.Popen([sys.executable, '-c', script, settings.moyai_database_url,
                                settings.moyai_database_schema, settings.model_dump_json()], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'owned'
        assert not coordinator.database.acquire_lease('crash-probe', 'replacement', 30)
    finally:
        process.kill()
        process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while not coordinator.database.acquire_lease('crash-probe', 'replacement', 30):
        assert time.monotonic() < deadline
        time.sleep(.05)
    coordinator.database.release_lease('crash-probe', 'replacement')


async def test_shared_admission_is_bounded_across_independent_worker_stores(cluster):
    coordinator, _, open_store = cluster
    managers = []
    for _ in range(2):
        store, settings = open_store()
        manager = DurableRunner(store, settings)
        manager.step = AsyncMock(return_value=True)
        managers.append(manager)
    runs = [coordinator.create_run('Synthetic work', '', 'demo', [], chat_enabled=True) for _ in range(12)]
    for run in runs:
        managers[0].submit(run)
    results = await asyncio.gather(*(managers[i % 2].advance(run['id']) for i, run in enumerate(runs)))
    assert results.count('capacity') == 9
    assert sum(managers[0].state(run['id']).get('phase') == 'prepare' for run in runs) == 3
    assert not coordinator.rows('SELECT * FROM runtime_leases')


async def test_competing_workers_do_not_advance_one_session_twice(cluster):
    coordinator, _, open_store = cluster
    managers = []
    for _ in range(2):
        store, settings = open_store()
        managers.append(DurableRunner(store, settings))
    run = coordinator.create_run('One turn', '', 'demo', [], chat_enabled=True)
    managers[0].submit(run)
    concurrent = maximum = 0

    async def step(run_id, state):
        nonlocal concurrent, maximum
        concurrent += 1
        maximum = max(maximum, concurrent)
        await asyncio.sleep(.1)
        concurrent -= 1
        return True

    for manager in managers:
        manager.step = step
    await asyncio.gather(*(manager.advance(run['id']) for manager in managers))
    assert maximum == 1
    assert len(coordinator.rows("SELECT * FROM messages WHERE status='running'")) == 1


async def test_scoped_session_writes_overlap_but_legacy_writer_remains_exclusive(cluster):
    coordinator, _, open_store = cluster
    second, _ = open_store()
    first_run = coordinator.create_run('A', '', 'demo', [])
    other_run = coordinator.create_run('B', '', 'demo', [])
    with coordinator.connect(write_scope=first_run['id']) as conn:
        conn.execute('UPDATE runs SET summary=? WHERE id=?', ('A', first_run['id']))
        await asyncio.wait_for(database(second.update_run, other_run['id'], summary='B'), 2)
        same = asyncio.create_task(database(second.update_run, first_run['id'], summary='after'))
        legacy = asyncio.create_task(database(second.execute, 'UPDATE runs SET error=? WHERE id=?', ('legacy', other_run['id'])))
        await asyncio.sleep(.1)
        assert not same.done() and not legacy.done()
    await asyncio.gather(same, legacy)
    assert coordinator.run(first_run['id'])['summary'] == 'after'


async def test_three_thousand_model_tasks_respect_configured_capacity_and_cancel_cleanly():
    slots = ModelSlots(256)
    release = asyncio.Event()
    admitted = asyncio.Event()

    async def request():
        async with slots:
            if slots.active == 256:
                admitted.set()
            await release.wait()

    tasks = [asyncio.create_task(request()) for _ in range(3000)]
    await asyncio.wait_for(admitted.wait(), 3)
    assert slots.snapshot()['active'] == 256
    assert slots.snapshot()['waiting'] == 2744
    for task in tasks[256:]:
        task.cancel()
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert slots.snapshot()['active'] == slots.snapshot()['waiting'] == 0


def test_worker_startup_leaves_coordinator_jobs_untouched_and_rejects_http(cluster, monkeypatch):
    coordinator, settings, _ = cluster
    from app.temporal_runtime import TemporalRunManager
    from app.context_maintenance import ContextMaintenance
    recovery = AsyncMock()
    monkeypatch.setattr(TemporalRunManager, 'recover', recovery)
    monkeypatch.setattr(ContextMaintenance, 'recover', lambda self: pytest.fail('Worker interrupted coordinator compaction'))
    options = settings.model_copy(update={'moyai_runtime_role': 'worker', 'data_dir': settings.data_dir / 'worker-api'})
    app = create_app(options)
    with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 50000)) as client:
        assert client.get('/health').status_code == 503
        assert client.get('/api/config').status_code == 404
        assert client.post('/broker/example/control', json={}).status_code == 404
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/computer/example'):
                pass
    recovery.assert_awaited_once()


async def test_real_temporal_workers_finish_after_one_worker_restarts(cluster):
    from temporalio.testing import WorkflowEnvironment
    from temporalio.client import Client
    from temporalio.runtime import Runtime, TelemetryConfig
    from app.temporal_runtime import TemporalRunManager
    from test_temporal_integration import eventually

    coordinator, settings, open_store = cluster
    apps = []
    # Release the fixture's coordinator process fence; create the complete app.
    coordinator.close()
    control = create_app(settings)
    apps.append(control)
    for number in range(2):
        apps.append(create_app(settings.model_copy(update={'moyai_runtime_role': 'worker',
                    'data_dir': settings.data_dir / f'worker-{number}'})))
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        for app in apps:
            # Real deployments have independent processes/SDK runtimes. One
            # SDK runtime refuses overlapping workers on the same task queue.
            client = await Client.connect(env.client.service_client.config.target_host,
                runtime=Runtime(telemetry=TelemetryConfig()), identity=app.state.manager.identity)
            app.state.manager.connect_temporal = AsyncMock(return_value=client)
        try:
            # Only managers run: external periodic services are unnecessary here.
            for app in apps:
                await app.state.manager.recover()
            await asyncio.wait_for(asyncio.gather(*(app.state.manager.ready.wait() for app in apps)), 15)
            assert control.state.manager.worker is None
            first_started = asyncio.Event()
            original_step = apps[1].state.manager.step

            async def pause_first_activity(run_id, state):
                first_started.set()
                # Shutdown must detach this activity; the surviving worker
                # resumes its persisted reservation through Temporal retry.
                await asyncio.Event().wait()
                return await original_step(run_id, state)

            apps[1].state.manager.step = pause_first_activity
            runs = []
            for i in range(8):
                run = control.state.store.create_run('Demo ' + str(i), '', 'demo', [], chat_enabled=True)
                control.state.manager.submit(run)
                runs.append(run)
            await asyncio.wait_for(first_started.wait(), 15)
            await apps[1].state.manager.shutdown()
            await eventually(lambda: all(control.state.store.run(run['id'])['status'] == 'idle' for run in runs), seconds=45)
            for run in runs:
                messages = control.state.store.messages(run['id'])
                assert sum(message['role'] == 'assistant' for message in messages) == 1
                await env.client.get_workflow_handle('moyai-session-' + run['id']).terminate('Test complete')
            apps[2].state.store.database.owner.close()
            await eventually(lambda: not apps[2].state.manager.ready.is_set()
                             and apps[2].state.manager.worker_task.done(), seconds=10)
        finally:
            for app in reversed(apps):
                await app.state.manager.shutdown()
                await app.state.manager.modal_clients.close()
                app.state.store.close()


def test_distributed_startup_refuses_unmigrated_local_artifacts(cluster):
    from app.runtime_coordination import require_shared_artifacts
    coordinator, _, _ = cluster
    root = coordinator.artifacts.root
    root.mkdir(parents=True)
    (root / 'legacy.zip').write_bytes(b'local-only')
    with pytest.raises(ValueError, match='Migrate and verify'):
        require_shared_artifacts(coordinator)
    coordinator.execute('INSERT INTO artifact_objects VALUES(?,?,?,?,?)',
                        ('legacy.zip', 'synthetic-reference', 10, 'synthetic-hash', ''))
    require_shared_artifacts(coordinator)
