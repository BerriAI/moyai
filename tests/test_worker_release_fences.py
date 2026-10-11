"""Worker release races at lease admission and asynchronous poller startup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from app import main
from app.database import DatabaseError, PostgresConnection
from app.temporal_runtime import TemporalRunManager
from test_api_compatibility import execute
from test_api_lifecycle import cluster  # noqa: F401
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401
from test_worker_compatibility import worker_settings


@pytest.mark.parametrize('operation', ['acquire', 'renew', 'release'])
def test_contract_revocation_waits_for_raw_lease_transaction(cluster, monkeypatch, operation):
    _, settings = cluster
    app = main.create_app(worker_settings(settings))
    database = app.state.store.database
    checked, finish = threading.Event(), threading.Event()
    original = PostgresConnection.check_owner
    if operation != 'acquire':
        assert database.acquire_lease('session:fixture', 'fixture-token', 30)

    def pause_after_check(conn, **kwargs):
        original(conn, **kwargs)
        if conn.worker_policy:
            checked.set()
            assert finish.wait(10)

    monkeypatch.setattr(PostgresConnection, 'check_owner', pause_after_check)
    actions = {
        'acquire': lambda: database.acquire_lease('session:fixture', 'fixture-token', 30),
        'renew': lambda: database.renew_lease('session:fixture', 'fixture-token', 30),
        'release': lambda: database.release_lease('session:fixture', 'fixture-token'),
    }
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            lease_operation = pool.submit(actions[operation])
            assert checked.wait(5)
            revocation = pool.submit(execute, settings, 'DELETE FROM runtime_worker_policy')
            try:
                deadline = time.monotonic() + 5
                while not execute(settings, "SELECT 1 FROM pg_stat_activity WHERE wait_event_type='Lock' AND query='DELETE FROM runtime_worker_policy'"):
                    assert not revocation.done(), 'Revocation passed an admitted lease transaction'
                    assert time.monotonic() < deadline
                    time.sleep(.01)
            finally:
                finish.set()
            lease_operation.result(timeout=5)
            revocation.result(timeout=5)
        with pytest.raises(DatabaseError, match='Worker compatibility changed'):
            actions[operation]()
    finally:
        finish.set()
        app.state.store.close()


class DeferredWorker:
    """SDK run() validates asynchronously before is_running becomes true."""
    def __init__(self, *, failure=False):
        self.started = asyncio.Event()
        self.validated = asyncio.Event()
        self.stopped = asyncio.Event()
        self.finished = asyncio.Event()
        self.is_running = False
        self.failure = failure
        self.shutdown_calls = 0

    async def run(self):
        try:
            self.started.set()
            await self.validated.wait()
            if self.failure:
                raise RuntimeError('Synthetic asynchronous namespace validation failure')
            self.is_running = True
            await self.stopped.wait()
        finally:
            self.is_running = False
            self.finished.set()

    async def shutdown(self):
        # The SDK's shutdown() waits for run() to finish. A validation that
        # never finishes must be cancelled instead of waiting for shutdown.
        self.shutdown_calls += 1
        self.stopped.set()
        await self.finished.wait()


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('pending_pool', ['controller', 'agent'])
async def test_worker_readiness_waits_for_asynchronous_validation(cluster, monkeypatch, failure, pending_pool):
    _, settings = cluster
    worker = DeferredWorker(failure=failure)
    ready_worker = DeferredWorker()
    ready_worker.validated.set()
    controller, agent = (worker, ready_worker) if pending_pool == 'controller' else (ready_worker, worker)
    monkeypatch.setattr(TemporalRunManager, 'make_worker', lambda *args: controller)
    monkeypatch.setattr(TemporalRunManager, 'make_agent_worker', lambda *args: agent)
    app = main.create_app(worker_settings(settings, temporal_startup_timeout_seconds=1))
    manager = app.state.manager
    try:
        await manager.recover()
        await asyncio.wait_for(worker.started.wait(), 1)
        await asyncio.sleep(.05)
        assert not manager.ready.is_set(), 'Worker was ready while SDK namespace validation was pending'
        worker.validated.set()
        if failure:
            with pytest.raises(RuntimeError, match='startup deadline'):
                await manager.wait_ready()
            assert not manager.ready.is_set()
        else:
            await manager.wait_ready()
            assert controller.is_running and agent.is_running
    finally:
        worker.validated.set()
        await asyncio.wait_for(manager.shutdown(), 2)
        await manager.modal_clients.close()
        app.state.store.close()


@pytest.mark.parametrize('pending_pool', ['controller', 'agent'])
async def test_startup_timeout_cancels_worker_stuck_in_validation(cluster, monkeypatch, pending_pool):
    _, settings = cluster
    worker = DeferredWorker()
    ready_worker = DeferredWorker()
    ready_worker.validated.set()
    controller, agent = (worker, ready_worker) if pending_pool == 'controller' else (ready_worker, worker)
    monkeypatch.setattr(TemporalRunManager, 'make_worker', lambda *args: controller)
    monkeypatch.setattr(TemporalRunManager, 'make_agent_worker', lambda *args: agent)
    app = main.create_app(worker_settings(settings, temporal_startup_timeout_seconds=1))
    manager = app.state.manager
    try:
        await manager.recover()
        with pytest.raises(RuntimeError, match='startup deadline'):
            await manager.wait_ready()
        await asyncio.wait_for(manager.shutdown(), 2)
        pending_task = manager.worker_task if pending_pool == 'controller' else manager.agent_worker_task
        assert pending_task.cancelled()
        assert worker.shutdown_calls == 0
        assert worker.finished.is_set()
        assert ready_worker.shutdown_calls == 1 and ready_worker.finished.is_set()
    finally:
        worker.validated.set()
        await asyncio.wait_for(manager.shutdown(), 2)
        await manager.modal_clients.close()
        app.state.store.close()
