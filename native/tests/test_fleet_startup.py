"""Real PostgreSQL startup/seal ordering and ownership-session isolation."""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
from app.db import Store
from fastapi import FastAPI
from psycopg import sql
from test_fleet_registry import proof

from moyai.fleet_admission import FleetAdmission
from moyai.fleet_registry import FleetAdmissionError, FleetRegistry
from moyai.product_lifecycle import ProductRoleLifecycle, ProductRoleMiddleware
from moyai.product_role_authority import ProductRoleAuthority


@pytest.fixture
def admitted_role(tmp_path, monkeypatch):
    dsn = os.environ.get("MOYAI_TEST_POSTGRES")
    if not dsn:
        pytest.skip("Requires isolated PostgreSQL.")
    assert urlsplit(dsn).hostname in {"127.0.0.1", "localhost", "::1"}
    suffix = uuid4().hex[:16]
    source, schema = "startup_source_" + suffix, "moyai_fleet_admission_" + suffix
    with psycopg.connect(dsn, autocommit=True) as operator:
        operator.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(source)))
        store = Store(
            tmp_path / "source", database_url=dsn, database_schema=source,
            database_initialize=True, database_pool_size=1,
        )
        try:
            owner = store.database
            owner.owner.execute("SELECT pg_advisory_lock_shared(726940,%s::regnamespace::oid::int)", (source,))
            pid, started = owner.owner.execute(
                "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()",
            ).fetchone()
            owner.owner_pid = pid
            service = "srv-" + suffix + "0000"
            monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
            monkeypatch.setenv("MOYAI_LOCAL_FLEET_SERVICE_ID", service)
            monkeypatch.setenv("MOYAI_LOCAL_FLEET_INSTANCE_ID", service + "-abcde")
            monkeypatch.setenv("MOYAI_LOCAL_FLEET_SERVICE_NAME", "blue-service")
            monkeypatch.setenv("MOYAI_RELEASE_GENERATION", "blue")
            registry = FleetRegistry(operator, schema=schema)
            registry.initialize()
            operator.execute(registry.query(
                "INSERT INTO {schema}.fleets VALUES('production',%s,%s,'native-queue',true)",
            ), (source, source + "_n"))

            def make(role="api"):
                operator.execute(registry.query("""
                    INSERT INTO {schema}.roles(
                        fleet,generation,role,service_name,service_id,source_sha,cluster_policy,
                        role_policy,worker_deployment,worker_build,state
                    ) VALUES('production','blue',%s,'blue-service',%s,%s,%s,%s,'workers','blue-build','accepting')
                """), (role, service, "a" * 40, "b" * 64, "c" * 64))
                runtime = SimpleNamespace(
                    product=SimpleNamespace(
                        store=store,
                        settings=SimpleNamespace(
                            moyai_build_sha="a" * 40, moyai_database_schema=source,
                            temporal_startup_timeout_seconds=3,
                        ),
                        authority=ProductRoleAuthority(source, role, pid, started, "b" * 64, "c" * 64),
                    ),
                    product_config=SimpleNamespace(role=role, native_schema=source + "_n", local_rehearsal=True),
                    settings=SimpleNamespace(
                        temporal_queue="native-queue", worker_deployment="workers", worker_build_id="blue-build",
                        validate_worker=lambda **_: None,
                    ),
                    product_inference=None, product_slack=None,
                )
                life = ProductRoleLifecycle(runtime)
                admission = FleetAdmission(life)
                admission.schema = schema
                life.admission = admission
                return life, admission

            def seal(life, entered=None):
                with psycopg.connect(dsn, autocommit=True) as connection:
                    other = FleetRegistry(connection, schema=schema)
                    arguments = {"service_id": service, "identity": life.quiescence.identity()}
                    if entered is not None:
                        entered.set()
                    return other.seal(
                        fleet="production", generation="blue", role=life.role, service_id=service,
                        request_id="startup-seal", proof=proof(arguments, life.role),
                    )

            yield SimpleNamespace(
                make=make, seal=seal, registry=registry, store=store, dsn=dsn, schema=schema, service=service,
            )
        finally:
            store.close()
            operator.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            operator.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(source)))


