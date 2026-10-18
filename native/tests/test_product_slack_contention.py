"""Actual disposable local PostgreSQL contention, no provider/network sends."""

import asyncio
import itertools
import os
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
from app.coordinator_handoff import HookInbox, initialize_schema
from app.db import Store, database


@pytest.mark.parametrize("contention", ["writer", "pool"])
async def test_canonical_hook_offload_drains_cancellation_with_competing_poll(tmp_path, contention):
    url = os.environ.get("MOYAI_TEST_POSTGRES")
    if not url:
        pytest.skip("Set a disposable local PostgreSQL URL.")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    schema = "moyai_slack_probe_" + uuid4().hex

    def make_store():
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{schema}"')
        value = None
        try:
            value = Store(
                tmp_path / "source", database_url=url, database_schema=schema,
                database_initialize=True, application_instance=False, database_pool_size=2,
            )
            initialize_schema(value)
            return value
        except BaseException:
            if value is not None:
                value.close()
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            raise

    store = await database(make_store)
    release, entered = threading.Event(), threading.Event()
    holder = None
    tasks = []
    try:
        def hold():
            if contention == "pool":
                connections = [store.database.pool.getconn() for _ in range(2)]
                try:
                    entered.set()
                    assert release.wait(5)
                finally:
                    for connection in connections:
                        store.database.pool.putconn(connection)
            else:
                with store.connect() as connection:
                    connection.begin_write()
                    entered.set()
                    assert release.wait(5)

        holder = asyncio.create_task(database(hold))
        assert await asyncio.to_thread(entered.wait, 3)
        inbox = HookInbox(store, SimpleNamespace(), list)
        payload = {"type": "event_callback", "team_id": "TLOCAL001", "event_id": "LOCAL001"}
        tasks.append(asyncio.create_task(database(inbox.enqueue, "local-one", payload)))
        await asyncio.sleep(0.05)
        tasks[0].cancel()
        tasks.append(asyncio.create_task(database(inbox.enqueue, "local-one", payload)))
        health = asyncio.create_task(database(store.rows, "SELECT 1"))
        ticks = []
        for _ in range(20):
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.025)
        assert not tasks[0].done()  # Cancellation waits for the owned write.
        assert not tasks[1].done()
        assert max(b - a for a, b in itertools.pairwise(ticks)) < 0.3
        if contention == "pool":
            assert not health.done()  # DB wait is separate from loop responsiveness.
        release.set()
        await holder
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(outcomes[0], asyncio.CancelledError) and outcomes[1] is False
        await health
        rows = await database(store.rows, "SELECT id,status FROM coordinator_hooks")
        assert rows == [{"id": "local-one", "status": "pending"}]
    finally:
        release.set()
        if holder is not None:
            await asyncio.gather(holder, *tasks, return_exceptions=True)
        await database(store.close)

        def cleanup():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await database(cleanup)


async def test_connection_resolution_reuses_transaction_while_pool_one_writer_waits(tmp_path, monkeypatch):
    from app.security import Security
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from test_product_identity import settings_for

    from moyai.product_connections import ProductConnections

    url = os.environ.get("MOYAI_TEST_POSTGRES")
    if not url:
        pytest.skip("Set a disposable local PostgreSQL URL.")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    schema = "moyai_grant_probe_" + uuid4().hex
    store = None
    engine = create_async_engine("postgresql+asyncpg://" + url.split("://", 1)[1])
    entered, finished, acquired = threading.Event(), threading.Event(), threading.Event()
    holder = None
    try:
        def setup():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'CREATE SCHEMA "{schema}"')
            return Store(
                tmp_path / "source", database_url=url, database_schema=schema,
                database_initialize=True, application_instance=False, database_pool_size=1,
            )

        store = await database(setup)
        settings = settings_for(tmp_path)
        service = ProductConnections(SimpleNamespace(
            store_proxy=store, settings=settings, security=Security(settings),
            identity=SimpleNamespace(schema=schema),
        ), SimpleNamespace())
        await database(store.execute,
                       "INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       ("google:owner", "google", "owner@example.test", "Owner", "c", "u"))
        await database(service.connectors.save, "linear", {"access_token": "local-only"}, "Local")
        await database(service.connectors.save, "notion", {"access_token": "local-only"}, "Local")
        await database(service.connectors.save, "github", {
            "kind": "github_app", "installation_id": 1, "account_id": 1, "repository_ids": [7],
        }, "Local")
        await database(service.connectors.save, "slack", {
            "access_token": "local-only", "bot": {"access_token": "local-bot-only"},
        }, "Local")
        personal = service.connectors.personal_slack
        revision = await database(personal.revision, "google:owner")
        await database(personal.save, "google:owner", {"access_token": "local-personal-only"}, "Personal", revision)

        def writer():
            with store.connect() as connection:
                entered.set()
                connection.begin_write()
                acquired.set()
                assert finished.wait(5)

        async with AsyncSession(engine) as db, db.begin():
            await db.execute(text(
                "SELECT pg_advisory_xact_lock_shared(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)",
            ), {"schema": schema})
            holder = asyncio.create_task(database(writer))
            assert await asyncio.to_thread(entered.wait, 2)

            def forbidden_pool_checkout(*args, **kwargs):
                pytest.fail("Grant resolution attempted a second source-pool checkout under the canonical lock.")

            monkeypatch.setattr(store, "rows", forbidden_pool_checkout)
            grants = await asyncio.wait_for(service.resolve_grants(
                "google:owner", ["linear", "notion", "github", "slack"], db=db,
            ), 2)
            assert len(grants) == 5 and all(grant.credential == "" for grant in grants)
            installation = await asyncio.wait_for(service.slack_installation(db=db), 2)
            assert installation["installed"] and "access_token" not in installation
            assert not acquired.is_set()
        finished.set()
        await asyncio.wait_for(holder, 3)
        assert acquired.is_set()
    finally:
        finished.set()
        if holder is not None:
            await asyncio.gather(holder, return_exceptions=True)
        await engine.dispose()
        if store is not None:
            await database(store.close)

        def cleanup():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await database(cleanup)
