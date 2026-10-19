"""Read-only accounting boundary for the one Moyai product.

The caller owns authorization, schema selection, read-only snapshot connections,
identity mappings and configuration epochs. This module never creates users,
runs or tables, settles receipts, schedules recovery, or calls a provider.

Packaging boundary: LegacyBillingRules.from_product loads the existing pure
helpers only in explicit product mode (repository root + product dependencies).
It does not initialize Store, Spend, Settings, providers, or legacy execution.
The standalone native wheel can instead inject those functions. Extract them
into a shared product package before removing the product source tree.
Keep ``app.spend_recovery.receipt`` validation and InfrastructureCosts.report's
monthly override/proration with their existing owners. Infrastructure rows below
are source records, NOT additive report costs. PR adapters consume explicit actor
and logical-session mappings; raw IDs across engines never establish linkage.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import MAX_EMAX, MIN_EMIN, Decimal, localcontext
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

from sqlalchemy import MetaData, Table, Text, and_, any_, bindparam, false, inspect, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import ColumnElement, Executable

from .analytics import ReportRange, actual_cost, integer, utc

if TYPE_CHECKING:
    from .billing_storage import Storage

Engine = Literal["legacy", "native"]
CostState = Literal["final", "pending", "missing", "estimated"]
MAX_RECORDS = 50_000
READ_BATCH_SIZE = 2_000
MAX_RECONCILIATION_ROUNDS = 4
LOOKUP_CHUNK = 200
LEDGER_LOOKUP_CHUNK = 10_000


class BillingReadError(ValueError):
    """A fixed metadata-only diagnostic, safe for the product service."""


def exact_sum(values: Iterable[Decimal]) -> Decimal | None:
    """Add recorded amounts without rounding or turning absent amounts into zero."""
    amounts = list(values)
    if not amounts:
        return None
    if any(not amount.is_finite() for amount in amounts):
        raise BillingReadError("invalid_accounting_amount")
    exponents = [amount.as_tuple().exponent for amount in amounts]
    assert all(isinstance(value, int) for value in exponents)
    lowest = min(int(value) for value in exponents)
    highest = max(amount.adjusted() for amount in amounts)
    carry = len(str(len(amounts))) + 1
    precision = max(1, highest - lowest + carry + 1)
    if precision > 10_000:
        raise BillingReadError("accounting_precision_bound_exceeded")
    with localcontext() as context:
        context.prec = precision
        context.Emax = min(MAX_EMAX, max(context.Emax, highest + carry))
        context.Emin = max(MIN_EMIN, min(context.Emin, lowest))
        return sum(amounts, Decimal(0))


class LegacyCostStatus(Protocol):
    def __call__(
        self, row: Mapping[str, Any], *, key_hash: str, gateway_scope: str, enabled: bool,
    ) -> str: ...


@dataclass(frozen=True)
class LegacyBillingRules:
    money: Callable[[object], str | None]
    cost_status: LegacyCostStatus
    infrastructure_amount: Callable[[object], Decimal]
    gateway_scope: Callable[[str], str]
    validate_recovery_receipt: Callable[[object, Mapping[str, Any]], tuple[str, str]]

    @classmethod
    def from_product(cls) -> LegacyBillingRules:
        """Reuse product functions without importing app.main or an agent loop."""
        from app.billing_sources import amount
        from app.spend import cost_status, gateway_scope, money
        from app.spend_recovery import receipt

        return cls(money, cost_status, amount, gateway_scope, receipt)


@dataclass(frozen=True, order=True)
class SourceRef:
    engine: Engine
    id: str

    @property
    def namespaced_id(self) -> str:
        return f"{self.engine}:{self.id}"


@dataclass(frozen=True)
class CanonicalRef:
    id: str
    provenance: str
    version: str


@dataclass(frozen=True)
class IdentityMappings:
    """Only supplied, versioned mappings may join identities across engines."""

    version: str
    provenance: str
    actors: Mapping[SourceRef, str] = field(default_factory=dict)
    sessions: Mapping[SourceRef, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.version or not self.provenance:
            raise BillingReadError("mapping_provenance_required")
        object.__setattr__(self, "actors", MappingProxyType(dict(self.actors)))
        object.__setattr__(self, "sessions", MappingProxyType(dict(self.sessions)))

    def resolve(self, source: SourceRef | None, *, actor: bool) -> CanonicalRef | None:
        if source is None:
            return None
        mapping = self.actors if actor else self.sessions
        return CanonicalRef(
            mapping.get(source, source.namespaced_id),
            self.provenance if source in mapping else "source_namespace",
            self.version,
        )


@dataclass(frozen=True)
class IdentityLink:
    source: SourceRef
    target: SourceRef | None
    kind: str
    method: str
    status: str
    usable: bool = False


@dataclass(frozen=True)
class GatewayKeyScope:
    """Already-computed product hashes, never API keys or gateway credentials."""

    gateway_scope: str
    key_hash: str

    @property
    def complete(self) -> bool:
        return bool(self.gateway_scope and self.key_hash)


@dataclass(frozen=True)
class RequestIdentity:
    kind: Literal["gateway_call", "receipt", "broker_request"]
    value: str


@dataclass(frozen=True)
class NativeBinding:
    """Per-observation proof, not a retroactive current-key assignment.

    Native provider_scope hashes use a different normalization from legacy
    gateway_scope; native rows also lack key_hash and header identity kind.
    The product owner must establish this binding from trusted request metadata.
    """

    scope: GatewayKeyScope
    identity: RequestIdentity
    provenance: str
    version: str

    def __post_init__(self) -> None:
        if not self.scope.complete or not self.identity.value or not self.provenance or not self.version:
            raise BillingReadError("native_binding_provenance_required")


@dataclass(frozen=True)
class ObservationLink:
    """An explicit product/broker receipt links two observations of one request."""

    left: SourceRef
    right: SourceRef
    provenance: str
    version: str

    def __post_init__(self) -> None:
        if not self.provenance or not self.version:
            raise BillingReadError("observation_link_provenance_required")


@dataclass(frozen=True)
class RecoveryPolicy:
    enabled: bool = False
    scope: GatewayKeyScope | None = None


@dataclass(frozen=True)
class RecoveryState:
    eligible: bool
    due: bool
    reason: str
    next_attempt_at: datetime | None = None
    attempts: int = 0
    error: str = ""


@dataclass(frozen=True)
class BillingObservation:
    source: SourceRef
    actor_source: SourceRef | None
    session_source: SourceRef
    session_family_source: SourceRef | None
    actor: CanonicalRef | None
    session: CanonicalRef
    turn_id: str | None
    message_id: str | None
    model: str
    occurred_at: datetime
    timestamp_basis: str
    cost_state: CostState
    amount: Decimal | None
    cost_source: str | None
    request_status: str
    scope: GatewayKeyScope | None
    identities: tuple[RequestIdentity, ...]
    provider_request_id: str | None
    recovery: RecoveryState
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    native_provider_scope: str | None = None
    scope_provenance: str | None = None
    scope_version: str | None = None
    issues: tuple[str, ...] = ()
    currency: Literal["USD"] = "USD"
    provider_response_id: str | None = None
    provider_timestamp: datetime | None = None
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class BillingCharge:
    """One proven physical request, or an explicitly unresolved overlap group."""

    observations: tuple[BillingObservation, ...]
    occurred_at: datetime | None
    timestamp_basis: str
    actor_id: str | None
    session_id: str | None
    cost_state: CostState
    amount: Decimal | None
    request_count: int | None
    issues: tuple[str, ...] = ()
    model: str | None = None
    observed_models: tuple[str, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    overlap_candidates: tuple[SourceRef, ...] = ()
    reasoning_tokens: int | None = None


@dataclass(frozen=True)
class BillingProjection:
    charges: tuple[BillingCharge, ...]
    identity_links: tuple[IdentityLink, ...] = ()
    currency: Literal["USD"] = "USD"

    @property
    def actual_subtotal(self) -> Decimal | None:
        return exact_sum(row.amount for row in self.charges if row.cost_state == "final" and row.amount is not None)

    @property
    def cost_complete(self) -> bool:
        return bool(self.charges) and all(row.cost_state == "final" for row in self.charges)

    @property
    def complete(self) -> bool:
        """Cost plus attribution/metadata coverage; inspect cost_complete separately."""
        return self.cost_complete and all(not row.issues for row in self.charges)


@dataclass(frozen=True)
class InfrastructureSource:
    """Unallocated source evidence; monthly rows override daily rows for a month."""

    kind: Literal["daily", "monthly"]
    provider: str
    period: str
    scope: str | None
    source_amount: Decimal
    cost_state: Literal["final", "estimated"]
    observed_at: str
    revision: int | None = None
    currency: Literal["USD"] = "USD"


@dataclass(frozen=True)
class InfrastructureSnapshot:
    sources: tuple[InfrastructureSource, ...]
    pending_syncs: tuple[tuple[str, str, str, str], ...]
    allocation_owner: str = "app.infrastructure_costs.InfrastructureCosts.report"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _time(value: object) -> datetime:
    result = utc(value)
    if result is None:
        raise BillingReadError("invalid_accounting_timestamp")
    return result


def _source(engine: Engine, value: object) -> SourceRef | None:
    return SourceRef(engine, value) if isinstance(value, str) and value else None


def _recovery(row: Mapping[str, Any], policy: RecoveryPolicy, clock: datetime) -> RecoveryState:
    """Expose the existing recovery poll's eligibility without running it."""
    next_at = utc(row.get("cost_next_attempt_at"))
    scope = policy.scope
    reason = (
        "already_priced" if row.get("cost") is not None else
        "in_flight" if row.get("status") == "pending" else
        "disabled" if not policy.enabled else
        "missing_scope" if scope is None or not scope.complete else
        "scope_changed" if (row.get("key_hash"), row.get("gateway_scope")) !=
        (scope.key_hash, scope.gateway_scope) else
        "unscheduled" if not row.get("cost_next_attempt_at") else
        "invalid_schedule" if next_at is None else
        "due" if next_at <= clock else "backoff"
    )
    return RecoveryState(
        eligible=reason in {"unscheduled", "invalid_schedule", "due", "backoff"},
        due=reason == "due", reason=reason, next_attempt_at=next_at,
        attempts=integer(row.get("cost_recovery_attempts")) or 0,
        error=str(row.get("cost_recovery_error") or ""),
    )


