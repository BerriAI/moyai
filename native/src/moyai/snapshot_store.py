"""Keep one snapshot's media and row inside one PostgreSQL operation.

Harness 0.55 externalizes each large message part through a separate pool
acquisition. With canonical ownership, each of those acquisitions also repeats
the admission and commit fences. Reuse one connection for this bounded SDK
operation, keeping its savepoints, serialization and idempotency unchanged.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass

from pydantic_ai_harness.media import PostgresConnection, PostgresMediaStore, PostgresPool
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, PostgresStepStore


@dataclass
class _SnapshotConnection:
    owner: asyncio.Task
    connection: PostgresConnection
    active: bool = True


class _SnapshotPool:
    def __init__(self, pool: PostgresPool, *, owned: bool):
        self.pool = pool
        self.owned = owned
        self.current: ContextVar[_SnapshotConnection | None] = ContextVar(
            "moyai_snapshot_connection", default=None,
        )

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PostgresConnection]:
        current = self.current.get()
        if current is not None and current.active:
            # ContextVars propagate into child tasks; asyncpg connections cannot
            # be shared concurrently. Fail closed if a future SDK parallelizes
            # its media walker, rather than borrowing another task's connection.
            if current.owner is not asyncio.current_task():
                raise RuntimeError("Snapshot connection belongs to another task.")
            yield current.connection
        else:
            async with self.pool.acquire() as connection:
                yield connection

    @asynccontextmanager
    async def snapshot(self) -> AsyncIterator[None]:
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Snapshot persistence requires an asyncio task.")
        if (current := self.current.get()) is not None and current.active:
            raise RuntimeError("Snapshot transactions cannot be nested.")
        async with self.pool.acquire() as connection:
            # OwnedHarnessPool already opens an outer transaction and fences
            # it before yielding and before commit. Do not add another pair
            # of savepoint round trips to that canonical path.
            transaction = nullcontext() if self.owned else connection.transaction()
            async with transaction:
                current = _SnapshotConnection(owner, connection)
                token = self.current.set(current)
                try:
                    yield
                finally:
                    current.active = False
                    self.current.reset(token)


class _SnapshotMediaStore(PostgresMediaStore):
    async def prepare(self) -> None:
        # Initialize outside the snapshot transaction. SDK schema-ready flags
        # must never survive rollback of their CREATE TABLE statements.
        await self._ensure_schema()


class SnapshotStepStore(PostgresStepStore):
    """Native SDK storage, with one awaited transaction per snapshot.

    Only save_snapshot coalesces acquisitions. Events, write-ahead tool effects,
    reads and all Temporal operation names/order remain the native SDK's.
    """

    def __init__(self, pool: PostgresPool, *, owned: bool = False):
        self.snapshot_pool = _SnapshotPool(pool, owned=owned)
        self.snapshot_media = _SnapshotMediaStore(
            self.snapshot_pool, table="step_persistence_media",
        )
        super().__init__(
            self.snapshot_pool, media_store=self.snapshot_media, max_snapshots_per_run=None,
        )

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        await self._ensure_schema()
        await self.snapshot_media.prepare()
        async with self.snapshot_pool.snapshot():
            await super().save_snapshot(snapshot)
