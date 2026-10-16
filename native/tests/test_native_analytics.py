"""Pure report semantics and native receipt/privacy tests; not provider E2E."""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic_ai import Agent, Conversation, ConversationTypeAdapter
from pydantic_ai.capabilities import LocalWorkspace, Toolset
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import Tool, ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage
from sqlalchemy import func, select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_native_settings import client_for, member

from moyai.agent import HarnessActivityContext
from moyai.analytics import UsageEvent, actual_cost, cost_per_merge, daily_usage, report_range, usage_totals
from moyai.analytics_service import inference_event, report, unique_publications
from moyai.contracts import Grant, NewMessage, NewSession, RunScope
from moyai.effects import EffectReceipts, effect_id
from moyai.inference_usage import InferenceUsage
from moyai.models import Connection, Effect, Session, SessionLink, Turn, User, new_id
from moyai.pr_publications import PRPublication, creation_receipt, verified_status
from moyai.pull_requests import PullRequestService
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

START, END = "2026-10-01T00:00:00+00:00", "2026-10-04T00:00:00+00:00"


def test_proven_not_dispatched_ledger_is_not_an_unpriced_http_request():
    row = InferenceUsage(
        id="attempt", turn_id="turn", session_id="session", actor_id="actor", model="model",
        created_at=START, status="not_dispatched", coverage_error="not_dispatched",
        cost_status="missing", cost_usd=None,
    )
    event = inference_event(row)
    assert event.turn_id == "turn" and event.requests == 0 and event.cost is None
    totals = usage_totals([event]).view()
    assert totals["requests"] == totals["priced_requests"] == totals["unpriced_requests"] == 0
    assert totals["cost_usd"] is None
    for uncertain in ("interrupted", "failed", "pending"):
        row.status = uncertain
        assert inference_event(row).requests == 1


def test_utc_presets_match_legacy_calendar_controls_and_rolling_24_hours():
    clock = datetime(2026, 1, 2, 17, 30, tzinfo=UTC)
    week = report_range("7d", clock=clock)
    assert week.start.isoformat() == "2025-12-27T00:00:00+00:00"
    assert week.end.isoformat() == "2026-01-03T00:00:00+00:00"
    assert len(week.dates()) == 7
    day = report_range("24h", clock=clock)
    assert day.start == clock - timedelta(hours=24) and day.end == clock
    assert not day.contains(day.end) and day.contains(day.start)
    custom = report_range("custom", "2026-10-01T01:00:00+01:00", END)
    assert custom.start.isoformat() == START and len(custom.dates()) == 3
    for start, end in ((END, START), (START, START), ("invalid", END), ("2020-01-01", END)):
        with pytest.raises(ValueError):
            report_range("custom", start, end)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1e9999", True, None, "not money"])
def test_invalid_or_unreported_cost_never_becomes_zero(value):
    assert actual_cost(value, "actual", "response_usage") is None
    assert actual_cost("10", "missing", "response_usage") is None
    assert actual_cost("10", "actual", None) is None


def test_daily_bins_preserve_unknown_cost_and_explicit_partial_cumulative_values():
    period = report_range("custom", START, END)
    events = [
        UsageEvent("a", "t1", "s", "u", "model-a", datetime(2026, 10, 2, tzinfo=UTC), cost=Decimal("0.1")),
        UsageEvent("b", "t2", "s", "u", "model-a", datetime(2026, 10, 2, tzinfo=UTC)),
        UsageEvent("c", "t3", "s", "u", "model-b", datetime(2026, 10, 3, tzinfo=UTC), cost=Decimal(0)),
    ]
    rows = daily_usage(events, period)
    assert rows[0]["requests"] == 0 and rows[0]["cost_usd"] is None
    assert rows[0]["cumulative_cost_usd"] is None
    assert rows[1]["cost_usd"] == .1 and rows[1]["pricing_status"] == "partial"
    assert rows[2]["cost_usd"] == 0 and rows[2]["pricing_status"] == "priced"
    assert rows[2]["cumulative_cost_usd"] == .1 and rows[2]["cumulative_pricing_status"] == "partial"
    assert rows[2]["models"][0]["cost_usd"] is None
    assert cost_per_merge(usage_totals(events), 1, statuses_complete=True) is None
    assert cost_per_merge(usage_totals([events[2]]), 1, statuses_complete=True) == 0
    assert cost_per_merge(usage_totals([events[0]]), 0, statuses_complete=True) is None