def legacy_observation(
    row: Mapping[str, Any], rules: LegacyBillingRules, mappings: IdentityMappings,
    links: Mapping[str, IdentityLink], *, policy: RecoveryPolicy, clock: datetime,
) -> BillingObservation:
    identity = _source("legacy", row.get("id"))
    session_source = _source("legacy", row.get("run_id"))
    if identity is None or session_source is None:
        raise BillingReadError("invalid_legacy_accounting_identity")
    actor_source = _source("legacy", row.get("user_id"))
    link = links.get(actor_source.id) if actor_source else None
    actor = mappings.resolve(link.target if link and link.usable else actor_source, actor=True)
    family = _source("legacy", row.get("root_id"))
    session = mappings.resolve(family or session_source, actor=False)
    assert session is not None
    source_row = {
        **row, "cost_recovery_error": row.get("cost_recovery_error") or "",
        "gateway_scope": row.get("gateway_scope") or "", "key_hash": row.get("key_hash") or "",
    }
    scope = GatewayKeyScope(str(row.get("gateway_scope") or ""), str(row.get("key_hash") or ""))
    display = rules.cost_status(
        source_row, key_hash=policy.scope.key_hash if policy.scope else "",
        gateway_scope=policy.scope.gateway_scope if policy.scope else "", enabled=policy.enabled,
    )
    parsed = rules.money(row.get("cost"))
    amount = Decimal(parsed) if parsed is not None else None
    issues = []
    if row.get("cost") is not None and amount is None:
        issues.append("invalid_legacy_cost")
    if family is None:
        issues.append("session_family_unavailable")
    if link and link.target and not link.usable:
        issues.append("unresolved_identity_link")
    state: CostState = "final" if amount is not None else "pending" if display == "pending" else "missing"
    identities = [RequestIdentity("broker_request", identity.id)]
    if gateway_id := _text(row.get("gateway_id")):
        identities.append(RequestIdentity("gateway_call", gateway_id))
    if receipt_id := _text(row.get("cost_receipt_id")):
        identities.append(RequestIdentity("receipt", receipt_id))
    return BillingObservation(
        identity, actor_source, session_source, family, actor, session,
        None, str(row["message_id"]) if row.get("message_id") is not None else None,
        str(row["model"]), _time(row["created_at"]), "legacy_request_created_at",
        state, amount, _text(row.get("cost_source")) or ("legacy_ledger" if amount is not None else None),
        str(row["status"]), scope, tuple(identities), _text(row.get("gateway_id")),
        _recovery(source_row, policy, clock),
        input_tokens=integer(row.get("prompt_tokens")), output_tokens=integer(row.get("completion_tokens")),
        total_tokens=integer(row.get("total_tokens")), cache_read_tokens=integer(row.get("cache_read_input_tokens")),
        cache_write_tokens=integer(row.get("cache_creation_input_tokens")),
        scope_provenance="legacy_ledger", scope_version="legacy", issues=tuple(issues),
    )


