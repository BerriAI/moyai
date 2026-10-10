"""Startup guarantees across real durable state and controllable provider waits."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db import database
from test_durable import durable, drive  # noqa: F401


async def test_committed_input_interrupts_dispatch_wait_from_writer_thread(durable):
    manager, _, run_id = durable
    manager.settings.moyai_runtime_role = 'coordinator'
    client = SimpleNamespace(start_workflow=AsyncMock())
    manager.connect_temporal = AsyncMock(return_value=client)
    idle = asyncio.Event()
    wait = manager.wait_for_dispatch

    async def waiting():
        idle.set()
        await wait()

    manager.wait_for_dispatch = waiting
    task = asyncio.create_task(manager.serve())
    try:
        await asyncio.wait_for(idle.wait(), 2)
        client.start_workflow.reset_mock()
        started = time.monotonic()
        await database(manager.submit, manager.store.run(run_id))
        async with asyncio.timeout(.5):
            while not client.start_workflow.await_count:
                await asyncio.sleep(.005)
        assert time.monotonic() - started < .5
    finally:
        manager.closing = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rollback_does_not_notify_or_publish_wake(durable):
    manager, _, run_id = durable
    manager.dispatch_loop = asyncio.get_running_loop()
    before = manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run_id,))
    with pytest.raises(RuntimeError):
        with manager.store.connect() as conn:
            manager.submit_in(conn, manager.store.run(run_id))
            assert not manager.dispatch_wake.is_set()
            raise RuntimeError('abort transaction')
    await asyncio.sleep(0)
    assert not manager.dispatch_wake.is_set()
    assert manager.store.rows('SELECT revision FROM durable_sessions WHERE run_id=?', (run_id,)) == before


async def test_wake_during_dispatch_is_not_cleared_before_wait(durable):
    manager, _, run_id = durable
    manager.dispatch_loop = asyncio.get_running_loop()
    run = manager.store.run(run_id)

    async def deliver(*args, **kwargs):
        await database(manager.submit, run)

    manager.temporal = SimpleNamespace(start_workflow=AsyncMock(side_effect=deliver))
    assert not await manager.dispatch()
    await asyncio.wait_for(manager.wait_for_dispatch(), .3)
    assert manager.store.rows('SELECT 1 FROM durable_sessions WHERE revision>delivered')


async def test_runtime_database_wait_does_not_stop_other_tasks(durable, monkeypatch):
    manager, _, run_id = durable
    await drive(manager, run_id, phase='launch')
    entered, release = threading.Event(), threading.Event()
    original = manager.running_status

    def contended(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(manager, 'running_status', contended)
    task = asyncio.create_task(manager.advance(run_id))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        started = time.monotonic()
        await asyncio.sleep(.05)
        assert time.monotonic() - started < .3
        assert not task.done()
    finally:
        release.set()
        await task


def enable_pool(manager):
    manager.settings.temporal_enabled = True
    manager.settings.sandbox_prepared_pool_size = 1
    manager.settings.max_concurrent_runs = 2


async def test_prepared_machine_is_clean_claimed_once_and_counts_toward_capacity(durable):
    manager, cloud, run_id = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    assert len(cloud.machines) == 1 and not cloud.launches
    ready = cloud.machines[0]
    assert not ready.spec and not cloud.launch_tokens
    await manager.advance(run_id)
    assert manager.state(run_id)['sandbox_id'] == ready.object_id
    assert manager.state(run_id)['prepared_workspace']
    assert not manager.prepared.count()
    await manager.prepared.maintain()
    assert len(cloud.machines) == 2
    assert not manager.has_capacity()  # One assigned + one prepared.
    second = manager.store.create_run('Second', '', 'modal', [], chat_enabled=True)
    manager.submit(second)
    await manager.advance(second['id'])
    assert manager.state(second['id'])['sandbox_id'] != ready.object_id
    await manager.prepared.maintain()
    assert len(cloud.machines) == 2 and not manager.prepared.count()
    await drive(manager, run_id)
    assert len(cloud.launches) == 1 and cloud.launch_tokens[0]


async def test_pool_reconciles_lost_create_ack_without_duplicate_machine(durable, monkeypatch):
    manager, cloud, _ = durable
    enable_pool(manager)
    create = cloud.create

    async def lost(**kwargs):
        assert not kwargs.get('secrets')
        await create(**kwargs)
        raise ConnectionError('lost acknowledgement')

    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create.aio', lost)
    with pytest.raises(ConnectionError):
        await manager.prepared.maintain()
    await manager.prepared.maintain()
    assert len(cloud.machines) == 1
    assert manager.store.rows('SELECT status FROM prepared_sandboxes') == [{'status': 'ready'}]


async def test_pool_does_not_replace_saved_or_repository_workspaces(durable):
    manager, cloud, run_id = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    clean = cloud.machines[0]
    manager.store.update_run(run_id, snapshot_id='im-existing')
    await manager.advance(run_id)
    assert not manager.state(run_id).get('prepared_workspace')
    assert manager.prepared.count() == 1
    await drive(manager, run_id, phase='monitor')
    assert manager.state(run_id)['sandbox_id'] != clean.object_id


async def test_disabling_pool_reclaims_only_unassigned_machines(durable):
    manager, cloud, run_id = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    await manager.advance(run_id)
    assigned = cloud.machines[0]
    await manager.prepared.maintain()
    manager.settings.sandbox_prepared_pool_size = 0
    await manager.prepared.maintain()
    assert assigned.alive
    assert not cloud.machines[1].alive
    assert not manager.prepared.count()


async def test_expired_or_old_build_pool_entries_are_not_claimed(durable):
    manager, cloud, run_id = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    manager.store.execute('UPDATE prepared_sandboxes SET expires_at=0')
    assert not manager.prepared.ready(run_id)
    await manager.prepared.maintain()
    assert not cloud.machines[0].alive and len(cloud.machines) == 2
    manager.prepared.build = 'next-release'
    assert not manager.prepared.ready(run_id)
    await manager.prepared.maintain()
    assert not cloud.machines[1].alive and len(cloud.machines) == 3


async def test_ambiguous_expired_pool_create_keeps_capacity_until_machine_appears(durable, monkeypatch):
    manager, cloud, _ = durable
    enable_pool(manager)
    create = cloud.create
    pending = {}

    async def lost_before_ack(**kwargs):
        pending.update(kwargs)
        raise ConnectionError('create outcome unknown')

    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create.aio', lost_before_ack)
    with pytest.raises(ConnectionError):
        await manager.prepared.maintain()
    manager.settings.sandbox_prepared_pool_size = 0
    await manager.prepared.maintain()
    assert manager.prepared.count() == 1
    assert manager.store.rows('SELECT status FROM prepared_sandboxes')[0]['status'] == 'deleting'
    machine = await create(**pending)  # The original provider request completes late.
    await manager.prepared.maintain()
    assert not machine.alive and not manager.prepared.count()


async def test_install_overlaps_runtime_upload_and_spec_reads(durable, monkeypatch):
    manager, _, run_id = durable
    await drive(manager, run_id, phase='install')
    uploads_started, spec_started = threading.Event(), threading.Event()
    original_spec = manager.spec

    async def upload(sandbox):
        uploads_started.set()
        assert await asyncio.to_thread(spec_started.wait, 2)

    def spec(run):
        spec_started.set()
        assert uploads_started.wait(2)
        return original_spec(run)

    monkeypatch.setattr('app.durable_runner.refresh_sandbox_files', upload)
    monkeypatch.setattr(manager, 'spec', spec)
    await manager.advance(run_id)
    assert manager.state(run_id)['phase'] == 'launch'