def test_missing_token_observations_are_not_reported_as_zero():
    event = UsageEvent("a", "t", "s", "u", "model", datetime(2026, 10, 2, tzinfo=UTC))
    assert usage_totals([event]).view()["total_tokens"] is None
    assert usage_totals([]).view()["total_tokens"] == 0
    known = UsageEvent("b", "t", "s", "u", "model", event.created_at, total_tokens=0)
    assert usage_totals([known]).view()["total_tokens"] == 0
    partial = usage_totals([event, known]).view()
    assert partial["total_tokens"] == 0 and partial["token_coverage"] == .5


def test_receipt_dedup_enriches_identity_without_changing_original_creation_attribution():
    rows = [
        PRPublication(id="first", actor_id="original", created_at=START, url_key="same", number=1),
        PRPublication(id="repo", actor_id="later", created_at=END, url_key="same", number=1, repository_id=55),
        PRPublication(id="rich", actor_id="later", created_at=END, url_key="same", number=1, repository_id=55, github_id=101),
    ]
    result = unique_publications(rows)
    assert len(result) == 1
    assert result[0].id == "first" and result[0].actor_id == "original" and result[0].created_at == START
    assert result[0].repository_id == 55 and result[0].github_id == 101


def test_creation_requires_exact_tool_grant_structured_receipt_and_matching_repository():
    grant = Grant("a1234567-0000-0000-0000-000000000000", "github", 1, "encrypted", read_only=False)
    scope = RunScope("s", "t", "u", "model", [grant])
    tool = "github_a1234567_create_pull_request"
    args = {"owner": "example", "repo": "synthetic"}
    result = {"id": "123", "url": "https://github.com/example/synthetic/pull/12"}
    receipt = creation_receipt(scope, tool, args, json.dumps(result), START)
    assert receipt and receipt.number == 12 and receipt.github_id == 123
    for name, value in (
        ("shell", result),
        ("attach_session_link", result),
        ("github_a1234567_pull_request_read", result),
        ("github_ffffffff_create_pull_request", result),
        (tool, "Created a pull request: https://github.com/example/synthetic/pull/12"),
        (tool, {**result, "isError": True}),
        (tool, {**result, "number": 13}),
        (tool, {**result, "url": "https://github.com/another/repo/pull/12"}),
        (tool, {**result, "url": "https://github.com@evil.invalid/example/synthetic/pull/12"}),
    ):
        assert creation_receipt(scope, name, args, value, START) is None
    assert creation_receipt(RunScope("s", "t", "u", "m"), tool, args, result, START) is None