def native_observation(
    row: Mapping[str, Any], mappings: IdentityMappings, *, binding: NativeBinding | None = None,
    policy: RecoveryPolicy | None = None, clock: datetime | None = None,
    identity_links: Mapping[str, IdentityLink] | None = None,
) -> BillingObservation:
    identity = _source("native", row.get("id"))
    session_source = _source("native", row.get("session_id"))
    if identity is None or session_source is None:
        raise BillingReadError("invalid_native_accounting_identity")
    actor_source = _source("native", row.get("actor_id"))
    session = mappings.resolve(session_source, actor=False)
    assert session is not None
    amount = actual_cost(row.get("cost_usd"), str(row.get("cost_status")), row.get("cost_source"))
    state: CostState = "final" if amount is not None else "pending" if row.get("status") == "pending" else "missing"
    if row.get("cost_status") == "estimated":
        amount = actual_cost(row.get("cost_usd"), "actual", row.get("cost_source"))
        state = "estimated" if amount is not None else "missing"
    coverage_error = _text(row.get("coverage_error"))
    recovery = RecoveryState(False, False, "native_recovery_not_integrated")
    admission = row.get("_admission")
    actor = mappings.resolve(actor_source, actor=True)
    if isinstance(admission, Mapping) and binding is not None:
        if any(admission.get(name) != row.get(name) for name in ("actor_id", "session_id", "turn_id", "model")):
            raise BillingReadError("native_admission_attribution_mismatch")
        # This original admission checked the canonical product account in the
        # same transaction. This is explicit scope proof, not an equal-ID guess.
        canonical = SourceRef("legacy", admission["actor_id"])
        linked = (identity_links or {}).get(canonical.id)
        actor = mappings.resolve(linked.target if linked and linked.usable else canonical, actor=True)
        source_row = {
            "cost": row.get("cost_usd"), "status": admission["status"],
            "gateway_scope": admission["gateway_scope"], "key_hash": admission["key_hash"],
            "cost_recovery_error": admission.get("recovery_error") or "",
            "cost_recovery_attempts": admission.get("recovery_attempts") or 0,
            "cost_next_attempt_at": admission.get("next_attempt_at") or "",
        }
        recovery = _recovery(source_row, policy or RecoveryPolicy(), clock or datetime.now(UTC))
        if admission["status"] == "not_dispatched" and amount is None:
            recovery = RecoveryState(False, False, "not_dispatched")
        elif admission.get("recovery_error") not in {None, "", "not_found", "gateway_unavailable"}:
            recovery = RecoveryState(
                False, False, "receipt_unavailable", error=str(admission["recovery_error"]),
                attempts=integer(admission.get("recovery_attempts")) or 0,
            )
        if amount is None and recovery.eligible and recovery.next_attempt_at is not None:
            state = "pending"
    return BillingObservation(
        identity, actor_source, session_source, session_source,
        actor, session, _text(row.get("turn_id")), None,
        str(row["model"]), _time(row["created_at"]), "native_request_created_at",
        state, amount, _text(row.get("cost_source")), str(row["status"]),
        binding.scope if binding else None, (binding.identity,) if binding else (),
        _text(row.get("provider_request_id")),
        recovery,
        input_tokens=integer(row.get("input_tokens")), output_tokens=integer(row.get("output_tokens")),
        total_tokens=integer(row.get("total_tokens")), cache_read_tokens=integer(row.get("cache_read_tokens")),
        cache_write_tokens=integer(row.get("cache_write_tokens")),
        native_provider_scope=_text(row.get("provider_scope")),
        scope_provenance=binding.provenance if binding else None,
        scope_version=binding.version if binding else None,
        issues=(coverage_error,) if coverage_error else (),
        provider_response_id=_text(row.get("response_id")), provider_timestamp=utc(row.get("provider_timestamp")),
        reasoning_tokens=integer(row.get("reasoning_tokens")),
    )


