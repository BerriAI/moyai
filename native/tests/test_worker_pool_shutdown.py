"""Cancellation must not release process ownership ahead of any poller cleanup."""

import asyncio

import pytest

from moyai.worker_capacity import WorkerPools


class ControlledWorker:
    def __init__(self, *, fail_start=False, polling=True):
        self.is_running = False
        self.fail_start = fail_start
        self.polling = polling
        self.closing = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aenter__(self):
        if self.fail_start:
            raise RuntimeError("Poller could not start.")
        self.is_running = self.polling
        return self

    async def __aexit__(self, *exc):
        await self.shutdown()

    async def shutdown(self):
        self.closing.set()
        await self.finish.wait()
        self.is_running = False
        self.closed.set()


async def test_repeated_cancellation_drains_every_pool_before_owner_release():
    workers = tuple(ControlledWorker() for _ in range(4))
    pools = WorkerPools(workers)
    entered, owner_released = asyncio.Event(), asyncio.Event()

    async def lifetime():
        try:
            async with pools:
                entered.set()
                await asyncio.Event().wait()
        finally:
            owner_released.set()

    task = asyncio.create_task(lifetime())
    await entered.wait()
    task.cancel()
    for worker in reversed(workers):
        await asyncio.wait_for(worker.closing.wait(), 2)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0)
        assert not owner_released.is_set()
        assert not task.done()
        worker.finish.set()
        await asyncio.wait_for(worker.closed.wait(), 2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert owner_released.is_set()
    assert all(worker.closed.is_set() for worker in workers)
    assert not pools.is_running


@pytest.mark.parametrize("polling", [False, True])
async def test_cancel_during_partial_start_failure_still_closes_started_poller(polling):
    first, failed = ControlledWorker(polling=polling), ControlledWorker(fail_start=True)
    pools = WorkerPools((first, failed))
    task = asyncio.create_task(pools.__aenter__())
    await asyncio.wait_for(first.closing.wait(), 2)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and first.is_running is polling
    first.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.closed.is_set() and not failed.is_running


async def test_explicit_shutdown_cannot_be_interrupted_halfway():
    workers = tuple(ControlledWorker() for _ in range(4))
    for worker in workers:
        await worker.__aenter__()
    pools = WorkerPools(workers)
    task = asyncio.create_task(pools.shutdown())
    for worker in reversed(workers):
        await asyncio.wait_for(worker.closing.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        worker.finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(worker.closed.is_set() for worker in workers)


@pytest.mark.parametrize("context_exit", [False, True])
async def test_every_poller_starts_its_grace_window_before_any_drain_finishes(context_exit):
    workers = tuple(ControlledWorker() for _ in range(4))
    pools = WorkerPools(workers)
    await pools.__aenter__()
    task = asyncio.create_task(pools.__aexit__(None, None, None) if context_exit else pools.shutdown())
    try:
        await asyncio.wait_for(asyncio.gather(*(worker.closing.wait() for worker in workers)), 2)
        assert not task.done()
        assert not any(worker.closed.is_set() for worker in workers)
    finally:
        for worker in workers:
            worker.finish.set()
        await task
    assert all(worker.closed.is_set() for worker in workers)


async def test_failed_poller_shutdown_still_waits_for_every_other_poller():
    class FailedWorker(ControlledWorker):
        async def shutdown(self):
            self.closing.set()
            raise RuntimeError("Poller shutdown failed.")

    failed, draining = FailedWorker(), ControlledWorker()
    await failed.__aenter__()
    await draining.__aenter__()
    task = asyncio.create_task(WorkerPools((failed, draining)).shutdown())
    await asyncio.wait_for(draining.closing.wait(), 2)
    assert failed.closing.is_set() and not task.done()
    draining.finish.set()
    with pytest.raises(RuntimeError, match="Poller shutdown failed"):
        await task
    assert draining.closed.is_set()
