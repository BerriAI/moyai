from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from moyai.settings import Settings
from moyai.stores import NativeStores


async def test_verified_stores_require_provisioned_schema_and_detect_drift(tmp_path):
    url = os.getenv("MOYAI_TEST_POSTGRES")
    if not url:
        pytest.skip("Set MOYAI_TEST_POSTGRES for actual PostgreSQL schema qualification.")
    schema = "native_harness_" + uuid4().hex
    connection = await asyncpg.connect(url)
    await connection.execute(f'CREATE SCHEMA "{schema}"')
    settings = Settings(
        _env_file=None, data_dir=tmp_path, database_url=url, database_schema=schema,
        native_schema_mode="verify", encryption_key="synthetic-harness-schema-test-key-only",
    )
    try:
        stores = NativeStores(settings)
        try:
            with pytest.raises(ValueError, match="not been provisioned"):
                await stores.start()
            assert not await connection.fetchval(f"SELECT to_regclass('{schema}.agent_memory')")
        finally:
            await stores.close()

        stores = NativeStores(settings)
        try:
            await stores.start(provision=True)
            await stores.memory.write("main/MEMORY.md", "Persistent product memory", expected_version=None)
        finally:
            await stores.close()

        stores = NativeStores(settings)
        try:
            await stores.start()
            saved = await stores.memory.read("main/MEMORY.md", max_chars=100)
            assert saved and saved.content == "Persistent product memory"
        finally:
            await stores.close()

        await connection.execute(f'DROP TABLE "{schema}".plan_items')
        stores = NativeStores(settings)
        try:
            with pytest.raises(ValueError, match="incomplete"):
                await stores.start()
            assert not await connection.fetchval(f"SELECT to_regclass('{schema}.plan_items')")
        finally:
            await stores.close()
    finally:
        await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await connection.close()
