"""The real durable controller with gated context/provider I/O, no model calls."""
import asyncio

import pytest
from fastapi import HTTPException

from app.temporal_runtime import TemporalRunManager
from test_durable import durable, drive  # noqa: F401


@pytest.mark.parametrize('held', ['context', 'provider'])
async def test_cold_context_and_acquisition_overlap_but_launch_waits_for_both(durable, monkeypatch, held):
    manager, cloud, run_id = durable
    entered = {name: asyncio.Event() for name in ('context', 'provider')}
    release = asyncio.Event()
    create = cloud.create

    async def context(identity):
        entered['context'].set()
        await entered['provider'].wait()
        if held == 'context':
            await release.wait()
        manager.store.execute('UPDATE messages SET content=? WHERE run_id=? AND status=\'running\'',
                              ('Actual task with prepared attachment context', identity))

    async def provider(**kwargs):
        entered['provider'].set()
        await entered['context'].wait()
        if held == 'provider':
            await release.wait()
        return await create(**kwargs)

    manager.prepare_context = context
    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create.aio', provider)
    await drive(manager, run_id, phase='provision')
    task = asyncio.create_task(manager.advance(run_id))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 2)
        assert not task.done() and not cloud.launches
        assert manager.state(run_id)['context_pending']
        assert all(not machine.spec for machine in cloud.machines)
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    assert not manager.state(run_id).get('context_pending')
    await drive(manager, run_id)
    assert len(cloud.machines) == len(cloud.launches) == 1
    assert cloud.machines[0].spec['prompt'] == 'Actual task with prepared attachment context'
    assert manager.store.messages(run_id)[0]['status'] == 'completed'


async def test_cancelled_activity_keeps_acquisition_owned_until_late_machine_is_saved(durable, monkeypatch):
    manager, cloud, run_id = durable
    entered, release = asyncio.Event(), asyncio.Event()
    source_cancelled = asyncio.Event()
    create = cloud.create

    async def provider(**kwargs):
        entered.set()
        await release.wait()
        return await create(**kwargs)

    async def context(identity):
        try:
            await asyncio.Event().wait()
        finally:
            source_cancelled.set()

    manager.prepare_context = context
    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create.aio', provider)
    await drive(manager, run_id, phase='provision')
    task = asyncio.create_task(manager.advance(run_id))
    await asyncio.wait_for(entered.wait(), 2)
    await manager.cancel(run_id)
    task.cancel()
    await asyncio.wait_for(source_cancelled.wait(), 2)
    task.cancel()  # A second cancellation must not release the ownership guard.
    contender = asyncio.create_task(manager.advance(run_id))
    try:
        await asyncio.sleep(.02)
        assert not task.done() and not contender.done()
        assert manager.locks[run_id].locked()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.wait_for(contender, 2)
    await drive(manager, run_id)
    assert len(cloud.machines) == 1 and not cloud.machines[0].alive
    assert not cloud.launches
    assert manager.store.run(run_id)['status'] == 'cancelled'


