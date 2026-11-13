"""Trace steps retain chronological work without expanding the reader's authority."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, TypeAdapter
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent
from sqlalchemy import delete, select
from test_application_boundaries import sign_in
from test_sidebar import member_client

from moyai.contracts import NewMessage, NewSession
from moyai.debug_trace import debug_trace
from moyai.delegation import DelegationService
from moyai.delegation_models import DelegatedSession
from moyai.models import Event, Message, Session, Turn, User
from moyai.trace_activity import trace_activity

START = datetime(2026, 10, 20, tzinfo=UTC)


def walk(items):
    for item in items:
        yield item
        yield from walk(item.get("children", []))


async def save_run(runtime, run_id, *, parent=None, private=False):
    await runtime.stores.steps.register_run(RunRecord(run_id=run_id, parent_run_id=parent, started_at=START))
    for offset, kind in ((0, "run_started"), (1, "model_request_started"), (3, "tool_call_started"), (5, "tool_call_completed")):
        await runtime.stores.steps.append_event(StepEvent(
            run_id=run_id, kind=kind, step_index=1, timestamp=START + timedelta(seconds=offset),
            tool_name="shell" if kind.startswith("tool") else None,
            tool_call_id="same-id" if kind.startswith("tool") else None,
            error="private-provider-error", metadata={"credential": "private-event-metadata"},
        ))
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=run_id, step_index=1, state="interrupted", messages=[
            ModelResponse(run_id="unrelated", parts=[ToolCallPart("shell", {"command": "inherited-secret"}, tool_call_id="same-id")]),
            ModelRequest(parts=[ToolReturnPart("shell", "inherited-result", tool_call_id="same-id")]),
            ModelResponse(run_id=run_id, timestamp=START + timedelta(seconds=2), parts=[
                ThinkingPart("recorded private reasoning"),
                TextPart("I am checking the directory.", provider_details={"phase": "commentary"}),
                ToolCallPart("credentials_run" if private else "shell", {"command": "pwd synthetic-gateway-secret"}, tool_call_id="same-id"),
                TextPart("private-phase-text", provider_details={"phase": "analysis"}),
                NativeToolCallPart("web_search", {"query": "public query"}, tool_call_id="native-call"),
                NativeToolReturnPart("web_search", "public web result", tool_call_id="native-call"),
            ]),
            ModelRequest(parts=[ToolReturnPart(
                "credentials_run" if private else "shell", f"directory for {run_id}", tool_call_id="same-id",
                timestamp=START + timedelta(seconds=4),
            )]),
        ],
    ))


async def test_trace_step_nested_order_lineage_and_same_call_ids(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Check", request_key="nested"))
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    await save_run(runtime, turn.id)
    await save_run(runtime, "child", parent=turn.id)
    await save_run(runtime, "grandchild", parent="child", private=True)
    await save_run(runtime, "unrelated-root")
    trace = await debug_trace(runtime, actor, session.id)
    step = next(step for step in trace["steps"] if step["source"] == "user")
    activity = step["extra"]["activity"]
    root = activity[0]
    assert root["snapshot_status"] == "interrupted"
    child = next(item for item in root["children"] if item.get("run_id") == "child")
    assert child["parent_run_id"] == turn.id
    grandchild = next(item for item in child["children"] if item.get("run_id") == "grandchild")
    assert grandchild["parent_run_id"] == "child"
    for run in (root, child, grandchild):
        assert [item["timestamp"] for item in run["children"]] == sorted(item["timestamp"] for item in run["children"])
        response = next(item for item in run["children"] if item["kind"] == "model_response")
        assert [item["kind"] for item in response["children"]] == ["reasoning", "commentary", "tool_call", "tool_call", "tool_result"]
        call = response["children"][2]
        result = call["children"][0]
        assert result["content"] == ("[redacted: result of private tool credentials_run is hidden in debug traces]"
                                     if run is grandchild else f"directory for {run['run_id']}")
    serialized = json.dumps(activity)
    for private in ("unrelated-root", "inherited-secret", "inherited-result", "private-phase-text", "private-provider-error", "private-event-metadata", "synthetic-gateway-secret"):
        assert private not in serialized
    assert '"command": "pwd [redacted]"' in serialized


async def test_all_teammate_turns_include_public_work_but_never_reasoning(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    async with member_client(runtime) as (_, teammate):
        for index in range(17):
            turn = await runtime.database.submit(teammate, session.id, NewMessage(prompt=f"Request {index}", request_key=f"sample-{index}"))
            await save_run(runtime, turn.id)
        trace = await debug_trace(runtime, actor, session.id)
    steps = [step for step in trace["steps"] if step["source"] == "user"]
    assert len(steps) == trace["extra"]["private_activity_omitted_turns"] == 17
    for step in steps:
        assert step["extra"]["activity_scope"] == "public"
        items = list(walk(step["extra"]["activity"]))
        assert {"commentary", "tool_call", "tool_result", "model_request_started"} <= {item["kind"] for item in items}
        assert not any(item["kind"] == "reasoning" for item in items)
    assert "recorded private reasoning" not in json.dumps(trace)
    assert "private-phase-text" not in json.dumps(trace)


async def test_missing_snapshot_lifecycle_and_no_input_message_get_an_anchor(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Interrupted", request_key="missing"))
    async with runtime.database.sessions.begin() as db:
        await db.execute(delete(Message).where(Message.turn_id == turn.id))
        saved = await db.get(Turn, turn.id)
        saved.status = "failed"
        db.add(Event(session_id=session.id, turn_id=turn.id, kind="commentary", identity=f"commentary:{turn.id}",
                     content=runtime.security.seal({"id": "saved-commentary", "text": "Durable progress", "created_at": START.isoformat(), "phase": "commentary"})))
    await runtime.stores.steps.append_event(StepEvent(run_id=turn.id, kind="tool_call_started", step_index=2, tool_call_id="unfinished", tool_name="shell", timestamp=START))
    trace = await debug_trace(runtime, actor, session.id)
    activity = next(step["extra"]["activity"] for step in trace["steps"] if "activity" in step.get("extra", {}))
    assert activity[0]["snapshot_status"] == "unavailable"
    assert {item["kind"] for item in walk(activity)} == {"run", "tool_call_started", "commentary"}
    assert not any(item["kind"] == "tool_result" for item in walk(activity))


@pytest.mark.parametrize("viewer", ["requester", "teammate"])
async def test_code_mode_subtools_survive_serialization_and_redact_independently(runtime, actor, viewer):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Nested tools", request_key="code-mode"))
    calls = {"parent__1": ToolCallPart("shell", {"command": "pwd"}, tool_call_id="parent__1"),
             "parent__2": ToolCallPart("credentials_get", {"key": "private-nested-input"}, tool_call_id="parent__2")}
    results = {key: ToolReturnPart(value.tool_name, "public-dir" if key.endswith("1") else "private-nested-output", tool_call_id=key, timestamp=START,
                                  outcome="success" if key.endswith("1") else "denied")
               for key, value in calls.items()}
    metadata = {"code_mode": True,
                "tool_calls": {key: TypeAdapter(ToolCallPart).dump_python(value, mode="json") for key, value in calls.items()},
                "tool_returns": {key: TypeAdapter(ToolReturnPart).dump_python(value, mode="json") for key, value in results.items()},
                "secret": "private-other-metadata"}
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(run_id=turn.id, step_index=1, messages=[
        ModelResponse(run_id=turn.id, timestamp=START, parts=[ToolCallPart("run_code", {"code": "credentials_get(key='private-nested-input')"}, tool_call_id="parent")]),
        ModelRequest(run_id=turn.id, parts=[ToolReturnPart("run_code", "private-nested-output", tool_call_id="parent", metadata=metadata, timestamp=START)]),
    ]))
    async with member_client(runtime) as (_, teammate):
        trace = await debug_trace(runtime, teammate if viewer == "teammate" else actor, session.id)
    activity = next(step["extra"]["activity"] for step in trace["steps"] if step["source"] == "user")
    nested = [item for item in walk(activity) if item["kind"] == "tool_call" and item["tool_call_id"].startswith("parent__")]
    assert [item["tool_name"] for item in nested] == ["shell", "credentials_get"]
    assert nested[0]["children"][0]["content"] == "public-dir"
    assert nested[1]["redacted"] and nested[1]["children"][0]["redacted"]
    assert nested[0]["children"][0]["outcome"] == "success"
    assert nested[1]["children"][0]["outcome"] == "denied"
    assert all(value not in json.dumps(trace) for value in ("private-nested-input", "private-nested-output", "private-other-metadata"))


async def test_recorded_segments_include_earlier_activity_after_compaction(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Continue", request_key="segments"))
    segment = f"{turn.id}:segment:1"
    await save_run(runtime, turn.id)
    await save_run(runtime, segment)
    async with runtime.database.sessions.begin() as db:
        db.add(Event(session_id=session.id, turn_id=turn.id, kind="history_segment", identity=f"segment:{segment}", content=runtime.security.seal({"run_id": segment})))
    activity = await trace_activity(runtime, turn, include_private=True)
    assert {item["run_id"] for item in activity} == {turn.id, segment}
    assert len([item for item in walk(activity) if item["kind"] == "model_response"]) == 2


async def test_delegation_exports_only_assignment_turns_and_rechecks_deletion(runtime, actor, active, monkeypatch):
    _parent, root, prepared = active
    delegated = await DelegationService(runtime.database).start(prepared.scope, "delegate-call", "Inspect the task.")
    async with runtime.database.sessions() as db:
        initial = await db.scalar(select(Turn).where(Turn.session_id == delegated["session_id"]))
    await save_run(runtime, initial.id)
    # A later private conversation turn shares the child's session but has no
    # assignment receipt. It must never enter the parent's export.
    later = await runtime.database.submit(actor, initial.session_id, NewMessage(prompt="private later prompt", request_key="private-later"))
    later_id = later.id
    await save_run(runtime, later_id)
    activity = await trace_activity(runtime, root, include_private=False)
    delegation = next(item for item in walk(activity) if item["kind"] == "delegation")
    assert delegation["tool_call_id"] == "delegate-call"
    assert initial.id in json.dumps(delegation) and later_id not in json.dumps(delegation)
    assert "recorded private reasoning" not in json.dumps(delegation)
    original = runtime.stores.steps.list_snapshots

    async def delete_during_read(*, run_id, include_interrupted=False):
        snapshot = await original(run_id=run_id, include_interrupted=include_interrupted)
        if run_id == initial.id:
            async with runtime.database.sessions.begin() as db:
                (await db.get(Session, initial.session_id)).deleted = True
        return snapshot

    monkeypatch.setattr(runtime.stores.steps, "list_snapshots", delete_during_read)
    activity = await trace_activity(runtime, root, include_private=False)
    assert not any(item["kind"] == "delegation" for item in walk(activity))


@pytest.mark.parametrize("revoke", ["auth", "session"])
async def test_trace_revocation_during_activity_read_drops_payload(runtime, active, actor, monkeypatch, revoke):
    session, turn, _ = active
    await save_run(runtime, turn.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = trace_activity

    async def delayed(*args, **kwargs):
        result = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr("moyai.debug_trace.trace_activity", delayed)
    async with sign_in(runtime) as client:
        request = asyncio.create_task(client.get(f"/api/sessions/{session.id}/debug-trace"))
        await asyncio.wait_for(entered.wait(), 5)
        async with runtime.database.sessions.begin() as db:
            if revoke == "auth":
                (await db.get(User, actor.id)).auth_version += 1
            else:
                (await db.get(Session, session.id)).deleted = True
        release.set()
        response = await request
    assert response.status_code in {401, 403, 404}
    assert "recorded private reasoning" not in response.text


@pytest.mark.parametrize("provider_response_id", [None, "response-1"])
async def test_retained_snapshots_recover_precompaction_work_without_duplicate_calls(runtime, actor, provider_response_id):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Keep history", request_key="snapshots"))
    response = ModelResponse(run_id=turn.id, timestamp=START, provider_response_id=provider_response_id, parts=[
        ThinkingPart("Earlier reasoning"), ToolCallPart("shell", {"command": "pwd"}, tool_call_id="early-call"),
    ])
    result = ModelRequest(parts=[ToolReturnPart("shell", "earlier-directory", tool_call_id="early-call", timestamp=START + timedelta(seconds=1))])
    final = ModelResponse(run_id=turn.id, timestamp=START + timedelta(seconds=2), parts=[TextPart("Final retained answer")])
    for index, messages in enumerate(([response], [response, result], [response, result, final], [final]), 1):
        await runtime.stores.steps.save_snapshot(ContinuableSnapshot(run_id=turn.id, step_index=index, messages=messages))
    trace = await debug_trace(runtime, actor, session.id)
    activity = next(step["extra"]["activity"] for step in trace["steps"] if step["source"] == "user")
    kinds = [item["kind"] for item in walk(activity)]
    assert activity[0]["retained_snapshots"] == 4
    assert kinds.count("model_response") == 2
    assert kinds.count("tool_call") == kinds.count("tool_result") == 1
    assert "Earlier reasoning" in json.dumps(activity) and "earlier-directory" in json.dumps(activity)


async def test_private_nested_wrappers_stay_redacted_across_snapshot_frontiers(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Private nested", request_key="private-frontiers"))
    inner_call = ToolCallPart("credentials_get", {"key": "private-recursive-input"}, tool_call_id="outer__1__1")
    inner_result = ToolReturnPart("credentials_get", "private-recursive-output", tool_call_id=inner_call.tool_call_id, timestamp=START)
    middle_call = ToolCallPart("run_code", {"code": "credentials_get(key='private-recursive-input')"}, tool_call_id="outer__1")
    middle_result = ToolReturnPart("run_code", "private-recursive-output", tool_call_id=middle_call.tool_call_id, timestamp=START,
        metadata={"code_mode": True, "tool_calls": {inner_call.tool_call_id: inner_call}, "tool_returns": {inner_call.tool_call_id: inner_result}})
    outer = ModelResponse(run_id=turn.id, timestamp=START, parts=[ToolCallPart("run_code", {"code": "run_code(code=private-recursive-input)"}, tool_call_id="outer")])
    result = ModelRequest(run_id=turn.id, parts=[ToolReturnPart("run_code", "private-recursive-output", tool_call_id="outer", timestamp=START,
        metadata={"code_mode": True, "tool_calls": {middle_call.tool_call_id: middle_call}, "tool_returns": {middle_call.tool_call_id: middle_result}})])
    # Later retained frontiers do not make already-known private descendants public.
    for index, messages in enumerate(([outer], [outer, result], [outer]), 1):
        await runtime.stores.steps.save_snapshot(ContinuableSnapshot(run_id=turn.id, step_index=index, messages=messages))
    trace = await debug_trace(runtime, actor, session.id)
    assert "private-recursive-input" not in json.dumps(trace)
    assert "private-recursive-output" not in json.dumps(trace)
    activity = next(step["extra"]["activity"] for step in trace["steps"] if step["source"] == "user")
    calls = [item for item in walk(activity) if item["kind"] == "tool_call"]
    assert len(calls) == 3 and all(call["redacted"] for call in calls)


async def test_child_deleted_while_later_parent_exports_is_pruned_from_entire_trace(runtime, actor, active, monkeypatch):
    session, _root, prepared = active
    delegated = await DelegationService(runtime.database).start(prepared.scope, "child-first", "Inspect the task.")
    async with runtime.database.sessions() as db:
        link = await db.get(DelegatedSession, delegated["session_id"])
    await save_run(runtime, link.initial_turn_id)
    later = await runtime.database.submit(actor, session.id, NewMessage(prompt="Next input", request_key="later-parent"))
    await save_run(runtime, later.id)
    original = runtime.stores.steps.list_snapshots

    async def delete_earlier_child(*, run_id, include_interrupted=False):
        result = await original(run_id=run_id, include_interrupted=include_interrupted)
        if run_id == later.id:
            async with runtime.database.sessions.begin() as db:
                (await db.get(Session, link.session_id)).deleted = True
        return result

    monkeypatch.setattr(runtime.stores.steps, "list_snapshots", delete_earlier_child)
    trace = await debug_trace(runtime, actor, session.id)
    assert link.initial_turn_id not in json.dumps(trace)
    assert not any(item["kind"] == "delegation" for step in trace["steps"] for item in walk(step.get("extra", {}).get("activity", [])))
