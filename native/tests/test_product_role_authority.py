"""Actual PostgreSQL fencing checks; supplementary to deployed-app qualification."""

from __future__ import annotations

import os
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from pydantic_ai_harness.memory import PostgresMemoryStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from moyai.product_role_authority import (
    OwnedHarnessPool,
    ProductOwnershipLost,
    ProductRoleAuthority,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)


@pytest_asyncio.fixture
async def role():
    url = os.environ["MOYAI_TEST_POSTGRES"]
    assert urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"}
    schema = "moyai_role_" + uuid4().hex
    admin = await asyncpg.connect(url)
    owner = await asyncpg.connect(url)
    engine = create_async_engine(
        "postgresql+asyncpg://" + url.split("://", 1)[1],
        connect_args={"server_settings": {"search_path": f'"{schema}",pg_catalog'}},
    )
    pool = None
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f"""
            CREATE TABLE "{schema}".runtime_policy(id integer PRIMARY KEY, fingerprint text);
            CREATE TABLE "{schema}".runtime_api_policy(
                id integer PRIMARY KEY, cluster_fingerprint text, fingerprint text
            );
            CREATE TABLE "{schema}".evidence(id integer PRIMARY KEY, value text);
        """)
        await admin.execute(f'INSERT INTO "{schema}".runtime_policy VALUES (1,$1)', "a" * 64)
        await admin.execute(
            f'INSERT INTO "{schema}".runtime_api_policy VALUES (1,$1,$2)', "a" * 64, "b" * 64,
        )
        await owner.execute("SELECT pg_advisory_lock_shared(726940,$1::regnamespace::oid::int)", schema)
        identity = await owner.fetchrow(
            "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()",
        )
        authority = ProductRoleAuthority(schema, "api", identity["pid"], identity["backend_start"], "a" * 64, "b" * 64)
        pool = await asyncpg.create_pool(
            url, min_size=1, max_size=3, server_settings={"search_path": f'"{schema}",pg_catalog'},
        )
        yield SimpleNamespace(
            schema=schema, admin=admin, owner=owner, engine=engine, pool=pool,
            authority=authority, sessions=authority.sessions(engine),
        )
    finally:
        await engine.dispose()
        if pool:
            await pool.close()
        await owner.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def test_native_transaction_keeps_runtime_ownership_until_commit(role):
    async with role.sessions.begin() as db:
        await db.execute(text("INSERT INTO evidence VALUES (1,'saved')"))
        # The transaction also holds runtime ownership: losing the long-lived
        # process owner cannot allow a replacement to publish during a write.
        await role.owner.close()
        assert not await role.admin.fetchval(
            "SELECT pg_try_advisory_lock(726940,$1::regnamespace::oid::int)", role.schema,
        )
        with pytest.raises(ProductOwnershipLost):
            await db.commit()
        await db.rollback()
    assert await role.admin.fetchval(f'SELECT count(*) FROM "{role.schema}".evidence') == 0
    assert await role.admin.fetchval(
        "SELECT pg_try_advisory_lock(726940,$1::regnamespace::oid::int)", role.schema,
    )
    await role.admin.execute("SELECT pg_advisory_unlock(726940,$1::regnamespace::oid::int)", role.schema)


async def test_revocation_before_commit_rolls_back_native_writes(role):
    with pytest.raises(ProductOwnershipLost):
        async with role.sessions.begin() as db:
            await db.execute(text("INSERT INTO evidence VALUES (1,'must roll back')"))
            await role.admin.execute(
                f'UPDATE "{role.schema}".runtime_api_policy SET fingerprint=$1', "c" * 64,
            )
    assert await role.admin.fetchval(f'SELECT count(*) FROM "{role.schema}".evidence') == 0
    with pytest.raises(ProductOwnershipLost):
        async with role.sessions() as db:
            await db.execute(text("SELECT 1"))


async def test_owner_lifetime_and_coordinator_context_are_checked(role):
    from app.coordinator_handoff import coordinator_owner

    impostor = replace(role.authority, owner_pid=role.authority.owner_pid + 1)
    with pytest.raises(ProductOwnershipLost):
        async with impostor.sessions(role.engine)() as db:
            await db.execute(text("SELECT 1"))
    token = coordinator_owner.set((role.schema, role.authority.owner_pid))
    try:
        with pytest.raises(ProductOwnershipLost):
            async with role.sessions() as db:
                await db.execute(text("SELECT 1"))
        await role.owner.execute(
            "SELECT pg_advisory_lock(726943,$1::regnamespace::oid::int)", role.schema,
        )
        async with role.sessions.begin() as db:
            await db.execute(text("INSERT INTO evidence VALUES (1,'leader')"))
    finally:
        coordinator_owner.reset(token)
    assert await role.admin.fetchval(f'SELECT value FROM "{role.schema}".evidence') == "leader"


