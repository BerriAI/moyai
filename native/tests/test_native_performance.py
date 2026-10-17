"""Timing contracts; real-app/real-gateway proof is documented separately."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from sqlalchemy import select
from starlette.responses import JSONResponse
from temporalio.worker import ExecuteActivityInput
from test_native_settings import client_for, member

from moyai.analytics import report_range
from moyai.contracts import NewMessage, NewSession
from moyai.execution import AuthorizedActivity
from moyai.inference_usage import INFERENCE_ACTIVITY, InferenceActivity, InferenceUsageTransport
from moyai.models import Event, Turn
from moyai.performance import PREFIX, computer_timing, evidence, record
from moyai.performance_report import cards, metrics, observations, summarize, timing_view


def stamp(seconds: int) -> str:
    return f"2026-10-16T10:00:{seconds:02d}+00:00"


async def drain(runtime):
    if runtime.database.performance_tasks:
        await asyncio.gather(*runtime.database.performance_tasks)


async def saved(runtime, turn_id, kind):
    async with runtime.database.sessions() as db:
        return await db.scalar(select(Event).where(Event.identity == f"{PREFIX}{kind}:{turn_id}"))


async def test_api_receipt_belongs_to_original_admission_and_ignores_client_clock(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    body = {"prompt": "Private message content", "request_key": "same-message"}
    async with client_for(runtime, actor) as client:
        first = await client.post(f"/api/sessions/{session.id}/messages", json=body, headers={
            "x-moyai-received-at": "1900-01-01T00:00:00Z",
        })
        assert first.status_code == 202
        receipt = await saved(runtime, first.json()["id"], "received")
        assert receipt is not None and receipt.created_at <= first.json()["created_at"]
        assert json.loads(receipt.content) == {"source": "http", "surface": "web", "coverage_version": 2}
        assert not receipt.created_at.startswith("1900")
        second = await client.post(f"/api/sessions/{session.id}/messages", json=body)
        assert second.json()["id"] == first.json()["id"]
        assert (await saved(runtime, receipt.turn_id, "received")).created_at == receipt.created_at
        rejected = await client.post(f"/api/sessions/{session.id}/messages", json={**body, "prompt": "Conflict"})
        assert rejected.status_code == 409
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(Event).where(Event.kind == PREFIX + "received")))) == 1


async def test_broker_ingress_is_captured_before_ownership_database_wait(monkeypatch):
    from moyai.product_lifecycle import ProductRoleMiddleware

    clock = {"at": stamp(0)}
    monkeypatch.setattr("moyai.product_lifecycle.now", lambda: clock["at"])

    async def gate(check):
        clock["at"] = stamp(3)
        check()

    monkeypatch.setattr("app.broker_ownership.database", gate)

    async def downstream(scope, receive, send):
        await JSONResponse({"received": scope["state"]["moyai_received_at"]})(scope, receive, send)

    owner = SimpleNamespace(draining=False, tasks=set(), check=lambda: None)
    app = ProductRoleMiddleware(downstream, SimpleNamespace(role="broker", healthy=True, broker=owner))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://broker") as client:
        response = await client.post("/broker/native/v1/responses", headers={"x-moyai-received-at": stamp(9)})
    assert response.json()["received"] == stamp(0) and clock["at"] == stamp(3)
    assert not owner.tasks


async def test_native_api_preserves_the_outer_role_ingress_timestamp(runtime, actor, monkeypatch):
    from moyai.api import COOKIE, create_app
    from moyai.product_lifecycle import ProductRoleMiddleware

    monkeypatch.setattr("moyai.product_lifecycle.now", lambda: stamp(0))
    session = await runtime.database.create_session(actor, NewSession())
    app = create_app(runtime, run_dispatcher=False)
    app.add_middleware(ProductRoleMiddleware, lifecycle=SimpleNamespace(role="api", healthy=True))
    token, csrf = runtime.security.issue_session(actor.id, actor.auth_version)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://api",
        cookies={COOKIE: token}, headers={"x-moyai-csrf": csrf},
    ) as client:
        response = await client.post(f"/api/sessions/{session.id}/messages", json={
            "prompt": "Measure the full middleware path", "request_key": "outer-receipt",
        })
    assert response.status_code == 202
    assert (await saved(runtime, response.json()["id"], "received")).created_at == stamp(0)


async def test_first_dispatch_keeps_its_broker_pair_when_settlement_arrives_out_of_order(runtime, active):
    session, turn, _ = active
    for outbound, broker, attempt in [(9, 8, "later"), (4, 3, "first"), (7, 6, "retry")]:
        await record(runtime.database, evidence(
            session.id, turn.id, "dispatch", stamp(outbound),
            broker_received_at=stamp(broker), attempt_id=attempt, route="broker",
        ))
    row = await saved(runtime, turn.id, "dispatch")
    assert row.created_at == stamp(4)
    assert json.loads(row.content) == {
        "broker_received_at": stamp(3), "attempt_id": "first", "route": "broker",
    }


def test_breakdown_means_share_a_cohort_and_missing_or_reversed_times_stay_unknown():
    full = timing_view({
        "received": {"at": stamp(0)}, "worker": {"at": stamp(1)},
        "dispatch": {"at": stamp(4), "broker_received_at": stamp(3)},
    })
    partial = timing_view({"received": {"at": stamp(0)}, "dispatch": {"at": stamp(10)}})
    skew = timing_view({
        "received": {"at": stamp(4)}, "worker": {"at": stamp(1)},
        "dispatch": {"at": stamp(3), "broker_received_at": stamp(2)},
    })
    result = metrics([full, partial, skew, timing_view({})])
    assert result["dispatch"] == {
        "samples": 2, "mean_ms": 7000, "p50_ms": 4000, "p95_ms": 10000, "max_ms": 10000,
    }
    assert result["missing_dispatch_samples"] == 2
    breakdown = result["breakdown"]
    assert breakdown["samples"] == 1
    assert sum(breakdown[key]["mean_ms"] for key in (
        "api_to_worker_ms", "worker_to_broker_ms", "broker_to_dispatch_ms",
    )) == breakdown["api_to_dispatch_ms"]["mean_ms"] == 4000
    assert skew["api_to_dispatch_ms"] is None
    assert all(partial[key] is None for key in ("api_to_worker_ms", "worker_to_broker_ms", "broker_to_dispatch_ms"))
    assert result["computer"]["cold"]["mean_ms"] is None


async def test_report_is_admin_only_root_scoped_bounded_and_csv_keeps_evidence(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Secret", request_key="one"))
    async with runtime.database.sessions.begin() as db:
        root = await db.get(Turn, turn.id)
        root.created_at = stamp(0)
        db.add(Turn(
            id="steering", session_id=session.id, actor_id=actor.id, model="test",
            request_key="steer", request_fingerprint="f", prompt="encrypted", workflow_id="steering",
            parent_turn=turn.id, created_at=stamp(0),
        ))
    for identity in (turn.id, "steering"):
        for kind, second, details in [
            ("received", 0, {}), ("worker", 1, {}),
            ("dispatch", 4, {"broker_received_at": stamp(3), "attempt_id": "opaque"}),
        ]:
            await record(runtime.database, evidence(session.id, identity, kind, stamp(second), **details))
    period = report_range("custom", "2026-10-16", "2026-10-17")
    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn)))
        values = await observations(db, turns, period)
        assert set(values) == {turn.id, "steering"}
        assert values["steering"]["startup_timing_status"] == "steering_input"
        assert await observations(db, turns, report_range("custom", "2026-10-17", "2026-10-18")) == {}
    async with client_for(runtime, actor) as client:
        query = "?preset=custom&start=2026-10-16&end=2026-10-17"
        response = await client.get("/api/settings/performance" + query)
        assert response.status_code == 200
        report = response.json()
        assert report["performance"]["dispatch"]["mean_ms"] == 4000
        assert report["performance"]["eligible_turns"] == 1
        assert report["performance"]["accounted_turns"] == 2
        assert report["performance"]["cards"]["startup"]["excluded"] == 1
        assert report["performance"]["cards"]["queue"]["mean_ms"] == 1000
        assert report["performance"]["cards"]["startup"]["max_ms"] == 4000
        assert len(report["performance"]["trend"]) == 24
        assert "Secret" not in response.text and "encrypted" not in response.text
        filtered = await client.get("/api/settings/performance" + query + "&model=absent")
        assert filtered.json()["performance"]["eligible_turns"] == 0
        csv = await client.get("/api/settings/performance" + query + "&format=csv")
        assert "api_to_dispatch_ms" in csv.text and "dispatch_attempt_id" in csv.text
    async with client_for(runtime, await member(runtime)) as client:
        assert (await client.get("/api/settings/performance")).status_code == 403


@pytest.mark.parametrize(("foreground", "surface", "path", "expected"), [
    (True, "web", "/v1/responses", True),
    (False, "web", "/v1/responses", False),
    (True, "memory_review", "/v1/responses", False),
    (True, "web", "/v1/responses/compact", False),
])
async def test_transport_observes_only_foreground_dispatch_after_admission(
    runtime, active, foreground, surface, path, expected,
):
    _, turn, prepared = active
    prepared.scope.surface = surface
    entered, release = asyncio.Event(), asyncio.Event()

    async def wire(request):
        entered.set()
        await release.wait()
        return httpx2.Response(200, json={"output": [], "usage": {}})

    transport = InferenceUsageTransport(httpx2.MockTransport(wire))
    context = InferenceActivity(runtime.database, prepared.scope, "attempt", foreground=foreground)
    token = INFERENCE_ACTIVITY.set(context)
    try:
        async with httpx2.AsyncClient(transport=transport) as client:
            task = asyncio.create_task(client.post("https://gateway.invalid" + path, json={"model": "test"}))
            await asyncio.wait_for(entered.wait(), 3)
            await drain(runtime)
            row = await saved(runtime, turn.id, "dispatch")
            assert (row is not None) is expected  # Captured while the upstream is still waiting.
            release.set()
            assert (await task).status_code == 200
    finally:
        release.set()
        INFERENCE_ACTIVITY.reset(token)


async def test_cancelled_attempt_retains_dispatch_and_telemetry_failure_does_not_retry(runtime, active, monkeypatch):
    _, turn, prepared = active
    entered = asyncio.Event()
    attempts = 0

    async def wire(request):
        nonlocal attempts
        attempts += 1
        entered.set()
        await asyncio.Event().wait()

    context = InferenceActivity(runtime.database, prepared.scope, "cancelled", foreground=True)
    token = INFERENCE_ACTIVITY.set(context)
    try:
        async with httpx2.AsyncClient(transport=InferenceUsageTransport(httpx2.MockTransport(wire))) as client:
            task = asyncio.create_task(client.post("https://gateway.invalid/v1/responses", json={"model": "test"}))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await drain(runtime)
        assert (await saved(runtime, turn.id, "dispatch")) is not None and attempts == 1
    finally:
        INFERENCE_ACTIVITY.reset(token)
    # An unavailable timing sink is not a failed model operation.
    class FailedSessions:
        def begin(self):
            raise RuntimeError("Must not escape to provider retry logic")
    database = SimpleNamespace(sessions=FailedSessions(), settings=runtime.settings)
    await record(database, evidence("session", "turn", "worker", stamp(0)))


@pytest.mark.parametrize("mode", ["cold", "restore", "reuse"])
async def test_computer_first_attempt_success_failure_and_retry_coverage(runtime, active, mode):
    _, turn, prepared = active
    async with computer_timing(runtime.database, prepared.scope, mode):
        pass
    first = await saved(runtime, turn.id, "computer")
    assert json.loads(first.content)["mode"] == mode
    with pytest.raises(RuntimeError):
        async with computer_timing(runtime.database, prepared.scope, "cold"):
            raise RuntimeError("Later attempt cannot overwrite the original")
    assert (await saved(runtime, turn.id, "computer")).content == first.content
    other = await runtime.database.submit(
        await runtime.database.user(prepared.scope.actor_id), prepared.scope.session_id,
        NewMessage(prompt="Another turn", request_key="two"),
    )
    prepared.scope.turn_id = other.id
    with pytest.raises(asyncio.CancelledError):
        async with computer_timing(runtime.database, prepared.scope, mode):
            raise asyncio.CancelledError
    failure = await saved(runtime, other.id, "computer")
    view = timing_view({"computer": {"at": failure.created_at, **json.loads(failure.content)}})
    assert view["computer_ready_ms"] is None
    assert metrics([view])["computer"]["failed"] == 1


async def test_activity_binds_root_model_and_measures_real_workspace_ensure_contract(runtime, active, monkeypatch):
    _, turn, prepared = active
    info = SimpleNamespace(
        activity_type="agent__moyai__model_request_stream", workflow_namespace="default",
        workflow_id="workflow", workflow_run_id="run", activity_id="activity", attempt=1,
    )
    monkeypatch.setattr("moyai.execution.activity.info", lambda: info)
    seen = []

    class Next:
        async def execute_activity(self, input):
            seen.append(INFERENCE_ACTIVITY.get())
            return SimpleNamespace(error=None)

    interceptor = AuthorizedActivity(Next(), runtime.database)
    wire = SimpleNamespace(serialized_run_context={"run_id": turn.id}, call=SimpleNamespace(method="ensure"), ref=None)
    request = ExecuteActivityInput(fn=lambda: None, args=[wire, prepared.scope], executor=None, headers={})
    await interceptor.execute_activity(request)
    assert seen[-1].foreground is True and INFERENCE_ACTIVITY.get() is None
    wire.serialized_run_context["run_id"] = "delegate"
    await interceptor.execute_activity(request)
    assert seen[-1].foreground is False
    wire.serialized_run_context["run_id"] = turn.id
    info.activity_type = "agent__moyai__workspace__call"
    await interceptor.execute_activity(request)
    row = await saved(runtime, turn.id, "computer")
    assert json.loads(row.content)["mode"] == "cold"
    assert json.loads(row.content)["status"] == "ready"


async def test_restore_is_not_ready_until_sdk_ensure_and_keeps_original_mode_and_start(runtime, active):
    _, turn, prepared = active
    async with computer_timing(runtime.database, prepared.scope, "reuse", preparation=True) as timing:
        timing.mode = "restore"
    before = await saved(runtime, turn.id, "computer")
    assert json.loads(before.content)["status"] == "prepared"
    assert metrics([timing_view({"computer": {
        "at": before.created_at, **json.loads(before.content),
    }})])["computer"]["pending"] == 1
    async with computer_timing(runtime.database, prepared.scope, "reuse", ensure=True):
        pass
    after = await saved(runtime, turn.id, "computer")
    assert after.created_at == before.created_at
    assert json.loads(after.content)["status"] == "ready"
    assert json.loads(after.content)["mode"] == "restore"


def test_cards_measure_partial_stages_without_changing_the_additive_cohort():
    full = timing_view({
        "received": {"at": stamp(0)}, "worker": {"at": stamp(1)},
        "dispatch": {"at": stamp(4), "broker_received_at": stamp(3)},
    })
    waiting = {**timing_view({
        "received": {"at": stamp(0)}, "worker": {"at": stamp(2)},
        "computer": {"at": stamp(2), "mode": "cold", "status": "pending"},
    }), "_status": "running"}
    historical = {**timing_view({}), "_status": "completed"}
    result = cards([full, waiting, historical])
    assert result["queue"]["samples"] == 2
    assert result["queue"]["mean_ms"] == 1500 and result["queue"]["max_ms"] == 2000
    assert result["startup"]["samples"] == result["broker"]["samples"] == 1
    assert result["startup"]["pending"] == result["startup"]["missing"] == 1
    assert result["computer"]["samples"] == 0
    assert result["computer"]["missing"] == 1  # History has no instrumentation, not a known model-only turn.
    assert result["computer"]["excluded"] == 1
    assert result["computer"]["pending"] == 1  # Historical/model-only turns are not missing computers.
    assert metrics([full, waiting, historical])["breakdown"]["samples"] == 1


def test_cards_keep_zero_measurements_and_do_not_call_reversed_timestamps_waiting():
    zero = timing_view({
        "received": {"at": stamp(0)}, "worker": {"at": stamp(0)},
        "dispatch": {"at": stamp(0), "broker_received_at": stamp(0)},
        "computer": {"at": stamp(0), "finished_at": stamp(0), "mode": "reuse", "status": "ready"},
    })
    invalid = {**timing_view({
        "received": {"at": stamp(4)}, "worker": {"at": stamp(3)},
        "dispatch": {"at": stamp(2), "broker_received_at": stamp(5)},
        "computer": {"at": stamp(4), "finished_at": stamp(1), "mode": "restore", "status": "ready"},
    }), "_status": "running"}
    for metric in cards([zero, invalid]).values():
        assert metric["samples"] == 1 and metric["max_ms"] == metric["mean_ms"] == 0
        assert metric["pending"] == 0 and metric["missing"] == 1


def test_card_trends_use_all_records_and_keep_empty_intervals_as_gaps():
    period = report_range("custom", "2026-10-16", "2026-10-17")
    timing = {str(index): timing_view({
        "received": {"at": stamp(0)}, "worker": {"at": stamp(1)},
        "dispatch": {"at": stamp(4), "broker_received_at": stamp(3)},
    }) for index in range(51)}
    turns = [SimpleNamespace(
        id=str(index), status="completed",
        created_at="2026-10-16T01:00:00+00:00" if index < 50 else "2026-10-16T23:59:59+00:00",
    ) for index in range(51)]
    report = summarize(timing, turns, period)
    assert report["cards"]["startup"]["samples"] == 51  # Not the 50-row UI page.
    assert len(report["trend"]) == 24
    assert report["trend"][0]["startup"]["mean_ms"] is None
    assert report["trend"][1]["startup"]["samples"] == 50
    assert report["trend"][23]["startup"]["samples"] == 1
    assert sum(item["queue"]["samples"] for item in report["trend"]) == 51
    assert all(item["computer"]["mean_ms"] is None for item in report["trend"])
    assert report["trend"][-1]["end"] == period.end.isoformat()


def test_trend_handles_sub_micro_bucket_ranges_without_zero_width_intervals():
    period = report_range("custom", stamp(0), "2026-10-16T10:00:00.000001+00:00")
    turns = [SimpleNamespace(id="one", status="completed", created_at=stamp(0))]
    report = summarize({"one": timing_view({})}, turns, period)
    assert len(report["trend"]) == 1
    assert report["trend"][0]["start"] == period.start.isoformat()
    assert report["trend"][0]["end"] == period.end.isoformat()
    assert report["trend"][0]["startup"]["mean_ms"] is None
