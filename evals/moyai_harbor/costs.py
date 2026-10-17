"""Export actual inference charges without treating missing receipts as free."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, localcontext
from typing import Any

from moyai.analytics import actual_cost, integer


def recorded_costs(
    rows: Iterable[Mapping[str, Any]], sdk_usage: Mapping[str, Any], *, execution_complete: bool = True,
) -> dict[str, Any]:
    """Summarize the unified ledger once; product ledger rows are duplicates."""
    records = [row for row in rows if row.get("status") != "not_dispatched" or row.get("cost_usd") is not None]
    costs = [actual_cost(row.get("cost_usd"), row.get("cost_status", ""), row.get("cost_source"))
             for row in records]
    priced = [amount for amount in costs if amount is not None]
    sdk_requests = integer(sdk_usage.get("requests"))
    capture_gap = bool(
        not records or not execution_complete
        or any(row.get("coverage_error") for row in records)
        or (sdk_requests is not None and sdk_requests > len(records))
    )
    with localcontext() as context:
        context.prec = 128
        subtotal = sum(priced, Decimal(0))
    status = "unknown" if not priced else "partial" if capture_gap or len(priced) < len(records) else "complete"
    return {
        "cost_usd": float(subtotal) if priced else None,
        "cost_usd_exact": str(subtotal) if priced else None,
        "cost_status": status,
        "priced_requests": len(priced),
        "unpriced_requests": len(records) - len(priced),
        "recorded_requests": len(records),
        "cost_capture_gap": capture_gap,
        "cost_sources": sorted({row["cost_source"] for row, amount in zip(records, costs) if amount is not None}),
    }
