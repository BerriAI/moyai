"""One model concurrency limit, with reclaimable tool-free maintenance slots."""
import asyncio
import time
from collections.abc import Coroutine
from typing import TypeVar


Result = TypeVar('Result')


class ModelSlots:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.active = self.waiting = self.queue_responses = 0
        self.max_wait_ms = 0.0
        self._slots = asyncio.Semaphore(capacity)
        self._maintenance: set[asyncio.Task] = set()
        self._holders: dict[asyncio.Task, int] = {}

    def _background_holder(self) -> asyncio.Task | None:
        return next((task for task in self._holders if not task.done()), None)

    def locked(self) -> bool:
        if not self._slots.locked():
            return False
        # Background requests still receive the ordinary unbilled queue reply.
        # Foreground requests can reclaim a slot after its holder cleans up.
        return self._background_holder() is None or asyncio.current_task() in self._maintenance

    async def acquire(self) -> bool:
        current = asyncio.current_task()
        assert current is not None
        if current not in self._maintenance:
            while self._slots.locked() and (holder := self._background_holder()) is not None:
                # Several foreground requests may await the same holder. A
                # second cancel would interrupt its accounting/HTTP cleanup.
                if not holder.cancelling():
                    holder.cancel()
                try:
                    await asyncio.shield(holder)
                except asyncio.CancelledError:
                    if current.cancelling():
                        raise
                except Exception:
                    pass  # The maintenance owner receives its own failure.
        started = time.monotonic()
        self.waiting += 1
        try:
            await self._slots.acquire()
        finally:
            self.waiting -= 1
        self.active += 1
        self.max_wait_ms = max(self.max_wait_ms, (time.monotonic() - started) * 1000)
        if current in self._maintenance:
            self._holders[current] = self._holders.get(current, 0) + 1
        return True

    def release(self) -> None:
        self.active -= 1
        current = asyncio.current_task() if self._holders else None
        if current in self._holders:
            remaining = self._holders[current] - 1
            if remaining:
                self._holders[current] = remaining
            else:
                del self._holders[current]
        self._slots.release()

    def record_queue(self) -> None:
        self.queue_responses += 1

    def snapshot(self) -> dict:
        return {'capacity': self.capacity, 'active': self.active, 'waiting': self.waiting,
                'queue_responses': self.queue_responses, 'max_wait_ms': round(self.max_wait_ms, 2)}

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self.release()

    def run_maintenance(self, coroutine: Coroutine[object, object, Result]) -> asyncio.Task[Result]:
        """Schedule and return an awaitable task; never retry a cancelled call."""
        entered = False

        async def run() -> Result:
            nonlocal entered
            current = asyncio.current_task()
            assert current is not None
            self._maintenance.add(current)
            try:
                entered = True
                return await coroutine
            finally:
                self._maintenance.discard(current)

        runner = run()
        try:
            task = asyncio.create_task(runner)
        except BaseException:
            runner.close()
            coroutine.close()
            raise

        def close_unstarted(completed: asyncio.Task[Result]) -> None:
            # Cancellation can prevent run() from entering its try/finally.
            if not entered:
                coroutine.close()

        task.add_done_callback(close_unstarted)
        return task
