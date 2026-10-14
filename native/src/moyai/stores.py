"""Use the Harness's stores, with application-authenticated conversation scopes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import asyncpg
from pydantic_ai_harness.memory import MemoryStore, PostgresMemoryStore, SqliteMemoryStore
from pydantic_ai_harness.memory import PostgresPool as MemoryPool
from pydantic_ai_harness.planning import PlanStore, PostgresPlanStore, SqlitePlanStore
from pydantic_ai_harness.planning import PostgresPool as PlanPool
from pydantic_ai_harness.step_persistence import PostgresPool as StepPool
from pydantic_ai_harness.step_persistence import PostgresStepStore, SqliteStepStore

from .settings import Settings


class NativeStores:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self.memory: MemoryStore
        self.steps: PostgresStepStore | SqliteStepStore
        self.plans: dict[str, PlanStore] = {}
        self.database: Path = settings.data_dir / "agent-state.db"

    async def start(self) -> None:
        if self.settings.postgres_url:
            self.pool = await asyncpg.create_pool(self.settings.postgres_url, min_size=1, max_size=10)
            # Harness's structural pool protocols describe asyncpg at runtime;
            # asyncpg's stub leaves acquisition/transaction return types unspecified.
            self.memory = PostgresMemoryStore(cast(MemoryPool, self.pool))
            self.steps = PostgresStepStore(cast(StepPool, self.pool), max_snapshots_per_run=None)
        else:
            self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.memory = await asyncio.to_thread(SqliteMemoryStore, database=self.database)
            self.steps = await asyncio.to_thread(
                SqliteStepStore, database=self.database, max_snapshots_per_run=None
            )

    def plan(self, session_id: str) -> PlanStore:
        # Constructors only select a namespace; reads/writes are native durable operations.
        if session_id not in self.plans:
            self.plans[session_id] = (
                PostgresPlanStore(cast(PlanPool, self.pool), session=session_id)
                if self.pool else SqlitePlanStore(str(self.database), session=session_id)
            )
        return self.plans[session_id]

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
        for store in (getattr(self, "steps", None), getattr(self, "memory", None)):
            close = getattr(store, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