def reconcile(
    observations: Iterable[BillingObservation], *, links: Iterable[ObservationLink] = (),
    period: ReportRange | None = None, require_native_provenance: bool = False,
) -> tuple[BillingCharge, ...]:
    """Reconcile BEFORE filtering dates; retries with distinct IDs stay distinct.

    Equal provider IDs without proven scope/type leave the native observation
    unresolved while retaining the existing broker charge. They cannot add a
    second charge or erase historical accounting. Conflicting proven amounts
    are retained as evidence, never silently selected or added.
    """
    by_source: dict[SourceRef, BillingObservation] = {}
    for item in observations:
        if item.source in by_source and by_source[item.source] != item:
            raise BillingReadError("conflicting_source_versions")
        by_source[item.source] = item
    items = sorted(by_source.values(), key=lambda item: item.source)
    positions = {item.source: index for index, item in enumerate(items)}
    parents = list(range(len(items)))
    ambiguous: set[int] = set()
    candidates_by_native: dict[int, set[SourceRef]] = defaultdict(set)

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def join(left: int, right: int) -> None:
        parents[root(right)] = root(left)

    known: dict[tuple[str, str, str, str], int] = {}
    possible: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(items):
        if item.scope and item.scope.complete:
            for identity in item.identities:
                key = (item.scope.gateway_scope, item.scope.key_hash, identity.kind, identity.value)
                if key in known:
                    join(known[key], index)
                else:
                    known[key] = index
        if item.provider_request_id:
            possible[item.provider_request_id].append(index)
    for link in links:
        if link.left not in positions or link.right not in positions:
            raise BillingReadError("missing_linked_observation")
        left, right = items[positions[link.left]], items[positions[link.right]]
        if left.scope and left.scope.complete and right.scope and right.scope.complete and left.scope != right.scope:
            raise BillingReadError("conflicting_link_scope")
        join(positions[link.left], positions[link.right])
    for candidates in possible.values():
        if len(candidates) > 128:
            raise BillingReadError("ambiguous_provider_identity_fanout")
        for index in candidates:
            for other in candidates:
                left, right = items[index], items[other]
                if index >= other or root(index) == root(other) or left.source.engine == right.source.engine:
                    continue
                if left.scope and left.scope.complete and right.scope and right.scope.complete:
                    if left.scope != right.scope:
                        continue  # Key rotations / gateways are separate billing scopes.
                    if left.identities and right.identities:
                        continue  # Known different identity kinds are not aliases.
                native_index = index if left.source.engine == "native" else other
                legacy_index = other if left.source.engine == "native" else index
                ambiguous.add(native_index)
                candidates_by_native[native_index].add(items[legacy_index].source)
    grouped: dict[int, list[int]] = defaultdict(list)
    for index in range(len(items)):
        grouped[root(index)].append(index)
    charges = []
    for indices in grouped.values():
        group = tuple(items[index] for index in indices)
        # Legacy broker owns its admission clock; hydration keeps cross-midnight
        # duplicate observations from appearing as separate daily charges.
        preferred = min(group, key=lambda item: (item.source.engine != "legacy", item.occurred_at, item.source))
        issues = {issue for item in group for issue in item.issues}
        actual = {item.amount for item in group if item.cost_state == "final"}
        estimates = {item.amount for item in group if item.cost_state == "estimated"}
        amount, state = None, "missing"
        uncertain = bool(ambiguous.intersection(indices)) and all(item.source.engine == "native" for item in group)
        if period and not (
            any(period.contains(item.occurred_at) for item in group) if uncertain
            else period.contains(preferred.occurred_at)
        ):
            continue
        unbound = (
            require_native_provenance and all(item.source.engine == "native" for item in group)
            and any(not item.scope or not item.scope.complete or not item.identities for item in group)
        )
        if uncertain:
            issues.add("unproven_observation_overlap")
        elif unbound:
            issues.add("native_scope_unbound")
        elif len(actual) > 1:
            issues.add("conflicting_actual_costs")
        elif actual:
            amount, state = next(iter(actual)), "final"
        elif len(estimates) == 1:
            amount, state = next(iter(estimates)), "estimated"
        elif len(estimates) > 1:
            issues.add("conflicting_estimates")
        elif any(item.cost_state == "pending" for item in group):
            state = "pending"
        actors = {item.actor.id if item.actor else None for item in group}
        sessions = {item.session.id for item in group}
        if len(actors) > 1:
            issues.add("actor_mapping_conflict")
        if len(sessions) > 1:
            issues.add("session_mapping_conflict")
        models = tuple(sorted({item.model for item in group}))
        if len(models) > 1:
            issues.add("model_mapping_conflict")
        tokens = {}
        for name in (
            "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        ):
            observed = {getattr(item, name) for item in group if getattr(item, name) is not None}
            tokens[name] = next(iter(observed)) if len(observed) == 1 and not (uncertain or unbound) else None
            if len(observed) > 1:
                issues.add(f"conflicting_{name}")
        charges.append(BillingCharge(
            group, None if uncertain else preferred.occurred_at,
            "unresolved_observation_overlap" if uncertain else preferred.timestamp_basis,
            next(iter(actors)) if len(actors) == 1 else None,
            next(iter(sessions)) if len(sessions) == 1 else None,
            state, amount, None if uncertain or unbound else 1, tuple(sorted(issues)),
            model=models[0] if len(models) == 1 else None, observed_models=models, **tokens,
            overlap_candidates=tuple(sorted({source for index in indices for source in candidates_by_native[index]})),
        ))
    return tuple(sorted(charges, key=lambda item: (
        item.occurred_at or min(row.occurred_at for row in item.observations), item.observations[0].source,
    )))


LEGACY_FIELDS = (
    "id", "key_hash", "gateway_id", "run_id", "message_id", "user_id", "model",
    "created_at", "finished_at", "status", "cost", "cost_source", "gateway_scope", "cost_receipt_id",
    "cost_next_attempt_at", "cost_recovery_attempts", "cost_recovery_error",
    "prompt_tokens", "completion_tokens", "total_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
)
NATIVE_FIELDS = (
    "id", "request_key", "session_id", "turn_id", "actor_id", "model", "provider_scope", "provider_request_id",
    "created_at", "status", "cost_usd", "cost_status", "cost_source", "coverage_error",
    "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
    "response_id", "provider_timestamp",
)


class BillingReader:
    """SELECT-only, batched adapter over a caller-owned read-only connection.

    Reflection reads column metadata, never arbitrary row contents. Callers must
    authorize aggregate access and select the configured schema explicitly.
    Missing sources fail visibly; they never initialize an empty replacement DB.
    """

    def __init__(
        self, connection: AsyncConnection, *, schema: str | None = None, max_records: int | None = None,
        storage: Storage | None = None, billing_connection: AsyncConnection | None = None,
    ):
        if max_records is not None and not 1 <= max_records <= MAX_RECORDS:
            raise BillingReadError("invalid_read_bound")
        self.connection, self.schema, self.max_records = connection, schema, max_records
        self.storage = storage
        self.billing_connection = billing_connection if billing_connection is not None else connection

    async def records(
        self, statement: Executable, *, connection: AsyncConnection | None = None,
        parameters: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[RowMapping]:
        """Bound the fetch buffer, not the report's accounting population.

        The caller retains the same read-only snapshot across all batches and
        counterpart lookups. An explicitly requested diagnostic bound still
        fails closed rather than returning a partial total.
        """
        selected = connection if connection is not None else self.connection
        async with selected.stream(
            statement, parameters, execution_options={"yield_per": READ_BATCH_SIZE},
        ) as result:
            async for batch in result.mappings().partitions(READ_BATCH_SIZE):
                for row in batch:
                    yield row

    async def table(self, name: str, fields: Iterable[str], required: Iterable[str]) -> Table:
        def reflect(connection: Any) -> Table:
            inspector = inspect(connection)
            if not inspector.has_table(name, schema=self.schema):
                raise BillingReadError(f"missing_accounting_source:{name}")
            available = {row["name"] for row in inspector.get_columns(name, schema=self.schema)}
            if not set(required) <= available:
                raise BillingReadError(f"incompatible_accounting_source:{name}")
            return Table(
                name, MetaData(), schema=self.schema, autoload_with=connection,
                include_columns=sorted(set(fields) & available), resolve_fks=False,
            )
        return await self.connection.run_sync(reflect)

    def _matching(
        self, table: Table, names: Iterable[str], values: list[str],
    ) -> ColumnElement[bool]:
        columns = [table.c[name] for name in names if name in table.c]
        if self.connection.dialect.name == "postgresql":
            # One array parameter avoids recompiling thousands of scalar binds
            # for every admission/counterpart batch.
            wanted = bindparam("billing_identities", value=values, type_=ARRAY(Text))
            return or_(*(column == any_(wanted) for column in columns))
        return or_(*(column.in_(values) for column in columns))

    async def rows(
        self, engine: Engine, period: ReportRange, *, matching: Iterable[str] = (),
        outside_period: bool = False,
    ) -> list[dict[str, Any]]:
        """Read the period and its aliases, or only aliases outside that period."""
        candidates = sorted(set(matching))
        if outside_period and not candidates:
            return []
        fields = LEGACY_FIELDS if engine == "legacy" else NATIVE_FIELDS
        table = None
        if self.storage is None:
            table = await self.table(
                "model_requests" if engine == "legacy" else "moyai_inference_usage", fields,
                ("id", "model", "created_at", "status", "run_id", "user_id", "cost") if engine == "legacy"
                else ("id", "model", "created_at", "status", "session_id", "actor_id", "cost_usd", "cost_status"),
            )
        admissions = None
        if engine == "native":
            admissions = await self.admission_table()
        # PostgreSQL has room for larger parameter batches. Keep SQLite below
        # its bind limit (three identity columns per lookup).
        lookup_chunk = LEDGER_LOOKUP_CHUNK if self.connection.dialect.name == "postgresql" else LOOKUP_CHUNK
        if admissions is not None and candidates:
            for offset in range(0, len(candidates), lookup_chunk):
                values = candidates[offset:offset + lookup_chunk]
                async for row in self.records(select(admissions.c.request_key).where(self._matching(
                    admissions, ("id", "gateway_id", "cost_receipt_id"), values,
                ))):
                    candidates.append(row["request_key"])
            candidates = sorted(set(candidates))
        if self.max_records is not None and len(candidates) > self.max_records * 4:
            raise BillingReadError("too_many_accounting_identities")
        if self.storage is not None:
            result = await self._consolidated_rows(
                engine, period, candidates, fields, outside_period=outside_period,
            )
        else:
            assert table is not None
            statements = [] if outside_period else [select(table).where(
                table.c.created_at >= period.start.isoformat(), table.c.created_at < period.end.isoformat(),
            )]
            names = (
                ("id", "gateway_id", "cost_receipt_id") if engine == "legacy"
                else ("id", "request_key", "provider_request_id")
            )
            for offset in range(0, len(candidates), lookup_chunk):
                values = candidates[offset:offset + lookup_chunk]
                statement = select(table).where(self._matching(table, names, values))
                if outside_period:
                    statement = statement.where(or_(
                        table.c.created_at < period.start.isoformat(),
                        table.c.created_at >= period.end.isoformat(), table.c.created_at.is_(None),
                    ))
                statements.append(statement)
            result: dict[str, dict[str, Any]] = {}
            for statement in statements:
                if self.max_records is not None:
                    statement = statement.limit(self.max_records + 1)
                async for row in self.records(statement):
                    result[row["id"]] = dict(row)
                    if self.max_records is not None and len(result) > self.max_records:
                        raise BillingReadError("too_many_accounting_records")
        if admissions is not None:
            identifiers = list(result)
            for offset in range(0, len(identifiers), lookup_chunk):
                for row in (await self.connection.execute(select(admissions).where(self._matching(
                    admissions, ("request_key",), identifiers[offset:offset + lookup_chunk],
                )))).mappings():
                    result[row["request_key"]]["_admission"] = dict(row)
        return list(result.values())

    async def _consolidated_rows(
        self, engine: Engine, period: ReportRange, candidates: list[str], fields: tuple[str, ...],
        *, outside_period: bool = False,
    ) -> dict[str, dict[str, Any]]:
        from sqlalchemy import text

        from .billing_storage_models import storage_column

        assert self.storage is not None
        parameters: dict[str, Any] = {
            "source": engine, "start": period.start.isoformat(), "end": period.end.isoformat(),
        }
        predicates = [] if outside_period else [("created_at>=:start AND created_at<:end", parameters)]
        for offset in range(0, len(candidates), LEDGER_LOOKUP_CHUNK):
            identities = (
                ("id", "gateway_id", "cost_receipt_id") if engine == "legacy"
                else ("id", "request_key", "provider_request_id")
            )
            # An OR of three ANY predicates repeatedly scans the ledger when
            # one identity column has no leading index. Join each identity
            # separately so PostgreSQL can use its index or a bounded hash join.
            # UNION retains one source row even when several aliases match.
            matches = " UNION ".join(
                f'SELECT matched.source_id FROM {self.storage.ledger} matched '
                f'JOIN unnest(CAST(:identities AS text[])) wanted(identity) '
                f'ON matched."{storage_column(engine, name)}"=wanted.identity '
                "WHERE matched.source=:source"
                for name in identities
            )
            predicate = f"source_id IN ({matches})"
            if outside_period:
                predicate += " AND (created_at<:start OR created_at>=:end OR created_at IS NULL)"
            predicates.append((
                predicate,
                {**parameters, "identities": candidates[offset:offset + LEDGER_LOOKUP_CHUNK]},
            ))
        result: dict[str, dict[str, Any]] = {}
        columns = ",".join(f'"{storage_column(engine, name)}" AS "{name}"' for name in fields)
        for predicate, params in predicates:
            limit = "" if self.max_records is None else " LIMIT :limit"
            if self.max_records is not None:
                params = {**params, "limit": self.max_records + 1}
            async for row in self.records(text(
                f"SELECT {columns} FROM {self.storage.ledger} "
                f"WHERE source=:source AND ({predicate}){limit}"
            ), parameters=params, connection=self.billing_connection):
                result[row["id"]] = dict(row)
                if self.max_records is not None and len(result) > self.max_records:
                    raise BillingReadError("too_many_accounting_records")
        return result

    async def admission_table(self) -> Table | None:
        name = "moyai_product_inference_requests"
        exists = await self.connection.run_sync(lambda conn: inspect(conn).has_table(name, schema=self.schema))
        if not exists:
            return None  # Historical native observations have no admission proof.
        fields = {
            "id", "request_key", "gateway_scope", "key_hash", "gateway_id", "cost_receipt_id",
            "status", "recovery_error", "recovery_attempts", "next_attempt_at",
            "actor_id", "session_id", "turn_id", "model",
        }
        return await self.table(name, fields, fields)

    async def identity_links(self) -> tuple[IdentityLink, ...]:
        users = await self.table("users", ("id", "kind", "linked_user_id", "link_method", "link_status"), ("id", "kind"))
        bound = self.max_records or MAX_RECORDS
        rows = list((await self.connection.execute(select(users).limit(bound + 1))).mappings())
        if len(rows) > bound:
            raise BillingReadError("too_many_accounting_identities")
        known = {row["id"]: row for row in rows}
        return tuple(IdentityLink(
            SourceRef("legacy", row["id"]),
            _source("legacy", row.get("linked_user_id")),
            row["kind"], str(row.get("link_method") or ""), str(row.get("link_status") or ""),
            row["kind"] == "slack" and known.get(row.get("linked_user_id"), {}).get("kind") in {"google", "cloudflare"},
        ) for row in rows)

    async def families(self, run_ids: Iterable[str]) -> dict[str, str]:
        roots = await self.table("run_roots", ("run_id", "root_id"), ("run_id", "root_id"))
        ids, result, read = sorted(set(run_ids)), {}, 0
        for offset in range(0, len(ids), LOOKUP_CHUNK):
            async for row in self.records(
                select(roots).where(roots.c.run_id.in_(ids[offset:offset + LOOKUP_CHUNK])),
            ):
                read += 1
                if self.max_records is not None and read > self.max_records:
                    raise BillingReadError("too_many_session_family_records")
                if row["run_id"] in result and result[row["run_id"]] != row["root_id"]:
                    raise BillingReadError("ambiguous_session_family")
                result[row["run_id"]] = row["root_id"]
        return result

    async def infrastructure(
        self, period: ReportRange, *, scopes: Mapping[str, str], rules: LegacyBillingRules,
    ) -> InfrastructureSnapshot:
        daily = await self.table(
            "infrastructure_daily_costs", ("provider", "scope", "day", "amount", "synced_at"),
            ("provider", "scope", "day", "amount", "synced_at"),
        )
        bills = await self.table(
            "infrastructure_bills", ("provider", "month", "amount", "kind", "revision", "updated_at"),
            ("provider", "month", "amount", "kind", "revision", "updated_at"),
        )
        jobs = await self.table(
            "infrastructure_sync_jobs", ("provider", "scope", "start", "end", "status"),
            ("provider", "scope", "start", "end", "status"),
        )
        # Sources retain their full period; existing product code owns allocation.
        lower = period.start.date().isoformat()
        upper = (period.end - timedelta(microseconds=1)).date().isoformat()
        def scoped(table: Table) -> Any:
            return or_(*(and_(table.c.provider == provider, table.c.scope == scope)
                         for provider, scope in scopes.items())) if scopes else false()
        sources = []
        bound = self.max_records or MAX_RECORDS
        daily_rows = list((await self.connection.execute(select(daily).where(
            daily.c.day >= lower, daily.c.day <= upper, scoped(daily),
        ).limit(bound + 1))).mappings())
        bill_rows = list((await self.connection.execute(select(bills).where(
            bills.c.month >= lower[:7], bills.c.month <= upper[:7],
        ).limit(bound + 1))).mappings())
        job_rows = list((await self.connection.execute(select(jobs).where(
            jobs.c.status == "pending", jobs.c.start <= upper, jobs.c.end >= lower, scoped(jobs),
        ).limit(bound + 1))).mappings())
        if max(len(daily_rows), len(bill_rows), len(job_rows)) > bound:
            raise BillingReadError("too_many_infrastructure_records")
        for row in daily_rows:
            sources.append(InfrastructureSource(
                "daily", row["provider"], row["day"], row["scope"], rules.infrastructure_amount(row["amount"]),
                "final", row["synced_at"],
            ))
        for row in bill_rows:
            if row["kind"] not in {"invoice", "estimate"}:
                raise BillingReadError("unknown_infrastructure_bill_kind")
            sources.append(InfrastructureSource(
                "monthly", row["provider"], row["month"], None, rules.infrastructure_amount(row["amount"]),
                "estimated" if row["kind"] == "estimate" else "final", row["updated_at"], row["revision"],
            ))
        pending = tuple(
            (row["provider"], row["scope"], row["start"], row["end"])
            for row in job_rows
        )
        if len(sources) > bound or len(pending) > bound:
            raise BillingReadError("too_many_infrastructure_records")
        return InfrastructureSnapshot(tuple(sources), pending)


async def read_product_billing(
    legacy: BillingReader, native: BillingReader, period: ReportRange, *, rules: LegacyBillingRules,
    mappings: IdentityMappings, policy: RecoveryPolicy, clock: datetime,
    native_bindings: Mapping[SourceRef, NativeBinding] | None = None,
    links: Iterable[ObservationLink] = (),
) -> BillingProjection:
    """Read both sources plus exact-ID counterparts before choosing report dates.

    The bounded closure prevents a broker/native clock crossing midnight from
    billing the same request in two periods. It is a SELECT projection, not a
    recovery loop: no writes or inference retries are possible here.
    """
    bindings = dict(native_bindings or {})
    if any(ref.engine != "native" for ref in bindings):
        raise BillingReadError("invalid_native_binding_source")
    explicit = tuple(links)
    wanted = {ref.id for link in explicit for ref in (link.left, link.right)}
    # Keep evidence only for this projection and its caller-owned snapshots.
    # Subsequent rounds fetch new aliases outside the already-complete period,
    # rather than materializing the same period and aliases again.
    old_by_id: dict[str, dict[str, Any]] = {}
    new_by_id: dict[str, dict[str, Any]] = {}
    old_searched: set[str] = set()
    new_searched: set[str] = set()
    for round_index in range(MAX_RECONCILIATION_ROUNDS):
        native_ids = {ref.id for ref, binding in bindings.items() if binding.identity.value in wanted}
        old_matching, new_matching = wanted - old_searched, (wanted | native_ids) - new_searched
        previous = len(old_by_id) + len(new_by_id)
        old = await legacy.rows("legacy", period, matching=old_matching, outside_period=round_index > 0)
        new = await native.rows("native", period, matching=new_matching, outside_period=round_index > 0)
        old_searched.update(old_matching)
        new_searched.update(new_matching)
        old_by_id.update((row["id"], row) for row in old)
        new_by_id.update((row["id"], row) for row in new)
        if any(reader.max_records is not None and len(rows) > reader.max_records
               for reader, rows in ((legacy, old_by_id), (native, new_by_id))):
            raise BillingReadError("too_many_accounting_records")
        for row in new:
            admission = row.get("_admission")
            if isinstance(admission, Mapping):
                ref = SourceRef("native", row["id"])
                saved = NativeBinding(
                    GatewayKeyScope(admission["gateway_scope"], admission["key_hash"]),
                    RequestIdentity("broker_request", admission["id"]),
                    "native_product_admission", "v1",
                )
                if ref in bindings and bindings[ref] != saved:
                    raise BillingReadError("conflicting_native_admission_binding")
                bindings[ref] = saved
        if len(old_by_id) + len(new_by_id) == previous:
            break
        for row in old:
            wanted.update(value for name in ("id", "gateway_id", "cost_receipt_id") if (value := _text(row.get(name))))
        for row in new:
            wanted.update(value for name in ("id", "provider_request_id") if (value := _text(row.get(name))))
            if binding := bindings.get(SourceRef("native", row["id"])):
                wanted.add(binding.identity.value)
    else:
        raise BillingReadError("accounting_counterpart_bound_exceeded")
    old, new = list(old_by_id.values()), list(new_by_id.values())
    identities = await legacy.identity_links()
    identity_map = {row.source.id: row for row in identities}
    roots = await legacy.families(row["run_id"] for row in old)
    new_observations = [
        native_observation(
            row, mappings, binding=bindings.get(SourceRef("native", row["id"])), policy=policy, clock=clock,
            identity_links=identity_map,
        ) for row in new
    ]
    observations = [
        legacy_observation(
            {**row, "root_id": roots.get(row["run_id"])}, rules, mappings, identity_map,
            policy=policy, clock=clock,
        ) for row in old
    ] + [
        # Proven unsent reservations remain in the durable audit ledger, but
        # are not HTTP charges or missing-cost coverage. No actual $0 is invented.
        item for item in new_observations if item.recovery.reason != "not_dispatched"
    ]
    return BillingProjection(
        reconcile(observations, links=explicit, period=period, require_native_provenance=True), identities,
    )