async def native_publication(runtime, actor, *, number=1, github_id=101, created_at=START):
    connection = Connection(
        id=new_id(), owner_id=actor.id, kind="github", name="Synthetic GitHub",
        credential=runtime.security.seal(f"synthetic-token-{actor.id}"), read_only=False,
    )
    async with runtime.database.sessions.begin() as db:
        db.add(connection)
    session = await runtime.database.create_session(actor, NewSession(title="PRIVATE_TITLE_DO_NOT_EXPORT"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="PRIVATE_PROMPT_DO_NOT_EXPORT", request_key=new_id(), connections=[connection.id],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.database.prepare(turn.id)).scope
    name = f"github_{connection.id[:8]}_create_pull_request"
    args = {"owner": "example", "repo": "synthetic"}
    result = {
        "id": github_id, "number": number, "html_url": f"https://github.com/example/synthetic/pull/{number}",
        "base": {"repo": {"id": 55, "full_name": "example/synthetic"}},
        "created_at": created_at,
    }
    ctx = SimpleNamespace(deps=scope, run_id=turn.id)
    call = ToolCallPart(name, args, tool_call_id=f"create-{number}")
    capability = EffectReceipts(runtime.database)
    tool = ToolDefinition(name=name)
    await capability.reserve(scope, effect_id(ctx, call), name, args)
    await capability.after_tool_execute(ctx, call=call, tool_def=tool, args=args, result=result)
    async with runtime.database.sessions.begin() as db:
        publication = await db.get(PRPublication, effect_id(ctx, call))
        publication.created_at = created_at
        (await db.get(Session, session.id)).created_at = created_at
        (await db.get(Turn, turn.id)).created_at = created_at
    return publication, session, turn, connection, ctx, call, tool, args, result


async def ledger(runtime, actor, session, turn, *, timestamp=START, cost="0.1", model="test"):
    identity = new_id()
    async with runtime.database.sessions.begin() as db:
        row = InferenceUsage(
            id=identity, request_key=identity, session_id=session.id, turn_id=turn.id, actor_id=actor.id,
            model=model, provider_scope="synthetic", created_at=timestamp, finished_at=timestamp,
            input_tokens=20, output_tokens=10, total_tokens=30,
            cost_usd=cost, cost_status="actual" if cost is not None else "missing",
            cost_source="response_usage" if cost is not None else None, status="completed",
        )
        db.add(row)
    return row


async def test_native_confirmed_creation_is_idempotent_and_analytics_failure_does_not_fail_write(runtime, actor, monkeypatch):
    publication, _, _, _, ctx, call, tool, args, result = await native_publication(runtime, actor)
    capability = EffectReceipts(runtime.database)
    assert await capability.after_tool_execute(ctx, call=call, tool_def=tool, args=args, result=result) == result
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == 1
        assert (await db.get(Effect, publication.id)).state == "confirmed"
        assert "https://" not in (await db.get(PRPublication, publication.id)).target
    async def fail_projection(*args):
        raise OSError("synthetic analytics failure")
    monkeypatch.setattr(capability, "record_pr_publication", fail_projection)
    assert await capability.after_tool_execute(ctx, call=call, tool_def=tool, args=args, result=result) == result
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, publication.id)).state == "confirmed"


async def test_report_uses_ledger_without_double_counting_zero_fills_and_exports_full_aggregates(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(title="PRIVATE_TITLE_DO_NOT_EXPORT"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="PRIVATE_PROMPT_DO_NOT_EXPORT", request_key="recorded",
    ))
    historic = await runtime.database.submit(actor, session.id, NewMessage(prompt="Old", request_key="historic"))
    day_two = "2026-10-02T12:00:00+00:00"
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).created_at = "2026-09-30T00:00:00+00:00"
        (await db.get(Turn, turn.id)).created_at = "2026-09-30T00:00:00+00:00"
        recorded = await db.get(Turn, turn.id)
        recorded.usage = json.dumps({"requests": 101, "input_tokens": 9999, "output_tokens": 1000})
        recorded.preparation = runtime.security.seal({"conversation": json.dumps({
            "messages": [], "usage": {"requests": 98, "input_tokens": 9939, "output_tokens": 970},
        })})
        old = await db.get(Turn, historic.id)
        old.created_at, old.usage = day_two, json.dumps({"requests": 2, "input_tokens": 5, "output_tokens": 5})
        old.preparation = runtime.security.seal({"conversation": None})
        db.add(SessionLink(
            session_id=session.id, actor_id=actor.id, kind="pull_request",
            url="https://github.com/example/synthetic/pull/999", title="Mention is not creation",
        ))
    await ledger(runtime, actor, session, turn, timestamp=day_two, cost="0.10")
    await ledger(runtime, actor, session, turn, timestamp="2026-10-03T00:00:00+00:00", cost="0", model="=UNSAFE")
    await ledger(runtime, actor, session, turn, timestamp=END, cost="500")
    params = {"start": START, "end": END, "limit": 1}
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/usage", params=params)
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["total"] == 2 and len(value["rows"]) == 1
        summary = value["summary"]
        assert summary["requests"] == 4 and summary["total_tokens"] == 70
        assert summary["cost_usd"] == .1 and summary["pricing_status"] == "partial"
        assert summary["sessions_created"] == 0 and summary["sessions_active"] == 1
        assert summary["prs_created"] == 0 and summary["prs_merged"] == 0
        assert value["coverage"]["ledger_requests"] == 2 and value["coverage"]["legacy_turns"] == 1
        assert len(value["series"]) == 3 and value["series"][0]["cost_usd"] is None
        assert value["series"][2]["cost_usd"] == 0
        assert value["by_user"][0]["requests"] == 4
        assert "PRIVATE_" not in response.text and "https://github.com" not in response.text
        for export in ("turns", "users", "daily", "models"):
            exported = await client.get("/api/settings/usage", params={**params, "format": "csv", "export": export})
            assert exported.status_code == 200
            parsed = list(csv.DictReader(io.StringIO(exported.text)))
            assert len(parsed) == {"turns": 2, "users": 1, "daily": 3, "models": 2}[export]
            assert "PRIVATE_" not in exported.text
        model_csv = await client.get("/api/settings/usage", params={**params, "format": "csv", "export": "models"})
        assert "'=UNSAFE" in model_csv.text