async def test_restart_after_acquisition_reloads_context_before_install_without_second_create(durable):
    manager, cloud, run_id = durable
    reading = asyncio.Event()

    async def context(identity):
        reading.set()
        await asyncio.Event().wait()

    manager.prepare_context = context
    await drive(manager, run_id, phase='provision')
    task = asyncio.create_task(manager.advance(run_id))
    await asyncio.wait_for(reading.wait(), 2)
    async with asyncio.timeout(2):
        while manager.state(run_id)['phase'] != 'install':
            await asyncio.sleep(.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert manager.state(run_id)['context_pending']
    replacement = cloud.attach(TemporalRunManager(manager.store, manager.settings))
    calls = []

    async def complete_context(identity):
        calls.append(identity)
        manager.store.execute('UPDATE messages SET content=? WHERE run_id=? AND status=\'running\'',
                              ('Recovered complete source', identity))

    replacement.prepare_context = complete_context
    await drive(replacement, run_id)
    assert calls == [run_id]
    assert len(cloud.machines) == len(cloud.launches) == 1
    assert cloud.machines[0].spec['prompt'] == 'Recovered complete source'


async def test_source_failure_drains_provider_and_cleans_up_without_agent_launch(durable, monkeypatch):
    manager, cloud, run_id = durable
    entered, release = asyncio.Event(), asyncio.Event()
    create = cloud.create

    async def provider(**kwargs):
        entered.set()
        await release.wait()
        return await create(**kwargs)

    async def context(identity):
        await entered.wait()
        raise HTTPException(403, 'Source access revoked')

    manager.prepare_context = context
    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create.aio', provider)
    await drive(manager, run_id, phase='provision')
    task = asyncio.create_task(manager.advance(run_id))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        await asyncio.sleep(.02)
        assert not task.done() and not cloud.launches
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    await drive(manager, run_id)
    assert manager.store.run(run_id)['status'] == 'failed'
    assert len(cloud.machines) == 1 and not cloud.machines[0].alive
    assert not cloud.launches


async def test_lost_create_ack_reconciles_same_machine_and_retries_only_source_reads(durable, monkeypatch):
    manager, cloud, run_id = durable
    create = cloud.create
    calls = []

    async def provider(**kwargs):
        await create(**kwargs)
        raise ConnectionError('lost create acknowledgement')

    async def context(identity):
        calls.append(identity)

    manager.prepare_context = context
    monkeypatch.setattr('app.durable_runner.modal.Sandbox.create.aio', provider)
    await drive(manager, run_id, phase='provision')
    with pytest.raises(ConnectionError):
        await manager.advance(run_id)
    state = manager.state(run_id)
    assert state['sandbox_name'] and state['context_pending']
    assert not cloud.launches and len(cloud.machines) == 1
    await drive(manager, run_id)
    assert calls == [run_id, run_id]
    assert len(cloud.machines) == len(cloud.launches) == 1


async def test_warm_followup_prepares_context_without_acquisition(durable):
    manager, cloud, run_id = durable
    manager.settings.sandbox_idle_seconds = 300
    await drive(manager, run_id, phase='warm')
    manager.store.enqueue_message(run_id, 'Continue', 'followup')
    calls = []

    async def context(identity):
        calls.append(identity)

    manager.prepare_context = context
    await drive(manager, run_id, phase='warm')
    assert calls == [run_id]
    assert len(cloud.machines) == 1 and len(cloud.launches) == 2


async def test_pending_environment_retains_context_barrier_and_pinned_snapshot(durable, monkeypatch):
    from types import SimpleNamespace
    from app.environments import EnvironmentPending
    manager, cloud, run_id = durable
    reading = asyncio.Event()
    calls, images = [], []
    environment_attempts = 0

    async def context(identity):
        calls.append(identity)
        reading.set()
        if len(calls) == 1:
            await asyncio.Event().wait()

    async def environment(identity):
        nonlocal environment_attempts
        environment_attempts += 1
        await reading.wait()
        if environment_attempts == 1:
            raise EnvironmentPending('Preparing project', 'build-pinned')
        return {'snapshot_id': 'im-pinned'}

    manager.prepare_context = context
    manager.environments = SimpleNamespace(prepare=environment, context=lambda run: {})
    monkeypatch.setattr('app.durable_runner.modal.Image.from_id',
                        lambda identity, **kwargs: images.append(identity) or 'image')
    await drive(manager, run_id, phase='waiting_environment')
    assert manager.state(run_id)['context_pending']
    assert not cloud.machines and not cloud.launches
    await drive(manager, run_id)
    assert images == ['im-pinned'] and calls == [run_id, run_id]
    assert len(cloud.machines) == len(cloud.launches) == 1


async def test_two_postgres_workers_share_acquisition_and_context_barrier(durable, monkeypatch):
    manager, cloud, run_id = durable
    if not manager.store.database:
        pytest.skip('Requires --postgres-backend for cross-worker leases')
    manager.settings.moyai_runtime_role = 'worker'
    first = cloud.attach(TemporalRunManager(manager.store, manager.settings))
    second = cloud.attach(TemporalRunManager(manager.store, manager.settings))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def context(identity):
        calls.append(identity)
        entered.set()
        await release.wait()

    first.prepare_context = second.prepare_context = context
    await drive(first, run_id, phase='provision')
    initial = asyncio.create_task(first.advance(run_id))
    await asyncio.wait_for(entered.wait(), 2)
    competing = asyncio.create_task(second.advance(run_id))
    try:
        await asyncio.sleep(.05)
        assert not competing.done() and not cloud.launches
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(initial, competing), 4)
    await drive(second, run_id)
    assert calls == [run_id]
    assert len(cloud.machines) == len(cloud.launches) == 1
