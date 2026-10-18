from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from test_product_deployment import configured
from test_product_deployment import environment as environment_fixture

from moyai.database import Database
from moyai.product_resources import ProductResources
from moyai.security import Security
from moyai.settings import Settings
from moyai.stores import NativeStores

environment = environment_fixture


def test_product_capacity_settings_reach_native_runtime(environment):
    environment.update({
        "MOYAI_DATABASE_POOL_SIZE": "1",
        "MOYAI_DATABASE_CONNECTION_BUDGET": "13",
        "TEMPORAL_WORKER_ACTIVITIES": "17",
        "TEMPORAL_WORKFLOW_CACHE_SIZE": "0",
        "TEMPORAL_DISPATCH_CONCURRENCY": "3",
        "TEMPORAL_DISPATCH_BATCH_SIZE": "31",
    })
    settings = Settings(_env_file=None, **configured(environment).native_overrides())
    assert settings.database_pool_size == 1
    assert settings.database_connection_budget == 13
    assert settings.postgres_budget.max_connections == 9
    assert settings.temporal_worker_activities == 17
    assert settings.temporal_workflow_cache_size == 0
    assert settings.temporal_dispatch_concurrency == 3
    assert settings.temporal_dispatch_batch_size == 31


def test_product_engines_use_the_same_aggregate_allocation(environment):
    settings = Settings(_env_file=None, **configured(environment).native_overrides())
    database = Database(settings, Security(settings.master_key))
    resources = ProductResources(configured(environment).legacy_settings(), database.engine)
    assert settings.postgres_budget.pool_size == 3
    assert isinstance(database.engine.pool, AsyncAdaptedQueuePool)
    assert isinstance(resources.engine.pool, AsyncAdaptedQueuePool)
    assert database.engine.pool.size() == 3
    assert resources.engine.pool.size() == 3
    assert resources.connection_budget == settings.postgres_budget


def test_native_budget_validation_happens_before_runtime_startup():
    with pytest.raises(ValueError, match="CONNECTION_BUDGET"):
        Settings(_env_file=None, product_mode="existing", database_connection_budget=8)
    assert Settings(_env_file=None).postgres_budget.pool_size == 8


async def assert_one_connection(engine: AsyncEngine) -> None:
    entered = asyncio.Event()

    async def query() -> int:
        entered.set()
        async with engine.connect() as connection:
            return await connection.scalar(text("SELECT 1"))

    pending = None
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
            pending = asyncio.create_task(query())
            await entered.wait()
            await asyncio.sleep(0.1)
            assert not pending.done(), "Pool overflow bypassed the configured connection budget."
        assert await asyncio.wait_for(pending, 3) == 1
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Requires disposable local PostgreSQL.")
async def test_all_native_pools_honor_one_connection_budget(tmp_path, environment):
    base = urlsplit(os.environ["MOYAI_TEST_POSTGRES"])
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    name = f"moyai_pool_{uuid4().hex}"
    admin = await asyncpg.connect(urlunsplit(base))
    await admin.execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE template0')
    url = urlunsplit(base._replace(path=f"/{name}"))
    settings = Settings(
        _env_file=None, data_dir=tmp_path / "native", database_url=url, database_pool_size=1,
        encryption_key="local-native-pool-budget-test-only",
    )
    database = Database(settings, Security(settings.master_key))
    stores = NativeStores(settings)
    environment.update(MOYAI_DATABASE_URL=url, MOYAI_DATABASE_POOL_SIZE="1")
    resources = ProductResources(configured(environment).legacy_settings(), database.engine)
    pending = None
    try:
        await assert_one_connection(database.engine)
        await assert_one_connection(resources.engine)
        await stores.start()
        assert stores.pool is not None
        async with stores.pool.acquire():
            pending = asyncio.create_task(stores.pool.fetchval("SELECT 1"))
            await asyncio.sleep(0.1)
            assert not pending.done(), "Harness bypassed the configured connection budget."
        assert await asyncio.wait_for(pending, 3) == 1
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stores.close()
        await database.close()
        await resources.engine.dispose()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()
