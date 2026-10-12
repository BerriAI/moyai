import asyncio

import pytest

from app.deepagents_workers import AgentWorkers


async def test_failed_worker_start_cancels_pending_sibling_and_preserves_failure():
    stopped = asyncio.Event()
    class Pending:
        is_running = False
        async def run(self):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    class Failed:
        is_running = False
        async def run(self):
            await asyncio.sleep(0)
            raise RuntimeError('fixture startup failed')
    workers = AgentWorkers(Pending(), Failed())
    with pytest.raises(RuntimeError, match='fixture startup failed'):
        await asyncio.wait_for(workers.run(), 1)
    assert stopped.is_set() and not workers.is_running


async def test_readiness_requires_every_pool_and_shutdown_drains_them():
    class Worker:
        is_running = False
        def __init__(self):
            self.stopped = asyncio.Event()
        async def run(self):
            self.is_running = True
            try:
                await self.stopped.wait()
            finally:
                self.is_running = False
        async def shutdown(self):
            self.stopped.set()
    first, second = Worker(), Worker()
    workers = AgentWorkers(first, second)
    assert not workers.is_running
    running = asyncio.create_task(workers.run())
    async with asyncio.timeout(1):
        while not workers.is_running:
            await asyncio.sleep(0)
    await workers.shutdown()
    await asyncio.wait_for(running, 1)
    assert not workers.is_running
