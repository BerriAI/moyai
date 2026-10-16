"""Cancellation must not release process ownership ahead of any poller cleanup."""

import asyncio

import pytest

from moyai.worker_capacity import WorkerPools


class ControlledWorker:
    def __init__(self, *, fail_start=False):
        self.is_running = False
        self.fail_start = fail_start
        self.closing = asyncio.Event()
        self.finish = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aenter__(self):
        if self.fail_start:
            raise RuntimeError("Poller could not start.")
        self.is_running = True
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


async def test_cancel_during_partial_start_failure_still_closes_started_poller():
    first, failed = ControlledWorker(), ControlledWorker(fail_start=True)
    pools = WorkerPools((first, failed))
    task = asyncio.create_task(pools.__aenter__())
    await asyncio.wait_for(first.closing.wait(), 2)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and first.is_running
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
