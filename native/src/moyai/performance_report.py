"""Bounded, root-turn timing cohorts for native administrative analytics."""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from .analytics import percentile, utc
from .models import Event
from .performance import KINDS, PREFIX


def elapsed(start: str | None, end: str | None) -> float | None:
    lower, upper = utc(start), utc(end)
    return (upper - lower).total_seconds() * 1000 if lower and upper and upper >= lower else None


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "samples": len(values), "mean_ms": sum(values) / len(values) if values else None,
        "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
    }


async def observations(db: Any, turns: list[Any], period: Any) -> dict[str, dict[str, Any]]:
    # Root admission cohort, independent of usage-ledger pagination/activity dates.
    ids = [turn.id for turn in turns if not turn.parent_turn and period.contains(utc(turn.created_at))]
    records = (await db.execute(select(
        Event.turn_id, Event.kind, Event.created_at, Event.content,
    ).where(
        Event.turn_id.in_(ids), Event.kind.in_([PREFIX + kind for kind in KINDS]),
    ).limit(len(ids) * len(KINDS) + 1))).all() if ids else []
    if len(records) > len(ids) * len(KINDS):
        raise HTTPException(503, "Native timing evidence exceeds its expected bound.")
    by_turn: dict[str, dict[str, Any]] = {identity: {} for identity in ids}
    for turn_id, kind, stamp, content in records:
        try:
            details = json.loads(content)
            if not isinstance(details, dict):
                continue
        except (ValueError, TypeError):
            continue
        by_turn[turn_id][kind.removeprefix(PREFIX)] = {"at": stamp, **details}
    return {identity: timing_view(value) for identity, value in by_turn.items()}


def timing_view(value: dict[str, Any]) -> dict[str, Any]:
    received = value.get("received", {}).get("at")
    worker = value.get("worker", {}).get("at")
    dispatch = value.get("dispatch", {})
    broker, outbound = dispatch.get("broker_received_at"), dispatch.get("at")
    computer = value.get("computer", {})
    parts = (
        elapsed(received, worker), elapsed(worker, broker), elapsed(broker, outbound),
    )
    complete = all(part is not None for part in parts)
    return {
        "api_received_at": received, "worker_picked_up_at": worker,
        "broker_received_at": broker, "first_dispatch_at": outbound,
        "dispatch_attempt_id": dispatch.get("attempt_id"), "dispatch_route": dispatch.get("route"),
        "api_to_dispatch_ms": elapsed(received, outbound),
        # Keep the additive breakdown on precisely the same complete cohort.
        "api_to_worker_ms": parts[0] if complete else None,
        "worker_to_broker_ms": parts[1] if complete else None,
        "broker_to_dispatch_ms": parts[2] if complete else None,
        "computer_requested_at": computer.get("at"), "computer_ready_at": (
            computer.get("finished_at") if computer.get("status") == "ready" else None
        ),
        "computer_mode": computer.get("mode"), "computer_status": computer.get("status"),
        "computer_ready_ms": (
            elapsed(computer.get("at"), computer.get("finished_at"))
            if computer.get("status") == "ready" else None
        ),
    }


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str, cohort: list[dict[str, Any]] = rows) -> list[float]:
        return [row[key] for row in cohort if row.get(key) is not None]

    complete = [row for row in rows if row.get("api_to_worker_ms") is not None]
    dispatch = distribution(values("api_to_dispatch_ms"))
    return {
        "eligible_turns": len(rows),
        "api_received_samples": sum(row.get("api_received_at") is not None for row in rows),
        "dispatch": dispatch,
        "missing_dispatch_samples": len(rows) - dispatch["samples"],
        "breakdown": {
            "samples": len(complete),
            **{key: distribution(values(key, complete)) for key in (
                "api_to_dispatch_ms", "api_to_worker_ms", "worker_to_broker_ms", "broker_to_dispatch_ms",
            )},
        },
        "computer": {
            **{mode: distribution(values("computer_ready_ms", [
                row for row in rows if row.get("computer_mode") == mode
            ])) for mode in ("cold", "restore", "reuse")},
            "requested": sum(row.get("computer_status") is not None for row in rows),
            "failed": sum(row.get("computer_status") == "failed" for row in rows),
            "pending": sum(row.get("computer_status") in {"pending", "prepared"} for row in rows),
        },
    }


METHODOLOGY = {
    "startup": (
        "API receipt → first foreground LiteLLM dispatch is server middleware entry before authentication "
        "and body validation → outbound transport attempt, after broker admission. It includes Moyai "
        "processing and queueing, not provider receipt, provider generation, or browser delivery. "
        "Only root turns created in range are eligible. Historical and non-HTTP admissions without "
        "receipt evidence stay missing. Steering, delegated and maintenance requests are excluded."
    ),
    "startup_breakdown": (
        "API → worker ends at entry to the first successful turn-preparation activity; it includes "
        "admission and queue wait. Worker → broker includes workspace/context preparation and handoff. "
        "Broker → dispatch includes authorization, capacity and accounting admission. In direct local "
        "mode, broker arrival means entry to local inference admission. All component means use the "
        "same ordered, complete cohort; they add to its mean total. Percentiles are not additive."
    ),
    "computer_readiness": (
        "First computer acquisition attempt per root turn, from acquisition/recovery entry until "
        "SDK workspace ensure succeeds, including any preceding native workspace preparation. "
        "Cold includes initial image/setup work; "
        "restore includes saved-file recovery; reuse verifies an existing computer. A prepared "
        "environment image is cold, not restoration of this session. Model-only turns need no computer. "
        "Failed/incomplete attempts are counted separately and are not zero-duration successes."
    ),
    "timing_coverage": (
        "Versioned, content-free timing evidence is best effort. Missing or reversed timestamps remain "
        "unknown, never backfilled from billing timestamps. Cross-process timings require synchronized "
        "server clocks. The first outbound attempt keeps its own broker arrival, including retries "
        "and late settlement after cancellation."
    ),
}
