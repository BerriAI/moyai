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
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .analytics import ReportRange, csv_export, report_range
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

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
REPORT_TIMEOUT_SECONDS = 15.0


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
        max_records: int = MAX_RECORDS,
    ):
        if source_engine.dialect.name == "sqlite" and not (
            source_engine.url.query.get("mode") == "ro" and source_engine.url.query.get("uri") == "true"
        ):
            raise ValueError("Product billing requires an existing SQLite source opened with mode=ro&uri=true.")
        for selected in (schema, native_schema):
            if selected is not None and not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,62}", selected):
                raise ValueError("Invalid accounting schema.")
        if not 1 <= max_records <= MAX_RECORDS:
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
    async def _reader(self, engine: AsyncEngine, schema: str | None) -> AsyncIterator[BillingReader]:
        async with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                await connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            # Source SQLite is physically read-only. Native SQLite operations
            # are only SELECTs; do not change pooled writer connection PRAGMAs.
            yield BillingReader(connection, schema=schema, max_records=self.max_records)

    async def _labels(self, reader: BillingReader) -> dict[str, dict[str, Any]]:
        users = await reader.table("users", ("id", "kind", "name", "email"), ("id", "kind"))
        values = list((await reader.connection.execute(select(users).limit(self.max_records + 1))).mappings())
        if len(values) > self.max_records:
            raise BillingReadError("too_many_accounting_identities")
        result = {}
        for row in values:
            person = self.mappings.resolve(SourceRef("legacy", row["id"]), actor=True)
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
    ) -> dict[str, Any]:
        if not 1 <= limit <= 500 or not 0 <= offset <= self.max_records * 2:
            raise HTTPException(422, "Choose a valid report page.")
        period = _period(preset, start, end)
        async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
            async with self._reader(self.source_engine, self.schema) as old:
                async with self._reader(self.native_engine, self.native_schema) as new:
                    policy = self.recovery_policy() if callable(self.recovery_policy) else self.recovery_policy
                    projection = await read_product_billing(
                        old, new, period, rules=self.rules, mappings=self.mappings, policy=policy,
                        clock=datetime.now(UTC), native_bindings=self.native_bindings, links=self.observation_links,
                    )
                labels = await self._labels(old)
            charges = [row for row in projection.charges if
                       (user_id is None or row.actor_id == user_id) and (model is None or row.model == model)]
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
            daily, accumulated = [], []
            for day, values in by_day.items():
                accumulated.extend(values)
                daily_models: dict[str | None, list[BillingCharge]] = defaultdict(list)
                for value in values:
                    daily_models[value.model].append(value)
                cumulative = _totals(accumulated)
                daily.append({
                    "date": day, **_totals(values),
                    "active_users": len({row.actor_id for row in values if row.actor_id}),
                    "models": [{"model": key, **_totals(items)} for key, items in daily_models.items()],
                    "cumulative_spend": cumulative["spend"],
                    "cumulative_pricing_status": (
                        "partial" if cumulative["spend"] is not None else "unpriced"
                    ) if undated else cumulative["pricing_status"],
                })
                if undated:
                    daily[-1].update(
                        cost_coverage=None,
                        pricing_status="partial" if daily[-1]["spend"] is not None else "unpriced",
                        undated_coverage=len(undated),
                    )
            infra = (
                None if personal else _unavailable_infrastructure("not_attributed_to_selected_filter")
                if user_id is not None or model is not None else await self.infrastructure(period)
            )
            # Infrastructure may have awaited a worker thread. Re-read privacy
            # in fresh snapshots at the detail release boundary.
            async with (
                self._reader(self.source_engine, self.schema) as old,
                self._reader(self.native_engine, self.native_schema) as new,
            ):
                visible = await self._visible_details(old, new, tuple(charges), viewer_id)
            actual = Decimal(total["spend"]) if total["spend"] is not None else None
            estimate = Decimal(total["estimated"]) if total["estimated"] is not None else None
            infra_total = Decimal(infra["spend"]) if infra and infra["spend"] is not None else None
            infra_estimate = Decimal(infra["estimated"]) if infra and infra["estimated"] is not None else None
            def visible_row(row: BillingCharge) -> bool:
                return tuple(item.source for item in row.observations) in visible
            details = [_public_charge(row) for row in reversed(charges) if visible_row(row)]
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
                "request_details": details if all_details else details[offset:offset + limit],
                "request_total": len(details), "limit": limit, "offset": offset,
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
                    "deduplicated_observations": sum(max(0, len(row.observations) - 1) for row in charges),
                    "unreconciled_native_observations": sum(
                        any(issue in row.issues for issue in ("native_scope_unbound", "unproven_observation_overlap"))
                        for row in charges
                    ),
                    "mapping_version": self.mappings.version,
                    "complete": all(row.cost_state == "final" and not row.issues for row in charges),
                },
                "capabilities": {
                    "read_only": True, "bill_mutations": False, "provider_sync": False, "identity_linking": False,
                },
                "methodology": {
                    "cost": "Recorded final USD amounts only. Estimates are separate; unresolved overlaps never add a second charge.",
                    "identity": "Explicit versioned mappings and saved Slack links; equal IDs across engines are not identity proof.",
                    "dates": "UTC. Date-only end inputs are inclusive; timestamp end inputs are exclusive. Duplicate observations use the broker's request timestamp only after proven linkage.",
                    "model": "Canonical requested inference alias; conflicting aliases remain unattributed.",
                    "infrastructure": "Existing product monthly override/allocation rules; rolling intraday ranges stay unavailable.",
                    "execution": "Read-only accounting. Native broker admission is not enabled by these reports.",
                    "privacy": "Financial totals retain private usage. Request and session identifiers follow session privacy; private legacy working sessions never expose detail identifiers.",
                },
            }


