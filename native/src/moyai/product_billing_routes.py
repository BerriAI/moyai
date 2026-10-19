"""Read-only product accounting service and authenticated HTTP routes.

Runtime seam::

    runtime.product_billing = ProductBillingService.from_runtime(runtime)
    app.include_router(routes(runtime.product_billing))

The caller owns engines, identity authority, explicit mappings and migrations.
This service starts no broker, recovery worker, provider sync, or legacy agent.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from .analytics import ReportRange, csv_export, report_range
from .analytics_identity import ReportIdentities, report_identities
from .product_billing import (
    LOOKUP_CHUNK,
    MAX_RECORDS,
    BillingCharge,
    BillingReader,
    BillingReadError,
    GatewayKeyScope,
    IdentityMappings,
    LegacyBillingRules,
    NativeBinding,
    ObservationLink,
    RecoveryPolicy,
    SourceRef,
    exact_sum,
    read_product_billing,
)
from .settings_views import AnalyticsReport

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
log = logging.getLogger(__name__)


class _ReportTiming:
    """Fixed phase names and durations only; never SQL, identities or credentials."""

    def __init__(self) -> None:
        self.started = self.phase_started = time.monotonic()
        self.phase = "connect"
        self.phases: dict[str, float] = {}

    def mark(self, phase: str) -> None:
        now = time.monotonic()
        self.phases[self.phase] = round((now - self.phase_started) * 1000, 2)
        self.phase, self.phase_started = phase, now

    def finish(self) -> dict[str, Any]:
        self.mark(self.phase)
        return {
            "phase": self.phase, "elapsed_ms": round((time.monotonic() - self.started) * 1000, 2),
            "phase_ms": self.phases,
        }


def accounting_failure(error: SQLAlchemyError | TimeoutError) -> HTTPException:
    if isinstance(error, (TimeoutError, DatabaseTimeoutError)):
        return HTTPException(503, "The accounting database did not respond. Please try again.")
    if isinstance(error, DBAPIError) and (
        getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
    ) == "57014":
        # PostgreSQL uses query_canceled for statement timeouts and explicit
        # server cancellation. Inspect the driver code, never exception text:
        # SQLAlchemy's message can contain SQL, parameters and credentials.
        return HTTPException(503, "The accounting database query was cancelled. Please try again.")
    return HTTPException(503, "Product accounting is temporarily unavailable.")


class BillingResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class BillingTotals(BillingResponse):
    """Recorded quantities for a set of charges; amounts are exact decimal strings."""

    requests: int
    unknown_requests: int
    observations: int
    priced_requests: int
    pending_costs: int
    missing_costs: int
    estimated_costs: int
    spend: str | None
    cost_usd_exact: str | None
    estimated: str | None
    cost_coverage: float | None
    pricing_status: Literal["partial", "priced", "unpriced", "empty"]
    sessions: int
    unattributed_charges: int
    unpriced_requests: int
    token_samples: int
    usage_samples: int
    token_coverage: float | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None


class BillingModelTotals(BillingTotals):
    model: str | None


class BillingDailyTotals(BillingTotals):
    date: str
    active_users: int
    models: list[BillingModelTotals]
    cumulative_spend: str | None
    cumulative_pricing_status: Literal["partial", "priced", "unpriced", "empty"]
    # Set only when some charges in the range have no timestamp.
    undated_coverage: int | None = None


class BillingUserTotals(BillingTotals):
    id: str | None
    name: str
    email: str
    kind: str


class BillingSessionTotals(BillingTotals):
    user_id: str | None
    user_name: str
    run_id: str | None
    session_id: str | None
    title: str


class BillingRequestSource(BillingResponse):
    engine: str
    id: str
    cost_state: str
    reported_amount: str | None
    cost_source: str | None
    request_status: str
    observed_at: str
    recovery_eligible: bool
    recovery_due: bool
    recovery_reason: str


class BillingRequestDetail(BillingResponse):
    id: str
    user_id: str | None
    session_id: str | None
    model: str | None
    observed_models: list[str]
    created_at: str | None
    timestamp_basis: str
    status: str
    cost: str | None
    estimated_cost: str | None
    cost_status: str
    requests: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    coverage_issues: list[str]
    sources: list[BillingRequestSource]


class BillingInfrastructureSync(BillingResponse):
    status: str


class BillingInfrastructureProvider(BillingResponse):
    provider: str | None
    name: str | None
    spend: str | None
    estimated: str | None
    missing_days: int | None
    covered_days: int | None
    last_synced_at: str | None
    sync_enabled: bool
    sync: BillingInfrastructureSync | None


class BillingInfrastructureBill(BillingResponse):
    provider: str | None
    month: str | None
    amount: str | None
    kind: str | None
    revision: int | None
    updated_at: str | None


class BillingInfrastructure(BillingResponse):
    spend: str | None
    estimated: str | None
    providers: list[BillingInfrastructureProvider]
    bills: list[BillingInfrastructureBill]
    incomplete: bool
    pending: bool
    read_only: bool
    # Set only when infrastructure spend is unavailable for the selected range.
    coverage_reason: str | None = None


class BillingCostSummary(BillingResponse):
    llm: str | None
    infrastructure: str | None
    total: str | None
    estimated: str | None
    incomplete: bool


class BillingCoverage(BillingResponse):
    undated_charges: int
    private_details_omitted: int
    source_observations: int
    legacy_observations: int
    native_observations: int
    deduplicated_observations: int
    unreconciled_native_observations: int
    mapping_version: str
    complete: bool


class BillingCapabilities(BillingResponse):
    read_only: bool
    bill_mutations: bool
    provider_sync: bool
    identity_linking: bool


class BillingReport(BillingResponse):
    # Not BillingResponse's defaults-required flag: it would leak into the nested
    # AnalyticsReport TypedDicts and mark their NotRequired keys required.
    model_config = ConfigDict(json_schema_serialization_defaults_required=False)

    scope: Literal["personal", "organization"]
    start: str
    end: str
    start_at: str
    end_before: str
    preset: str
    timezone: str
    currency: str
    total: BillingTotals
    priced_requests: int
    daily: list[BillingDailyTotals]
    users: list[BillingUserTotals]
    sessions: list[BillingSessionTotals]
    models: list[BillingModelTotals]
    request_details: list[BillingRequestDetail]
    request_total: int
    limit: int
    offset: int
    root_models: list[str]
    identities: list[dict[str, Any]]
    infrastructure: BillingInfrastructure | None
    cost_summary: BillingCostSummary
    coverage: BillingCoverage
    capabilities: BillingCapabilities
    methodology: dict[str, str]
    # The activity report (analytics_service) when include_activity=true.
    activity: AnalyticsReport | None = None


class Identity(Protocol):
    async def authenticate(self, request: Request, *, mutation: bool = False, admin: bool = False) -> Any: ...
    async def require_current(self, grant: Any) -> Any: ...


def _amount(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _unavailable_infrastructure(reason: str) -> dict[str, Any]:
    return {
        "spend": None, "estimated": None, "providers": [], "bills": [], "incomplete": True,
        "pending": False, "coverage_reason": reason, "read_only": True,
    }


def _period(preset: str, start: str | None, end: str | None) -> ReportRange:
    # Existing product date inputs are inclusive calendar dates. Timestamp
    # inputs retain the explicit exclusive upper boundary of native reports.
    if end and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        try:
            end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        except (ValueError, OverflowError):
            raise HTTPException(422, "Choose a valid end date.") from None
    try:
        return report_range(preset, start, end)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def _totals(charges: list[BillingCharge]) -> dict[str, Any]:
    """Report recorded quantities, not prices; canonical charges are deduped first."""
    actual = exact_sum(row.amount for row in charges if row.cost_state == "final" and row.amount is not None)
    estimated = exact_sum(row.amount for row in charges if row.cost_state == "estimated" and row.amount is not None)
    known = sum(row.request_count or 0 for row in charges)
    unknown = sum(row.request_count is None for row in charges)
    priced = sum(row.request_count or 0 for row in charges if row.cost_state == "final")
    pending = sum(row.cost_state == "pending" for row in charges)
    missing = sum(row.cost_state == "missing" for row in charges)
    partial = bool(pending or missing or unknown or estimated is not None)
    result = {
        "requests": known, "unknown_requests": unknown, "observations": sum(len(row.observations) for row in charges),
        "priced_requests": priced, "pending_costs": pending, "missing_costs": missing,
        "estimated_costs": sum(row.cost_state == "estimated" for row in charges),
        "spend": _amount(actual), "cost_usd_exact": _amount(actual), "estimated": _amount(estimated),
        "cost_coverage": priced / known if known and not unknown else None,
        "pricing_status": ("partial" if partial else "priced") if actual is not None else "unpriced" if charges else "empty",
        "sessions": len({row.session_id for row in charges if row.session_id}),
        "unattributed_charges": sum(row.actor_id is None or row.session_id is None for row in charges),
        "unpriced_requests": known - priced,
        "token_samples": sum(row.total_tokens is not None for row in charges),
        "usage_samples": len(charges),
        "token_coverage": (
            sum(all(getattr(row, name) is not None for name in
                    ("input_tokens", "output_tokens", "total_tokens")) for row in charges) / len(charges)
        ) if charges else None,
    }
    for name in TOKEN_FIELDS:
        values = [getattr(row, name) for row in charges if getattr(row, name) is not None]
        result[name] = sum(values) if values else None if charges else 0
    return result


def _public_charge(row: BillingCharge) -> dict[str, Any]:
    # Never serialize whole domain dataclasses: they contain internal key hashes,
    # mapping evidence and gateway scopes. Prompts/content are never selected.
    identity = hashlib.sha256("\0".join(item.source.namespaced_id for item in row.observations).encode()).hexdigest()
    return {
        "id": identity, "user_id": row.actor_id, "session_id": row.session_id, "model": row.model,
        "observed_models": list(row.observed_models),
        "created_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "timestamp_basis": row.timestamp_basis, "status": row.cost_state,
        "cost": _amount(row.amount) if row.cost_state == "final" else None,
        "estimated_cost": _amount(row.amount) if row.cost_state == "estimated" else None,
        "cost_status": row.cost_state, "requests": row.request_count,
        **{name: getattr(row, name) for name in TOKEN_FIELDS}, "coverage_issues": list(row.issues),
        "sources": [{
            "engine": item.source.engine, "id": item.source.id, "cost_state": item.cost_state,
            "reported_amount": _amount(item.amount), "cost_source": item.cost_source,
            "request_status": item.request_status,
            "observed_at": item.occurred_at.isoformat(),
            "recovery_eligible": item.recovery.eligible, "recovery_due": item.recovery.due,
            "recovery_reason": item.recovery.reason,
        } for item in row.observations],
    }


class ProductBillingService:
    def __init__(
        self, identity: Identity, source_engine: AsyncEngine, native_engine: AsyncEngine, *,
        schema: str | None = None, native_schema: str | None = None,
        mappings: IdentityMappings | None = None,
        recovery_policy: RecoveryPolicy | Callable[[], RecoveryPolicy] | None = None,
        native_bindings: Mapping[SourceRef, NativeBinding] | None = None,
        observation_links: tuple[ObservationLink, ...] = (),
        rules: LegacyBillingRules | None = None,
        infrastructure_reporter: Callable[[date, date], dict[str, Any]] | None = None,
        max_records: int | None = None,
    ):
        if source_engine.dialect.name == "sqlite" and not (
            source_engine.url.query.get("mode") == "ro" and source_engine.url.query.get("uri") == "true"
        ):
            raise ValueError("Product billing requires an existing SQLite source opened with mode=ro&uri=true.")
        for selected in (schema, native_schema):
            if selected is not None and not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", selected):
                raise ValueError("Invalid accounting schema.")
        if max_records is not None and not 1 <= max_records <= MAX_RECORDS:
            raise ValueError("Invalid accounting read bound.")
        self.identity, self.source_engine, self.native_engine = identity, source_engine, native_engine
        self.schema, self.native_schema = schema, native_schema
        self.mappings = mappings or IdentityMappings("source-namespaces-v1", "unreconciled-source-identities")
        self.recovery_policy = recovery_policy if recovery_policy is not None else RecoveryPolicy()
        self.native_bindings = native_bindings if native_bindings is not None else {}
        self.observation_links = observation_links
        self.rules = rules or LegacyBillingRules.from_product()
        self.infrastructure_reporter = infrastructure_reporter
        self.max_records = max_records

    @classmethod
    def from_runtime(cls, runtime: Any) -> ProductBillingService:
        """Use existing product resources; never bootstrap a Store or a run."""
        settings = getattr(runtime, "product_settings", None)
        source = getattr(runtime, "product_engine", None)
        identity = getattr(runtime, "product_identity", None)
        store = getattr(runtime, "product_store", None)
        if settings is None or source is None or identity is None:
            raise ValueError("Product billing requires configured product settings, source engine and identity.")
        rules = LegacyBillingRules.from_product()

        def recovery() -> RecoveryPolicy:
            # Hash only inside this trusted boundary. No credential or hash is
            # included in public report DTOs, diagnostics, or logs.
            key_hash = hashlib.sha256(settings.litellm_api_key.encode()).hexdigest() if settings.litellm_api_key else ""
            return RecoveryPolicy(
                settings.litellm_spend_recovery_enabled,
                GatewayKeyScope(rules.gateway_scope(settings.litellm_api_base), key_hash),
            )

        reporter = None
        if store is not None:
            if getattr(store, "schema_updates", True):
                raise ValueError("Product billing requires an existing Store in schema verification mode.")
            from app.infrastructure_costs import InfrastructureCosts

            # Existing allocator, no .start(), sync jobs, or mutation routes.
            infrastructure = InfrastructureCosts(store, settings, identity.legacy_security, None)
            reporter = infrastructure.report
        return cls(
            identity, source, runtime.database.engine,
            schema=settings.moyai_database_schema if source.dialect.name == "postgresql" else None,
            native_schema=(runtime.settings.database_schema or None)
            if runtime.database.engine.dialect.name == "postgresql" else None,
            mappings=getattr(runtime, "product_billing_mappings", None), recovery_policy=recovery,
            native_bindings=getattr(runtime, "product_billing_bindings", None),
            observation_links=tuple(getattr(runtime, "product_billing_links", ())), rules=rules,
            infrastructure_reporter=reporter,
        )

    @asynccontextmanager
    async def _report_read(self) -> AsyncIterator[_ReportTiming]:
        timing = _ReportTiming()
        try:
            yield timing
        except (SQLAlchemyError, TimeoutError, BillingReadError) as error:
            log.warning("Accounting report failed: %s", {
                **timing.finish(), "error_type": type(error).__name__,
            })
            raise
        else:
            log.info("Accounting report completed: %s", timing.finish())

    @asynccontextmanager
    async def _reader(self, engine: AsyncEngine, schema: str | None) -> AsyncIterator[BillingReader]:
        async with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                # Reports have no fixed execution deadline. Override the source
                # engine's five-second query cap only inside this transaction;
                # caller cancellation still closes the reader and rolls back.
                await connection.execute(text("SET LOCAL statement_timeout = '0'"))
                await connection.execute(text("SET LOCAL jit = off"))
            elif engine.dialect.name == "sqlite":
                # sqlite3's legacy transaction mode does not begin on SELECT.
                # Retained period rows and later aliases need one snapshot.
                await connection.execute(text("BEGIN"))
            # Source SQLite is physically read-only. Native SQLite operations
            # are only SELECTs; do not change pooled writer connection PRAGMAs.
            yield BillingReader(connection, schema=schema, max_records=self.max_records)

    async def _labels(self, reader: BillingReader, mappings: IdentityMappings) -> dict[str, dict[str, Any]]:
        users = await reader.table("users", ("id", "kind", "name", "email"), ("id", "kind"))
        bound = self.max_records or MAX_RECORDS
        values = list((await reader.connection.execute(select(users).limit(bound + 1))).mappings())
        if len(values) > bound:
            raise BillingReadError("too_many_accounting_identities")
        result = {}
        for row in values:
            person = mappings.resolve(SourceRef("legacy", row["id"]), actor=True)
            if person:
                result[person.id] = {
                    "id": person.id, "name": row.get("name") or person.id,
                    "email": row.get("email") or "", "kind": row["kind"],
                }
        return result

    async def _visible_details(
        self, old: BillingReader, new: BillingReader, charges: tuple[BillingCharge, ...], viewer_id: str | None,
    ) -> set[tuple[SourceRef, ...]]:
        """Retain spend while preserving product/private-session disclosure rules."""
        old_ids = sorted({
            reference.id for row in charges for item in row.observations if item.source.engine == "legacy"
            for reference in (item.session_source, item.session_family_source) if reference is not None
        })
        new_ids = sorted({
            item.session_source.id for row in charges for item in row.observations if item.source.engine == "native"
        })
        public_runs: set[str] = set()
        if old_ids:
            runs = await old.table("runs", ("id", "private_owner_id"), ("id", "private_owner_id"))
            for offset in range(0, len(old_ids), LOOKUP_CHUNK):
                public_runs.update((await old.connection.execute(select(runs.c.id).where(
                    runs.c.id.in_(old_ids[offset:offset + LOOKUP_CHUNK]), runs.c.private_owner_id == "",
                ))).scalars())
        visible_sessions: set[str] = set()
        viewer = self.mappings.resolve(SourceRef("legacy", viewer_id), actor=True) if viewer_id else None
        if new_ids:
            sessions = await new.table(
                "moyai_sessions", ("id", "owner_id", "visibility"), ("id", "owner_id", "visibility"),
            )
            for offset in range(0, len(new_ids), LOOKUP_CHUNK):
                for row in (await new.connection.execute(select(sessions).where(
                    sessions.c.id.in_(new_ids[offset:offset + LOOKUP_CHUNK]),
                ))).mappings():
                    owner = self.mappings.resolve(SourceRef("native", row["owner_id"]), actor=True)
                    # ProductIdentity.project_native_user preserves its verified
                    # canonical ID. This is the native ownership check, not an
                    # implicit cross-engine financial identity merge.
                    if row["visibility"] == "shared" or viewer_id is not None and (
                        row["owner_id"] == viewer_id or owner is not None and viewer is not None and owner.id == viewer.id
                    ):
                        visible_sessions.add(row["id"])
        return {
            tuple(item.source for item in row.observations) for row in charges
            if all(
                item.session_source.id in public_runs
                and (item.session_family_source is None or item.session_family_source.id in public_runs)
                if item.source.engine == "legacy" else item.session_source.id in visible_sessions
                for item in row.observations
            )
        }

    async def _root_cohorts(
        self, old: BillingReader, new: BillingReader, charges: tuple[BillingCharge, ...],
    ) -> dict[SourceRef, tuple[str, str]]:
        """Use explicit source lineage for root-model/status filters, never equal IDs."""
        references: dict[str, dict[str, set[SourceRef]]] = {"legacy": defaultdict(set), "native": defaultdict(set)}
        for charge in charges:
            for item in charge.observations:
                root = item.session_family_source if item.source.engine == "legacy" else None
                identity = root.id if root else (
                    item.session_source.id if item.source.engine == "legacy" else item.turn_id
                )
                if identity:
                    references[item.source.engine][identity].add(item.source)
        result = {}
        for engine, reader, name in (("legacy", old, "runs"), ("native", new, "moyai_turns")):
            by_id = references[engine]
            if not by_id:
                continue
            table = await reader.table(name, ("id", "model", "status"), ("id", "model", "status"))
            identities = sorted(by_id)
            for offset in range(0, len(identities), LOOKUP_CHUNK):
                rows = (await reader.connection.execute(select(table).where(
                    table.c.id.in_(identities[offset:offset + LOOKUP_CHUNK]),
                ))).mappings()
                for row in rows:
                    for source in by_id[row["id"]]:
                        result[source] = (row["model"], row["status"])
        return result

    async def infrastructure(self, period: ReportRange) -> dict[str, Any]:
        whole_days = period.start.time() == datetime.min.time() and period.end.time() == datetime.min.time()
        if self.infrastructure_reporter is None or not whole_days:
            return _unavailable_infrastructure(
                "not_configured" if self.infrastructure_reporter is None else "requires_utc_calendar_days",
            )
        try:
            raw = await asyncio.to_thread(
                self.infrastructure_reporter, period.start.date(), (period.end - timedelta(microseconds=1)).date(),
            )
        except Exception:  # noqa: BLE001 - never expose a provider/store exception or substitute a partial total
            raise BillingReadError("infrastructure_report_unavailable") from None
        # Reuse allocation, but expose neither free-form bill notes nor provider
        # error/state blobs. Existing mutation/sync capabilities stay disabled.
        return {
            "spend": str(raw["spend"]), "estimated": str(raw["estimated"]),
            "incomplete": bool(raw["incomplete"]), "pending": bool(raw["pending"]), "read_only": True,
            "providers": [{
                **{key: row.get(key) for key in (
                    "provider", "name", "spend", "estimated", "missing_days", "covered_days", "last_synced_at",
                )}, "sync_enabled": False,
                "sync": {"status": row["sync"]["status"]} if row.get("sync") else None,
            } for row in raw["providers"]],
            "bills": [{key: row.get(key) for key in ("provider", "month", "amount", "kind", "revision", "updated_at")}
                      for row in raw["bills"]],
        }

    async def report(
        self, *, preset: str = "7d", start: str | None = None, end: str | None = None,
        user_id: str | None = None, model: str | None = None, limit: int = 100, offset: int = 0,
        personal: bool = False, all_details: bool = False, viewer_id: str | None = None,
        root_model: str | None = None, turn_status: str | None = None,
        include_root_cohorts: bool = False,
        identities: ReportIdentities | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500 or offset < 0:
            raise HTTPException(422, "Choose a valid report page.")
        period = _period(preset, start, end)
        mappings = identities.accounting_mappings(self.mappings) if identities is not None else self.mappings
        async with self._report_read() as timing:
            async with self._reader(self.source_engine, self.schema) as old:
                async with self._reader(self.native_engine, self.native_schema) as new:
                    from .billing_storage import active_storage

                    timing.mark("storage")
                    storage = await active_storage(
                        new.connection, source_schema=self.schema, native_schema=self.native_schema,
                    )
                    old.storage = new.storage = storage
                    old.billing_connection = new.connection
                    policy = self.recovery_policy() if callable(self.recovery_policy) else self.recovery_policy
                    timing.mark("accounting")
                    projection = await read_product_billing(
                        old, new, period, rules=self.rules, mappings=mappings, policy=policy,
                        clock=datetime.now(UTC), native_bindings=self.native_bindings, links=self.observation_links,
                    )
                    timing.mark("cohorts")
                    cohorts = await self._root_cohorts(old, new, projection.charges) if (
                        root_model is not None or turn_status is not None or include_root_cohorts
                    ) else {}
                timing.mark("identities")
                labels = await self._labels(old, mappings)
            timing.mark("aggregates")
            charges = [row for row in projection.charges if
                       (user_id is None or row.actor_id == user_id) and (model is None or row.model == model)
                       and (root_model is None and turn_status is None or any(
                           item.source in cohorts
                           and (root_model is None or cohorts[item.source][0] == root_model)
                           and (turn_status is None or cohorts[item.source][1] == turn_status)
                           for item in row.observations
                       ))]
            by_user: dict[str | None, list[BillingCharge]] = defaultdict(list)
            by_session: dict[tuple[str | None, str | None], list[BillingCharge]] = defaultdict(list)
            by_model: dict[str | None, list[BillingCharge]] = defaultdict(list)
            by_day = {day: [] for day in period.dates()}
            undated = []
            for charge in charges:
                by_user[charge.actor_id].append(charge)
                by_session[(charge.actor_id, charge.session_id)].append(charge)
                by_model[charge.model].append(charge)
                if charge.occurred_at is None:
                    undated.append(charge)
                else:
                    by_day[charge.occurred_at.date().isoformat()].append(charge)
            total = _totals(charges)
            daily = []
            cumulative_spend: Decimal | None = None
            cumulative_partial = cumulative_has_usage = False
            for day, values in by_day.items():
                daily_models: dict[str | None, list[BillingCharge]] = defaultdict(list)
                for value in values:
                    daily_models[value.model].append(value)
                day_total = _totals(values)
                cumulative_spend = exact_sum(
                    amount for amount in (
                        cumulative_spend, Decimal(day_total["spend"]) if day_total["spend"] is not None else None,
                    ) if amount is not None
                )
                cumulative_has_usage = cumulative_has_usage or bool(values)
                cumulative_partial = cumulative_partial or bool(
                    day_total["pending_costs"] or day_total["missing_costs"] or day_total["unknown_requests"]
                    or day_total["estimated"] is not None
                )
                cumulative_status = (
                    ("partial" if cumulative_partial else "priced") if cumulative_spend is not None
                    else "unpriced" if cumulative_has_usage else "empty"
                )
                daily.append({
                    "date": day, **day_total,
                    "active_users": len({row.actor_id for row in values if row.actor_id}),
                    "models": [{"model": key, **_totals(items)} for key, items in daily_models.items()],
                    "cumulative_spend": _amount(cumulative_spend),
                    "cumulative_pricing_status": (
                        "partial" if cumulative_spend is not None else "unpriced"
                    ) if undated else cumulative_status,
                })
                if undated:
                    daily[-1].update(
                        cost_coverage=None,
                        pricing_status="partial" if daily[-1]["spend"] is not None else "unpriced",
                        undated_coverage=len(undated),
                    )
            timing.mark("infrastructure")
            infra = (
                None if personal else _unavailable_infrastructure("not_attributed_to_selected_filter")
                if any(value is not None for value in (user_id, model, root_model, turn_status))
                else await self.infrastructure(period)
            )
            # Infrastructure may have awaited a worker thread. Re-read privacy
            # in fresh snapshots at the detail release boundary.
            timing.mark("privacy")
            async with (
                self._reader(self.source_engine, self.schema) as old,
                self._reader(self.native_engine, self.native_schema) as new,
            ):
                visible = await self._visible_details(old, new, tuple(charges), viewer_id)
            timing.mark("response")
            actual = Decimal(total["spend"]) if total["spend"] is not None else None
            estimate = Decimal(total["estimated"]) if total["estimated"] is not None else None
            infra_total = Decimal(infra["spend"]) if infra and infra["spend"] is not None else None
            infra_estimate = Decimal(infra["estimated"]) if infra and infra["estimated"] is not None else None
            def visible_row(row: BillingCharge) -> bool:
                return tuple(item.source for item in row.observations) in visible
            visible_charges = [row for row in reversed(charges) if visible_row(row)]
            detail_page = visible_charges if all_details else visible_charges[offset:offset + limit]
            return {
                "scope": "personal" if personal else "organization",
                "start": period.start.date().isoformat(), "end": (period.end - timedelta(microseconds=1)).date().isoformat(),
                "start_at": period.start.isoformat(), "end_before": period.end.isoformat(),
                "preset": period.preset, "timezone": "UTC", "currency": "USD",
                "total": total, "priced_requests": total["priced_requests"], "daily": daily,
                "users": [{
                    **labels.get(key or "", {"id": key, "name": key or "Unattributed", "email": "", "kind": "unattributed"}),
                    **_totals(values),
                } for key, values in by_user.items()],
                "sessions": [{
                    "user_id": actor, "user_name": labels.get(actor or "", {}).get("name", actor or "Unattributed"),
                    "run_id": session, "session_id": session, "title": "Session", **_totals(values),
                } for (actor, session), values in by_session.items() if any(visible_row(row) for row in values)],
                "models": [{"model": key, **_totals(values)} for key, values in by_model.items()],
                "request_details": [_public_charge(row) for row in detail_page],
                "request_total": len(visible_charges), "limit": limit, "offset": offset,
                "root_models": sorted({value[0] for value in cohorts.values() if value[0]}),
                "identities": [],  # Account mutation/linking belongs to product identity.
                "infrastructure": infra,
                "cost_summary": {
                    "llm": _amount(actual), "infrastructure": _amount(infra_total),
                    "total": _amount(exact_sum(value for value in (actual, estimate, infra_total) if value is not None)),
                    "estimated": _amount(exact_sum(value for value in (estimate, infra_estimate) if value is not None)),
                    "incomplete": bool(total["pending_costs"] or total["missing_costs"] or total["unknown_requests"]
                                       or (infra and infra["incomplete"])),
                },
                "coverage": {
                    "undated_charges": len(undated),
                    "private_details_omitted": sum(not visible_row(row) for row in charges),
                    "source_observations": sum(len(row.observations) for row in charges),
                    "legacy_observations": sum(
                        item.source.engine == "legacy" for row in charges for item in row.observations
                    ),
                    "native_observations": sum(
                        item.source.engine == "native" for row in charges for item in row.observations
                    ),
                    "deduplicated_observations": sum(max(0, len(row.observations) - 1) for row in charges),
                    "unreconciled_native_observations": sum(
                        any(issue in row.issues for issue in ("native_scope_unbound", "unproven_observation_overlap"))
                        for row in charges
                    ),
                    "mapping_version": mappings.version,
                    "complete": all(row.cost_state == "final" and not row.issues for row in charges),
                },
                "capabilities": {
                    "read_only": True, "bill_mutations": False, "provider_sync": False, "identity_linking": False,
                },
                "methodology": {
                    "cost": "Recorded final USD amounts only. Estimates are separate; unresolved overlaps never add a second charge.",
                    "identity": "Explicit versioned mappings, verified product mirrors and saved Slack links; equal IDs across engines are not identity proof.",
                    "dates": "UTC. Date-only end inputs are inclusive; timestamp end inputs are exclusive. Duplicate observations use the broker's request timestamp only after proven linkage.",
                    "model": "Canonical requested inference alias; conflicting aliases remain unattributed.",
                    "root_model": "Root model and status filters include historical sessions and native turns.",
                    "infrastructure": "Existing product monthly override/allocation rules; rolling intraday ranges stay unavailable.",
                    "execution": "Read-only accounting. Native broker admission is not enabled by these reports.",
                    "privacy": "Financial totals retain private usage. Request and session identifiers follow session privacy; private legacy working sessions never expose detail identifiers.",
                },
            }


def routes(service: ProductBillingService, *, runtime: Any | None = None) -> APIRouter:
    router = APIRouter(tags=["product-billing"])

    async def actor(request: Request) -> Any:
        return await service.identity.authenticate(request, admin=request.url.path != "/api/spend")

    # format=csv returns a file Response, which bypasses the JSON response model.
    @router.get("/api/admin/spend", response_model=BillingReport, operation_id="getAdminSpendReport")
    @router.get("/api/settings/billing", response_model=BillingReport, operation_id="getBillingReport")
    @router.get("/api/spend", response_model=BillingReport, operation_id="getSpendReport")
    async def report(
        request: Request, response: Response, principal: Any = Depends(actor),
        preset: Literal["24h", "7d", "30d", "custom"] = "7d",
        start: str | None = None, end: str | None = None,
        user_id: str | None = None, model: str | None = None,
        root_model: str | None = Query(None, max_length=200),
        turn_status: str | None = Query(None, max_length=30),
        include_activity: bool = False,
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
        format: Literal["json", "csv"] = "json",
        export: Literal["daily", "users", "models", "sessions", "requests", "turns"] = "daily",
    ) -> Any:
        personal = principal.role != "admin"
        if include_activity and personal:
            raise HTTPException(403, "Administrator access is required for organization activity.")
        if export == "turns" and not include_activity:
            raise HTTPException(422, "Turn exports require include_activity=true.")
        if personal:
            if request.url.path != "/api/spend" or not principal.individual:
                raise HTTPException(403, "Sign in with an individual account to view personal spending.")
            own = service.mappings.resolve(SourceRef("legacy", principal.id), actor=True)
            assert own is not None
            if user_id is not None and user_id != own.id:
                raise HTTPException(403, "Another account's spending is unavailable.")
            user_id = own.id
        try:
            identities = await report_identities(runtime) if runtime is not None else None
            if identities is not None and not personal:
                from .analytics_billing import selected_user

                user_id = selected_user(service, identities, user_id)
            value = await service.report(
                preset=preset, start=start, end=end, user_id=user_id, model=model,
                limit=limit, offset=offset, personal=personal, all_details=format == "csv" and export == "requests",
                viewer_id=principal.id,
                root_model=root_model, turn_status=turn_status,
                include_root_cohorts=include_activity,
                identities=identities,
            )
            activity_export = None
            if include_activity:
                if runtime is None or runtime.product_identity is None:
                    raise HTTPException(503, "Product activity reporting is unavailable.")
                async with runtime.database.sessions() as db:
                    native_user = await runtime.product_identity.read_native_user(db, principal)
                if native_user is None:
                    raise HTTPException(401, "Sign in to Moyai to view usage activity.")
                from .analytics_service import report as activity_report

                activity = await activity_report(
                    runtime, native_user, value["start_at"], value["end_before"], root_model,
                    turn_status, user_id, limit, offset,
                    "csv" if format == "csv" and export in {"turns", "users", "daily", "models"} else "json",
                    preset=preset, export=export, billing=service, billing_data=value, identities=identities,
                )
                if isinstance(activity, Response):
                    activity_export = activity
                else:
                    value["activity"] = activity
        except BillingReadError as error:
            bounded = str(error).startswith("too_many_")
            raise HTTPException(
                422 if bounded else 503,
                "Too many accounting records; narrow the date range." if bounded else
                "Accounting evidence is unavailable or needs reconciliation. No partial total was substituted.",
            ) from None
        except (SQLAlchemyError, TimeoutError) as error:
            raise accounting_failure(error) from None
        fresh = await service.identity.require_current(principal.grant)
        if not fresh.enabled or (not personal and fresh.role != "admin"):
            raise HTTPException(403, "Your accounting access changed.")
        response.headers["Cache-Control"] = "no-store"
        if activity_export is not None:
            activity_export.headers["Cache-Control"] = "no-store"
            return activity_export
        if format == "csv":
            selected = value["request_details" if export == "requests" else export]
            rows = [{key: item for key, item in row.items() if key not in {"models", "sources", "observed_models"}}
                    for row in selected]
            columns = list(dict.fromkeys(key for row in rows for key in row))
            return Response(
                csv_export(rows, columns), media_type="text/csv",
                headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="moyai-billing-{export}.csv"'},
            )
        return value

    @router.get("/api/admin/spend/infrastructure", operation_id="getAdminInfrastructureSpend")
    async def infrastructure(
        response: Response, principal: Any = Depends(actor), preset: Literal["24h", "7d", "30d", "custom"] = "7d",
        start: str | None = None, end: str | None = None,
    ) -> BillingInfrastructure:
        try:
            async with service._report_read():
                value = await service.infrastructure(_period(preset, start, end))
        except (BillingReadError, SQLAlchemyError, TimeoutError):
            raise HTTPException(503, "Product infrastructure accounting is temporarily unavailable.") from None
        fresh = await service.identity.require_current(principal.grant)
        if not fresh.enabled or fresh.role != "admin":
            raise HTTPException(403, "Your accounting access changed.")
        response.headers["Cache-Control"] = "no-store"
        return BillingInfrastructure.model_validate(value)

    return router