def use_admission(monkeypatch, admission):
    import moyai.fleet_admission as module
    monkeypatch.setattr(module, "FleetAdmission", lambda _life: admission)


@pytest.mark.parametrize("role", ["api", "broker", "coordinator", "worker"])
@pytest.mark.parametrize("acknowledgement", ["stale", "lost"])
async def test_registration_reply_after_seal_never_starts_role_work(
    admitted_role, monkeypatch, role, acknowledgement,
):
    from app.database import DatabaseBusyError

    fixture = admitted_role
    life, admission = fixture.make(role)
    use_admission(monkeypatch, admission)
    original = admission._register
    connected = []
    registered = False

    def register_then_seal():
        nonlocal registered
        result = original()
        if not registered:
            registered = True
            assert fixture.seal(life)  # Captured after commit, before the acknowledgement is delivered.
            if acknowledgement == "lost":
                raise DatabaseBusyError("Registration committed before its reply was lost.")
        return result

    async def connect():
        connected.append(True)
        pytest.fail("A sealed startup reached role initialization.")

    monkeypatch.setattr(admission, "_register", register_then_seal)
    life.runtime.connect = connect
    with pytest.raises(FleetAdmissionError, match="sealed_before_start"):
        async with life.lifespan():
            pytest.fail("A sealed startup became ready.")
    assert not connected and not life.healthy and life.watch_task is None
    receipts = fixture.registry.drain_receipts(fleet="production", generation="blue", role=role)
    assert len(receipts) == 1 and receipts[0]["request_id"] == "startup-seal"
    assert receipts[0]["drained_at"] is not None
    assert admission._startup_lock is None


async def test_seal_waits_for_startup_and_then_drains_the_existing_owner(admitted_role, monkeypatch):
    fixture = admitted_role
    life, admission = fixture.make()
    use_admission(monkeypatch, admission)
    connecting, allow_connect, serving, stop = (asyncio.Event() for _ in range(4))
    seal_entered = threading.Event()

    async def connect():
        connecting.set()
        await allow_connect.wait()

    async def serve():
        async with life.lifespan():
            serving.set()
            await stop.wait()

    life.runtime.connect = connect
    task = asyncio.create_task(serve())
    sealing = None
    try:
        await asyncio.wait_for(connecting.wait(), 2)
        sealing = asyncio.create_task(asyncio.to_thread(fixture.seal, life, seal_entered))
        assert await asyncio.to_thread(seal_entered.wait, 2)
        await asyncio.sleep(.05)
        assert not sealing.done() and not serving.is_set()
        allow_connect.set()
        await asyncio.wait_for(serving.wait(), 2)
        captured = await asyncio.wait_for(sealing, 2)
        assert len(captured) == 1
        async with asyncio.timeout(2):
            while life.quiescence.state != "drained":
                await asyncio.sleep(.01)
        assert not life.healthy and not life.failed
        assert fixture.registry.drain_receipts(
            fleet="production", generation="blue", role="api",
        )[0]["request_id"] == "startup-seal"
    finally:
        allow_connect.set()
        stop.set()
        await asyncio.gather(task, *([sealing] if sealing else []), return_exceptions=True)


