"""One optional model concurrency limit, with reclaimable tool-free maintenance slots."""
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
        # Zero means unlimited: no request is ever queued or reclaimed.
        self._slots = asyncio.Semaphore(capacity) if capacity else None
        self._maintenance: set[asyncio.Task] = set()
        self._holders: dict[asyncio.Task, int] = {}

    def _background_holder(self) -> asyncio.Task | None:
        return next((task for task in self._holders if not task.done()), None)

    def locked(self) -> bool:
        if self._slots is None or not self._slots.locked():
            return False
        # Background requests still receive the ordinary unbilled queue reply.
        # Foreground requests can reclaim a slot after its holder cleans up.
        return self._background_holder() is None or asyncio.current_task() in self._maintenance

    async def is_full(self) -> bool:
        return self.locked()

    async def acquire(self) -> bool:
        current = asyncio.current_task()
        assert current is not None
        if self._slots is None:
            self.active += 1
            return True
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

    def release(self, permit=None) -> None:
        self.active -= 1
        if self._slots is None:
            return
        current = asyncio.current_task() if self._holders else None
        if current in self._holders:
            remaining = self._holders[current] - 1
            if remaining:
                self._holders[current] = remaining
            else:
                del self._holders[current]
        self._slots.release()

    async def release_async(self, permit=None) -> None:
        self.release(permit)

    def record_queue(self) -> None:
        self.queue_responses += 1

    def snapshot(self) -> dict:
        return {'capacity': self.capacity, 'active': self.active, 'waiting': self.waiting,
                'queue_responses': self.queue_responses, 'max_wait_ms': round(self.max_wait_ms, 2)}

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc: object) -> None:
        await self.release_async()

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


