"""Use the Harness's stores, with application-authenticated conversation scopes."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

import asyncpg
from pydantic_ai_harness.media import PostgresMediaStore
from pydantic_ai_harness.memory import MemoryStore, PostgresMemoryStore, SqliteMemoryStore
from pydantic_ai_harness.memory import PostgresPool as MemoryPool
from pydantic_ai_harness.planning import PlanStore, PostgresPlanStore, SqlitePlanStore
from pydantic_ai_harness.planning import PostgresPool as PlanPool
from pydantic_ai_harness.step_persistence import PostgresPool as StepPool
from pydantic_ai_harness.step_persistence import PostgresStepStore, SqliteStepStore

from .settings import Settings

if TYPE_CHECKING:
    from .product_role_authority import OwnedHarnessPool, ProductRoleAuthority


class NativeStores:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self.harness_pool: asyncpg.Pool | OwnedHarnessPool | None = None
        self.memory: MemoryStore
        self.steps: PostgresStepStore | SqliteStepStore
        self.plans: dict[str, PlanStore] = {}
        self.database: Path = settings.data_dir / "agent-state.db"

    async def start(self, *, provision: bool = False) -> None:
        if self.settings.postgres_url:
            schema = self.settings.database_schema
            if schema and not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
                raise ValueError("Invalid native schema.")
            self.pool = await asyncpg.create_pool(
                self.settings.postgres_url, min_size=1, max_size=self.settings.database_pool_size,
                server_settings={"search_path": f'"{schema}",pg_catalog'} if schema else {},
            )
            assert self.pool is not None
            self.harness_pool = self.pool
            if self.settings.native_schema_mode == "verify" and not provision:
                from .harness_schema import verify
                await verify(self.pool)
            # Harness's structural pool protocols describe asyncpg at runtime;
            # asyncpg's stub leaves acquisition/transaction return types unspecified.
            self.memory = PostgresMemoryStore(cast(MemoryPool, self.pool))
            self.steps = PostgresStepStore(cast(StepPool, self.pool), max_snapshots_per_run=None)
            if provision:
                # Public read operations initialize the SDK-owned schema without
                # adding a run, conversation, memory file, plan item, or blob.
                await self.memory.read("__schema_probe__", max_chars=1)
                await self.steps.get_run(run_id="__schema_probe__")
                await self.plan("__schema_probe__").get_items()
                media = PostgresMediaStore(cast(StepPool, self.pool), table="step_persistence_media")
                with suppress(FileNotFoundError):
                    await media.get("media+sha256://" + "0" * 64)
                from .harness_schema import record
                await record(self.pool)
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
                PostgresPlanStore(cast(PlanPool, self.harness_pool), session=session_id)
                if self.pool else SqlitePlanStore(str(self.database), session=session_id)
            )
        return self.plans[session_id]

    def bind_authority(self, authority: ProductRoleAuthority) -> None:
        """Bind before constructing the agent; leave the SDK store behavior intact."""
        if self.pool is None:
            raise ValueError("Canonical runtime ownership requires PostgreSQL Harness stores.")
        from .product_role_authority import OwnedHarnessPool
        self.harness_pool = OwnedHarnessPool(self.pool, authority)
        self.memory = PostgresMemoryStore(cast(MemoryPool, self.harness_pool))
        self.steps = PostgresStepStore(cast(StepPool, self.harness_pool), max_snapshots_per_run=None)
        self.plans.clear()

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
        for store in (getattr(self, "steps", None), getattr(self, "memory", None)):
            close = getattr(store, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

