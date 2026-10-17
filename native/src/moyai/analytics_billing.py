"""Adapt the shared product accounting report to the existing usage response.

Session/turn/PR activity keeps its native lineage. Financial cards, user/model
breakdowns, daily series and their CSVs all consume ProductBillingService.
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from .analytics_identity import ReportIdentities
from .product_billing import BillingReadError, SourceRef
from .product_billing_routes import ProductBillingService, _totals


def identity_map(service: ProductBillingService, identities: ReportIdentities) -> dict[str, str]:
    result = {}
    mappings = identities.accounting_mappings(service.mappings)
    for user in identities.users:
        identity = user["id"]
        source = SourceRef("legacy" if identity in identities.product_ids else "native", identity)
        resolved = mappings.resolve(source, actor=True)
        assert resolved is not None
        result[identity] = resolved.id
    for actor, canonical in identities.canonical_ids.items():
        if canonical in result:
            resolved = mappings.resolve(SourceRef("native", actor), actor=True)
            assert resolved is not None
            result[actor] = resolved.id
    return result


def selected_user(
    service: ProductBillingService, identities: ReportIdentities, user_id: str | None,
) -> str | None:
    if user_id is None:
        return None
    people = identity_map(service, identities)
    if user_id in set(people.values()) | set(service.mappings.actors.values()):
        return user_id
    return people.get(user_id, people.get(identities.canonical(user_id), user_id))


def chart_amount(value: str | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def metrics(row: dict[str, Any]) -> dict[str, Any]:
    """Rename fields only; the accounting service owns sums and coverage."""
    return {
        **{key: row.get(key) for key in (
            "requests", "input_tokens", "output_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
            "priced_requests", "unpriced_requests", "unknown_requests",
            "cost_coverage", "pricing_status", "token_samples", "usage_samples", "token_coverage",
        )},
        "cost_usd": chart_amount(row["spend"]), "cost_usd_exact": row["cost_usd_exact"],
        # Old SDK turn counters are activity evidence, not missing ledger charges.
        "unknown_request_turns": 0, "capture_gap_turns": 0,
        "priced_turns": 0, "unpriced_turns": 0, "legacy_turns": 0,
    }


async def read(
    service: ProductBillingService, identities: ReportIdentities, *, viewer_id: str,
    start: str, end: str, preset: str, user_id: str | None, model: str | None, status: str | None,
) -> dict[str, Any]:
    try:
        return await service.report(
            start=start, end=end, preset=preset,
            user_id=selected_user(service, identities, user_id),
            root_model=model, turn_status=status, include_root_cohorts=True,
            viewer_id=viewer_id, limit=1, identities=identities,
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


def apply(
    billing: dict[str, Any], identities: ReportIdentities, service: ProductBillingService,
    *, summary: dict[str, Any], series: list[dict[str, Any]], by_user: list[dict[str, Any]],
    users: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    people = identity_map(service, identities)
    summary.update(metrics(billing["total"]))
    days = {row["date"]: row for row in billing["daily"]}
    for day in series:
        financial = days[day["date"]]
        day.update(metrics(financial))
        day.update(
            models=[{"model": row["model"] or "Unattributed model", **metrics(row)}
                    for row in financial["models"]],
            cumulative_cost_usd=chart_amount(financial["cumulative_spend"]),
            cumulative_pricing_status=financial["cumulative_pricing_status"],
        )
    empty = metrics(_totals([]))
    # Activity was grouped by these same canonical identities before counting
    # sessions and PRs. Never combine completed aggregates by overwriting rows
    # or summing distinct-session counts after the fact.
    activity = {row["user_id"]: {**row, **empty} for row in by_user}
    labels = {
        people.get(row["id"], row["id"]): {**row, "id": people.get(row["id"], row["id"])}
        for row in users
    }
    known_ids = set(labels) | {row["id"] for row in billing["users"] if row["id"] is not None}
    unknown_key = "billing:unattributed"
    while unknown_key in known_ids:
        unknown_key += ":unknown"
    for row in billing["users"]:
        identity = row["id"]
        # A real unknown actor remains unattributed, rather than borrowing an
        # account. The explicit bucket is a display key, never authorization.
        key = identity if identity is not None else unknown_key
        existing = activity.get(key, {"user_id": key})
        activity[key] = {**existing, **metrics(row)}
        labels[key] = {
            **labels.get(key, {}), "id": key, "username": row["email"] or row["name"],
            "display_name": row["name"], "email": row["email"],
            "filterable": identity is not None,
        }
    models = [{"model": row["model"] or "Unattributed model", **metrics(row)} for row in billing["models"]]
    return list(activity.values()), models, list(labels.values())