class PostgresModelSlots(ModelSlots):
    """An optional cluster-wide limit, with one extra connection per broker.

    Locks have no expiry: a slow live call never frees somebody else's slot.
    This session is distinct from the runtime ownership session so losing that
    owner can cancel and drain local calls before releasing their capacity.
    At zero, tokens track cleanup locally without reserving capacity locks.
    """
    def __init__(self, capacity, database):
        import hashlib
        import threading
        from contextvars import ContextVar

        super().__init__(capacity)
        self.database = database
        self._guard = threading.RLock()
        self._connection = None
        self._closed = False
        self._failed = False
        self._keys = tuple(int.from_bytes(hashlib.sha256(
            f'moyai:model-slot:{database.schema}:{index}'.encode()).digest()[:4], 'big', signed=True)
            for index in range(capacity))
        self._unsigned_keys = tuple(key & 0xffffffff for key in self._keys)
        self._permits = {}
        self._entered = ContextVar('moyai_model_permits', default=())

    def check(self):
        from .database import DatabaseError
        import psycopg

        try:
            with self._guard:
                if self._closed or self._failed:
                    raise DatabaseError('Shared model capacity ownership was lost.')
                if self._connection is None:
                    self._connection = psycopg.connect(self.database.pool.conninfo, autocommit=True, connect_timeout=10)
                    self._connection.execute("SET statement_timeout = '2s'")
                held = [key & 0xffffffff for key, _ in self._permits.values() if key is not None]
                count = self._connection.execute("""SELECT count(*) FROM pg_locks
                    WHERE locktype='advisory' AND pid=pg_backend_pid() AND classid=726947
                    AND objsubid=2 AND objid=ANY(%s::oid[]) AND granted""", (held,)).fetchone()[0]
                if count != len(held):
                    raise DatabaseError('Shared model capacity ownership was lost.')
        except psycopg.Error:
            raise DatabaseError('Shared model capacity ownership was lost.') from None

    def _shared_count(self):
        with self._guard:
            self.check()
            if not self.capacity:
                return self.active
            return self._connection.execute('''SELECT count(*) FROM pg_locks
                WHERE locktype='advisory' AND classid=726947 AND objsubid=2
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND objid=ANY(%s::oid[]) AND granted''', (list(self._unsigned_keys),)).fetchone()[0]

    def locked(self):
        if not self.capacity:
            self.check()
            return False
        if self._shared_count() < self.capacity:
            return False
        return self._background_holder() is None or asyncio.current_task() in self._maintenance

    async def is_full(self):
        from .db import database

        if not self.capacity:
            await database(self.check)
            return False
        if await database(self._shared_count) < self.capacity:
            return False
        return self._background_holder() is None or asyncio.current_task() in self._maintenance

    def _claim(self, current):
        from uuid import uuid4

        with self.database.connect() as conn:
            conn.check_owner()
        with self._guard:
            self.check()
            if not self.capacity:
                # Keep request tokens and owner checks without allocating a
                # database capacity lock. Cancellation still drains this claim.
                token = uuid4().hex
                self._permits[token] = None, current
                return token
            held = {value[0] for value in self._permits.values()}
            candidates = [key for key in self._keys if key not in held]
            if not candidates:
                return None
            # Filter local holders: PG session locks are reentrant, whereas a
            # permit must belong to exactly one admitted request.
            row = self._connection.execute('''SELECT key FROM unnest(%s::int[]) AS slots(key)
                WHERE pg_try_advisory_lock(726947,key) LIMIT 1''', (candidates,)).fetchone()
            if not row:
                return None
            token = uuid4().hex
            self._permits[token] = row[0], current
            return token

    async def _claim_async(self, current):
        from .db import database, drain

        operation = asyncio.create_task(asyncio.to_thread(self._claim, current))
        try:
            return await drain(operation)
        except asyncio.CancelledError as cancelled:
            # A cancellation may arrive after PostgreSQL granted the lock but
            # before this task received its token. Drain then release that token.
            try:
                permit = operation.result()
            except BaseException as exc:
                raise cancelled from exc
            if permit:
                try:
                    await database(self.release, permit, counted=False)
                except BaseException as exc:
                    raise cancelled from exc
            raise cancelled

    async def acquire(self):
        current = asyncio.current_task()
        assert current is not None
        started = time.monotonic()
        self.waiting += 1
        try:
            while (token := await self._claim_async(current)) is None:
                if current not in self._maintenance and (holder := self._background_holder()) is not None:
                    if not holder.cancelling():
                        holder.cancel()
                    try:
                        await asyncio.shield(holder)
                    except asyncio.CancelledError:
                        if current.cancelling():
                            raise
                    except Exception:
                        pass
                else:
                    await asyncio.sleep(.025)
        finally:
            self.waiting -= 1
        self.active += 1
        self.max_wait_ms = max(self.max_wait_ms, (time.monotonic() - started) * 1000)
        if current in self._maintenance:
            self._holders[current] = self._holders.get(current, 0) + 1
        return token

    def release(self, permit=None, *, counted=True):
        from .database import DatabaseError

        import psycopg

        with self._guard:
            if permit not in self._permits:
                raise DatabaseError('Shared model capacity permit is missing or already released.')
            key, holder = self._permits[permit]
            # Keep the local permit until PostgreSQL acknowledges release. An
            # uncertain unlock must never make a session lock reentrant again.
            if self._connection.closed or self._connection.broken:
                self._failed = True
            elif key is not None:
                try:
                    unlocked = self._connection.execute('SELECT pg_advisory_unlock(726947,%s)', (key,)).fetchone()[0]
                except psycopg.Error:
                    self._failed = True
                    raise DatabaseError('Shared model capacity release was not acknowledged.') from None
                if not unlocked:
                    self._failed = True
                    raise DatabaseError('Shared model capacity ownership was lost.')
            del self._permits[permit]
            if counted:
                self.active -= 1
            if holder in self._holders:
                remaining = self._holders[holder] - 1
                if remaining:
                    self._holders[holder] = remaining
                else:
                    del self._holders[holder]

    async def release_async(self, permit=None):
        from .db import database

        await database(self.release, permit)

    async def __aenter__(self):
        permit = await self.acquire()
        self._entered.set((*self._entered.get(), permit))

    async def __aexit__(self, *exc):
        entered = self._entered.get()
        self._entered.set(entered[:-1])
        await self.release_async(entered[-1])

    def snapshot(self):
        return {**super().snapshot(), 'active': self._shared_count(), 'local_active': self.active,
                'active_scope': 'cluster' if self.capacity else 'broker'}

    def close(self):
        with self._guard:
            self._closed = True
            if self._connection is not None:
                self._connection.close()