async def test_receipt_cohorts_merge_dates_private_credentials_and_session_correlated_cost(runtime, actor):
    other = await member(runtime)
    old, old_session, old_turn, _own_connection, *_ = await native_publication(
        runtime, actor, number=1, github_id=101, created_at="2026-09-30T00:00:00+00:00",
    )
    await native_publication(runtime, actor, number=2, github_id=102, created_at="2026-10-03T00:00:00+00:00")
    await native_publication(runtime, other, number=3, github_id=103, created_at="2026-10-02T00:00:00+00:00")
    await ledger(runtime, actor, old_session, old_turn, timestamp="2026-09-30T12:00:00+00:00", cost=".2")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, old_turn.id)).usage = json.dumps({"requests": 1, "input_tokens": 20, "output_tokens": 10})
    headers = []
    def provider(request):
        headers.append(request.headers.get("authorization"))
        if not request.headers.get("authorization"):
            return httpx.Response(404)
        number = int(request.url.path.rsplit("/", 1)[1])
        return httpx.Response(200, json={
            "id": 100 + number, "number": number, "state": "closed" if number == 1 else "open",
            "draft": number == 2, "base": {"repo": {"id": 55}},
            "title": "PRIVATE_PROVIDER_TITLE_DO_NOT_EXPORT", "body": "PRIVATE_PROVIDER_BODY_DO_NOT_EXPORT",
            "created_at": "2026-09-30T00:00:00Z", "merged_at": "2026-10-02T12:00:00Z" if number == 1 else None,
        })
    service = PullRequestService(runtime, transport=httpx.MockTransport(provider))
    value = await report(runtime, actor, START, END, None, None, None, 100, 0, "json", service=service)
    summary = value["summary"]
    assert summary["prs_created"] == 2 and summary["prs_merged"] == 1
    assert summary["pr_status_counts"] == {"open": 0, "draft": 1, "closed": 0, "merged": 0, "unknown": 1}
    assert summary["cost_per_merged_pr_usd"] is None
    mine = next(row for row in value["by_user"] if row["user_id"] == actor.id)
    assert mine["cost_per_merged_pr_usd"] == .2 and mine["prs_merged"] == 1
    assert value["series"][1]["prs_merged"] == 1
    assert value["coverage"]["pr_status_unknown"] == 1
    assert all(f"synthetic-token-{other.id}" not in (header or "") for header in headers)
    serialized = json.dumps(value)
    assert "PRIVATE_" not in serialized and "https://github.com" not in serialized and "synthetic-token" not in serialized
    assert verified_status(old, {"id": 999, "number": 1, "state": "closed", "draft": False}) is None


