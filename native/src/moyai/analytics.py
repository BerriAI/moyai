"""Reusable, database-independent report arithmetic.

Calendar presets match app/static/analytics.js. Receipt-cohort versus merge-event
semantics follow app/pr_analytics.py; no legacy engine or database is imported.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens", "cache_write_tokens")


def utc(value: Any, *, require_timezone: bool = False) -> datetime | None:
    if not isinstance(value, (str, datetime)):
        return None
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
        if parsed.tzinfo is None:
            return None if require_timezone else parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class ReportRange:
    start: datetime
    end: datetime
    preset: str

    def contains(self, value: datetime | None) -> bool:
        return value is not None and self.start <= value < self.end

    def dates(self) -> list[str]:
        first, last = self.start.date(), (self.end - timedelta(microseconds=1)).date()
        return [(first + timedelta(days=index)).isoformat() for index in range((last - first).days + 1)]


def report_range(
    preset: str = "7d", start: str | None = None, end: str | None = None, *, clock: datetime | None = None,
) -> ReportRange:
    current = utc(clock or datetime.now(UTC))
    assert current is not None
    if preset not in {"24h", "7d", "30d", "custom"}:
        raise ValueError("Choose 24h, 7d, 30d, or custom.")
    if start is not None or end is not None or preset == "custom":
        lower, upper = utc(start), utc(end)
        selected = "custom"
    elif preset == "24h":
        lower, upper, selected = current - timedelta(hours=24), current, preset
    else:
        upper = datetime.combine(current.date() + timedelta(days=1), datetime.min.time(), UTC)
        lower, selected = upper - timedelta(days=int(preset[:-1])), preset
    if lower is None or upper is None or lower >= upper or upper - lower > timedelta(days=366):
        raise ValueError("Choose start and exclusive end within a range of at most 366 days.")
    return ReportRange(lower, upper, selected)


def integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**63 else None


def actual_cost(value: Any, status: str, source: str | None) -> Decimal | None:
    """Only a recorded, finite provider amount with provenance can be priced."""
    if status != "actual" or not source or not isinstance(value, (str, Decimal)):
        return None
    if len(str(value)) > 100:
        return None
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount.adjusted() > 30:
            return None
        exponent = amount.as_tuple().exponent
        if not isinstance(exponent, int) or exponent < -60:
            return None
        return amount
    except (InvalidOperation, ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class UsageEvent:
    identity: str
    turn_id: str
    session_id: str
    user_id: str
    model: str
    created_at: datetime
    requests: int = 1
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost: Decimal | None = None
    legacy: bool = False
    request_count_known: bool = True


@dataclass
class UsageTotals:
    requests: int = 0
    priced_requests: int = 0
    unpriced_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    known_cost: Decimal = field(default_factory=lambda: Decimal(0))
    legacy_turns: set[str] = field(default_factory=set)
    unknown_request_turns: set[str] = field(default_factory=set)
    capture_gap_turns: set[str] = field(default_factory=set)
    token_samples: int = 0
    usage_samples: int = 0
    token_observations: dict[str, int] = field(default_factory=dict)

    def add(self, item: UsageEvent) -> None:
        self.usage_samples += 1
        requests = integer(item.requests) or 0
        self.requests += requests
        if item.legacy:
            self.legacy_turns.add(item.turn_id)
            if not item.request_count_known:
                self.unknown_request_turns.add(item.turn_id)
        if item.cost is not None:
            self.priced_requests += requests
            with localcontext() as context:
                context.prec = 128
                self.known_cost += item.cost
        else:
            self.unpriced_requests += requests
        for name in TOKEN_FIELDS:
            value = integer(getattr(item, name))
            if value is not None:
                setattr(self, name, getattr(self, name) + value)
                self.token_observations[name] = self.token_observations.get(name, 0) + 1
        self.token_samples = self.token_observations.get("total_tokens", 0)

    @property
    def pricing_status(self) -> str:
        missing = self.unpriced_requests > 0 or bool(
            self.legacy_turns or self.unknown_request_turns or self.capture_gap_turns
        )
        if self.priced_requests:
            return "partial" if missing else "priced"
        return "unpriced" if missing else "empty"

    @property
    def complete(self) -> bool:
        return self.pricing_status == "priced"

    def view(self) -> dict[str, Any]:
        empty = not self.usage_samples and not self.unknown_request_turns and not self.capture_gap_turns
        return {
            "requests": self.requests,
            **{key: getattr(self, key) if self.token_observations.get(key) or empty else None for key in TOKEN_FIELDS},
            "cost_usd": float(self.known_cost) if self.priced_requests else None,
            "cost_usd_exact": str(self.known_cost) if self.priced_requests else None,
            "priced_requests": self.priced_requests, "unpriced_requests": self.unpriced_requests,
            "cost_coverage": self.priced_requests / self.requests
            if self.requests and not self.unknown_request_turns and not self.capture_gap_turns else None,
            "pricing_status": self.pricing_status, "legacy_turns": len(self.legacy_turns),
            "unknown_request_turns": len(self.unknown_request_turns),
            "capture_gap_turns": len(self.capture_gap_turns),
            "token_samples": self.token_samples,
            "usage_samples": self.usage_samples,
            "token_coverage": self.token_samples / self.usage_samples
            if self.usage_samples and not self.capture_gap_turns else None,
        }


def usage_totals(
    events: Iterable[UsageEvent], *, unknown_turns: Iterable[str] = (), capture_gaps: Iterable[str] = (),
) -> UsageTotals:
    totals = UsageTotals()
    totals.unknown_request_turns.update(unknown_turns)
    totals.capture_gap_turns.update(capture_gaps)
    for item in events:
        totals.add(item)
    return totals


def model_totals(events: Iterable[UsageEvent], models: Iterable[str] = ()) -> list[dict[str, Any]]:
    grouped = {model: UsageTotals() for model in models}
    for item in events:
        grouped.setdefault(item.model, UsageTotals()).add(item)
    return [{"model": model, **totals.view()} for model, totals in sorted(grouped.items())]


def daily_usage(events: Iterable[UsageEvent], period: ReportRange) -> list[dict[str, Any]]:
    items = [item for item in events if period.contains(item.created_at)]
    models = sorted({item.model for item in items})
    grouped: dict[str, list[UsageEvent]] = {day: [] for day in period.dates()}
    for item in items:
        grouped[item.created_at.date().isoformat()].append(item)
    cumulative = UsageTotals()
    rows = []
    for day, values in grouped.items():
        for item in values:
            cumulative.add(item)
        rows.append({
            "date": day, **usage_totals(values).view(), "models": model_totals(values, models),
            "cumulative_cost_usd": cumulative.view()["cost_usd"],
            "cumulative_cost_usd_exact": cumulative.view()["cost_usd_exact"],
            "cumulative_pricing_status": cumulative.pricing_status,
        })
    return rows


def cost_per_merge(totals: UsageTotals, merged: int, *, statuses_complete: bool) -> float | None:
    if merged <= 0 or not statuses_complete or not totals.complete:
        return None
    with localcontext() as context:
        context.prec = 128
        return float(totals.known_cost / merged)


def percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def csv_export(rows: Iterable[Mapping[str, Any]], columns: Iterable[str]) -> str:
    """Export exactly the caller-selected full aggregate, retaining null cells."""
    output = io.StringIO(newline="")
    columns = list(columns)
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({
            key: "'" + value if isinstance(value, str) and re.match(r"^[\s\ufeff]*[=+@-]", value) else value
            for key, value in row.items()
        })
    return output.getvalue()
