"""Isolated read-only stores; no legacy runtime, provider calls, or credentials."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, insert, text
from sqlalchemy.ext.asyncio import create_async_engine
from test_product_identity import google_cookie, settings_for
from test_product_identity import product as product_fixture

from moyai.analytics import report_range
from moyai.inference_usage import InferenceUsage
from moyai.product_billing import (
    BillingProjection,
    BillingReader,
    BillingReadError,
    GatewayKeyScope,
    IdentityLink,
    IdentityMappings,
    LegacyBillingRules,
    NativeBinding,
    ObservationLink,
    RecoveryPolicy,
    RequestIdentity,
    SourceRef,
    legacy_observation,
    native_observation,
    read_product_billing,
    reconcile,
)
from moyai.product_billing_routes import ProductBillingService, routes

GATEWAY = hashlib.sha256(b"https://gateway.invalid").hexdigest()
KEY = hashlib.sha256(b"synthetic-key-hash-input").hexdigest()
SCOPE = GatewayKeyScope(GATEWAY, KEY)
PERIOD = report_range("custom", "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
CLOCK = datetime(2026, 10, 15, tzinfo=UTC)
MAPPINGS = IdentityMappings(
    version="identity-migration-1", provenance="verified-product-map",
    actors={SourceRef("legacy", "google-user"): "person", SourceRef("native", "native-user"): "person"},
    sessions={SourceRef("legacy", "historical-root"): "logical-session",
              SourceRef("native", "native-session"): "logical-session"},
)


@pytest.fixture
def rules():
    """Product mode imports existing rules normally; no runtime is initialized."""
    return LegacyBillingRules.from_product()


def old_row(**overrides):
    return {
        "id": "broker-request", "run_id": "historical-child", "root_id": "historical-root",
        "message_id": 7, "user_id": "slack-user", "model": "route",
        "created_at": "2026-10-02T00:00:00+00:00", "finished_at": "2026-10-02T00:00:01+00:00",
        "status": "complete", "cost": "0.123400", "cost_source": "response_usage",
        "key_hash": KEY, "gateway_scope": GATEWAY, "gateway_id": "gateway-call",
        "cost_receipt_id": "", "cost_next_attempt_at": "2026-10-03T00:00:00+00:00",
        "cost_recovery_attempts": 2, "cost_recovery_error": "",
        "prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
        "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2,
        **overrides,
    }


def new_row(**overrides):
    return {
        "id": "native-observation", "request_key": "physical-attempt-1",
        "session_id": "native-session", "turn_id": "native-turn", "actor_id": "native-user",
        "model": "route", "provider_scope": "different-native-hash-convention",
        "provider_request_id": "gateway-call", "response_id": "response-cache-id",
        "provider_timestamp": "2026-10-02T00:00:00+00:00", "reasoning_tokens": 2,
        "created_at": "2026-10-02T00:00:00.001+00:00", "finished_at": "2026-10-02T00:00:01+00:00",
        "cost_usd": "0.1234", "cost_status": "actual", "cost_source": "response_usage", "status": "completed",
        "input_tokens": 12, "output_tokens": 4, "total_tokens": 16,
        "cache_read_tokens": 3, "cache_write_tokens": 2,
        **overrides,
    }


def binding(call="gateway-call", *, scope=SCOPE):
    return NativeBinding(scope, RequestIdentity("gateway_call", call), "verified-broker-metadata", "request-epoch-1")


@pytest.mark.parametrize("usable", [True, False])
def test_native_admission_preserves_canonical_slack_accounting_link(usable):
    row = new_row(actor_id="slack-user")
    row["_admission"] = {
        **{name: row[name] for name in ("actor_id", "session_id", "turn_id", "model")},
        "gateway_scope": GATEWAY, "key_hash": KEY, "status": "completed",
    }
    observed = native_observation(
        row, MAPPINGS, binding=binding(), identity_links={
            "slack-user": IdentityLink(
                SourceRef("legacy", "slack-user"), SourceRef("legacy", "google-user"),
                "slack", "manual", "manual", usable,
            ),
        },
    )
    assert observed.actor.id == ("person" if usable else "legacy:slack-user")
    assert observed.actor_source == SourceRef("native", "slack-user")
    assert observed.amount == Decimal("0.1234")


@pytest_asyncio.fixture
async def store(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'existing-product.sqlite'}")
    async with engine.begin() as conn:
        # These are retained historical fixtures, not bridge rows created by
        # the adapter. The native session intentionally has NO legacy run row.
        await conn.exec_driver_sql(
            "CREATE TABLE runs (id TEXT PRIMARY KEY,parent_run_id TEXT,private_payload TEXT,"
            "private_owner_id TEXT DEFAULT '')"
        )
        await conn.exec_driver_sql(
            "INSERT INTO runs(id,parent_run_id,private_payload) VALUES ('historical-root','','PRIVATE_RUN'),"
            "('historical-child','historical-root','PRIVATE_CHILD')"
        )
        await conn.exec_driver_sql(
            "CREATE VIEW run_roots AS SELECT id AS run_id,CASE WHEN parent_run_id='' THEN id "
            "ELSE parent_run_id END AS root_id FROM runs"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE users(id TEXT PRIMARY KEY,kind TEXT,linked_user_id TEXT,link_method TEXT,"
            "link_status TEXT,password TEXT)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO users VALUES ('google-user','google',NULL,'','','PRIVATE_PASSWORD'),"
            "('slack-user','slack','google-user','manual','manual','PRIVATE_PASSWORD'),"
            "('dangling-slack','slack','missing-user','email','review','PRIVATE_PASSWORD')"
        )
        await conn.exec_driver_sql("""
            CREATE TABLE model_requests(
                id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs(id),message_id INTEGER,user_id TEXT,model TEXT,
                created_at TEXT,finished_at TEXT,status TEXT,cost TEXT,cost_source TEXT,
                key_hash TEXT,gateway_scope TEXT,gateway_id TEXT,cost_receipt_id TEXT,
                cost_next_attempt_at TEXT,cost_recovery_attempts INTEGER,cost_recovery_error TEXT,
                prompt_tokens INTEGER,completion_tokens INTEGER,total_tokens INTEGER,
                cache_read_input_tokens INTEGER,cache_creation_input_tokens INTEGER,
                private_payload TEXT DEFAULT 'PRIVATE_RESPONSE',api_key TEXT DEFAULT 'PRIVATE_KEY'
            )
        """)
        await conn.exec_driver_sql(
            "CREATE TABLE infrastructure_daily_costs(provider TEXT,scope TEXT,day TEXT,amount TEXT,synced_at TEXT)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE infrastructure_bills(provider TEXT,month TEXT,amount TEXT,kind TEXT,"
            "revision INTEGER,updated_at TEXT,note TEXT,actor TEXT)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE infrastructure_sync_jobs(provider TEXT,scope TEXT,start TEXT,\"end\" TEXT,status TEXT,"
            "state TEXT,error TEXT DEFAULT '',updated_at TEXT DEFAULT '')"
        )
        await conn.run_sync(InferenceUsage.__table__.create)
    yield engine
    await engine.dispose()


async def seed(store, *, old=None, new=None):
    async with store.begin() as conn:
        for row in old or []:
            values = {key: value for key, value in row.items() if key != "root_id"}
            columns = ",".join(values)
            parameters = ",".join(f":{key}" for key in values)
            await conn.execute(text(f"INSERT INTO model_requests({columns}) VALUES({parameters})"), values)
        for row in new or []:
            await conn.execute(insert(InferenceUsage).values(**row))


async def project(store, rules, *, period=PERIOD, bindings=None, links=(), mappings=MAPPINGS, max_records=50_000):
    async with store.connect() as conn:
        await conn.exec_driver_sql("PRAGMA query_only=ON")
        reader = BillingReader(conn, max_records=max_records)
        try:
            return await read_product_billing(
                reader, reader, period, rules=rules, mappings=mappings,
                policy=RecoveryPolicy(True, SCOPE), clock=CLOCK,
                native_bindings=bindings, links=links,
            )
        finally:
            await conn.exec_driver_sql("PRAGMA query_only=OFF")


async def test_existing_rules_and_read_only_projection_preserve_identity_decimal_and_scope(store, rules):
    await seed(store, old=[old_row()], new=[new_row()])
    queries = []
    @event.listens_for(store.sync_engine, "before_cursor_execute")
    def observed(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)
    result = await project(store, rules, bindings={SourceRef("native", "native-observation"): binding()})
    charge, = result.charges
    assert charge.request_count == 1 and len(charge.observations) == 2
    assert charge.cost_state == "final" and charge.amount == Decimal("0.123400")
    assert (charge.input_tokens, charge.output_tokens, charge.total_tokens) == (12, 4, 16)
    assert charge.model == "route" and charge.observed_models == ("route",)
    assert result.actual_subtotal == Decimal("0.1234") and result.complete
    assert charge.actor_id == "person" and charge.session_id == "logical-session"
    legacy, native = charge.observations
    assert legacy.actor_source == SourceRef("legacy", "slack-user")
    assert legacy.session_source == SourceRef("legacy", "historical-child")
    assert legacy.session_family_source == SourceRef("legacy", "historical-root")
    assert legacy.actor.provenance == "verified-product-map" and legacy.actor.version == "identity-migration-1"
    assert native.native_provider_scope == "different-native-hash-convention"
    assert native.scope == SCOPE and native.scope_version == "request-epoch-1"
    assert native.provider_response_id == "response-cache-id"
    assert native.provider_timestamp == datetime(2026, 10, 2, tzinfo=UTC)
    assert charge.reasoning_tokens == 2
    assert native.input_tokens == 12 and legacy.cache_write_tokens == 2
    assert charge.timestamp_basis == "legacy_request_created_at"
    assert any(link.source.id == "slack-user" and link.target.id == "google-user" and link.usable
               for link in result.identity_links)
    assert not any(value in repr(result) for value in ("PRIVATE_RESPONSE", "PRIVATE_KEY", "PRIVATE_PASSWORD"))
    assert not any(query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
                   for query in queries)
    data_selects = [query for query in queries if query.lstrip().startswith("SELECT model_requests.")]
    assert data_selects and all("private_payload" not in query and "api_key" not in query for query in data_selects)
    async with store.connect() as conn:
        assert (await conn.exec_driver_sql("SELECT count(*) FROM runs")).scalar_one() == 2
        assert (await conn.exec_driver_sql("SELECT count(*) FROM runs WHERE id='native-session'")).scalar_one() == 0


async def test_unproven_overlap_retains_evidence_without_double_billing(store, rules):
    await seed(store, old=[old_row()], new=[new_row()])
    result = await project(store, rules)
    assert len(result.charges) == 2
    charge = next(row for row in result.charges if row.cost_state == "missing")
    assert charge.cost_state == "missing" and charge.amount is None and charge.request_count is None
    assert charge.occurred_at is None and charge.timestamp_basis == "unresolved_observation_overlap"
    assert "unproven_observation_overlap" in charge.issues
    assert charge.overlap_candidates == (SourceRef("legacy", "broker-request"),)
    assert result.actual_subtotal == Decimal("0.1234") and not result.complete
    assert charge.observations[0].amount == Decimal("0.1234")


async def test_unbound_native_candidate_cannot_erase_two_proven_legacy_charges(store, rules):
    await seed(store, old=[
        old_row(id="legacy-a", cost=".1"), old_row(id="legacy-b", key_hash="rotated-key", cost=".2"),
    ], new=[new_row(cost_usd=".1")])
    result = await project(store, rules)
    assert result.actual_subtotal == Decimal(".3")
    assert len(result.charges) == 3
    assert sorted(row.amount for row in result.charges if row.cost_state == "final") == [Decimal(".1"), Decimal(".2")]
    assert next(row for row in result.charges if row.cost_state == "missing").request_count is None


async def test_unbound_native_observation_is_not_silently_added_to_mixed_product_total(store, rules):
    await seed(store, old=[old_row()], new=[new_row(provider_request_id="other-call")])
    result = await project(store, rules)
    assert result.actual_subtotal == Decimal("0.1234") and not result.complete
    missing = next(row for row in result.charges if "native_scope_unbound" in row.issues)
    assert missing.amount is None and missing.observations[0].cost_state == "final"
    assert missing.observations[0].amount == Decimal("0.1234")


@pytest.mark.parametrize("scope", [GatewayKeyScope(GATEWAY, "rotated-key-hash"), GatewayKeyScope("other-gateway", KEY)])
async def test_known_key_rotations_and_gateway_boundaries_do_not_collapse_receipts(store, rules, scope):
    await seed(store, old=[old_row()], new=[new_row()])
    result = await project(store, rules, bindings={SourceRef("native", "native-observation"): binding(scope=scope)})
    assert len(result.charges) == 2
    assert result.actual_subtotal == Decimal("0.2468")


async def test_distinct_paid_retries_with_same_model_response_are_not_deduplicated(store, rules):
    await seed(store, new=[
        new_row(id="attempt-1", request_key="attempt-1", provider_request_id="call-1"),
        new_row(id="attempt-2", request_key="attempt-2", provider_request_id="call-2"),
    ])
    result = await project(store, rules, bindings={
        SourceRef("native", f"attempt-{i}"): binding(f"call-{i}") for i in (1, 2)
    })
    assert len(result.charges) == 2 and sum(row.request_count for row in result.charges) == 2
    assert result.actual_subtotal == Decimal("0.2468")


async def test_explicit_broker_link_can_reconcile_pre_binding_receipts(store, rules):
    await seed(store, old=[old_row(gateway_id="")], new=[new_row(provider_request_id=None)])
    link = ObservationLink(SourceRef("legacy", "broker-request"), SourceRef("native", "native-observation"),
                           "verified-broker-acknowledgement", "receipt-v1")
    result = await project(store, rules, links=[link])
    assert len(result.charges) == 1 and result.actual_subtotal == Decimal("0.1234")


async def test_conflicting_actual_amounts_are_not_chosen_or_summed(store, rules):
    await seed(store, old=[old_row(cost=".1")], new=[new_row(cost_usd=".2")])
    result = await project(store, rules, bindings={SourceRef("native", "native-observation"): binding()})
    charge, = result.charges
    assert charge.request_count == 1 and charge.cost_state == "missing" and charge.amount is None
    assert "conflicting_actual_costs" in charge.issues and result.actual_subtotal is None


async def test_raw_id_coincidence_never_maps_people_or_logical_sessions(store, rules):
    await seed(store, old=[old_row(user_id="same-id")],
               new=[new_row(actor_id="same-id", session_id="historical-root")])
    result = await project(
        store, rules, bindings={SourceRef("native", "native-observation"): binding()},
        mappings=IdentityMappings("unmapped-v1", "no-cross-engine-map"),
    )
    charge, = result.charges
    assert charge.amount == Decimal("0.1234") and charge.actor_id is None and charge.session_id is None
    assert {"actor_mapping_conflict", "session_mapping_conflict"} <= set(charge.issues)
    assert result.cost_complete and not result.complete


@pytest.mark.parametrize(("overrides", "policy", "state", "eligible", "due", "reason"), [
    ({}, RecoveryPolicy(True, SCOPE), "pending", True, True, "due"),
    ({"cost_next_attempt_at": "2026-10-16T00:00:00Z"}, RecoveryPolicy(True, SCOPE), "pending", True, False, "backoff"),
    ({"status": "pending"}, RecoveryPolicy(True, SCOPE), "pending", False, False, "in_flight"),
    ({}, RecoveryPolicy(False, SCOPE), "missing", False, False, "disabled"),
    ({}, RecoveryPolicy(True, GatewayKeyScope(GATEWAY, "new-key")), "missing", False, False, "scope_changed"),
    ({}, RecoveryPolicy(True), "missing", False, False, "missing_scope"),
    ({"cost_next_attempt_at": ""}, RecoveryPolicy(True, SCOPE), "pending", True, False, "unscheduled"),
    ({"cost_next_attempt_at": "invalid"}, RecoveryPolicy(True, SCOPE), "pending", True, False, "invalid_schedule"),
    ({"cost_recovery_error": "receipt_access_denied"}, RecoveryPolicy(True, SCOPE), "missing", True, True, "due"),
])
def test_recovery_eligibility_preserves_existing_display_and_poll_rules(rules, overrides, policy, state, eligible, due, reason):
    row = legacy_observation(old_row(cost=None, **overrides), rules, MAPPINGS, {}, policy=policy, clock=CLOCK)
    assert row.cost_state == state
    assert (row.recovery.eligible, row.recovery.due, row.recovery.reason) == (eligible, due, reason)
    assert row.recovery.attempts == 2 and row.scope == SCOPE


async def test_actual_zero_and_pending_legacy_counterpart_are_distinct(store, rules):
    await seed(store, old=[old_row(cost=None)], new=[new_row(cost_usd="0")])
    result = await project(store, rules, bindings={SourceRef("native", "native-observation"): binding()})
    charge, = result.charges
    assert charge.amount == Decimal(0) and charge.cost_state == "final"
    assert result.actual_subtotal == Decimal(0)
    legacy = next(row for row in charge.observations if row.source.engine == "legacy")
    assert legacy.cost_state == "pending" and legacy.recovery.due


async def test_native_estimates_never_become_final_actual_cost(store, rules):
    await seed(store, new=[new_row(cost_status="estimated", cost_source="explicit-estimate")])
    result = await project(store, rules, bindings={SourceRef("native", "native-observation"): binding()})
    charge, = result.charges
    assert charge.cost_state == "estimated" and charge.amount == Decimal("0.1234")
    assert result.actual_subtotal is None and not result.complete


async def test_counterpart_lookup_precedes_date_filter_across_midnight(store, rules):
    await seed(store, old=[old_row(created_at="2026-10-01T23:59:59+00:00")],
               new=[new_row(created_at="2026-10-02T00:00:01+00:00")])
    bindings = {SourceRef("native", "native-observation"): binding()}
    first = await project(store, rules, bindings=bindings,
                          period=report_range("custom", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"))
    second = await project(store, rules, bindings=bindings,
                           period=report_range("custom", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z"))
    assert len(first.charges) == 1 and len(first.charges[0].observations) == 2
    assert second.charges == ()
    unresolved = await project(store, rules,
                               period=report_range("custom", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z"))
    assert len(unresolved.charges) == 1 and unresolved.charges[0].occurred_at is None


async def test_reconciliation_reads_period_once_and_follows_new_cross_date_aliases(store, rules, monkeypatch):
    await seed(store, old=[
        old_row(id="inside", gateway_id="first-call", created_at="2026-10-02T00:00:00+00:00"),
        old_row(id="outside-broker", gateway_id="second-call", created_at="2026-09-30T23:59:59+00:00"),
    ], new=[new_row(provider_request_id="first-call", created_at="2026-09-30T23:59:59+00:00")])
    original = BillingReader.records
    inside_reads = 0

    async def counted(reader, *args, **kwargs):
        nonlocal inside_reads
        async for row in original(reader, *args, **kwargs):
            inside_reads += row.get("id") == "inside"
            yield row

    monkeypatch.setattr(BillingReader, "records", counted)
    result = await project(store, rules, bindings={
        SourceRef("native", "native-observation"): NativeBinding(
            SCOPE, RequestIdentity("broker_request", "outside-broker"), "verified", "1",
        ),
    })
    # The out-of-period broker anchors its native counterpart outside October.
    # The in-period request must not be fetched again for every closure round.
    assert len(result.charges) == 1
    assert result.charges[0].observations[0].source == SourceRef("legacy", "inside")
    assert result.actual_subtotal == Decimal("0.123400")
    assert inside_reads == 1


async def test_explicit_read_bound_covers_accumulated_counterparts(store, rules):
    await seed(store, old=[
        old_row(id="inside", gateway_id="shared-call"),
        old_row(id="outside-1", gateway_id="shared-call", created_at="2026-09-30T00:00:00+00:00"),
        old_row(id="outside-2", gateway_id="shared-call", created_at="2026-09-30T00:00:01+00:00"),
    ])
    with pytest.raises(BillingReadError, match="too_many_accounting_records"):
        await project(store, rules, max_records=2)


async def test_counterpart_with_missing_date_is_not_silently_discarded(store, rules):
    await seed(store, old=[old_row(), old_row(id="undated", created_at=None)])
    with pytest.raises(BillingReadError, match="invalid_accounting_timestamp"):
        await project(store, rules)


async def test_read_bound_and_missing_schema_raise_without_creating_bridge_rows(store, rules):
    await seed(store, old=[old_row(id=f"request-{i}", gateway_id=f"call-{i}") for i in range(3)])
    with pytest.raises(BillingReadError, match="too_many_accounting_records"):
        await project(store, rules, max_records=2)
    async with store.connect() as conn:
        await conn.exec_driver_sql("PRAGMA query_only=ON")
        with pytest.raises(BillingReadError, match="missing_accounting_source"):
            await BillingReader(conn).table("missing_source", ["id"], ["id"])
        assert (await conn.exec_driver_sql("SELECT count(*) FROM runs")).scalar_one() == 2
        assert (await conn.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='missing_source'")).scalar_one() == 0


async def test_report_exceeds_old_record_ceiling_without_losing_totals_or_detail_pages(
    billing_product, monkeypatch,
):
    import moyai.product_billing_routes as billing_routes

    # This is an accuracy/population regression, not a runner-speed benchmark.
    # Keep it bounded on slower CI workers; deadline enforcement is tested below.
    monkeypatch.setattr(billing_routes, "REPORT_TIMEOUT_SECONDS", 60.0)
    context = billing_product
    count = 50_001
    base = old_row(user_id="google:admin-sub", cost="0.000000000000000001")
    await seed(context.writer, old=[base], new=[new_row(cost_usd=base["cost"])])
    async with context.writer.begin() as connection:
        await connection.execute(text("""
            WITH RECURSIVE numbers(n) AS (
                SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<:count
            )
            INSERT INTO model_requests(
                id,run_id,user_id,model,created_at,status,cost,key_hash,gateway_scope,gateway_id,
                prompt_tokens,completion_tokens,total_tokens
            )
            SELECT 'bulk-' || n,'historical-child','google:admin-sub','route',
                   '2026-10-03T00:00:00+00:00','complete','0.000000000000000001',
                   :key,:scope,'bulk-call-' || n,12,4,16
            FROM numbers
        """), {"count": count - 1, "key": KEY, "scope": GATEWAY})
    result = await context.service.report(
        start="2026-10-01", end="2026-10-31", viewer_id="google:admin-sub", offset=50_000,
    )
    assert result["total"]["requests"] == count
    assert Decimal(result["total"]["spend"]) == Decimal("0.000000000000050001")
    assert result["total"]["total_tokens"] == count * 16
    assert result["coverage"]["deduplicated_observations"] == 1
    assert result["request_total"] == count and len(result["request_details"]) == 1
    assert sum(Decimal(row["spend"] or "0") for row in result["daily"]) == Decimal(result["total"]["spend"])
    assert result["daily"][-1]["cumulative_spend"] == result["total"]["spend"]
    assert result["users"][0]["spend"] == result["models"][0]["spend"] == result["total"]["spend"]


@pytest.mark.parametrize("interrupt", ["deadline", "caller"])
async def test_report_cancellation_releases_readers(billing_product, monkeypatch, interrupt):
    import moyai.product_billing_routes as billing_routes

    context = billing_product
    started, cancelled = asyncio.Event(), asyncio.Event()
    deadlines = []
    real_timeout = asyncio.timeout
    real_read = billing_routes.read_product_billing

    def observed_timeout(delay):
        # Verify the production budget, then expire its real timeout only once
        # both database readers are open. No sleep or machine-speed assumption.
        assert delay == 15.0
        deadline = real_timeout(delay)
        deadlines.append(deadline)
        return deadline

    async def blocked_read(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(billing_routes.asyncio, "timeout", observed_timeout)
    monkeypatch.setattr(billing_routes, "read_product_billing", blocked_read)
    task = asyncio.create_task(context.service.report(start="2026-10-01", end="2026-10-31"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert len(deadlines) == 1
        if interrupt == "deadline":
            deadlines[0].reschedule(asyncio.get_running_loop().time())
        else:
            task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        assert task in done, "The report must stop at its own deadline or caller cancellation."
        with pytest.raises(TimeoutError if interrupt == "deadline" else asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert deadlines[0].expired() is (interrupt == "deadline")
        assert context.source.pool.checkedout() == context.writer.pool.checkedout() == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    monkeypatch.setattr(billing_routes, "read_product_billing", real_read)
    result = await context.service.report(start="2026-10-01", end="2026-10-31")
    assert result["total"]["requests"] == 0


def test_reconciliation_population_is_not_limited_by_fetch_bounds(rules, monkeypatch):
    import moyai.product_billing as billing

    monkeypatch.setattr(billing, "MAX_RECORDS", 2)
    observations = [
        legacy_observation(
            old_row(id=f"request-{index}", gateway_id=f"call-{index}"),
            rules, MAPPINGS, {}, policy=RecoveryPolicy(), clock=CLOCK,
        ) for index in range(5)
    ]
    assert len(reconcile(observations, period=PERIOD)) == 5


async def test_daily_cumulative_totals_preserve_missing_costs_and_exact_precision(billing_product):
    context = billing_product
    await seed(context.writer, old=[
        old_row(id="missing", gateway_id="missing-call", cost=None, cost_next_attempt_at="",
                user_id="google:admin-sub", created_at="2026-10-02T00:00:00+00:00"),
        old_row(id="priced", gateway_id="priced-call", cost="0.000000000000000001",
                user_id="google:admin-sub", created_at="2026-10-03T00:00:00+00:00"),
        old_row(id="zero", gateway_id="zero-call", cost="0.00",
                user_id="google:admin-sub", created_at="2026-10-04T00:00:00+00:00"),
    ])
    value = await context.service.report(start="2026-10-01", end="2026-10-05")
    assert [row["cumulative_pricing_status"] for row in value["daily"]] == [
        "empty", "unpriced", "partial", "partial", "partial",
    ]
    assert [row["cumulative_spend"] for row in value["daily"]] == [None, None, "1E-18", "1E-18", "1E-18"]
    assert value["daily"][-1]["cumulative_spend"] == value["total"]["spend"]


async def test_service_reader_keeps_one_sqlite_snapshot_during_cost_recovery(billing_product):
    context = billing_product
    await seed(context.writer, old=[old_row()])
    async with context.writer.connect() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    async with context.service._reader(context.source, None) as reader:
        first = await reader.rows("legacy", PERIOD)
        async with context.writer.begin() as writer:
            await writer.execute(text("UPDATE model_requests SET cost='9.99'"))
        again = await reader.rows("legacy", PERIOD)
        assert first[0]["cost"] == again[0]["cost"] == "0.123400"
    async with context.service._reader(context.source, None) as reader:
        fresh = await reader.rows("legacy", PERIOD)
        assert fresh[0]["cost"] == "9.99"


async def test_infrastructure_sources_preserve_credits_estimates_overrides_scope_and_exclusive_end(store, rules):
    async with store.begin() as conn:
        await conn.exec_driver_sql("""
            INSERT INTO infrastructure_daily_costs VALUES
            ('modal','current','2026-10-01','2.125000','2026-10-02'),
            ('modal','old-scope','2026-10-01','999','2026-10-02'),
            ('modal','current','2026-11-01','999','2026-11-02')
        """)
        await conn.exec_driver_sql("""
            INSERT INTO infrastructure_bills VALUES
            ('modal','2026-10','-3.000000','invoice',2,'2026-10-03','PRIVATE_NOTE','PRIVATE_ACTOR'),
            ('render','2026-10','7.25','estimate',1,'2026-10-03','PRIVATE_NOTE','PRIVATE_ACTOR'),
            ('render','2026-11','999','invoice',1,'2026-11-03','','')
        """)
        await conn.exec_driver_sql("""
            INSERT INTO infrastructure_sync_jobs(provider,scope,start,"end",status,state) VALUES
            ('modal','current','2026-10-01','2026-10-03','pending','PRIVATE_STATE'),
            ('modal','old-scope','2026-10-01','2026-10-03','pending','PRIVATE_STATE')
        """)
    async with store.connect() as conn:
        await conn.exec_driver_sql("PRAGMA query_only=ON")
        result = await BillingReader(conn).infrastructure(PERIOD, scopes={"modal": "current"}, rules=rules)
    assert len(result.sources) == 3 and len(result.pending_syncs) == 1
    assert next(row.source_amount for row in result.sources if row.kind == "daily") == Decimal("2.125000")
    assert next(row.source_amount for row in result.sources if row.kind == "monthly" and row.provider == "modal") == Decimal("-3.000000")
    assert next(row.cost_state for row in result.sources if row.provider == "render") == "estimated"
    assert result.allocation_owner == "app.infrastructure_costs.InfrastructureCosts.report"
    assert "PRIVATE_" not in repr(result)
    # Deliberately no total here: the monthly invoice replaces daily amounts.
    assert not hasattr(result, "total") and not hasattr(result, "spend")


async def test_infrastructure_scope_filter_is_applied_before_limit(store, rules):
    async with store.begin() as conn:
        await conn.exec_driver_sql("""
            INSERT INTO infrastructure_daily_costs VALUES
            ('modal','old','2026-10-01','99','x'),('modal','old','2026-10-02','99','x'),
            ('modal','current','2026-10-01','1','x')
        """)
    async with store.connect() as conn:
        result = await BillingReader(conn, max_records=1).infrastructure(PERIOD, scopes={"modal": "current"}, rules=rules)
    assert len(result.sources) == 1 and result.sources[0].source_amount == 1


def test_adapter_does_not_import_legacy_execution_and_keeps_conflicting_proofs_explicit(rules):
    from app.billing_sources import amount
    from app.spend import cost_status, gateway_scope, money
    from app.spend_recovery import receipt

    assert rules.money is money and rules.cost_status is cost_status
    assert rules.infrastructure_amount is amount and rules.gateway_scope is gateway_scope
    assert rules.validate_recovery_receipt is receipt
    assert "app.main" not in sys.modules and "app.agent" not in sys.modules
    old = legacy_observation(old_row(), rules, MAPPINGS, {}, policy=RecoveryPolicy(), clock=CLOCK)
    new = native_observation(new_row(), MAPPINGS, binding=binding(scope=GatewayKeyScope(GATEWAY, "different-key")))
    link = ObservationLink(old.source, new.source, "proof", "1")
    with pytest.raises(BillingReadError, match="conflicting_link_scope"):
        reconcile([old, new], links=[link])
    with pytest.raises(BillingReadError, match="conflicting_source_versions"):
        reconcile([old, replace(old, amount=Decimal(9))])
    assert BillingProjection(reconcile([old, old])).actual_subtotal == Decimal("0.1234")


def test_legacy_subtotal_keeps_precision_beyond_native_receipt_bound(rules):
    amount = "0." + "1234567890" * 18
    row = legacy_observation(old_row(cost=amount), rules, MAPPINGS, {}, policy=RecoveryPolicy(), clock=CLOCK)
    subtotal = BillingProjection(reconcile([row])).actual_subtotal
    assert subtotal.as_tuple() == Decimal(amount).as_tuple()


def test_conflicting_token_or_model_observations_do_not_double_count_or_guess(rules):
    old = legacy_observation(old_row(), rules, MAPPINGS, {}, policy=RecoveryPolicy(), clock=CLOCK)
    new = native_observation(new_row(input_tokens=13, model="other-route"), MAPPINGS, binding=binding())
    charge, = reconcile([old, new])
    assert charge.cost_state == "final" and charge.amount == Decimal("0.1234")
    assert charge.input_tokens is None and charge.output_tokens == 4
    assert charge.model is None and charge.observed_models == ("other-route", "route")
    assert {"conflicting_input_tokens", "model_mapping_conflict"} <= set(charge.issues)


def test_existing_recovery_validator_still_rejects_unverified_zero_and_wrong_key(rules):
    from app.spend_recovery import ReceiptError

    response = {
        "total_is_capped": False, "total": 1, "page_size": 2, "total_pages": 1, "page": 1,
        "data": [{"request_id": "broker-request", "api_key": KEY, "status": "success", "spend": "0"}],
    }
    with pytest.raises(ReceiptError, match="unverified_zero"):
        rules.validate_recovery_receipt(response, old_row(cost=None))
    response["data"][0]["spend"] = "0.123400"
    assert rules.validate_recovery_receipt(response, old_row(cost=None)) == ("broker-request", "0.123400")
    response["data"][0]["api_key"] = "wrong-hash"
    with pytest.raises(ReceiptError, match="scope_mismatch"):
        rules.validate_recovery_receipt(response, old_row(cost=None))


@pytest_asyncio.fixture
async def billing_authority(tmp_path):
    fixture = product_fixture.__wrapped__(tmp_path)
    try:
        yield await anext(fixture)
    finally:
        await fixture.aclose()


@pytest_asyncio.fixture
async def billing_product(store, billing_authority):
    # Use the real product identity fixture, and transplant only the empty
    # accounting schema into its isolated canonical database.
    product = billing_authority
    async with store.connect() as connection:
        definitions = list((await connection.execute(text(
            "SELECT type,sql FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT IN ('users','sqlite_sequence') AND sql IS NOT NULL "
            "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END,name"
        ))).all())
    async with product.writer.begin() as connection:
        for _, statement in definitions:
            await connection.exec_driver_sql(statement)
        await connection.exec_driver_sql(
            "INSERT INTO runs(id,parent_run_id,private_payload) VALUES ('historical-root','','PRIVATE_RUN'),"
            "('historical-child','historical-root','PRIVATE_CHILD')"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE moyai_sessions(id TEXT PRIMARY KEY,owner_id TEXT,visibility TEXT)"
        )
        await connection.exec_driver_sql(
            "INSERT INTO moyai_sessions VALUES ('native-session','native-user','private')"
        )
    product.settings.litellm_api_key = "synthetic-key-hash-input"
    product.settings.litellm_api_base = "https://gateway.invalid/v1"
    product.settings.litellm_spend_recovery_enabled = True
    product.settings.modal_token_id = product.settings.modal_token_secret = ""
    product.settings.temporal_billing_api_key = ""
    mapping = IdentityMappings(
        "verified-product-test-v1", "explicit-fixture-proof",
        actors={
            SourceRef("legacy", "google:admin-sub"): "google:admin-sub",
            SourceRef("native", "native-user"): "google:admin-sub",
            SourceRef("legacy", "google:member-sub"): "google:member-sub",
        },
        sessions=MAPPINGS.sessions,
    )

    class ReadOnlyStore:
        schema_updates = False

        @contextmanager
        def connect(self):
            from app.database import sqlite_connection

            connection = sqlite_connection(product.writer.url.database)
            connection.execute("PRAGMA query_only=ON")
            try:
                yield connection
            finally:
                connection.close()

        def rows(self, statement, parameters=()):
            with self.connect() as connection:
                return [dict(row) for row in connection.execute(statement, parameters)]

    runtime = SimpleNamespace(
        product_settings=product.settings, product_identity=product.adapter, product_engine=product.source,
        product_store=ReadOnlyStore(), database=SimpleNamespace(engine=product.writer),
        settings=SimpleNamespace(database_schema=None), product_billing_mappings=mapping,
        product_billing_bindings={SourceRef("native", "native-observation"): binding()},
    )
    service = ProductBillingService.from_runtime(runtime)
    app = FastAPI()
    app.include_router(routes(service))
    return SimpleNamespace(
        service=service, app=app, runtime=runtime, source=product.source, writer=product.writer,
        identity=product.adapter, settings=product.settings,
    )


async def test_admin_routes_reuse_product_identity_full_aggregates_and_redact_internal_scopes(billing_product):
    context = billing_product
    await seed(context.writer, old=[
        old_row(user_id="google:admin-sub"),
        old_row(id="legacy-only", gateway_id="other-call", user_id="google:admin-sub", cost="0.200000"),
    ], new=[new_row()])
    cookie, _ = google_cookie(context.identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        assert (await client.get("/api/admin/spend")).status_code == 401
        client.cookies.set("workspace_session", cookie)
        for path in ("/api/admin/spend", "/api/settings/billing", "/api/spend"):
            response = await client.get(path, params={"start": "2026-10-01", "end": "2026-10-31", "limit": 1})
            assert response.status_code == 200, response.text
            value = response.json()
            assert value["total"]["spend"] == "0.323400" and value["total"]["requests"] == 2
            assert value["request_total"] == 2 and len(value["request_details"]) == 1
            assert value["coverage"]["deduplicated_observations"] == 1
            assert value["end_before"] == "2026-11-01T00:00:00+00:00"
            assert value["capabilities"]["read_only"] and response.headers["cache-control"] == "no-store"
            assert not any(secret in response.text for secret in (
                KEY, GATEWAY, "synthetic-key-hash-input", "PRIVATE_", "different-native-hash-convention",
            ))
        exported = await client.get("/api/admin/spend", params={
            "start": "2026-10-01", "end": "2026-10-31", "limit": 1, "format": "csv", "export": "requests",
        })
        assert len(list(csv.DictReader(io.StringIO(exported.text)))) == 2
        assert KEY not in exported.text and "PRIVATE_" not in exported.text
        assert (await client.post("/api/admin/spend/infrastructure/sync")).status_code in {404, 405}


async def test_personal_route_does_not_expose_other_accounts_or_infrastructure(billing_product):
    context = billing_product
    await seed(context.writer, old=[
        old_row(user_id="google:admin-sub", cost="8"),
        old_row(id="member-request", gateway_id="member-call", user_id="google:member-sub", cost=".25"),
    ])
    cookie, _ = google_cookie(context.identity, sub="member-sub", email="member@example.test")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        client.cookies.set("workspace_session", cookie)
        assert (await client.get("/api/admin/spend")).status_code == 403
        response = await client.get("/api/spend", params={"start": "2026-10-01", "end": "2026-10-31"})
        assert response.status_code == 200
        value = response.json()
        assert value["scope"] == "personal" and value["total"]["spend"] == "0.25"
        assert value["infrastructure"] is None and len(value["users"]) == 1
        assert "admin@example.test" not in response.text and "google:admin-sub" not in response.text
        assert (await client.get("/api/spend", params={"user_id": "google:admin-sub"})).status_code == 403


async def test_live_role_change_during_report_denies_the_result(billing_product, monkeypatch):
    context = billing_product
    original = context.service.report

    async def revoke_after_read(**kwargs):
        result = await original(**kwargs)
        async with context.writer.begin() as connection:
            await connection.execute(text(
                "INSERT INTO user_roles VALUES ('admin@example.test','member',1,'synthetic-admin','now')"
            ))
        return result

    monkeypatch.setattr(context.service, "report", revoke_after_read)
    cookie, _ = google_cookie(context.identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        client.cookies.set("workspace_session", cookie)
        response = await client.get("/api/admin/spend", params={"start": "2026-10-01", "end": "2026-10-31"})
    assert response.status_code == 401
    assert "request_details" not in response.text


async def test_existing_infrastructure_allocator_overrides_daily_and_stays_read_only(billing_product):
    context = billing_product
    await seed(context.writer, old=[old_row(user_id="google:admin-sub", cost=".1")])
    allocator = context.service.infrastructure_reporter.__self__
    scope = allocator.scope("modal")
    async with context.writer.begin() as connection:
        await connection.execute(text(
            "INSERT INTO infrastructure_daily_costs VALUES ('modal',:scope,'2026-10-01','500','now')"
        ), {"scope": scope})
        await connection.exec_driver_sql("""
            INSERT INTO infrastructure_bills VALUES
            ('modal','2026-10','31','invoice',1,'now','PRIVATE_NOTE','PRIVATE_ACTOR'),
            ('render','2026-10','10','estimate',1,'now','PRIVATE_NOTE','PRIVATE_ACTOR')
        """)
    value = await context.service.report(start="2026-10-01", end="2026-10-31")
    assert value["infrastructure"]["spend"] == "41" and value["infrastructure"]["estimated"] == "10"
    assert value["cost_summary"]["total"] == "41.1" and value["cost_summary"]["estimated"] == "10"
    assert all(not row["sync_enabled"] for row in value["infrastructure"]["providers"])
    assert "PRIVATE_" not in str(value)
    rolling = await context.service.report(preset="24h")
    assert rolling["infrastructure"]["spend"] is None
    assert rolling["infrastructure"]["coverage_reason"] == "requires_utc_calendar_days"
    filtered = await context.service.report(start="2026-10-01", end="2026-10-31", model="route")
    assert filtered["infrastructure"]["coverage_reason"] == "not_attributed_to_selected_filter"
    assert filtered["cost_summary"]["total"] == "0.1"


async def test_infrastructure_failure_is_not_a_fake_partial_success_or_secret_leak(billing_product):
    def fail(_start, _end):
        raise RuntimeError("PRIVATE_DSN_PASSWORD")
    context = billing_product
    context.service.infrastructure_reporter = fail
    cookie, _ = google_cookie(context.identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        client.cookies.set("workspace_session", cookie)
        response = await client.get("/api/admin/spend", params={"start": "2026-10-01", "end": "2026-10-31"})
        infra = await client.get("/api/admin/spend/infrastructure", params={"start": "2026-10-01", "end": "2026-10-31"})
    assert response.status_code == infra.status_code == 503
    assert "PRIVATE_" not in response.text + infra.text


async def test_private_legacy_ids_are_withheld_without_dropping_recorded_spend(billing_product):
    context = billing_product
    async with context.writer.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO runs(id,parent_run_id,private_payload,private_owner_id) "
            "VALUES ('hidden-run','','PRIVATE_CONTEXT','google:member-sub')"
        )
    await seed(context.writer, old=[
        old_row(user_id="google:admin-sub"),
        old_row(id="hidden-request", run_id="hidden-run", user_id="google:member-sub", gateway_id="hidden-call", cost="5"),
    ])
    cookie, _ = google_cookie(context.identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        client.cookies.set("workspace_session", cookie)
        response = await client.get("/api/admin/spend", params={"start": "2026-10-01", "end": "2026-10-31"})
    assert response.status_code == 200
    value = response.json()
    assert value["total"]["spend"] == "5.123400" and value["total"]["requests"] == 2
    assert value["coverage"]["private_details_omitted"] == 1
    assert value["request_total"] == 1 and len(value["sessions"]) == 1
    assert "hidden-run" not in response.text and "hidden-request" not in response.text and "PRIVATE_" not in response.text


async def test_native_private_billing_details_have_no_admin_bypass(billing_product):
    context = billing_product
    context.service.mappings = IdentityMappings(
        "verified-product-test-v2", "explicit-fixture-proof",
        actors={**context.service.mappings.actors, SourceRef("native", "other-native-user"): "google:member-sub"},
        sessions=context.service.mappings.sessions,
    )
    async with context.writer.begin() as connection:
        await connection.execute(text("UPDATE moyai_sessions SET owner_id='other-native-user'"))
    await seed(context.writer, new=[new_row(actor_id="other-native-user")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        cookie, _ = google_cookie(context.identity)
        client.cookies.set("workspace_session", cookie)
        value = (await client.get("/api/admin/spend", params={"start": "2026-10-01", "end": "2026-10-31"})).json()
        assert value["total"]["spend"] == "0.1234" and value["request_details"] == []
        cookie, _ = google_cookie(context.identity, sub="member-sub", email="member@example.test")
        client.cookies.set("workspace_session", cookie)
        value = (await client.get("/api/spend", params={"start": "2026-10-01", "end": "2026-10-31"})).json()
        assert value["scope"] == "personal" and len(value["request_details"]) == 1


async def test_privacy_is_rechecked_after_infrastructure_await(billing_product, monkeypatch):
    context = billing_product
    await seed(context.writer, old=[old_row(user_id="google:admin-sub")])
    original = context.service.infrastructure

    async def change_privacy(period):
        result = await original(period)
        async with context.writer.begin() as connection:
            await connection.execute(text("UPDATE runs SET private_owner_id='google:member-sub'"))
        return result

    monkeypatch.setattr(context.service, "infrastructure", change_privacy)
    cookie, _ = google_cookie(context.identity)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context.app), base_url="http://testserver") as client:
        client.cookies.set("workspace_session", cookie)
        value = (await client.get("/api/admin/spend", params={"start": "2026-10-01", "end": "2026-10-31"})).json()
    assert value["total"]["spend"] == "0.123400" and value["request_details"] == []
    assert value["coverage"]["private_details_omitted"] == 1


def test_from_runtime_keeps_schemas_distinct_and_does_no_io_before_lifespan(tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Constructor attempted I/O before start")

    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=forbidden)
    proxy = SimpleNamespace(schema_updates=False, rows=forbidden)
    runtime = SimpleNamespace(
        product_settings=settings_for(tmp_path, moyai_database_schema="moyai_source",
                                      litellm_api_key="synthetic-only", litellm_api_base="https://gateway.invalid/v1"),
        product_engine=engine, database=SimpleNamespace(engine=engine),
        product_store=proxy, product_identity=SimpleNamespace(legacy_security=object()),
        settings=SimpleNamespace(database_schema="moyai_core"),
    )
    service = ProductBillingService.from_runtime(runtime)
    assert service.schema == "moyai_source" and service.native_schema == "moyai_core"
    assert service.infrastructure_reporter.__self__.store is proxy
    assert "app.main" not in sys.modules and "app.agent" not in sys.modules
