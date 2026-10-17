"""Native persistence/authorization adapter for reusable analytics arithmetic."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Response
from sqlalchemy import func, or_, select, tuple_

from . import performance_report
from .analytics import (
    ReportRange,
    UsageEvent,
    actual_cost,
    cost_per_merge,
    csv_export,
    daily_usage,
    integer,
    model_totals,
    percentile,
    report_range,
    usage_totals,
    utc,
)
from .analytics_identity import report_identities
from .inference_usage import InferenceUsage
from .models import Effect, ProductTurnAuthority, Session, Turn, User
from .pr_publications import PRPublication, verified_status
from .pull_requests import PullRequestService, parse_pr_url

MAX_TURNS = 10_000
MAX_INFERENCES = 50_000
MAX_PR_STATUS_READS = 100
MAX_BASELINE_BYTES = 8 * 1024 * 1024
USAGE_COUNTERS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "tool_calls")
TURN_COLUMNS = (
    Turn.id, Turn.actor_id, Turn.session_id, Turn.model, Turn.status, Turn.usage,
    Turn.created_at, Turn.accepted_at, Turn.finished_at,
    Turn.parent_turn, Turn.preparation.is_not(None).label("has_preparation"),
)


def usage_json(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def record_time(value: str) -> datetime:
    parsed = utc(value)
    if parsed is None:
        raise HTTPException(503, "An analytics record has an invalid timestamp.")
    return parsed


async def usage_deltas(runtime: Any, db: Any, records: list[Any]) -> dict[str, dict[str, int | None]]:
    """Subtract the exact admitted seed; never sum cumulative SDK RunUsage.

    Preparation contains private content, so read a bounded amount only inside
    this adapter and return counters alone. Missing/oversize/invalid seeds stay
    unknown, rather than borrowing another turn's possibly different context.
    """
    lengths = (await db.execute(select(Turn.id, func.length(Turn.preparation)).where(
        Turn.id.in_([row.id for row in records]),
    ))).all()
    selected, remaining = [], MAX_BASELINE_BYTES
    for identity, size in sorted(lengths, key=lambda item: item[1] or 0):
        if size is not None and 0 < size <= min(2 * 1024 * 1024, remaining):
            selected.append(identity)
            remaining -= size
    baselines: dict[str, dict[str, Any] | None] = {}
    for identity, encrypted in (await db.execute(select(Turn.id, Turn.preparation).where(Turn.id.in_(selected)))).all():
        try:
            prepared = json.loads(runtime.security.unseal(encrypted))
            if not isinstance(prepared, dict) or "conversation" not in prepared:
                continue
            if prepared["conversation"] is None:
                baselines[identity] = dict.fromkeys(USAGE_COUNTERS, 0)
            elif isinstance(prepared["conversation"], str):
                conversation = json.loads(prepared["conversation"])
                seed = conversation.get("usage") if isinstance(conversation, dict) else None
                if isinstance(seed, dict):
                    baselines[identity] = seed
        except (ValueError, TypeError, InvalidToken, UnicodeError):
            continue
    result = {}
    for row in records:
        current, seed = usage_json(row.usage), baselines.get(row.id)
        deltas = {}
        for key in USAGE_COUNTERS:
            value = integer(current.get(key))
            previous = integer(seed.get(key)) if seed is not None else None
            deltas[key] = value - previous if value is not None and previous is not None and value >= previous else None
        result[row.id] = deltas
    return result


def legacy_event(row: Any, usage: dict[str, int | None]) -> UsageEvent:
    inputs, outputs = usage["input_tokens"], usage["output_tokens"]
    return UsageEvent(
        f"legacy:{row.id}", row.id, row.session_id, row.actor_id, row.model, record_time(row.created_at),
        requests=usage["requests"] or 0, input_tokens=inputs, output_tokens=outputs,
        total_tokens=inputs + outputs if inputs is not None and outputs is not None else None,
        cache_read_tokens=usage["cache_read_tokens"],
        cache_write_tokens=usage["cache_write_tokens"], legacy=True,
        request_count_known=usage["requests"] is not None,
    )


async def undispatched_failures(runtime: Any, db: Any, records: list[Any]) -> set[str]:
    """Canonical dispatch commits an admission before any upstream request.

    A terminal failure with product authority and no SDK usage is known unsent
    only when both ledgers have no attempts or exclusively proven-not-dispatched
    reservations. Historical/standalone unknowns and dispatched attempts retain
    their existing missing-cost treatment.
    """
    if not getattr(runtime.database, "product_authority_enabled", False):
        return set()
    from .product_inference import ProductInferenceRequest

    candidates = {
        row.id for row in records if row.status == "failed"
        and not usage_json(row.usage)
    }
    if not candidates:
        return set()
    return set(await db.scalars(select(ProductTurnAuthority.turn_id).where(
        ProductTurnAuthority.turn_id.in_(candidates),
        ~select(ProductInferenceRequest.id).where(
            ProductInferenceRequest.turn_id == ProductTurnAuthority.turn_id,
            or_(ProductInferenceRequest.status != "not_dispatched", ProductInferenceRequest.cost_usd.is_not(None)),
        ).exists(),
        ~select(InferenceUsage.id).where(
            InferenceUsage.turn_id == ProductTurnAuthority.turn_id,
            or_(InferenceUsage.status != "not_dispatched", InferenceUsage.cost_usd.is_not(None)),
        ).exists(),
    )))


def inference_event(row: InferenceUsage) -> UsageEvent:
    return UsageEvent(
        row.id, row.turn_id, row.session_id, row.actor_id, row.model, record_time(row.created_at),
        requests=0 if row.status == "not_dispatched" else 1,
        input_tokens=integer(row.input_tokens), output_tokens=integer(row.output_tokens),
        total_tokens=integer(row.total_tokens), cache_read_tokens=integer(row.cache_read_tokens),
        cache_write_tokens=integer(row.cache_write_tokens),
        cost=actual_cost(row.cost_usd, row.cost_status, row.cost_source),
    )


def elapsed(start: str | None, end: str | None) -> float | None:
    lower, upper = utc(start), utc(end)
    return (upper - lower).total_seconds() * 1000 if lower and upper and upper >= lower else None


def turn_view(
    row: Any, events: list[UsageEvent], delta: dict[str, int | None], unknown: set[str], gaps: set[str],
    *, steering: bool = False,
) -> dict[str, Any]:
    totals = usage_totals(events, unknown_turns=unknown & {row.id}, capture_gaps=gaps & {row.id})
    return {
        "id": row.id, "user_id": row.actor_id, "model": row.model, "status": row.status,
        "cost_models": sorted({event.model for event in events}),
        "usage_role": "steering_input" if steering else "inference_turn",
        "created_at": row.created_at, "accepted_at": row.accepted_at, "finished_at": row.finished_at,
        **totals.view(),
        "tool_calls": delta["tool_calls"] or 0,
        "queue_ms": elapsed(row.created_at, row.accepted_at),
        "latency_ms": elapsed(row.accepted_at, row.finished_at),
        "total_ms": elapsed(row.created_at, row.finished_at),
    }


def turn_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [row["latency_ms"] for row in rows if row["latency_ms"] is not None]
    return {
        "turns": len(rows), "completed": sum(row["status"] == "completed" for row in rows),
        "failed": sum(row["status"] == "failed" for row in rows),
        "latency_samples": len(durations),
        "mean_latency_ms": sum(durations) / len(durations) if durations else None,
        "p50_latency_ms": percentile(durations, .5), "p95_latency_ms": percentile(durations, .95),
        "priced_turns": sum(row["pricing_status"] == "priced" for row in rows if row["usage_role"] != "steering_input"),
        "unpriced_turns": sum(row["pricing_status"] in {"partial", "unpriced"} for row in rows
                             if row["usage_role"] != "steering_input"),
    }


def filtered(query: Any, model: str | None, status: str | None, actor_ids: set[str] | None, *, table: Any = Turn):
    if model:
        query = query.where(table.model == model)
    if actor_ids is not None:
        query = query.where(table.actor_id.in_(actor_ids))
    if status:
        query = query.where(Turn.status == status)
    return query


def unique_publications(rows: list[PRPublication]) -> list[PRPublication]:
    """Prefer stable provider IDs; aliases alone never replace a known identity."""
    github_ids: dict[tuple[int, int], set[int]] = defaultdict(set)
    for row in rows:
        if row.github_id and row.repository_id:
            github_ids[(row.repository_id, row.number)].add(row.github_id)

    def stable_key(row: PRPublication) -> str | None:
        if row.github_id:
            return f"github:{row.github_id}"
        if row.repository_id:
            candidates = github_ids[(row.repository_id, row.number)]
            return f"github:{next(iter(candidates))}" if len(candidates) == 1 else f"repo:{row.repository_id}:{row.number}"
        return None

    identities: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if key := stable_key(row):
            identities[row.url_key].add(key)
    result: dict[str, PRPublication] = {}
    for row in sorted(rows, key=lambda value: (value.created_at, value.id)):
        known = identities[row.url_key]
        key = stable_key(row) or (next(iter(known)) if len(known) == 1 else f"url:{row.url_key}")
        if key not in result:
            result[key] = row
        elif not result[key].github_id and row.github_id or not result[key].repository_id and row.repository_id:
            # Enrich a minimal earlier receipt without changing its creator or
            # confirmation date, and without mutating a persisted ORM record.
            first = result[key]
            projected = PRPublication(**{
                column.name: getattr(first, column.name) for column in PRPublication.__table__.columns
            })
            projected.github_id = first.github_id or row.github_id
            projected.repository_id = first.repository_id or row.repository_id
            result[key] = projected
    return list(result.values())


async def publication_statuses(
    runtime: Any, user: User, publications: list[PRPublication], period: ReportRange, service: PullRequestService,
) -> tuple[dict[str, dict[str, Any]], int]:
    ids = list({row.session_id for row in publications})
    scopes = await service.contexts(user, ids)
    statuses: dict[str, dict[str, Any]] = {}
    checked = 0
    # Prioritize the visible creation cohort; older receipts can contribute
    # merge events, but an unbounded historical network scan is not permitted.
    wanted = sorted(publications, key=lambda row: (
        not period.contains(utc(row.created_at)), row.created_at,
    ))[:MAX_PR_STATUS_READS]

    async def load(row: PRPublication) -> None:
        nonlocal checked
        scope = scopes.get(row.session_id)
        if scope is None:
            return
        try:
            target = json.loads(runtime.security.unseal(row.target))
            pr = parse_pr_url(target["url"])
        except (ValueError, UnicodeError, InvalidToken, KeyError, HTTPException):
            return
        result = await service.get(scope, pr.path)
        checked += 1
        if result.error:
            return
        snapshot = verified_status(row, result.value)
        if snapshot:
            statuses[row.id] = snapshot

    try:
        async with asyncio.timeout(10):
            await asyncio.gather(*(load(row) for row in wanted))
    except TimeoutError:
        pass
    # Cache entries are scoped to the viewer's CURRENT native connections.
    # A read must not survive account revocation while GitHub is awaited.
    fresh = await service.contexts(user, ids)
    for row in publications:
        if row.session_id not in fresh or fresh[row.session_id] != scopes.get(row.session_id):
            statuses.pop(row.id, None)
    return statuses, checked


def pr_metrics(
    publications: list[PRPublication], statuses: dict[str, dict[str, Any]], period: ReportRange,
) -> dict[str, Any]:
    created = [row for row in publications if period.contains(utc(row.created_at))]
    distribution = dict.fromkeys(("open", "draft", "closed", "merged", "unknown"), 0)
    for row in created:
        distribution[statuses.get(row.id, {}).get("state", "unknown")] += 1
    merged = [
        row for row in publications
        if statuses.get(row.id, {}).get("merged_at") and period.contains(statuses[row.id]["merged_at"])
    ]
    return {
        "prs_created": len(created), "prs_merged": len(merged), "pr_status_counts": distribution,
        "prs_open": distribution["open"], "prs_draft": distribution["draft"],
        "prs_closed": distribution["closed"], "prs_unknown": distribution["unknown"],
        "prs_merged_current": distribution["merged"],
        "pr_status_coverage": (len(created) - distribution["unknown"]) / len(created) if created else None,
    }


async def report(
    runtime: Any, user: User, start: str | None, end: str | None, model: str | None,
    status: str | None, user_id: str | None, limit: int, offset: int, format: str,
    *, preset: str = "7d", export: str = "turns", service: PullRequestService | None = None,
) -> Any:
    if user.role != "admin" or not user.enabled:
        raise HTTPException(403, "Administrator access is required.")
    try:
        period = report_range(preset, start, end)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    lower, upper = period.start.isoformat(), period.end.isoformat()
    identities = await report_identities(runtime)
    actor_ids = identities.actors(user_id)
    person = identities.canonical
    users = identities.users
    async with runtime.database.sessions() as db:
        turns = list((await db.execute(filtered(select(*TURN_COLUMNS).where(
            Turn.created_at >= lower, Turn.created_at < upper,
        ), model, status, actor_ids).limit(MAX_TURNS + 1))).all())
        ledger_query = select(InferenceUsage).join(
            Turn, Turn.id == InferenceUsage.turn_id, isouter=True,
        ).where(InferenceUsage.created_at >= lower, InferenceUsage.created_at < upper)
        if model:
            ledger_query = ledger_query.where(Turn.model == model)
        ledger = list(await db.scalars(filtered(
            ledger_query, None, status, actor_ids, table=InferenceUsage,
        ).limit(MAX_INFERENCES + 1)))
        if len(turns) > MAX_TURNS or len(ledger) > MAX_INFERENCES:
            raise HTTPException(422, "Too many matching records. Narrow the date range or filters.")
        ids = {row.id for row in turns}
        missing_ids = {row.turn_id for row in ledger} - ids
        # Native admission IDs are bounded by ledger records; do not load prompt,
        # result, conversation or session title columns.
        if missing_ids:
            turns.extend((await db.execute(select(*TURN_COLUMNS).where(Turn.id.in_(missing_ids)))).all())
        if len(turns) > MAX_TURNS:
            raise HTTPException(422, "More than 10,000 active turns match. Narrow the filters.")
        timing = await performance_report.observations(db, turns, period)
        ledger_counts = dict((await db.execute(select(InferenceUsage.turn_id, func.count()).where(
            InferenceUsage.turn_id.in_([row.id for row in turns]),
        ).group_by(InferenceUsage.turn_id))).all())
        steering = {row.id for row in turns if row.parent_turn and not row.has_preparation and row.id not in ledger_counts}
        undispatched = await undispatched_failures(runtime, db, turns)
        inference_turns = [row for row in turns if row.id not in steering | undispatched]
        deltas = await usage_deltas(runtime, db, inference_turns)
        unknown = {row.id for row in inference_turns if deltas[row.id]["requests"] is None}
        gaps = {
            row.id for row in inference_turns if ledger_counts.get(row.id, 0)
            and deltas[row.id]["requests"] is not None
            and deltas[row.id]["requests"] > ledger_counts[row.id]
        }
        session_query = select(Session.id, Session.owner_id, Session.created_at).where(
            Session.created_at >= lower, Session.created_at < upper,
        )
        if actor_ids is not None:
            session_query = session_query.where(Session.owner_id.in_(actor_ids))
        if model:
            session_query = session_query.where(Session.model == model)
        if status:
            session_query = session_query.where(select(Turn.id).where(
                Turn.session_id == Session.id, Turn.status == status,
            ).exists())
        created_sessions = list((await db.execute(session_query.limit(MAX_TURNS + 1))).all())
        publications = list(await db.scalars(filtered(select(PRPublication).join(
            Turn, Turn.id == PRPublication.turn_id, isouter=True,
        ).where(PRPublication.created_at < upper), model, status, actor_ids, table=PRPublication).limit(MAX_TURNS + 1)))
        if len(publications) > MAX_TURNS or len(created_sessions) > MAX_TURNS:
            raise HTTPException(422, "Too many sessions or publication receipts match. Narrow the filters.")
        if publications and (user_id or model or status):
            # Filters select candidate identities, never their creator. Bring
            # every receipt for those identities into canonical attribution
            # before filtering; a later actor cannot claim an existing PR.
            github_ids = {row.github_id for row in publications if row.github_id}
            repo_ids = {(row.repository_id, row.number) for row in publications if row.repository_id}
            urls = {row.url_key for row in publications}
            publications = list(await db.scalars(select(PRPublication).where(
                PRPublication.created_at < upper, or_(
                    PRPublication.github_id.in_(github_ids),
                    tuple_(PRPublication.repository_id, PRPublication.number).in_(repo_ids),
                    PRPublication.url_key.in_(urls),
                ),
            ).limit(MAX_TURNS + 1)))
            if len(publications) > MAX_TURNS:
                raise HTTPException(422, "Too many correlated publication receipts match. Narrow the filters.")
        publication_turn_status = dict((await db.execute(select(Turn.id, Turn.status).where(
            Turn.id.in_({row.turn_id for row in publications}),
        ))).all()) if status else {}
        candidate_effects = select(Effect.actor_id, func.count()).select_from(Effect).join(Turn, Turn.id == Effect.turn_id).where(
            Effect.state == "confirmed", Effect.tool_name.like("github\\_%\\_create\\_pull\\_request", escape="\\"),
            Effect.created_at < upper,
            ~select(PRPublication.id).where(PRPublication.id == Effect.id).exists(),
        ).group_by(Effect.actor_id)
        untracked_by_user: dict[str, int] = defaultdict(int)
        for actor_id, count in (await db.execute(filtered(candidate_effects, model, status, actor_ids))).all():
            untracked_by_user[person(actor_id)] += count
        untracked = sum(untracked_by_user.values())
    events = [inference_event(row) for row in ledger]
    events.extend(legacy_event(row, deltas[row.id]) for row in inference_turns if row.id not in ledger_counts)
    events = [replace(event, user_id=person(event.user_id)) for event in events]
    per_turn: dict[str, list[UsageEvent]] = defaultdict(list)
    for event in events:
        per_turn[event.turn_id].append(event)
    turns.sort(key=lambda row: (row.created_at, row.id), reverse=True)
    rows = [turn_view(
        row, per_turn[row.id], deltas.get(row.id, dict.fromkeys(USAGE_COUNTERS, 0)), unknown, gaps,
        steering=row.id in steering,
    ) for row in turns]
    for row in rows:
        if person(row["user_id"]) != row["user_id"]:
            row["actor_id"], row["user_id"] = row["user_id"], person(row["user_id"])
        row.update(timing.get(row["id"], performance_report.timing_view({})))
    receipt_rows = publications
    publications = [
        row for row in unique_publications(publications)
        if (actor_ids is None or row.actor_id in actor_ids) and (not model or row.model == model)
        and (not status or publication_turn_status.get(row.turn_id) == status)
    ]
    service = service or PullRequestService(runtime)
    statuses, checked = await publication_statuses(runtime, user, publications, period, service)

    # Legacy PR analytics attribute cost to explicitly linked session families.
    # Native sessions already contain their delegated turns. Count each actor /
    # session pair once and require complete cost + status coverage for ratios.
    merged = [row for row in publications if statuses.get(row.id, {}).get("merged_at")
              and period.contains(statuses[row.id]["merged_at"])]
    linked_pairs = {
        (person(row.actor_id), receipt.session_id)
        for row in merged for receipt in receipt_rows
        if (
            row.github_id and row.github_id == receipt.github_id
            or row.url_key == receipt.url_key and not (
                row.github_id and receipt.github_id and row.github_id != receipt.github_id
            )
        )
    }
    linked_events: list[UsageEvent] = []
    linked_unknown: set[str] = set()
    linked_gaps: set[str] = set()
    linked_actors: dict[str, str] = {}
    linked_limited = False
    if linked_pairs:
        linked_ids = {session for _, session in linked_pairs}
        async with runtime.database.sessions() as db:
            linked_ledger_query = select(InferenceUsage).join(Turn, Turn.id == InferenceUsage.turn_id, isouter=True).where(
                InferenceUsage.session_id.in_(linked_ids), InferenceUsage.created_at < upper,
            )
            linked_turn_query = select(*TURN_COLUMNS).where(
                Turn.session_id.in_(linked_ids), Turn.created_at < upper,
            )
            if model:
                linked_ledger_query = linked_ledger_query.where(Turn.model == model)
                linked_turn_query = linked_turn_query.where(Turn.model == model)
            linked_ledger = list(await db.scalars(linked_ledger_query.limit(MAX_INFERENCES + 1)))
            linked_turns = list((await db.execute(linked_turn_query.limit(MAX_TURNS + 1))).all())
            linked_limited = len(linked_ledger) > MAX_INFERENCES or len(linked_turns) > MAX_TURNS
            linked_deltas = await usage_deltas(runtime, db, linked_turns)
            linked_counts = dict((await db.execute(select(InferenceUsage.turn_id, func.count()).where(
                InferenceUsage.turn_id.in_([row.id for row in linked_turns]),
            ).group_by(InferenceUsage.turn_id))).all())
            linked_undispatched = await undispatched_failures(runtime, db, linked_turns)
            linked_turns = [
                row for row in linked_turns if row.id not in linked_undispatched
                and (not row.parent_turn or row.has_preparation or row.id in linked_counts)
            ]
        if not linked_limited:
            linked_actors = {row.id: person(row.actor_id) for row in linked_turns}
            linked_unknown = {
                row.id for row in linked_turns if (person(row.actor_id), row.session_id) in linked_pairs
                and linked_deltas[row.id]["requests"] is None
            }
            linked_gaps = {
                row.id for row in linked_turns if (person(row.actor_id), row.session_id) in linked_pairs
                and linked_counts.get(row.id, 0) and linked_deltas[row.id]["requests"] is not None
                and linked_deltas[row.id]["requests"] > linked_counts[row.id]
            }
            linked_events = [
                inference_event(row) for row in linked_ledger if (person(row.actor_id), row.session_id) in linked_pairs
            ] + [
                legacy_event(row, linked_deltas[row.id]) for row in linked_turns
                if row.id not in linked_counts and (person(row.actor_id), row.session_id) in linked_pairs
            ]
            linked_events = [replace(event, user_id=person(event.user_id)) for event in linked_events]
    def complete_status(values: list[PRPublication]) -> bool:
        return all(row.id in statuses and not (
            statuses[row.id]["state"] == "merged" and statuses[row.id]["merged_at"] is None
        ) for row in values)

    active_sessions = {row.session_id for row in turns} | {row.session_id for row in ledger}
    totals = usage_totals(events, unknown_turns=unknown, capture_gaps=gaps)
    summary = {
        **turn_metrics(rows), **totals.view(), "sessions": len(active_sessions),
        "sessions_active": len(active_sessions), "sessions_created": len(created_sessions),
        **pr_metrics(publications, statuses, period),
        "cost_per_merged_pr_usd": cost_per_merge(
            usage_totals(linked_events, unknown_turns=linked_unknown, capture_gaps=linked_gaps), len(merged),
            statuses_complete=complete_status(publications) and not untracked,
        ),
    }
    by_user = []
    contributors = (
        {person(row.actor_id) for row in turns} | {item.user_id for item in events}
        | {person(row.owner_id) for row in created_sessions}
        | {person(row.actor_id) for row in publications if period.contains(utc(row.created_at))}
        | {person(row.actor_id) for row in merged}
    )
    for identity in sorted(contributors):
        user_events = [item for item in events if item.user_id == identity]
        user_turns = [row for row in turns if person(row.actor_id) == identity]
        user_prs = [row for row in publications if person(row.actor_id) == identity]
        user_merged = [row for row in merged if person(row.actor_id) == identity]
        user_ids = {row.id for row in user_turns}
        linked_user_ids = {key for key, actor_id in linked_actors.items() if actor_id == identity}
        active = {row.session_id for row in user_turns} | {item.session_id for item in user_events}
        by_user.append({
            "user_id": identity, **turn_metrics([row for row in rows if row["user_id"] == identity]),
            **usage_totals(user_events, unknown_turns=unknown & user_ids, capture_gaps=gaps & user_ids).view(),
            "sessions": len(active), "sessions_active": len(active),
            "sessions_created": sum(person(row.owner_id) == identity for row in created_sessions),
            **pr_metrics(user_prs, statuses, period),
            "cost_per_merged_pr_usd": cost_per_merge(
                usage_totals(
                    (item for item in linked_events if item.user_id == identity),
                    unknown_turns=linked_unknown & linked_user_ids, capture_gaps=linked_gaps & linked_user_ids,
                ), len(user_merged),
                statuses_complete=complete_status(user_prs) and not untracked_by_user.get(identity, 0),
            ),
        })
    by_user.sort(key=lambda row: (-row["prs_merged"], -row["turns"], row["user_id"]))
    series = daily_usage(events, period)
    cumulative_incomplete = False
    for day in series:
        day_start = utc(day["date"])
        assert day_start is not None
        day_period = ReportRange(max(day_start, period.start), min(day_start + timedelta(days=1), period.end), "custom")
        day_events = [item for item in events if day_period.contains(item.created_at)]
        created_turns = [row for row in turns if day_period.contains(utc(row.created_at))]
        day_turn_ids = {item.turn_id for item in day_events} | {row.id for row in created_turns}
        day_active = {item.session_id for item in day_events} | {row.session_id for row in created_turns}
        possible_ids = {
            row.id for row in turns if record_time(row.created_at) < day_period.end
            and (not row.finished_at or record_time(row.finished_at) >= day_period.start)
        }
        day_unknown, day_gaps = unknown & possible_ids, gaps & possible_ids
        cumulative_incomplete |= bool(day_unknown or day_gaps)
        if cumulative_incomplete:
            day["cumulative_pricing_status"] = "partial" if day["cumulative_cost_usd"] is not None else "unpriced"
        day.update({
            **usage_totals(day_events, unknown_turns=day_unknown, capture_gaps=day_gaps).view(),
            **turn_metrics([row for row in rows if row["id"] in day_turn_ids]),
            "sessions": len(day_active), "sessions_active": len(day_active),
            "sessions_created": sum(day_period.contains(utc(row.created_at)) for row in created_sessions),
            **pr_metrics(publications, statuses, day_period),
        })
        for entry in day["models"]:
            model_ids = {row.id for row in turns if row.model == entry["model"]} | {
                item.turn_id for item in events if item.model == entry["model"]
            }
            entry.update(usage_totals(
                (item for item in day_events if item.model == entry["model"]),
                unknown_turns=day_unknown & model_ids, capture_gaps=day_gaps & model_ids,
            ).view())
    by_model = model_totals(events)
    for entry in by_model:
        model_ids = {row.id for row in turns if row.model == entry["model"]} | {
            item.turn_id for item in events if item.model == entry["model"]
        }
        entry.update(usage_totals(
            (item for item in events if item.model == entry["model"]),
            unknown_turns=unknown & model_ids, capture_gaps=gaps & model_ids,
        ).view())
    if format == "csv":
        exported = rows if export == "turns" else by_user if export == "users" else series if export == "daily" else by_model
        if export == "users":
            user_labels = {row["id"]: row for row in users}
            exported = [{
                **row, "display_name": user_labels.get(row["user_id"], {}).get("display_name"),
                "username": user_labels.get(row["user_id"], {}).get("username"),
            } for row in exported]
        flattened = [{
            **row, **({"cost_models": json.dumps(row["cost_models"])} if "cost_models" in row else {}),
            **{f"prs_status_{key}": value for key, value in row.get("pr_status_counts", {}).items()},
        } for row in exported]
        columns = list(dict.fromkeys(
            key for row in flattened for key in row if key not in {"pr_status_counts", "models"}
        )) or (["id"] if export == "turns" else ["user_id"] if export == "users" else ["date"] if export == "daily" else ["model"])
        # The exact decimal subtotal is exported alongside its numeric chart value.
        response: Any = Response(
            csv_export(flattened, columns), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="moyai-{export}.csv"'},
        )
    else:
        response = {
            "rows": rows[offset:offset + limit], "summary": summary, "series": series,
            "by_user": by_user, "by_model": by_model, "models": sorted({row.model for row in turns}),
            "users": users, "total": len(rows), "limit": limit, "offset": offset,
            "start": lower, "end": upper, "preset": period.preset, "timezone": "UTC", "currency": "USD",
            "performance": performance_report.summarize(timing, turns, period),
            "coverage": {
                "legacy_turns": len(totals.legacy_turns), "ledger_requests": len(ledger),
                "unknown_request_turns": len(totals.unknown_request_turns),
                "capture_gap_turns": len(gaps),
                "undispatched_failed_turns": len(undispatched),
                "linked_cost_limited": linked_limited,
                "priced_requests": totals.priced_requests, "unpriced_requests": totals.unpriced_requests,
                "pr_receipts": len(publications), "pr_status_checked": checked,
                "pr_status_unknown": sum(row.id not in statuses for row in publications),
                "pr_merge_date_unknown": sum(
                    statuses.get(row.id, {}).get("state") == "merged" and not statuses[row.id]["merged_at"]
                    for row in publications
                ),
                "unclassified_confirmed_create_calls": untracked,
                "pr_tracking": "confirmed_native_github_tool_receipts",
            },
            "methodology": {
                **performance_report.METHODOLOGY,
                "users": "User totals and filters combine saved Slack accounting links with their Google or SSO account. Names and emails alone never merge users; recorded actor IDs are preserved.",
                "sessions": "Created uses Session.created_at; active is distinct sessions with a newly admitted turn or recorded inference in range. Daily active counts are distinct per UTC date.",
                "prs_created": "Confirmed native GitHub create_pull_request receipts, dated by confirmation. Attachments, mentions, shell/CLI creations and old engine history are not counted.",
                "prs_merged": "Verified GitHub merge events in range, including older creations. Status counts describe the creation cohort's current verified state; unavailable reads remain unknown.",
                "cost": "Actual reported USD subtotal only; partial coverage excludes missing amounts, never estimates them. Inference attempts use their recorded timestamp. Historical usage subtracts the exact admitted conversation's cumulative seed and stays unknown if that seed is unavailable. Only turns with no attempt ledger use this unpriced fallback.",
                "requests": "New ledger requests count observed HTTP attempts, including retries. Historical fallback uses the admitted-seed usage delta. Attached steering inputs remain in input/turn counts but their inference belongs to the parent turn; they do not add missing-cost records.",
                "coverage": "Coverage concerns observed records. Failed product turns with no SDK usage and no dispatched request in either durable ledger do not add missing costs. A verified SDK request delta above ALL attempt rows demonstrates a capture gap; retries can mask gaps, so absence of that signal is not proof of complete billing. Baseline reads are bounded and missing baselines remain unknown.",
                "model": "The model filter selects root turns and their PR/session cohort, including child-model attempts. Ledger breakdowns use the actual requested alias. Historical fallback is attributed only to its recorded root model and marked legacy; historical per-request aliases are unavailable.",
                "cost_per_merged_pr": "Actor inference spend through end across distinct sessions linked to that actor's merged PRs, matching the selected root-turn model filter, divided by verified merge events. Null when linked cost or PR status coverage is incomplete; this is session correlation, not exact per-PR billing.",
            },
            "capabilities": {"pr_analytics": True, "reported_costs": True},
            "limits": {"max_turns": MAX_TURNS, "max_inferences": MAX_INFERENCES,
                       "max_pr_status_reads": MAX_PR_STATUS_READS,
                       "max_baseline_bytes_per_cohort": MAX_BASELINE_BYTES},
            "cost_note": "Only reported costs are summed. Missing and historical prices remain unpriced; partial subtotals exclude them.",
            "latency_note": "Latency is accepted-to-finished wall time, including tools and waits; it is not model TTFT.",
        }
    fresh = await runtime.database.user(user.id)
    if fresh.role != "admin" or fresh.auth_version != user.auth_version:
        raise HTTPException(401, "Your access changed. Sign in again.")
    return response
