"""Bounded, root-turn timing cohorts for native administrative analytics."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import Counter
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from .analytics import ReportRange, percentile, utc
from .models import Event
from .performance import KINDS, PREFIX


def elapsed(start: str | None, end: str | None) -> float | None:
    lower, upper = utc(start), utc(end)
    return (upper - lower).total_seconds() * 1000 if lower and upper and upper >= lower else None


def distribution(values: list[float]) -> dict[str, Any]:
    return {
        "samples": len(values), "mean_ms": sum(values) / len(values) if values else None,
        "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
        "max_ms": max(values) if values else None,
    }


async def observations(db: Any, turns: list[Any], period: Any) -> dict[str, dict[str, Any]]:
    # Account for every admission, including inputs whose work belongs to a parent.
    ids = [turn.id for turn in turns if period.contains(utc(turn.created_at))]
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
    return {
        turn.id: turn_timing(timing_view(by_turn[turn.id]), turn)
        for turn in turns if turn.id in by_turn
    }


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
        "timing_observed": bool(value),
        "timing_start_source": value.get("received", {}).get("source", "http" if received else None),
        "timing_surface": value.get("received", {}).get("surface"),
        "api_received_at": received, "worker_picked_up_at": worker,
        "broker_received_at": broker, "first_dispatch_at": outbound,
        "dispatch_attempt_id": dispatch.get("attempt_id"), "dispatch_route": dispatch.get("route"),
        "api_to_dispatch_ms": elapsed(received, outbound),
        "queue_pickup_ms": elapsed(received, worker),
        "broker_preparation_ms": elapsed(broker, outbound),
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


CARD_FIELDS = {
    "startup": ("api_to_dispatch_ms", "first_dispatch_at"),
    "queue": ("queue_pickup_ms", "worker_picked_up_at"),
    "broker": ("broker_preparation_ms", "first_dispatch_at"),
    "computer": ("computer_ready_ms", "computer_ready_at"),
}

LIVE_STATUSES = {"queued", "starting", "running", "saving", "waiting", "steering", "signal_sent", "accepted"}
EXCLUDED_REASONS = {"steering_input", "control", "no_acquisition"}


def reason(row: dict[str, Any], name: str) -> str:
    """Explain an absent sample without treating absent evidence as a known zero."""
    if row.get("_parent_turn"):
        return "steering_input"
    if row.get("timing_start_source") == "control" or row.get("_control"):
        return "control"
    field, end = CARD_FIELDS[name]
    if row.get(field) is not None:
        return "measured"
    if not row.get("timing_observed"):
        return "not_instrumented"
    live = row.get("_status") in LIVE_STATUSES
    if name == "computer":
        status = row.get("computer_status")
        if status is None:
            return "no_acquisition"
        if status == "failed":
            return "acquisition_failed"
        if status in {"pending", "prepared"}:
            return "waiting" if live else "acquisition_incomplete"
    elif row.get(end) is None:
        if live:
            return "waiting"
        if row.get("_has_preparation") is False and not row.get("first_dispatch_at"):
            return "ended_before_preparation"
        if row.get("_status") in {"failed", "stopped"}:
            return "ended_without_observation"
    start = row.get(
        "computer_requested_at" if name == "computer"
        else "broker_received_at" if name == "broker" else "api_received_at"
    )
    if start is not None and row.get(end) is not None:
        return "invalid_timestamps"
    if name in {"startup", "queue"} and not row.get("api_received_at"):
        return "missing_start"
    return "missing_telemetry"


def turn_timing(value: dict[str, Any], turn: Any) -> dict[str, Any]:
    row = {
        **value, "_status": turn.status,
        "_parent_turn": getattr(turn, "parent_turn", None),
        "_has_preparation": getattr(turn, "has_preparation", None),
        "_control": getattr(turn, "workflow_id", "").startswith("moyai-slack-control-"),
    }
    return {**value, **{f"{name}_timing_status": reason(row, name) for name in CARD_FIELDS}}


def cards(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for name, (field, end) in CARD_FIELDS.items():
        reasons = Counter(row.get(f"{name}_timing_status") or reason(row, name) for row in rows)
        observed = [
            row[field] for row in rows
            if (row.get(f"{name}_timing_status") or reason(row, name)) == "measured"
        ]
        pending = reasons["waiting"]
        excluded = sum(reasons[key] for key in EXCLUDED_REASONS)
        result[name] = {
            **distribution(observed), "pending": pending,
            "missing": len(rows) - len(observed) - pending - excluded,
            "excluded": excluded, "total": len(rows), "reasons": dict(reasons),
        }
    return result


def summarize(timing: dict[str, dict[str, Any]], turns: list[Any], period: ReportRange) -> dict[str, Any]:
    """Cards and 24 fixed intervals use the full bounded cohort, never one page."""
    rows = [{**turn_timing(timing[turn.id], turn), "_status": turn.status, "_created_at": turn.created_at}
            for turn in turns if turn.id in timing]
    duration = period.end - period.start
    count = min(24, duration // timedelta(microseconds=1))
    boundaries = [period.start + duration * index / count for index in range(count + 1)]
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(count)]
    for row in rows:
        created = utc(row["_created_at"])
        if created is not None and period.contains(created):
            buckets[bisect_right(boundaries, created) - 1].append(row)
    return {
        **metrics([row for row in rows if row["startup_timing_status"] not in EXCLUDED_REASONS]),
        "accounted_turns": len(rows), "cards": cards(rows),
        "trend": [{
            "start": boundaries[index].isoformat(),
            "end": boundaries[index + 1].isoformat(),
            **cards(bucket),
        } for index, bucket in enumerate(buckets)],
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
        "Receipt → first foreground LiteLLM dispatch starts at server middleware entry for HTTP messages "
        "and approval replies, or at native admission entry for Slack, schedules and internal turns. "
        "Each row identifies its start source; admission timing excludes upstream delivery. It ends "
        "at the outbound transport attempt, after broker admission. It includes Moyai "
        "processing and queueing, not provider receipt, provider generation, or browser delivery. "
        "Every turn created in range is accounted for. Steering inputs and Slack control notices "
        "are classified separately, not added to startup averages. Delegated and maintenance model "
        "calls remain outside the first foreground dispatch measurement."
    ),
    "startup_breakdown": (
        "Receipt → worker ends at entry to the first successful turn-preparation activity; it includes "
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
        "Admission and successful preparation evidence commit with their existing transactions. "
        "Dispatch observations are coalesced, batched and retried by one bounded background writer, "
        "never awaited before transport. Process crashes, sustained database failures or buffer exhaustion "
        "can still lose observations. Computer evidence remains bounded best effort. Missing or reversed timestamps "
        "remain unknown, never backfilled from billing timestamps. Cross-process timings require synchronized "
        "server clocks. The first outbound attempt keeps its own broker arrival, including retries "
        "and late settlement after cancellation."
    ),
    "timing_cards": (
        "The four cards measure startup, queue pickup, broker preparation and computer readiness "
        "independently. A queue sample can exist before any model request. Each card classifies every "
        "turn as measured, waiting, excluded or an explicit missing reason. No instrumentation record "
        "includes older turns; ended without observation does not assert that dispatch never occurred. "
        "No acquisition recorded is not a zero-duration computer. Percentiles use nearest rank. "
        "The trend uses up to 24 fixed intervals across the "
        "selected range, grouped by root-turn admission time, from the whole filtered cohort. Empty "
        "intervals are gaps. The four series overlap and must not be added together."
    ),
}