async def test_cancellation_after_lock_acquisition_releases_it_without_starting(admitted_role, monkeypatch):
    fixture = admitted_role
    _life, admission = fixture.make()
    acquired, release = threading.Event(), threading.Event()
    original = admission._lock_startup

    def lock_with_lost_reply():
        result = original()
        assert result
        acquired.set()
        assert release.wait(3)
        return result

    monkeypatch.setattr(admission, "_lock_startup", lock_with_lost_reply)

    async def start():
        async with admission.startup():
            pytest.fail("Cancelled startup reached role work.")

    task = asyncio.create_task(start())
    try:
        assert await asyncio.to_thread(acquired.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        key = fixture.registry.startup_key(fleet="production", generation="blue", role="api")
        assert not fixture.registry.connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fixture.registry.connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
    fixture.registry.connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
    assert admission._startup_lock is None
    fixture.store.database.check_owner()


async def test_lost_canonical_owner_releases_startup_gate_but_never_becomes_ready(admitted_role, monkeypatch):
    from app.database import DatabaseError

    fixture = admitted_role
    life, admission = fixture.make()
    use_admission(monkeypatch, admission)

    async def connect():
        fixture.store.database.owner.close()

    life.runtime.connect = connect
    with pytest.raises(DatabaseError):
        async with life.lifespan():
            pytest.fail("A process whose startup gate owner died became ready.")
    assert not life.ready and admission._startup_lock is None
    key = fixture.registry.startup_key(fleet="production", generation="blue", role="api")
    assert fixture.registry.connection.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()[0]
    fixture.registry.connection.execute("SELECT pg_advisory_unlock(%s)", (key,))


async def test_failed_startup_keeps_seal_blocked_until_partial_worker_cleanup(admitted_role, monkeypatch):
    import moyai.worker as worker_module

    fixture = admitted_role
    life, admission = fixture.make("worker")
    life.runtime.product.settings.temporal_startup_timeout_seconds = .1
    use_admission(monkeypatch, admission)
    cleaning, release = asyncio.Event(), asyncio.Event()
    seal_entered = threading.Event()

    async def connect():
        pass

    @asynccontextmanager
    async def worker(_runtime):
        try:
            yield SimpleNamespace(is_running=False)
        finally:
            cleaning.set()
            await release.wait()

    life.runtime.connect = connect
    monkeypatch.setattr(worker_module, "make_worker", worker)

    async def start():
        async with life.lifespan():
            pytest.fail("Failed worker startup became ready.")

    task = asyncio.create_task(start())
    await asyncio.wait_for(cleaning.wait(), 2)
    sealing = asyncio.create_task(asyncio.to_thread(fixture.seal, life, seal_entered))
    try:
        assert await asyncio.to_thread(seal_entered.wait, 2)
        await asyncio.sleep(.05)
        assert not sealing.done()
    finally:
        release.set()
    with pytest.raises(TimeoutError):
        await task
    assert await asyncio.wait_for(sealing, 2)
    assert not life.healthy and admission._startup_lock is None


@pytest.mark.parametrize("contention", ["metadata", "pool"])
async def test_registry_wait_does_not_block_owner_health_or_retire_the_process(admitted_role, contention):
    fixture = admitted_role
    life, admission = fixture.make()
    await admission.start()
    life.ready = True
    retired = []
    life.retire = lambda: retired.append(True)
    fixture.store.database.owner.execute("SET lock_timeout='500ms'")
    with fixture.store.connect() as connection:
        connection.raw.execute("SET lock_timeout='500ms'")
    fixture.store.database.pool.timeout = .5
    app = FastAPI()
    app.add_middleware(ProductRoleMiddleware, lifecycle=life)
    held = fixture.store.database.pool.getconn() if contention == "pool" else None
    watching = None
    try:
        with psycopg.connect(fixture.dsn, autocommit=True) as blocker, blocker.transaction():
            if contention == "metadata":
                blocker.execute(sql.SQL("LOCK TABLE {}.roles IN ACCESS EXCLUSIVE MODE").format(
                    sql.Identifier(fixture.schema),
                ))
            watching = asyncio.create_task(life.watch())
            # Wait for the actual database/pool wait, not a guessed scheduling delay.
            async with asyncio.timeout(2):
                while True:
                    if contention == "pool":
                        waiting = fixture.store.database.pool.get_stats().get("requests_waiting", 0)
                    else:
                        waiting = blocker.execute(
                            "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' AND query LIKE %s",
                            ("%" + fixture.schema + "%",),
                        ).fetchone()[0]
                        blocker.execute("SELECT pg_stat_clear_snapshot()")
                    if waiting:
                        break
                    await asyncio.sleep(.01)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as client:
                async with asyncio.timeout(.3):
                    assert (await client.get("/health")).status_code == 200
            async with asyncio.timeout(2):
                while not life.admission_busy and not life.failed:
                    await asyncio.sleep(.01)
            assert life.admission_busy and not life.failed and retired == []
    finally:
        if held is not None:
            fixture.store.database.pool.putconn(held)
        if watching is not None:
            watching.cancel()
            await asyncio.gather(watching, return_exceptions=True)
