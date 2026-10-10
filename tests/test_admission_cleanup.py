"""Real admission/state/leases with controllable sandbox termination delays."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest

from app.config import Settings
from app.db import Store
from app.durable_runner import DurableRunner
from test_postgres_runtime import target  # noqa: F401
from test_runtime_scaling import cluster  # noqa: F401


@pytest.fixture(params=['sqlite', 'postgres', 'workers'])
def admissions(request, tmp_path):
    options = {}
    if request.param != 'sqlite':
        url, schema = request.getfixturevalue('target')
        options.update(moyai_database_url=url, moyai_database_schema=schema,
                       moyai_database_initialize=True)
    settings = Settings(_env_file=None, data_dir=tmp_path, max_concurrent_runs=2,
        session_secret='admission-cleanup-test-key', encryption_key=Fernet.generate_key().decode(), temporal_enabled=True,
        moyai_runtime_role='coordinator' if request.param == 'workers' else 'standalone',
        object_storage_bucket='synthetic-only', **options)
    stores = []

    def manager(role):
        config = settings.model_copy(update={'moyai_runtime_role': role})
        store = Store(tmp_path, database_url=config.moyai_database_url,
            database_schema=config.moyai_database_schema, database_initialize=True,
            application_instance=True, runtime_role=role, runtime_settings=config)
        stores.append(store)
        if store.database:
            store.database.configure_runtime(config)
        runner = DurableRunner(store, config)
        runner.step = AsyncMock(return_value=True)
        return runner

    try:
        first = manager(settings.moyai_runtime_role)
        if request.param == 'workers':
            first, second = manager('worker'), manager('worker')
        else:
            second = first
        yield first, second
    finally:
        for store in reversed(stores):
            store.close()


def session(manager, phase=None, *, order=1):
    run = manager.store.create_run('Synthetic admission cleanup', '', 'demo', [],
                                   chat_enabled=phase is None)
    manager.submit(run)
    if phase:
        manager.save(run['id'], {'phase': phase, 'sandbox_id': 'synthetic-' + run['id'],
                                'idle_until': order, 'machine_started': 1})
    return run['id']


def occupied(manager):
    return len(manager.store.rows(f'SELECT run_id FROM durable_sessions WHERE {manager.occupied_session}'))


async def test_slow_cleanup_does_not_block_other_admissions(admissions):
    first, second = admissions
    slow, fast = session(first, 'warm'), session(first, 'warm', order=2)
    waiting, independent, overflow = [session(first) for _ in range(3)]
    entered, release = asyncio.Event(), asyncio.Event()
    cleaned = []

    async def cleanup(state, run_id):
        if run_id == slow:
            entered.set()
            await release.wait()
        cleaned.append(run_id)

    first.cleanup = second.cleanup = cleanup
    task = asyncio.create_task(first.advance(waiting))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert first.state(slow)['phase'] == 'warm_cleanup'
        assert occupied(first) == 2  # Termination has not freed this reservation.
        assert await asyncio.wait_for(second.advance(independent), 2) is True
        assert cleaned == [fast]
        assert not task.done()
        assert occupied(first) == 2
        assert await asyncio.wait_for(second.advance(overflow), 2) == 'capacity'
        assert first.store.has_queued_messages(overflow)
    finally:
        release.set()
        await asyncio.wait_for(task, 3)
    assert first.state(waiting)['phase'] == first.state(independent)['phase'] == 'prepare'
    assert occupied(first) == 2
    assert cleaned == [fast, slow]


async def test_capacity_is_rechecked_when_another_session_takes_the_reclaimed_slot(admissions):
    first, second = admissions
    session(first, 'warm')
    session(first, 'monitor')
    waiting, independent = session(first), session(first)
    first.cleanup = AsyncMock()
    reclaimed, resume = asyncio.Event(), asyncio.Event()
    make_capacity = first.make_capacity

    async def pause_after_cleanup(run_id, **kwargs):
        available = await make_capacity(run_id, **kwargs)
        if run_id == waiting and available:
            reclaimed.set()
            await resume.wait()
        return available

    first.make_capacity = pause_after_cleanup
    task = asyncio.create_task(first.advance(waiting))
    try:
        await asyncio.wait_for(reclaimed.wait(), 3)
        assert occupied(first) == 1
        assert await asyncio.wait_for(second.advance(independent), 2) is True
    finally:
        resume.set()
        result = await asyncio.wait_for(task, 3)
    assert result == 'capacity'
    assert first.store.has_queued_messages(waiting)
    assert first.state(waiting) == {}
    assert occupied(first) == 2


async def test_cancelled_cleanup_retains_reservation_and_is_recovered(admissions):
    first, _ = admissions
    victim = session(first, 'warm')
    session(first, 'monitor')
    waiting = session(first)
    entered = asyncio.Event()

    async def cleanup(state, run_id):
        entered.set()
        await asyncio.Event().wait()

    first.cleanup = cleanup
    task = asyncio.create_task(first.advance(waiting))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert first.state(victim)['phase'] == 'warm_cleanup'
    assert first.state(victim)['sandbox_id'] == 'synthetic-' + victim
    assert occupied(first) == 2 and first.store.has_queued_messages(waiting)
    if first.coordinated_database:
        assert not first.store.rows('SELECT 1 FROM runtime_leases')
    replacement = DurableRunner(first.store, first.settings)
    replacement.cleanup = AsyncMock()
    replacement.step = AsyncMock(return_value=True)
    assert await replacement.advance(waiting) is True
    replacement.cleanup.assert_awaited_once()
    assert replacement.state(victim)['phase'] == 'idle'
    assert occupied(replacement) == 2
    assert len(first.store.rows("SELECT id FROM messages WHERE run_id=? AND status='running'", (waiting,))) == 1


async def test_killed_cleanup_worker_retains_slot_until_replacement_finishes(cluster):
    coordinator, settings, open_store = cluster
    manager = DurableRunner(coordinator, settings)
    victim = session(manager, 'warm')
    session(manager, 'monitor')
    session(manager, 'monitor')
    waiting = session(manager)
    script = '''
import asyncio,json,sys
from app.config import Settings
from app.durable_runner import DurableRunner
from app import runtime_coordination
from scripts.runtime_capacity_probe import open_store

payload = json.loads(sys.stdin.readline())
settings = Settings(_env_file=None, **payload['settings'])
original_lease = runtime_coordination.lease
def short_lease(*args, **kwargs):
    return original_lease(*args, **dict(kwargs, ttl=.5))
runtime_coordination.lease = short_lease
store = open_store(settings)
manager = DurableRunner(store, settings)
async def cleanup(state, run_id):
    print('cleanup-started', flush=True)
    await asyncio.Event().wait()
manager.cleanup = cleanup
asyncio.run(manager.advance(payload['run_id']))
'''
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', script,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    options = settings.model_copy(update={'moyai_runtime_role': 'worker'}).model_dump(mode='json')
    try:
        process.stdin.write((json.dumps({'settings': options, 'run_id': waiting}) + '\n').encode())
        await process.stdin.drain()
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b'cleanup-started\n'
        assert manager.state(victim)['phase'] == 'warm_cleanup'
        assert occupied(manager) == 3
        assert not coordinator.rows("SELECT 1 FROM runtime_leases WHERE name='sandbox-admission'")
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
    store, options = open_store()
    replacement = DurableRunner(store, options)
    replacement.cleanup = AsyncMock()
    replacement.step = AsyncMock(return_value=True)
    async with asyncio.timeout(5):
        while await replacement.advance(waiting) == 'capacity':
            assert occupied(replacement) == 3
            await asyncio.sleep(.05)
    assert replacement.state(victim)['phase'] == 'idle'
    assert replacement.state(waiting)['phase'] == 'prepare'
    assert occupied(replacement) == 3
    replacement.cleanup.assert_awaited_once()
    assert not store.rows('SELECT 1 FROM runtime_leases')