def routes(service: ProductBillingService) -> APIRouter:
    router = APIRouter(tags=["product-billing"])

    async def actor(request: Request) -> Any:
        return await service.identity.authenticate(request, admin=request.url.path != "/api/spend")

    @router.get("/api/admin/spend")
    @router.get("/api/settings/billing")
    @router.get("/api/spend")
    async def report(
        request: Request, response: Response, principal: Any = Depends(actor),
        preset: Literal["24h", "7d", "30d", "custom"] = "7d",
        start: str | None = None, end: str | None = None,
        user_id: str | None = None, model: str | None = None,
        limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=MAX_RECORDS * 2),
        format: Literal["json", "csv"] = "json",
        export: Literal["daily", "users", "models", "sessions", "requests"] = "daily",
    ) -> Any:
        personal = principal.role != "admin"
        if personal:
            if request.url.path != "/api/spend" or not principal.individual:
                raise HTTPException(403, "Sign in with an individual account to view personal spending.")
            own = service.mappings.resolve(SourceRef("legacy", principal.id), actor=True)
            assert own is not None
            if user_id is not None and user_id != own.id:
                raise HTTPException(403, "Another account's spending is unavailable.")
            user_id = own.id
        try:
            value = await service.report(
                preset=preset, start=start, end=end, user_id=user_id, model=model,
                limit=limit, offset=offset, personal=personal, all_details=format == "csv" and export == "requests",
                viewer_id=principal.id,
            )
        except BillingReadError as error:
            bounded = str(error).startswith("too_many_")
            raise HTTPException(
                422 if bounded else 503,
                "Too many accounting records; narrow the date range." if bounded else
                "Accounting evidence is unavailable or needs reconciliation. No partial total was substituted.",
            ) from None
        except (SQLAlchemyError, TimeoutError):
            raise HTTPException(503, "Product accounting is temporarily unavailable.") from None
        fresh = await service.identity.require_current(principal.grant)
        if not fresh.enabled or (not personal and fresh.role != "admin"):
            raise HTTPException(403, "Your accounting access changed.")
        response.headers["Cache-Control"] = "no-store"
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

    @router.get("/api/admin/spend/infrastructure")
    async def infrastructure(
        response: Response, principal: Any = Depends(actor), preset: Literal["24h", "7d", "30d", "custom"] = "7d",
        start: str | None = None, end: str | None = None,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(REPORT_TIMEOUT_SECONDS):
                value = await service.infrastructure(_period(preset, start, end))
        except (BillingReadError, SQLAlchemyError, TimeoutError):
            raise HTTPException(503, "Product infrastructure accounting is temporarily unavailable.") from None
        fresh = await service.identity.require_current(principal.grant)
        if not fresh.enabled or fresh.role != "admin":
            raise HTTPException(403, "Your accounting access changed.")
        response.headers["Cache-Control"] = "no-store"
        return value

    return router