async def test_revoked_connection_during_status_lookup_cannot_reuse_cached_observation(runtime, actor):
    publication, *_ = await native_publication(runtime, actor)
    entered, release = asyncio.Event(), asyncio.Event()
    async def provider(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json={
            "id": 101, "number": 1, "state": "closed", "draft": False, "base": {"repo": {"id": 55}},
            "created_at": START, "merged_at": "2026-10-02T00:00:00Z",
        })
    service = PullRequestService(runtime, transport=httpx.MockTransport(provider))
    pending = asyncio.create_task(report(runtime, actor, START, END, None, None, None, 100, 0, "json", service=service))
    await asyncio.wait_for(entered.wait(), 5)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Connection, publication.connection_id)).enabled = False
    release.set()
    value = await asyncio.wait_for(pending, 5)
    assert value["summary"]["prs_merged"] == 0
    assert value["summary"]["pr_status_counts"]["unknown"] == 1


async def test_publication_attribution_is_canonical_before_user_model_and_status_filters(runtime, actor):
    other = await member(runtime)
    original, _session, original_turn, *_ = await native_publication(
        runtime, actor, number=1, github_id=101, created_at="2026-09-30T00:00:00+00:00",
    )
    duplicate, _session, duplicate_turn, *_ = await native_publication(
        runtime, other, number=1, github_id=101, created_at="2026-10-02T00:00:00+00:00",
    )
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, original_turn.id)).status = "completed"
        (await db.get(Turn, duplicate_turn.id)).status = "failed"
        (await db.get(Turn, duplicate_turn.id)).model = "other-root"
        (await db.get(PRPublication, duplicate.id)).model = "other-root"
    def provider(request):
        return httpx.Response(200, json={
            "id": 101, "number": 1, "state": "closed", "draft": False, "base": {"repo": {"id": 55}},
            "created_at": "2026-09-30T00:00:00Z", "merged_at": "2026-10-02T00:00:00Z",
        })
    service = PullRequestService(runtime, transport=httpx.MockTransport(provider))
    overall = await report(runtime, actor, START, END, None, None, None, 100, 0, "json", service=service)
    assert overall["summary"]["prs_created"] == 0 and overall["summary"]["prs_merged"] == 1
    for model, status, user_id in ((None, None, other.id), ("other-root", None, None), (None, "failed", None)):
        filtered = await report(runtime, actor, START, END, model, status, user_id, 100, 0, "json", service=service)
        assert filtered["summary"]["prs_created"] == 0 and filtered["summary"]["prs_merged"] == 0
    creator = await report(runtime, actor, START, END, None, None, actor.id, 100, 0, "json", service=service)
    assert creator["summary"]["prs_created"] == 0 and creator["summary"]["prs_merged"] == 1
    assert original.actor_id == actor.id


async def test_reports_are_admin_only(runtime, actor):
    other = await member(runtime)
    with pytest.raises(HTTPException) as denied:
        await report(runtime, other, START, END, None, None, None, 100, 0, "json")
    assert denied.value.status_code == 403


async def test_analytics_filter_preserves_long_canonical_product_actor_ids(runtime, actor):
    canonical = User(
        id="google:" + "x" * 500, username="synthetic-canonical-user",
        display_name="Synthetic Canonical User", password=actor.password, role="member",
    )
    async with runtime.database.sessions.begin() as db:
        db.add(canonical)
    session = await runtime.database.create_session(canonical, NewSession())
    turn = await runtime.database.submit(
        canonical, session.id, NewMessage(prompt="Synthetic", request_key="canonical-filter"),
    )
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).created_at = START
        (await db.get(Turn, turn.id)).created_at = START
    async with client_for(runtime, actor) as client:
        response = await client.get("/api/settings/usage", params={
            "start": START, "end": END, "user_id": canonical.id,
        })
    assert response.status_code == 200
    assert response.json()["by_user"][0]["user_id"] == canonical.id
    assert PRPublication.__table__.c.actor_id.type.length == 512
    assert PRPublication.__table__.c.session_id.type.length == 36


