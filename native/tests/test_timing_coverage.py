"""Coverage identities and transport responsiveness, including a saturated timing sink."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from sqlalchemy import select
from test_native_performance import drain, saved, stamp

from moyai.contracts import NewMessage, NewSession
from moyai.inference_usage import INFERENCE_ACTIVITY, InferenceActivity, InferenceUsageTransport
from moyai.models import Event, Session, Turn
from moyai.performance import PREFIX, evidence, record_dispatch
from moyai.performance_report import cards, timing_view, turn_timing


@pytest.mark.parametrize("surface", ["web", "slack", "automation"])
async def test_every_native_admission_has_a_durable_start_and_retries_keep_it(runtime, actor, surface):
    session = await runtime.database.create_session(actor, NewSession())
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).surface = surface
    body = NewMessage(prompt="Safe test input", request_key="receipt")
    turn = await runtime.database.submit(actor, session.id, body)
    first = await saved(runtime, turn.id, "received")
    assert first.created_at <= turn.created_at
    assert json.loads(first.content) == {"source": "admission", "surface": surface, "coverage_version": 2}
    again = await runtime.database.submit(actor, session.id, body, received_at="2099-01-01T00:00:00Z")
    assert again.id == turn.id
    assert (await saved(runtime, turn.id, "received")).created_at == first.created_at


async def test_preparation_receipt_commits_with_preparation_and_survives_activity_retry(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Safe", request_key="worker"))
    await runtime.database.claim_next()
    await runtime.database.prepare(turn.id, picked_up_at=stamp(1))
    # No asynchronous or second best-effort timing transaction is needed.
    assert not runtime.database.performance_tasks
    assert (await saved(runtime, turn.id, "worker")).created_at == stamp(1)
    await runtime.database.prepare(turn.id, picked_up_at=stamp(9))
    assert (await saved(runtime, turn.id, "worker")).created_at == stamp(1)


async def test_approval_resume_has_its_own_receipt_without_rewriting_original(runtime, active, actor):
    session, original, _ = active
    original_receipt = await saved(runtime, original.id, "received")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, original.id)).status = "waiting"
        (await db.get(Session, session.id)).status = "waiting"
    resumed = await runtime.database.resume(
        actor, session.id, original.id, "answer", {}, received_at=stamp(3),
    )
    receipt = await saved(runtime, resumed.id, "received")
    assert receipt.created_at == stamp(3)
    assert json.loads(receipt.content)["source"] == "http"
    again = await runtime.database.resume(actor, session.id, original.id, "answer", {})
    assert again.id == resumed.id
    assert (await saved(runtime, original.id, "received")).created_at == original_receipt.created_at


async def test_dispatch_batches_a_burst_beyond_old_capacity_and_deduplicates(runtime, active, monkeypatch):
    from moyai import performance
    session, original, _ = active
    async with runtime.database.sessions.begin() as db:
        for i in range(300):
            db.add(Turn(
                id=f"burst-{i}", session_id=session.id, actor_id=original.actor_id,
                request_key=f"burst-{i}", request_fingerprint="safe", prompt="safe",
                model="test", workflow_id=f"burst-{i}",
            ))
    writes = 0
    original_write = performance.write

    async def counted(*args, **kwargs):
        nonlocal writes
        writes += 1
        await original_write(*args, **kwargs)

    monkeypatch.setattr(performance, "write", counted)
    for _ in range(5):
        for i in range(300):
            record_dispatch(runtime.database, evidence(
                session.id, f"burst-{i}", "dispatch", stamp(4), broker_received_at=stamp(3),
            ))
    assert len(runtime.database.performance_tasks) == 1
    await drain(runtime)
    assert writes == 10
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Event).where(Event.kind == PREFIX + "dispatch")))
    assert len(rows) == 300
    for i in range(300):
        record_dispatch(runtime.database, evidence(session.id, f"burst-{i}", "dispatch", stamp(9)))
    await drain(runtime)
    assert writes == 10


async def test_slow_telemetry_does_not_delay_transport_or_block_event_loop(runtime, active, monkeypatch):
    from moyai import performance
    _, turn, prepared = active
    sink_entered, release_sink, outbound = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_write = performance.write

    async def blocked(*args, **kwargs):
        sink_entered.set()
        await release_sink.wait()
        await original_write(*args, **kwargs)

    async def wire(request):
        outbound.set()
        return httpx2.Response(200, json={"output": [], "usage": {}})

    monkeypatch.setattr(performance, "write", blocked)
    token = INFERENCE_ACTIVITY.set(InferenceActivity(
        runtime.database, prepared.scope, "contention", foreground=True,
    ))
    try:
        async with httpx2.AsyncClient(transport=InferenceUsageTransport(httpx2.MockTransport(wire))) as client:
            request = asyncio.create_task(client.post("https://gateway.invalid/v1/responses", json={"model": "test"}))
            await asyncio.wait_for(outbound.wait(), .5)
            await asyncio.wait_for(sink_entered.wait(), .5)
            assert not release_sink.is_set()
            # Another runnable coroutine progresses while persistence is blocked.
            await asyncio.wait_for(asyncio.sleep(0), .1)
            assert (await asyncio.wait_for(request, .5)).status_code == 200
    finally:
        release_sink.set()
        INFERENCE_ACTIVITY.reset(token)
        await drain(runtime)
    assert (await saved(runtime, turn.id, "dispatch")) is not None


async def test_transient_write_failure_retries_only_evidence_and_preserves_earliest_pair(runtime, active, monkeypatch):
    from moyai import performance
    session, turn, _ = active
    write = performance.write
    attempts = 0

    async def intermittent(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError()
        await write(*args, **kwargs)

    monkeypatch.setattr(performance, "write", intermittent)
    for outbound, broker in [(9, 8), (4, 3), (7, 6)]:
        record_dispatch(runtime.database, evidence(
            session.id, turn.id, "dispatch", stamp(outbound), broker_received_at=stamp(broker),
        ))
    await drain(runtime)
    row = await saved(runtime, turn.id, "dispatch")
    assert attempts == 2 and row.created_at == stamp(4)
    assert json.loads(row.content)["broker_received_at"] == stamp(3)


async def test_deleted_turn_does_not_poison_a_dispatch_batch(runtime, active):
    session, turn, _ = active
    for identity in ("deleted-turn", turn.id):
        record_dispatch(runtime.database, evidence(session.id, identity, "dispatch", stamp(4)))
    await drain(runtime)
    assert (await saved(runtime, turn.id, "dispatch")) is not None
    assert f"{PREFIX}dispatch:deleted-turn" not in runtime.database.dispatch_timings.saved


async def test_writer_restarts_even_before_its_done_callback_runs(runtime, active):
    session, turn, _ = active
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    runtime.database.performance_tasks.add(done)
    record_dispatch(runtime.database, evidence(session.id, turn.id, "dispatch", stamp(4)))
    await drain(runtime)
    runtime.database.performance_tasks.discard(done)
    assert (await saved(runtime, turn.id, "dispatch")) is not None


@pytest.mark.parametrize(("observations", "status", "parent", "prepared", "expected"), [
    ({}, "completed", None, False, "not_instrumented"),
    ({}, "included", "parent", False, "steering_input"),
    ({"received": {"at": stamp(0), "source": "control"}}, "completed", None, False, "control"),
    ({"received": {"at": stamp(0)}}, "failed", None, False, "ended_before_preparation"),
    ({"received": {"at": stamp(0)}}, "failed", None, True, "ended_without_observation"),
    ({"received": {"at": stamp(0)}}, "completed", None, True, "missing_telemetry"),
    ({"received": {"at": stamp(0)}}, "running", None, True, "waiting"),
    ({"dispatch": {"at": stamp(4)}}, "completed", None, True, "missing_start"),
    ({"received": {"at": stamp(4)}, "dispatch": {"at": stamp(0)}}, "completed", None, True, "invalid_timestamps"),
    ({"received": {"at": stamp(0)}, "dispatch": {"at": stamp(4)}}, "completed", None, True, "measured"),
])
def test_every_turn_has_an_exclusive_reason_and_card_counts_reconcile(observations, status, parent, prepared, expected):
    row = turn_timing(timing_view(observations), SimpleNamespace(
        status=status, parent_turn=parent, has_preparation=prepared,
    ))
    assert row["startup_timing_status"] == expected
    for metric in cards([row]).values():
        assert metric["total"] == 1
        assert metric["samples"] + metric["pending"] + metric["missing"] + metric["excluded"] == 1
        assert sum(metric["reasons"].values()) == 1
