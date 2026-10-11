"""Cross-process wake delivery and prepared-pool ownership on real Postgres."""
import asyncio
from unittest.mock import AsyncMock

from app.db import database
from app.temporal_runtime import TemporalRunManager
from test_durable import Cloud
from test_runtime_scaling import cluster, target  # noqa: F401


async def test_remote_commit_notifies_dispatcher_and_rollback_does_not(cluster):
    store, settings, open_store = cluster
    worker_store, worker_settings = open_store()
    dispatcher = TemporalRunManager(store, settings)
    writer = TemporalRunManager(worker_store, worker_settings)
    wake = asyncio.Event()
    dispatcher.notify_dispatch = wake.set
    listener = asyncio.create_task(dispatcher.listen_dispatch())
    try:
        await asyncio.wait_for(wake.wait(), 2)  # LISTEN is acknowledged.
        wake.clear()
        run = store.create_run('Cross-process input', '', 'demo', [])
        await database(writer.submit, run)
        await asyncio.wait_for(wake.wait(), .5)
        wake.clear()

        def rollback():
            try:
                with worker_store.connect() as conn:
                    writer.submit_in(conn, run)
                    raise RuntimeError('roll back')
            except RuntimeError:
                pass

        await database(rollback)
        try:
            await asyncio.wait_for(wake.wait(), .15)
            assert False, 'A rolled-back transaction woke dispatch'
        except TimeoutError:
            pass
        dispatcher.temporal = type('Client', (), {'start_workflow': AsyncMock()})()
        await dispatcher.dispatch()
        assert dispatcher.temporal.start_workflow.await_count == 1
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)


async def test_workers_share_pool_reservation_and_assign_exactly_once(cluster, monkeypatch):
    coordinator, settings, open_store = cluster
    settings.sandbox_prepared_pool_size = 1
    coordinator.database.configure_runtime(settings)
    first_store, first_settings = open_store(sandbox_prepared_pool_size=1)
    second_store, second_settings = open_store(sandbox_prepared_pool_size=1)
    runs = [coordinator.create_run('Pool ownership', '', 'modal', [], chat_enabled=True) for _ in range(2)]
    cloud = Cloud(monkeypatch, first_store, runs[0]['id'])
    first = cloud.attach(TemporalRunManager(first_store, first_settings))
    second = cloud.attach(TemporalRunManager(second_store, second_settings))
    await asyncio.gather(first.prepared.maintain(), second.prepared.maintain())
    assert len(cloud.machines) == 1 and first.prepared.count() == 1
    for run in runs:
        first.submit(run)
    await asyncio.gather(first.advance(runs[0]['id']), second.advance(runs[1]['id']))
    states = [first.state(run['id']) for run in runs]
    assert sum(bool(state.get('prepared_workspace')) for state in states) == 1
    assert first.prepared.count() == 0
    assert len({state.get('sandbox_id') for state in states}) == 2
