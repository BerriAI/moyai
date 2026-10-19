"""Real PostgreSQL migration/concurrency tests; actual-app proof is separate."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, insert, text
from test_product_billing import CLOCK, MAPPINGS, PERIOD, SCOPE, new_row, old_row

from moyai.analytics import report_range
from moyai.billing_storage import (
    BillingStorageError,
    Storage,
    activate,
    active_storage,
    backfill_batch,
    prepare,
    rollback,
    status,
)
from moyai.inference_usage import InferenceUsage
from moyai.product_billing import (
    BillingReader,
    BillingReadError,
    LegacyBillingRules,
    RecoveryPolicy,
    read_product_billing,
)
from moyai.product_inference import ProductInferenceRequest

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)


@pytest_asyncio.fixture
async def billing(prepared_product_database):
    product = prepared_product_database
    storage = Storage("moyai", product.schema)
    async with product.source.begin() as db:
        await db.execute(text("""
            CREATE TABLE moyai.model_requests(
                id TEXT PRIMARY KEY,run_id TEXT,message_id INTEGER,user_id TEXT,model TEXT,
                created_at TEXT,finished_at TEXT,status TEXT,cost TEXT,cost_source TEXT,
                key_hash TEXT,gateway_scope TEXT,gateway_id TEXT,cost_receipt_id TEXT,
                cost_next_attempt_at TEXT,cost_recovery_attempts INTEGER,cost_recovery_error TEXT,
                prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,
                cache_read_input_tokens INTEGER,cache_creation_input_tokens INTEGER,
                private_payload TEXT DEFAULT 'PRIVATE_RESPONSE',api_key TEXT DEFAULT 'PRIVATE_KEY'
            )
        """))
        await db.execute(text(
            "CREATE TABLE moyai.run_roots(run_id TEXT PRIMARY KEY,root_id TEXT)"
        ))
        await db.execute(text(
            "INSERT INTO moyai.run_roots VALUES ('historical-child','historical-root')"
        ))
    return SimpleNamespace(engine=product.source, native=product.database.engine, storage=storage)


async def seed(ctx, *, old=(), new=()):
    async with ctx.engine.begin() as db:
        for row in old:
            values = {name: value for name, value in row.items() if name != "root_id"}
            await db.execute(text(
                f"INSERT INTO moyai.model_requests ({','.join(values)}) "
                f"VALUES ({','.join(':' + name for name in values)})"
            ), values)
    async with ctx.native.begin() as db:
        for row in new:
            await db.execute(insert(InferenceUsage).values(**row))


async def finish_backfill(ctx, size=1):
    for source in ("legacy", "native"):
        while not await backfill_batch(ctx.engine, ctx.storage, source, batch_size=size):
            pass


async def projection(ctx):
    async with ctx.engine.connect() as old_conn, ctx.native.connect() as new_conn:
        await old_conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await new_conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        selected = await active_storage(
            new_conn, source_schema=ctx.storage.source_schema, native_schema=ctx.storage.native_schema,
        )
        old = BillingReader(old_conn, schema=ctx.storage.source_schema, storage=selected, billing_connection=new_conn)
        new = BillingReader(new_conn, schema=ctx.storage.native_schema, storage=selected)
        return await read_product_billing(
            old, new, PERIOD, rules=LegacyBillingRules.from_product(), mappings=MAPPINGS,
            policy=RecoveryPolicy(True, SCOPE), clock=CLOCK,
        )


async def test_batched_counterpart_lookup_keeps_aliases_namespaces_and_exact_rows(billing):
    ctx = billing
    await seed(ctx, old=[
        old_row(id="shared-id", gateway_id="legacy-only", cost_receipt_id=""),
        old_row(id="alias-row", gateway_id="wanted-alias", cost_receipt_id="wanted-alias"),
    ], new=[
        new_row(id="shared-id", request_key="native-key", provider_request_id="wanted-alias"),
    ])
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    await activate(ctx.engine, ctx.storage)
    # No rows are selected by date: only counterpart aliases can discover them.
    outside = report_range("custom", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
    identities = [f"absent-{index}" for index in range(10_100)] + ["wanted-alias", "shared-id"]
    async with ctx.engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        old = BillingReader(connection, schema=ctx.storage.source_schema, storage=ctx.storage)
        new = BillingReader(connection, schema=ctx.storage.native_schema, storage=ctx.storage)
        historical = await old.rows("legacy", outside, matching=identities)
        current = await new.rows("native", outside, matching=identities)
    assert {row["id"] for row in historical} == {"shared-id", "alias-row"}
    assert len(historical) == 2  # Matching both aliases never duplicates a charge.
    assert len(current) == 1 and current[0]["id"] == "shared-id"
    assert all(row["cost"] == "0.123400" for row in historical)
    assert current[0]["cost_usd"] == "0.1234"


async def test_consolidated_reader_fetches_more_than_fifty_thousand_records(billing):
    ctx = billing
    async with ctx.engine.begin() as connection:
        await connection.execute(text("""
            INSERT INTO moyai.model_requests(id,run_id,user_id,model,created_at,status,cost,gateway_id)
            SELECT 'bulk-' || n,'historical-child','google-user','route',
                   '2026-10-02T00:00:00+00:00','complete','0.000000000000000001','call-' || n
            FROM generate_series(1,50001) n
        """))
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx, size=5000)
    await activate(ctx.engine, ctx.storage)
    async with ctx.engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        reader = BillingReader(connection, schema=ctx.storage.source_schema, storage=ctx.storage)
        records = await reader.rows("legacy", PERIOD)
        assert len(records) == 50_001
        assert {row["id"] for row in records} == {f"bulk-{n}" for n in range(1, 50_002)}
        assert all(row["cost"] == "0.000000000000000001" for row in records)
        bounded = BillingReader(connection, schema=ctx.storage.source_schema, storage=ctx.storage, max_records=2)
        with pytest.raises(BillingReadError, match="too_many_accounting_records"):
            await bounded.rows("legacy", PERIOD)


async def test_large_native_counterpart_lookup_keeps_admission_proof_with_few_queries(billing):
    ctx = billing
    await seed(ctx, new=[new_row(id="native-key", request_key="native-key")])
    async with ctx.native.begin() as connection:
        await connection.execute(insert(ProductInferenceRequest).values(
            id="broker-key", request_key="native-key", logical_key="logical-key",
            session_id="native-session", turn_id="native-turn", actor_id="native-user",
            authority_version="fixture", model="route", gateway_scope=SCOPE.gateway_scope,
            key_hash=SCOPE.key_hash, status="completed", gateway_id="known-gateway",
            cost_receipt_id="known-receipt", created_at="2026-10-02T00:00:00+00:00",
            owner_pid=1, owner_started="2026-10-02T00:00:00+00:00",
        ))
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    await activate(ctx.engine, ctx.storage)
    queries = []

    @event.listens_for(ctx.native.sync_engine, "before_cursor_execute")
    def observed(_conn, _cursor, statement, _parameters, _context, _many):
        if f"FROM {ctx.storage.native_schema}.moyai_product_inference_requests" in statement:
            queries.append(statement)

    outside = report_range("custom", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
    candidates = [f"absent-{index}" for index in range(10_100)] + [
        "known-gateway", "known-receipt", "broker-key",
    ]
    async with ctx.native.connect() as connection:
        reader = BillingReader(connection, schema=ctx.storage.native_schema, storage=ctx.storage)
        records = await reader.rows("native", outside, matching=candidates, outside_period=True)
    assert len(records) == 1 and records[0]["id"] == "native-key"
    assert records[0]["_admission"]["id"] == "broker-key"
    assert records[0]["_admission"]["cost_receipt_id"] == "known-receipt"
    assert 1 <= len(queries) <= 4  # Avoid one admission query per 200 aliases.


async def test_exact_projection_survives_backfill_cutover_and_rollback(billing):
    ctx = billing
    await seed(ctx, old=[
        old_row(id="same-id", cost="0.12340000000000000001", user_id="google-user"),
        old_row(id="missing-cost", cost=None, prompt_tokens=None, gateway_id="missing"),
    ], new=[
        new_row(id="same-id", request_key="native-same", cost_usd="0.99", provider_request_id="distinct"),
        new_row(id="native-missing", request_key="native-missing", cost_usd=None, cost_status="missing"),
    ])
    before = await projection(ctx)
    await prepare(ctx.engine, ctx.storage)
    await prepare(ctx.engine, ctx.storage)
    assert await projection(ctx) == before
    with pytest.raises(BillingStorageError, match="Finish"):
        await activate(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    assert await activate(ctx.engine, ctx.storage) == {
        "legacy": {"source_rows": 2, "mismatches": 0, "extra_rows": 0},
        "native": {"source_rows": 2, "mismatches": 0, "extra_rows": 0},
    }
    queries = []
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        queries.append(statement)
    event.listen(ctx.engine.sync_engine, "before_cursor_execute", capture)
    event.listen(ctx.native.sync_engine, "before_cursor_execute", capture)
    try:
        assert await projection(ctx) == before
    finally:
        event.remove(ctx.engine.sync_engine, "before_cursor_execute", capture)
        event.remove(ctx.native.sync_engine, "before_cursor_execute", capture)
    assert any("moyai_billing_records WHERE" in query for query in queries)
    assert not any(query.startswith(("SELECT moyai.model_requests.", f'SELECT "{ctx.storage.native_schema}".moyai_inference_usage.')) for query in queries)
    async with ctx.engine.connect() as db:
        records = (await db.execute(text(f"SELECT * FROM {ctx.storage.ledger}"))).mappings().all()
        assert len(records) == 4
        assert "PRIVATE_" not in repr(records) and "api_key" not in repr(records) and "private_payload" not in repr(records)
        legacy = next(row for row in records if row["source"] == "legacy" and row["source_id"] == "same-id")
        assert legacy["cost_usd"] == "0.12340000000000000001"
    await rollback(ctx.engine, ctx.storage)
    assert await projection(ctx) == before
    assert (await status(ctx.engine, ctx.storage))["phase"] == "shadow"


async def test_mirror_captures_commits_recovery_and_deletes_but_not_rollbacks(billing):
    ctx = billing
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    await activate(ctx.engine, ctx.storage)
    await seed(ctx, old=[old_row(cost=None)], new=[new_row(cost_usd=None, cost_status="missing")])
    async with ctx.engine.connect() as db:
        transaction = await db.begin()
        await db.execute(text("UPDATE moyai.model_requests SET cost='999' WHERE id='broker-request'"))
        await transaction.rollback()
    async with ctx.engine.begin() as db:
        await db.execute(text(
            "UPDATE moyai.model_requests SET cost='0.123400',cost_source='gateway_recovery' WHERE id='broker-request'"
        ))
        await db.execute(text(
            f"UPDATE {ctx.storage.source_table('native')} SET cost_usd='0.10000009',cost_status='actual' "
            "WHERE id='native-observation'"
        ))
    async with ctx.engine.connect() as db:
        records = {row["source"]: row for row in (
            await db.execute(text(f"SELECT * FROM {ctx.storage.ledger}"))
        ).mappings()}
        assert records["legacy"]["cost_usd"] == "0.123400"
        assert records["legacy"]["cost_source"] == "gateway_recovery"
        assert records["native"]["cost_usd"] == "0.10000009"
    await activate(ctx.engine, ctx.storage)  # Exact comparison still holds after late updates.
    async with ctx.engine.begin() as db:
        await db.execute(text("DELETE FROM moyai.model_requests WHERE id='broker-request'"))
        await db.execute(text(f"TRUNCATE {ctx.storage.source_table('native')}"))
    assert (await status(ctx.engine, ctx.storage))["records"] == {}


async def test_resume_and_updates_before_or_behind_cursor_are_not_lost(billing):
    ctx = billing
    await seed(ctx, old=[old_row(id="b"), old_row(id="d", gateway_id="later")])
    await prepare(ctx.engine, ctx.storage)
    assert not await backfill_batch(ctx.engine, ctx.storage, "legacy", batch_size=1)
    await seed(ctx, old=[old_row(id="a", gateway_id="new-earlier-id")])
    async with ctx.engine.begin() as db:
        await db.execute(text("UPDATE moyai.model_requests SET cost='0.8' WHERE id='d'"))
        await db.execute(text("UPDATE moyai.model_requests SET id='c' WHERE id='b'"))
    await finish_backfill(ctx)
    await finish_backfill(ctx)
    comparison = await activate(ctx.engine, ctx.storage)
    assert comparison["legacy"] == {"source_rows": 3, "mismatches": 0, "extra_rows": 0}
    async with ctx.engine.connect() as db:
        saved = dict((await db.execute(text(
            f"SELECT source_id,cost_usd FROM {ctx.storage.ledger} WHERE source='legacy'"
        ))).all())
        assert saved == {"a": "0.123400", "c": "0.123400", "d": "0.8"}


@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_backfill_race_preserves_committed_updates_and_deletions(billing, mutation):
    ctx = billing
    await seed(ctx, old=[old_row(cost=None)])
    await prepare(ctx.engine, ctx.storage)
    async with ctx.engine.connect() as writer:
        transaction = await writer.begin()
        await writer.execute(text(
            "UPDATE moyai.model_requests SET cost='0.712345' WHERE id='broker-request'" if mutation == "update"
            else "DELETE FROM moyai.model_requests WHERE id='broker-request'"
        ))
        task = asyncio.create_task(backfill_batch(ctx.engine, ctx.storage, "legacy"))
        try:
            async with ctx.engine.connect() as observer:
                for _ in range(200):
                    await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    blocked = await observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query LIKE '%WITH batch AS MATERIALIZED%'"
                    ))
                    if blocked:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("The backfill did not reach the source-row lock.")
            await transaction.commit()
            assert await asyncio.wait_for(task, 5)
        finally:
            if transaction.is_active:
                await transaction.rollback()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await finish_backfill(ctx)
    assert (await activate(ctx.engine, ctx.storage))["legacy"] == {
        "source_rows": 1 if mutation == "update" else 0, "mismatches": 0, "extra_rows": 0,
    }


@pytest.mark.parametrize("tamper", ["record", "extra", "disabled_mirror"])
async def test_cutover_refuses_incomplete_or_unreliable_copy(billing, tamper):
    ctx = billing
    await seed(ctx, old=[old_row()])
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    async with ctx.engine.begin() as db:
        if tamper == "record":
            await db.execute(text(f"UPDATE {ctx.storage.ledger} SET cost_usd='999'"))
        elif tamper == "extra":
            await db.execute(text(
                f"INSERT INTO {ctx.storage.ledger}(source,source_id,created_at,cost_usd) "
                f"SELECT source,'extra',created_at,cost_usd "
                f"FROM {ctx.storage.ledger}"
            ))
        else:
            await db.execute(text("ALTER TABLE moyai.model_requests DISABLE TRIGGER moyai_billing_mirror"))
    with pytest.raises(BillingStorageError, match="differ|mirror"):
        await activate(ctx.engine, ctx.storage)
    async with ctx.engine.connect() as db:
        assert await db.scalar(text(f"SELECT phase FROM {ctx.storage.state} WHERE id=1")) == "shadow"


async def test_active_report_fails_closed_if_mirror_is_disabled(billing):
    ctx = billing
    await prepare(ctx.engine, ctx.storage)
    await finish_backfill(ctx)
    await activate(ctx.engine, ctx.storage)
    async with ctx.engine.begin() as db:
        await db.execute(text("ALTER TABLE moyai.model_requests DISABLE TRIGGER moyai_billing_mirror"))
    with pytest.raises(BillingReadError, match="consolidated_accounting_storage_unavailable"):
        await projection(ctx)
