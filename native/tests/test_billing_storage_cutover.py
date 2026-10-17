"""One physical store, including real PostgreSQL compatibility-view writers."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import pytest_asyncio
import test_billing_storage as storage_fixtures
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_billing_storage import finish_backfill, projection, seed
from test_product_billing import new_row, old_row

from moyai.billing_storage import BillingStorageError, prepare, rollback, status
from moyai.billing_storage_cutover import consolidate, verify_consolidated
from moyai.inference_usage import InferenceUsage
from moyai.product_schema import verify_product_schema

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
billing = storage_fixtures.billing


@pytest_asyncio.fixture
async def cutover(billing):
    ctx = billing
    async with ctx.engine.begin() as db:
        await db.execute(text("ALTER TABLE moyai.model_requests DROP COLUMN private_payload, DROP COLUMN api_key"))
        await db.execute(text("CREATE TABLE moyai.runs(id TEXT PRIMARY KEY)"))
        await db.execute(text("INSERT INTO moyai.runs VALUES('historical-child')"))
        await db.execute(text(
            "ALTER TABLE moyai.model_requests ADD FOREIGN KEY(run_id) REFERENCES moyai.runs(id)"
        ))
        await db.execute(text("CREATE TABLE moyai.schema_state(id integer PRIMARY KEY,revision integer,dirty integer)"))
        await db.execute(text("INSERT INTO moyai.schema_state VALUES(1,22,0)"))
    await seed(ctx, old=[old_row()], new=[new_row(provider_request_id="distinct")])
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    return ctx


async def test_cutover_leaves_one_store_and_exact_reports(cutover):
    ctx = cutover
    before = await projection(ctx)
    await consolidate(ctx.engine, ctx.storage)
    await consolidate(ctx.engine, ctx.storage)
    assert await projection(ctx) == before
    assert (await status(ctx.engine, ctx.storage))["physical_billing_stores"] == 1
    async with ctx.engine.connect() as db:
        for source in ("legacy", "native"):
            assert await db.scalar(text("SELECT relkind::text FROM pg_class WHERE oid=CAST(:name AS regclass)"), {
                "name": ctx.storage.source_table(source),
            }) == "v"
        assert await db.scalar(text("SELECT relkind::text FROM pg_class WHERE oid=CAST(:name AS regclass)"), {
            "name": ctx.storage.ledger,
        }) == "r"
        assert await db.scalar(text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname=:schema AND p.proname LIKE 'moyai_billing_mirror_%'"
        ), {"schema": ctx.storage.native_schema}) == 0
        await verify_consolidated(db, ctx.storage)
    await verify_product_schema(ctx.engine, schema=ctx.storage.native_schema)
    with pytest.raises(BillingStorageError, match="physically consolidated"):
        await rollback(ctx.engine, ctx.storage)


async def test_guard_runs_inside_exclusive_ownership_before_any_table_retirement(cutover):
    ctx = cutover
    before = await projection(ctx)
    called = []

    async def reject(connection):
        pid = await connection.scalar(text("SELECT pg_backend_pid()"))
        held = (await connection.execute(text(
            "SELECT pid,mode FROM pg_locks WHERE locktype='advisory' AND classid=726940 "
            "AND objid='moyai'::regnamespace::oid AND objsubid=2 AND granted"
        ))).all()
        assert held == [(pid, "ExclusiveLock")]
        assert await connection.scalar(text(
            "SELECT relkind::text FROM pg_class WHERE oid='moyai.model_requests'::regclass"
        )) == "r"
        called.append(pid)
        raise BillingStorageError("Synthetic release guard refused cutover.")

    with pytest.raises(BillingStorageError, match="release guard"):
        await consolidate(ctx.engine, ctx.storage, before_consolidate=reject)
    assert len(called) == 1 and (await status(ctx.engine, ctx.storage))["phase"] == "shadow"
    assert await projection(ctx) == before
    comparison = await consolidate(ctx.engine, ctx.storage)
    assert comparison == {
        "legacy": {"source_rows": 1, "mismatches": 0, "extra_rows": 0},
        "native": {"source_rows": 1, "mismatches": 0, "extra_rows": 0},
    }


async def test_native_insert_upsert_locks_and_recovery_updates_use_the_same_row(cutover):
    ctx = cutover
    await consolidate(ctx.engine, ctx.storage)
    sessions = async_sessionmaker(ctx.native, expire_on_commit=False)
    value = new_row(id="later-native", request_key="later-key", cost_usd=None, cost_status="missing")
    statement = insert(InferenceUsage).values(**value)
    async with sessions.begin() as db:
        await db.execute(statement.on_conflict_do_update(
            index_elements=["request_key"], set_={
                "cost_usd": func.coalesce(InferenceUsage.cost_usd, statement.excluded.cost_usd),
            },
        ))
    async with sessions.begin() as db:
        row = await db.get(InferenceUsage, value["id"], with_for_update=True)
        row.cost_usd, row.cost_status, row.cost_source = "0.20000001", "actual", "gateway_recovery"
    async with sessions.begin() as db:
        await db.execute(statement.on_conflict_do_update(
            index_elements=["request_key"], set_={
                "cost_usd": func.coalesce(InferenceUsage.cost_usd, statement.excluded.cost_usd),
            },
        ))
        assert (await db.get(InferenceUsage, value["id"])).cost_usd == "0.20000001"
    async with ctx.engine.connect() as db:
        assert await db.scalar(text(
            f"SELECT cost_usd FROM {ctx.storage.ledger} WHERE source='native' AND source_id='later-native'"
        )) == "0.20000001"
        assert await db.scalar(text(f"SELECT count(*) FROM {ctx.storage.ledger}")) == 3


async def test_legacy_write_and_parent_delete_constraints_survive(cutover):
    ctx = cutover
    await consolidate(ctx.engine, ctx.storage)
    await seed(ctx, old=[old_row(id="later-legacy", cost=None)])
    async with ctx.engine.begin() as db:
        assert (await db.execute(text(
            "UPDATE moyai.model_requests SET cost='1.23456789',cost_source='gateway_recovery' "
            "WHERE id='later-legacy'"
        ))).rowcount == 1
    async with ctx.engine.connect() as db:
        assert await db.scalar(text(
            f"SELECT cost_usd FROM {ctx.storage.ledger} WHERE source='legacy' AND source_id='later-legacy'"
        )) == "1.23456789"
    with pytest.raises(IntegrityError):
        async with ctx.engine.begin() as db:
            await db.execute(text("DELETE FROM moyai.runs WHERE id='historical-child'"))
    with pytest.raises(IntegrityError):
        async with ctx.native.begin() as db:
            await db.execute(text("UPDATE moyai_inference_usage SET actor_id=NULL"))
    with pytest.raises(DBAPIError) as denied:
        async with ctx.native.begin() as db:
            await db.execute(text("UPDATE moyai_inference_usage SET source='legacy'"))
    assert getattr(denied.value.orig, "sqlstate", None) == "44000"


async def test_runtime_owner_prevents_physical_cutover(cutover):
    ctx = cutover
    async with ctx.engine.connect() as owner:
        await owner.execute(text(
            "SELECT pg_advisory_lock_shared(726940,'moyai'::regnamespace::oid::int)"
        ))
        try:
            with pytest.raises(BillingStorageError, match="stop all Moyai"):
                await consolidate(ctx.engine, ctx.storage)
        finally:
            await owner.execute(text(
                "SELECT pg_advisory_unlock_shared(726940,'moyai'::regnamespace::oid::int)"
            ))
    assert (await status(ctx.engine, ctx.storage))["phase"] == "shadow"


async def test_unknown_source_columns_stop_retirement_without_data_loss(cutover):
    ctx = cutover
    async with ctx.engine.begin() as db:
        await db.execute(text("ALTER TABLE moyai.model_requests ADD COLUMN custom_evidence text"))
    with pytest.raises(BillingStorageError, match="unmapped columns"):
        await consolidate(ctx.engine, ctx.storage)
    async with ctx.engine.connect() as db:
        assert await db.scalar(text(
            "SELECT relkind::text FROM pg_class WHERE oid='moyai.model_requests'::regclass"
        )) == "r"
        assert await db.scalar(text("SELECT count(*) FROM moyai.model_requests")) == 1


async def test_existing_view_grantee_cannot_read_the_other_source_or_base_table(cutover):
    ctx = cutover
    role = "billing_reader_" + uuid4().hex
    async with ctx.engine.begin() as db:
        await db.execute(text(f'CREATE ROLE "{role}"'))
        await db.execute(text(f'GRANT USAGE ON SCHEMA moyai TO "{role}"'))
        await db.execute(text(f'GRANT SELECT ON moyai.model_requests TO "{role}"'))
    try:
        await consolidate(ctx.engine, ctx.storage)
        async with ctx.engine.begin() as db:
            await db.execute(text(f'SET LOCAL ROLE "{role}"'))
            assert await db.scalar(text("SELECT count(*) FROM moyai.model_requests")) == 1
        with pytest.raises(ProgrammingError):
            async with ctx.engine.begin() as db:
                await db.execute(text(f'SET LOCAL ROLE "{role}"'))
                await db.execute(text(f"SELECT * FROM {ctx.storage.ledger}"))
    finally:
        async with ctx.engine.begin() as db:
            await db.execute(text(f'DROP OWNED BY "{role}"'))
            await db.execute(text(f'DROP ROLE "{role}"'))


async def test_native_view_definition_drift_is_rejected(cutover):
    ctx = cutover
    await consolidate(ctx.engine, ctx.storage)
    async with ctx.engine.begin() as db:
        await db.execute(text(f"ALTER VIEW {ctx.storage.source_table('native')} RESET(security_barrier)"))
    with pytest.raises(BillingStorageError, match="differs"):
        await verify_product_schema(ctx.engine, schema=ctx.storage.native_schema)


async def test_scoped_legacy_receipt_uniqueness_and_cross_source_ids_survive(cutover):
    ctx = cutover
    async with ctx.engine.begin() as db:
        await db.execute(text(
            "CREATE UNIQUE INDEX original_receipt ON moyai.model_requests"
            "(gateway_scope,key_hash,cost_receipt_id) WHERE cost_receipt_id<>''"
        ))
    await consolidate(ctx.engine, ctx.storage)
    # An existing native ID remains a valid, separate historical source ID.
    await seed(ctx, old=[old_row(id="native-observation", cost_receipt_id="receipt")])
    with pytest.raises(IntegrityError):
        await seed(ctx, old=[old_row(id="duplicate-receipt", cost_receipt_id="receipt")])
    async with ctx.engine.connect() as db:
        assert await db.scalar(text(
            f"SELECT count(*) FROM {ctx.storage.ledger} WHERE source_id='native-observation'"
        )) == 2


async def test_long_historical_identity_survives_without_relaxing_native_identity_limits(cutover):
    ctx = cutover
    identity = "historical:" + "u" * 600
    await seed(ctx, old=[old_row(id="long-history", user_id=identity)])
    await consolidate(ctx.engine, ctx.storage)
    async with ctx.engine.connect() as db:
        assert await db.scalar(text(
            "SELECT user_id FROM moyai.model_requests WHERE id='long-history'"
        )) == identity
    with pytest.raises(IntegrityError) as rejected:
        await seed(ctx, new=[new_row(id="long-native", request_key="long-native-key", actor_id=identity)])
    assert getattr(rejected.value.orig, "sqlstate", None) == "23514"
