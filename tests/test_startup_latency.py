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
    assert manager.store.run(run_id)['sandbox_id'] == ready.object_id
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


async def test_invalid_install_spec_still_finishes_instead_of_retrying_forever(durable, monkeypatch):
    manager, _, run_id = durable
    await drive(manager, run_id, phase='install')

    def invalid(run):
        raise ValueError('invalid model')

    monkeypatch.setattr(manager, 'spec', invalid)
    await drive(manager, run_id)
    assert manager.store.messages(run_id)[0]['status'] == 'failed'


async def test_cancelled_provision_commits_both_workspace_references(durable, monkeypatch):
    from contextlib import contextmanager
    manager, cloud, run_id = durable
    await drive(manager, run_id, phase='provision')
    entered, release = threading.Event(), threading.Event()
    connect = manager.store.connect

    @contextmanager
    def held(*args, **kwargs):
        with connect(*args, **kwargs) as conn:
            class Connection:
                def __getattr__(self, key):
                    return getattr(conn, key)
                def execute(self, sql, params=()):
                    result = conn.execute(sql, params)
                    if sql.startswith('UPDATE durable_sessions SET state=') and '"phase": "install"' in params[0]:
                        entered.set()
                        assert release.wait(3)
                    return result
            yield Connection()

    monkeypatch.setattr(manager.store, 'connect', held)
    task = asyncio.create_task(manager.advance(run_id))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    try:
        await asyncio.sleep(.03)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    state = manager.state(run_id)
    assert state['phase'] == 'install'
    assert state['sandbox_id'] == manager.store.run(run_id)['sandbox_id'] == cloud.machines[0].object_id


async def test_default_environment_bypasses_generic_prepared_pool(durable, monkeypatch):
    from app.environments import Environments
    from test_environments import prepared
    manager, cloud, run_id = durable
    enable_pool(manager)
    env = Environments(manager.store, manager.settings, None, manager, None, SimpleNamespace(flush=AsyncMock()))
    manager.environments = env
    build_id = prepared(env)
    await manager.prepared.maintain()
    images = []
    monkeypatch.setattr('app.durable_runner.modal.Image.from_id', lambda value, **kw: images.append(value) or 'image')
    await drive(manager, run_id, phase='monitor')
    assert not manager.state(run_id).get('prepared_workspace')
    assert manager.prepared.count() == 1
    assert manager.store.run(run_id)['environment_build_id'] == build_id
    assert images == ['im-project']
    assert cloud.machines[-1].spec['project_environment']['build_id'] == build_id


async def test_computer_wake_cannot_spend_an_unclaimed_pool_reservation(durable, monkeypatch):
    from fastapi import HTTPException
    manager, _, run_id = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    occupied = manager.store.create_run('Active work', '', 'modal', [])
    manager.submit(occupied)
    manager.save(occupied['id'], {'phase': 'monitor'})
    manager.store.execute('DELETE FROM messages WHERE run_id=?', (run_id,))
    manager.store.update_run(run_id, status='idle')
    monkeypatch.setattr(manager.settings.__class__, 'missing_sandbox', lambda *args: [])
    # Cleanup is held elsewhere. Computer wake must not treat an unassigned
    # ready pool entry as free capacity and create a third reservation.
    manager.prepared.reclaim = AsyncMock(return_value=False)
    with pytest.raises(HTTPException) as error:
        await manager.wake_computer(run_id)
    assert error.value.status_code == 409
    assert manager.state(run_id) == {} and manager.prepared.count() == 1


async def test_pool_expiry_between_capacity_check_and_assignment_does_not_over_admit(durable, monkeypatch):
    manager, _, run_id = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    occupied = manager.store.create_run('Active work', '', 'modal', [])
    manager.submit(occupied)
    manager.save(occupied['id'], {'phase': 'monitor'})
    assign = manager.prepared.assign

    def expired(*args):
        manager.store.execute('UPDATE prepared_sandboxes SET expires_at=0')
        return assign(*args)

    monkeypatch.setattr(manager.prepared, 'assign', expired)
    await manager.advance(run_id)
    assert manager.state(run_id) == {}  # Claimed input survives without an extra machine slot.
    assert manager.store.messages(run_id)[0]['status'] == 'running'
    assert not manager.has_capacity()


async def test_pool_reclaim_never_waits_on_local_inflight_preparation(durable):
    manager, _, _ = durable
    async with manager.prepared.guard() as acquired:
        assert acquired
        assert await asyncio.wait_for(manager.prepared.reclaim(), .2) is False


async def test_reclaim_skips_uncertain_pool_entry_before_evicting_user_workspace(durable):
    manager, cloud, _ = durable
    enable_pool(manager)
    await manager.prepared.maintain()
    manager.store.execute('''INSERT INTO prepared_sandboxes(name,build,status,created_at,expires_at)
        VALUES('uncertain',?,'deleting',0,?)''', (manager.prepared.build, time.time() + 300))
    assert await manager.prepared.reclaim()
    assert not cloud.machines[0].alive
    assert manager.store.rows('SELECT name FROM prepared_sandboxes') == [{'name': 'uncertain'}]