async def test_harness_uses_native_store_and_revoked_operations_roll_back(role):
    owned = OwnedHarnessPool(role.pool, role.authority)
    memory = PostgresMemoryStore(owned)
    first = await memory.write("person/notes.md", "native memory", expected_version=None)
    assert (await memory.read("person/notes.md", max_chars=100)).content == "native memory"
    assert first.version
    with pytest.raises(ProductOwnershipLost):
        async with owned.acquire() as connection, connection.transaction():
            await connection.execute("INSERT INTO evidence VALUES (1,'must roll back')")
            await role.admin.execute(
                f'UPDATE "{role.schema}".runtime_api_policy SET fingerprint=$1', "c" * 64,
            )
    assert await role.admin.fetchval(f'SELECT count(*) FROM "{role.schema}".evidence') == 0
    with pytest.raises(ProductOwnershipLost):
        await memory.write("person/notes.md", "revoked", expected_version=first.version)
    assert await role.admin.fetchval(
        f'SELECT content FROM "{role.schema}".agent_memory WHERE path=$1', "person/notes.md",
    ) == "native memory"


async def test_orm_connection_binding_is_stable_and_not_reentrantly_acquired(role):
    identities = []
    for _ in range(2):
        async with role.sessions.begin() as db:
            identities.append(await db.scalar(text("SELECT pg_backend_pid()")))
            marker = (await db.execute(text("""
                SELECT objid::bigint,mode FROM pg_locks WHERE pid=pg_backend_pid()
                AND locktype='advisory' AND classid=726948 AND granted
            """))).one()
            assert tuple(marker) == (role.authority.owner_pid, "ShareLock")
    assert identities[0] == identities[1]
    async with role.engine.connect() as connection:
        # One unlock removes the entire binding. Reacquiring on every operation
        # would leave another session-level reference after the first unlock.
        for expected in (True, False):
            assert await connection.scalar(text("SELECT pg_advisory_unlock_shared(726948,:pid)"), {
                "pid": role.authority.owner_pid,
            }) is expected


async def test_harness_connection_has_binding_before_its_write_and_reset_releases_it(role):
    owned = OwnedHarnessPool(role.pool, role.authority)
    for _ in range(2):
        async with owned.acquire() as connection:
            pid = await connection.fetchval("SELECT pg_backend_pid()")
            assert await connection.fetchval("""
                SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid()
                AND classid=726948 AND objid=$1 AND mode='ShareLock' AND granted
            """, role.authority.owner_pid) == 1
        assert await role.admin.fetchval(
            "SELECT count(*) FROM pg_locks WHERE pid=$1 AND classid=726948", pid,
        ) == 0  # Native asyncpg reset removes session advisory locks.


@pytest.mark.parametrize("fault", ["wrong_parent", "exclusive_marker", "role_owner", "chained_parent"])
async def test_invalid_connection_bindings_are_never_repaired_or_hidden(role, fault):
    async with role.engine.begin() as connection:
        if fault == "wrong_parent":
            await connection.execute(text("SELECT pg_advisory_lock_shared(726948,:pid)"), {
                "pid": role.authority.owner_pid + 1,
            })
        elif fault == "exclusive_marker":
            await connection.execute(text("SELECT pg_advisory_lock(726948,:pid)"), {
                "pid": role.authority.owner_pid,
            })
        elif fault == "role_owner":
            await connection.execute(text("""
                SELECT pg_advisory_lock_shared(726944,CAST(:schema AS regnamespace)::oid::int)
            """), {"schema": role.schema})
        else:
            await role.owner.execute(
                "SELECT pg_advisory_lock_shared(726948,$1)", role.authority.owner_pid + 1,
            )
    with pytest.raises(ProductOwnershipLost):
        async with role.sessions.begin() as db:
            await db.execute(text("INSERT INTO evidence VALUES (1,'must not be admitted')"))
    assert await role.admin.fetchval(f'SELECT count(*) FROM "{role.schema}".evidence') == 0