async def test_historical_fallback_subtracts_exact_seed_and_does_not_guess_missing_baseline(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turns = [await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Synthetic baseline test", request_key=f"baseline-{index}",
    )) for index in range(3)]
    current = [
        {"requests": 1, "input_tokens": 10, "output_tokens": 5},
        {"requests": 2, "input_tokens": 25, "output_tokens": 12},
        {"requests": 50, "input_tokens": 500, "output_tokens": 500},
    ]
    async with runtime.database.sessions.begin() as db:
        for index, turn in enumerate(turns):
            row = await db.get(Turn, turn.id)
            row.created_at = "2026-10-02T00:00:00+00:00"
            row.usage = json.dumps(current[index])
            row.preparation = runtime.security.seal({
                "prompt": "PRIVATE_BASELINE_DO_NOT_EXPORT",
                "conversation": None if index == 0 else json.dumps({"usage": current[0]}),
            }) if index < 2 else None
    value = await report(runtime, actor, START, END, None, None, None, 1, 0, "json")
    assert value["summary"]["requests"] == 2
    assert value["summary"]["input_tokens"] == 25 and value["summary"]["output_tokens"] == 12
    assert value["summary"]["total_tokens"] == 37
    assert value["summary"]["cost_usd"] is None
    assert value["coverage"]["unknown_request_turns"] == 1
    assert "PRIVATE_BASELINE_DO_NOT_EXPORT" not in json.dumps(value)


async def test_capture_gap_compares_usage_delta_to_all_attempts_and_never_invents_cost(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Synthetic", request_key="gap"))
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Turn, turn.id)
        row.created_at = "2026-10-02T00:00:00+00:00"
        row.preparation = runtime.security.seal({"conversation": json.dumps({
            "usage": {"requests": 3, "input_tokens": 30, "output_tokens": 20},
        })})
        row.usage = json.dumps({"requests": 5, "input_tokens": 70, "output_tokens": 40})
    await ledger(runtime, actor, session, turn, timestamp="2026-10-02T01:00:00+00:00", cost=".1")
    # A later attempt outside the selected window still belongs to the SDK's
    # saved cumulative total and must not produce a false capture-gap alarm.
    await ledger(runtime, actor, session, turn, timestamp=END, cost=".9")
    complete = await report(runtime, actor, START, END, None, None, None, 100, 0, "json")
    assert complete["summary"]["requests"] == 1 and complete["summary"]["cost_usd"] == .1
    assert complete["coverage"]["capture_gap_turns"] == 0
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, turn.id)).usage = json.dumps({"requests": 6, "input_tokens": 80, "output_tokens": 50})
    incomplete = await report(runtime, actor, START, END, None, None, None, 100, 0, "json")
    assert incomplete["coverage"]["capture_gap_turns"] == 1
    assert incomplete["summary"]["requests"] == 1 and incomplete["summary"]["cost_usd"] == .1
    assert incomplete["summary"]["cost_coverage"] is None
    assert incomplete["summary"]["pricing_status"] == "partial"
    assert incomplete["summary"]["cost_per_merged_pr_usd"] is None
    assert "not proof" in incomplete["methodology"]["coverage"]
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, turn.id)).preparation = runtime.security.seal("invalid preparation")
    unknown = await report(runtime, actor, START, END, None, None, None, 100, 0, "json")
    assert unknown["coverage"]["unknown_request_turns"] == 1
    assert unknown["summary"]["pricing_status"] == "partial"


