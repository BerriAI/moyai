"""Public work presentation checks; actual gateway/UI proof is separate."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent
from test_application_boundaries import sign_in
from test_sidebar import member_client
from test_workspace_overview import native_run

from moyai.commentary import Commentary
from moyai.models import Session, User
from moyai.work_progress import TOOL_LIMIT, WorkProgress, tools


async def public_parent(runtime, active):
    session, turn, prepared = active
    await Commentary(runtime.database).save(prepared.scope, 1, [{
        "id": "replaced", "text": "Inspecting the parent task.", "phase": "commentary",
        "created_at": "2026-10-17T10:00:00+00:00",
    }])
    await runtime.stores.steps.register_run(RunRecord(run_id=turn.id, agent_name="moyai"))
    return session, turn


async def test_live_work_contains_only_authorized_public_child_updates(runtime, active):
    session, turn = await public_parent(runtime, active)
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    child = await native_run(runtime, turn.id, terminal=None)
    nested = await native_run(runtime, child)
    unrelated = await native_run(runtime, "another-requester-root")
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=child, parent_run_id=turn.id, step_index=3, messages=[
            ModelResponse(run_id=turn.id, parts=[TextPart("private inherited answer")]),
            ModelResponse(run_id=child, parts=[
                ThinkingPart("private reasoning"),
                TextPart("private analysis", provider_details={"phase": "analysis"}),
                TextPart("Checking synthetic-gateway-secret.", provider_details={"phase": "commentary"}),
            ]),
        ],
    ))
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
    path = f"/api/sessions/{session.id}/progress/{turn.id}?include_activity=true"
    async with sign_in(runtime) as owner, member_client(runtime) as (member, _):
        response = await owner.get(path)
        assert response.status_code == 200, response.text
        value = response.json()
        assert [item["text"] for item in value["messages"]] == ["Inspecting the parent task."]
        assert value["messages"][0]["created_at"] == "2026-10-17T10:00:00+00:00"
        children = {item["id"]: item for item in value["subagents"]}
        assert set(children) == {child, nested}
        assert children[child]["status"] == "running"
        assert [item["text"] for item in children[child]["messages"]] == ["Checking [redacted]."]
        assert children[nested]["parent_name"] == children[child]["name"]
        assert children[nested]["status"] == "completed"
        assert children[nested]["messages"][-1]["phase"] == "final_answer"
        assert unrelated not in response.text
        for private in [
            "synthetic-gateway-secret", "private inherited answer", "private reasoning",
            "private analysis", "private-sdk-agent", "private-run-metadata",
            "private-event-metadata", "private-provider-error", "private-tool-credential",
            "private-tool-return", "private-delegation-prompt",
        ]:
            assert private not in response.text
        assert (await member.get(path)).status_code == 403
        assert (await owner.get(f"/api/sessions/{session.id}/progress/missing?include_activity=true")).status_code == 404


async def test_unchanged_child_progress_reuses_only_the_public_projection(runtime, active, monkeypatch):
    _, turn = await public_parent(runtime, active)
    child = await native_run(runtime, turn.id)
    original = runtime.stores.steps.latest_snapshot
    reads = []

    async def snapshot(**kwargs):
        reads.append(kwargs["run_id"])
        return await original(**kwargs)

    monkeypatch.setattr(runtime.stores.steps, "latest_snapshot", snapshot)
    reader = WorkProgress(runtime)
    first = await reader.read(turn)
    second = await reader.read(turn)
    assert first == second and reads == [child]
    cached = next(iter(reader._snapshots.values()))
    assert "private" not in json.dumps(cached.messages)
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=child, parent_run_id=turn.id, step_index=4,
        messages=[ModelResponse(run_id=child, parts=[
            TextPart("A new verified finding.", provider_details={"phase": "commentary"}),
        ])],
    ))
    await runtime.stores.steps.append_event(StepEvent(
        run_id=child, kind="model_request_started", step_index=4,
    ))
    third = await reader.read(turn)
    assert third["subagents"][0]["messages"][-1]["text"] == "A new verified finding."
    assert reads == [child, child]
    reader.clear()
    assert not reader._snapshots


async def test_model_completed_frontier_race_is_not_cached_as_the_new_update(runtime, active):
    _, turn = await public_parent(runtime, active)
    child = await native_run(runtime, turn.id, terminal=None)
    await runtime.stores.steps.append_event(StepEvent(
        run_id=child, kind="model_request_completed", step_index=3,
    ))
    reader = WorkProgress(runtime)
    await reader.read(turn)
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=child, parent_run_id=turn.id, step_index=3,
        messages=[ModelResponse(run_id=child, parts=[
            TextPart("The frontier is now saved.", provider_details={"phase": "commentary"}),
        ])],
    ))
    refreshed = await reader.read(turn)
    assert refreshed["subagents"][0]["messages"][-1]["text"] == "The frontier is now saved."


async def test_permission_revocation_during_progress_read_never_returns_cached_details(runtime, active, actor, monkeypatch):
    session, turn = await public_parent(runtime, active)
    await native_run(runtime, turn.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = WorkProgress.read

    async def delayed(self, turn):
        result = await original(self, turn)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(WorkProgress, "read", delayed)
    async with sign_in(runtime) as client:
        request = asyncio.create_task(client.get(
            f"/api/sessions/{session.id}/progress/{turn.id}?include_activity=true",
        ))
        await asyncio.wait_for(entered.wait(), 5)
        async with runtime.database.sessions.begin() as db:
            user = await db.get(User, actor.id)
            user.auth_version += 1
        release.set()
        response = await request
        assert response.status_code == 401
        assert "Visible delegate answer." not in response.text


def test_tool_lifecycle_is_bounded_and_does_not_invent_success_after_interruption():
    start = datetime(2026, 10, 17, tzinfo=UTC)
    events = [
        StepEvent(
            run_id="root", kind="tool_call_started", step_index=index,
            tool_call_id=f"call-{index}", tool_name="read_file",
            timestamp=start + timedelta(seconds=index),
            metadata={"secret": "private-metadata"},
        ) for index in range(TOOL_LIMIT + 2)
    ]
    events.append(StepEvent(
        run_id="root", kind="tool_call_failed", step_index=TOOL_LIMIT + 1,
        tool_call_id=f"call-{TOOL_LIMIT + 1}", tool_name="read_file",
        error="private-provider-failure",
    ))
    projected, limited = tools(events, terminal=True, secrets=[])
    assert limited and len(projected) == TOOL_LIMIT
    assert projected[-1]["status"] == "failed"
    assert all(item["status"] == "interrupted" for item in projected[:-1])
    assert "private" not in json.dumps(projected)
