"""Standalone readiness includes the Harness's otherwise-lazy SQLite schema."""

import asyncio
import sqlite3

from moyai.settings import Settings
from moyai.stores import NativeStores


async def test_sqlite_stores_are_ready_before_concurrent_agents(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url="")
    stores = NativeStores(settings)
    await stores.start()
    try:
        with sqlite3.connect(stores.database) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        # Independent sessions share the database file, as real swarm members do.
        async def read_session(index):
            assert await stores.steps.get_run(run_id=f"run-{index}") is None
            assert await stores.memory.read(f"session-{index}", max_chars=1) is None
            assert await stores.plan(f"session-{index}").get_items() == []

        await asyncio.gather(*(read_session(index) for index in range(24)))
        assert await stores.steps.get_run(run_id="__schema_probe__") is None
        assert await stores.memory.read("__schema_probe__", max_chars=1) is None
        assert await stores.plan("__schema_probe__").get_items() == []
    finally:
        await stores.close()
