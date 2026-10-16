"""An explicit provisioning receipt for the pinned Harness PostgreSQL stores.

The stores still run their documented lazy, idempotent DDL on first use. This
module never patches SDK internals. Product startup verifies the provisioned
shape first, so a missing table or SDK upgrade requires an operator migration.
"""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import version

import asyncpg
from asyncpg.pool import PoolConnectionProxy

TABLES = (
    "agent_memory", "agent_memory_operations", "agent_memory_metadata", "plan_items",
    "step_persistence_runs", "step_persistence_events", "step_persistence_snapshots",
    "step_persistence_snapshot_keys", "step_persistence_tool_effects", "step_persistence_media",
)


async def shape(connection: asyncpg.Connection | PoolConnectionProxy) -> str:
    columns = await connection.fetch("""
        SELECT table_name,column_name,ordinal_position,data_type,is_nullable,column_default,is_identity
        FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=ANY($1::text[])
        ORDER BY table_name,ordinal_position
    """, list(TABLES))
    if {row["table_name"] for row in columns} != set(TABLES):
        raise ValueError("Harness schema is incomplete. Run the explicit native schema provisioning command.")
    indexes = await connection.fetch("""
        SELECT tablename,indexname,indexdef FROM pg_indexes
        WHERE schemaname=current_schema() AND tablename=ANY($1::text[])
        ORDER BY tablename,indexname
    """, list(TABLES))
    sequences = await connection.fetch("""
        SELECT sequencename,data_type,start_value,min_value,increment_by
        FROM pg_sequences WHERE schemaname=current_schema()
          AND sequencename IN ('agent_memory_versions','step_persistence_events_seq_seq',
            'step_persistence_snapshots_seq_seq')
        ORDER BY sequencename
    """)
    if len(sequences) != 3:
        raise ValueError("Harness schema sequences are incomplete.")
    encoded = json.dumps(
        [[dict(row) for row in rows] for rows in (columns, indexes, sequences)],
        sort_keys=True, default=str,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


async def verify(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        exists = await connection.fetchval(
            "SELECT to_regclass(quote_ident(current_schema()) || '.moyai_harness_schema')",
        )
        if exists is None:
            raise ValueError("Harness schema has not been provisioned. Run the native migration command.")
        receipt = await connection.fetchrow("SELECT * FROM moyai_harness_schema WHERE id=1")
        if receipt is None or (
            receipt["sdk_version"] != version("pydantic-ai")
            or receipt["harness_version"] != version("pydantic-ai-harness")
            or receipt["shape"] != await shape(connection)
        ):
            raise ValueError("Harness schema/version differs from its reviewed provisioning receipt.")


async def record(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute("""
            CREATE TABLE IF NOT EXISTS moyai_harness_schema (
                id INTEGER PRIMARY KEY CHECK(id=1), sdk_version TEXT NOT NULL,
                harness_version TEXT NOT NULL, shape TEXT NOT NULL
            )
        """)
        await connection.execute("""
            INSERT INTO moyai_harness_schema VALUES (1,$1,$2,$3)
            ON CONFLICT(id) DO UPDATE SET sdk_version=excluded.sdk_version,
                harness_version=excluded.harness_version,shape=excluded.shape
        """, version("pydantic-ai"), version("pydantic-ai-harness"), await shape(connection))