async def test_model_filter_is_root_cohort_and_steering_inputs_do_not_invent_missing_inference(runtime, actor):
    publication, session, parent, connection, ctx, *_ = await native_publication(runtime, actor)
    await ledger(runtime, actor, session, parent, timestamp=START, cost=".1", model="test")
    await ledger(runtime, actor, session, parent, timestamp=START, cost=".9", model="child")
    child = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Synthetic correction", request_key="included-correction",
        mode="asap", connections=[connection.id],
    ))
    await runtime.database.steering_receipt(child.id, "native-enqueue-receipt")
    usage = {"requests": 2, "input_tokens": 40, "output_tokens": 20}
    assert await runtime.database.save_result(
        ctx.deps, conversation=ConversationTypeAdapter.dump_json(Conversation(usage=RunUsage(**usage))).decode(),
        output="Synthetic result", workspace=None, usage=usage,
    )
    await runtime.database.settle(parent.id)
    async with runtime.database.sessions.begin() as db:
        included = await db.get(Turn, child.id)
        assert included.status == "included" and included.parent_turn == parent.id
        assert included.preparation is None and included.usage == "{}"
        included.created_at = "2026-10-02T00:00:00+00:00"
    def provider(request):
        return httpx.Response(200, json={
            "id": publication.github_id, "number": publication.number, "state": "closed", "draft": False,
            "base": {"repo": {"id": 55}}, "created_at": START, "merged_at": "2026-10-02T01:00:00Z",
        })
    service = PullRequestService(runtime, transport=httpx.MockTransport(provider))
    for model in (None, "test"):
        result = await report(runtime, actor, START, END, model, None, None, 100, 0, "json", service=service)
        assert result["summary"]["turns"] == 2 and result["summary"]["requests"] == 2
        assert result["summary"]["cost_usd"] == 1 and result["summary"]["cost_per_merged_pr_usd"] == 1
        assert result["summary"]["pricing_status"] == "priced" and result["summary"]["cost_coverage"] == 1
        assert result["coverage"]["unknown_request_turns"] == 0
        assert result["coverage"]["capture_gap_turns"] == 0 and result["summary"]["unpriced_turns"] == 0
        assert {row["model"]: row["cost_usd"] for row in result["by_model"]} == {"test": .1, "child": .9}
        included_row = next(row for row in result["rows"] if row["id"] == child.id)
        assert included_row["usage_role"] == "steering_input" and included_row["requests"] == 0
        root_row = next(row for row in result["rows"] if row["id"] == parent.id)
        assert root_row["cost_models"] == ["child", "test"]
    child_only = await report(runtime, actor, START, END, "child", None, None, 100, 0, "json", service=service)
    assert child_only["rows"] == [] and child_only["summary"]["requests"] == 0


@pytest.mark.parametrize("old_history", [False, True])
async def test_confirmed_native_pr_receipt_and_pre_projection_history_replay(runtime, actor, old_history):
    """Real native workflow, controlled GitHub tool; no external publication."""
    connection = Connection(
        id=new_id(), owner_id=actor.id, kind="github", name="Controlled GitHub",
        credential=runtime.security.seal("synthetic-only"), read_only=False,
    )
    async with runtime.database.sessions.begin() as db:
        db.add(connection)
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Create the synthetic PR", request_key="synthetic-native-pr", connections=[connection.id],
    ))
    await runtime.database.claim_next()
    name = f"github_{connection.id[:8]}_create_pull_request"
    calls = 0

    async def create(owner: str, repo: str) -> dict:
        nonlocal calls
        calls += 1
        return {"id": 901, "number": 9, "url": f"https://github.com/{owner}/{repo}/pull/9"}

    def respond(messages, info):
        if any(isinstance(part, ToolReturnPart) and part.tool_name == name for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("Synthetic publication confirmed.")])
        return ModelResponse(parts=[ToolCallPart(name, {"owner": "example", "repo": "synthetic"}, tool_call_id="native-create")])

    async def stream(messages, info):
        for part in respond(messages, info).parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id)}

    class BeforeProjection(EffectReceipts):
        async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
            await self.confirm(effect_id(ctx, call))
            return result

    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}

    def build(legacy):
        runtime.durability = TemporalDurability(models=runtime.models, run_context_type=HarnessActivityContext)
        return Agent(runtime.models["test"], name="moyai", deps_type=RunScope, capabilities=[
            LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
            (BeforeProjection if legacy else EffectReceipts)(runtime.database),
            Toolset(FunctionToolset(
                tools=[Tool(create, name=name, metadata={"annotations": {"readOnlyHint": False}})],
                id="controlled_github",
            )),
            runtime.durability,
        ])

    runtime.agent = build(old_history)
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            execution = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(execution.result(), 30))["status"] == "completed"
            history = await execution.fetch_history()
        runtime.agent = build(False)
        TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    assert calls == 1
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(PRPublication)) == (0 if old_history else 1)
        assert (await db.scalar(select(Effect))).state == "confirmed"
