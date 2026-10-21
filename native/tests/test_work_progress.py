"""Public work presentation checks; actual gateway/UI proof is separate."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent
from test_application_boundaries import sign_in
from test_sidebar import member_client
from test_workspace_overview import native_run

from moyai.commentary import Commentary
from moyai.contracts import NewMessage
from moyai.delegation import DelegationService
from moyai.models import Session, Turn, User
from moyai.work_progress import TOOL_LIMIT, WorkProgress, tools


@pytest.mark.parametrize("tool_type", [None, ToolCallPart, NativeToolCallPart])
@pytest.mark.parametrize("phase", [
    None, "commentary", "final_answer", "analysis", "unknown", pytest.param(["invalid"], id="malformed"),
])
async def test_saved_root_and_child_text_use_the_same_public_phase_rules(runtime, active, tool_type, phase):
    session, turn, _ = active
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    await runtime.stores.steps.register_run(RunRecord(run_id=turn.id, agent_name="moyai"))
    child = await native_run(runtime, turn.id, terminal=None)
    timestamp = datetime(2026, 10, 17, 10, tzinfo=UTC)
    for run_id in (turn.id, child):
        parts = [
            ThinkingPart("private reasoning"),
            TextPart("Checking synthetic-gateway-secret.", provider_details={"phase": phase}),
        ]
        if tool_type:
            parts.append(tool_type("web_search", {"query": "private tool arguments"}, tool_call_id="search"))
        if tool_type is NativeToolCallPart:
            parts.append(NativeToolReturnPart("web_search", "private native result", tool_call_id="search"))
        await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
            run_id=run_id, parent_run_id=turn.id if run_id == child else None, step_index=3,
            messages=[ModelResponse(run_id=run_id, parts=parts, timestamp=timestamp, provider_response_id="response")],
        ))
    expected = (
        ("commentary" if tool_type else "final_answer") if phase is None
        else phase if phase in ("commentary", "final_answer") else None
    )
    path = f"/api/sessions/{session.id}/progress/{turn.id}?include_activity=true"
    async with sign_in(runtime) as client:
        first = await client.get(path)
        assert first.status_code == 200
        value = first.json()
        assert [item["text"] for item in value["messages"]] == (
            ["Checking [redacted]."] if expected == "commentary" else []
        )
        child_view = value["subagents"][0]
        assert child_view["status"] == "running", "Text phase must not change execution status."
        if expected is None:
            assert child_view["messages"] == []
        else:
            assert child_view["messages"] == [{
                "id": f"{child}:response:1", "text": "Checking [redacted].", "phase": expected,
                "created_at": timestamp.isoformat(),
            }]
        assert all(secret not in first.text for secret in (
            "private reasoning", "private tool arguments", "private native result", "synthetic-gateway-secret",
        ))
        assert (await client.get(path)).json() == value, "Cached snapshots retain the same classification."
        runtime.work_progress.clear()
        assert (await client.get(path)).json() == value, "Re-reading saved native history gives the same result."


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
        shared = await member.get(path)
        assert shared.status_code == 200, shared.text
        assert shared.json() == value
        assert (await owner.get(f"/api/sessions/{session.id}/progress/missing?include_activity=true")).status_code == 404


@pytest.mark.parametrize("persisted", [False, True], ids=["legacy-snapshot", "commentary-events"])
async def test_shared_viewers_read_root_commentary_without_private_activity(runtime, active, persisted):
    session, turn, prepared = active
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    await runtime.stores.steps.register_run(RunRecord(run_id=turn.id, agent_name="moyai"))
    response = ModelResponse(run_id=turn.id, parts=[
        ThinkingPart("private reasoning"),
        TextPart("private analysis", provider_details={"phase": "analysis"}),
        TextPart("Checking synthetic-gateway-secret.", provider_details={"phase": "commentary"}),
        ToolCallPart("shell", {"command": "private command"}, tool_call_id="private-call"),
    ])
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=turn.id, step_index=1, messages=[response],
    ))
    if persisted:
        from moyai.commentary import saved_commentary
        await Commentary(runtime.database).save(prepared.scope, 1, saved_commentary([response], turn.id))
    path = f"/api/sessions/{session.id}/progress/{turn.id}"
    async with member_client(runtime) as (client, _):
        assert (await client.get(path)).status_code == 404
        async with runtime.database.sessions.begin() as db:
            (await db.get(Session, session.id)).visibility = "shared"
        for query in ("", "?include_activity=true"):
            result = await client.get(path + query)
            assert result.status_code == 200, result.text
            assert [item["text"] for item in result.json()["messages"]] == ["Checking [redacted]."]
            assert not result.json()["tools"]
            assert not result.json()["subagents"]
            assert all(value not in result.text for value in (
                "synthetic-gateway-secret", "private reasoning", "private analysis", "private command",
            ))
        assert (await client.get(f"/api/sessions/{session.id}/progress/missing")).status_code == 404


@pytest.mark.parametrize("revocation", ["session", "identity"])
async def test_shared_progress_rechecks_access_after_read(runtime, active, monkeypatch, revocation):
    import moyai.progress

    session, turn = await public_parent(runtime, active)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
    original = moyai.progress.turn_progress

    async with member_client(runtime) as (client, member):
        async def revoke_during_read(runtime, turn):
            result = await original(runtime, turn)
            async with runtime.database.sessions.begin() as db:
                if revocation == "session":
                    (await db.get(Session, session.id)).visibility = "private"
                else:
                    (await db.get(User, member.id)).auth_version += 1
            return result

        monkeypatch.setattr(moyai.progress, "turn_progress", revoke_during_read)
        result = await client.get(f"/api/sessions/{session.id}/progress/{turn.id}")
        assert result.status_code == (404 if revocation == "session" else 401)
        assert "Inspecting the parent task." not in result.text


async def test_saved_activity_is_shared_and_uses_live_credential_redaction(runtime, active):
    from test_action_details import saved_action

    session, turn, _ = active
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    await saved_action(runtime, turn.id, output="Shared result synthetic-gateway-secret")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
        (await db.get(Turn, turn.id)).status = "completed"
    async with sign_in(runtime) as owner, member_client(runtime) as (member, _):
        path = f"/api/sessions/{session.id}"
        result = await member.get(path + "/activity")
        assert result.status_code == 200, result.text
        assert result.json() == (await owner.get(path + "/activity")).json()
        assert result.json()[0]["arguments"] == {"command": "ls [redacted]"}
        assert "synthetic-gateway-secret" not in result.text
        inline = await member.get(path + "?include_activity=true")
        assert inline.status_code == 200, inline.text
        assert inline.json()["activity"] == result.json()


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


async def test_interactive_child_progress_excludes_later_private_conversations(runtime, active, actor):
    from test_delegated_sessions import finish

    _, root, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "one", "Inspect the assigned task.")
    initial = await runtime.database.claim_next()
    context = await runtime.database.prepare(initial.id)
    await Commentary(runtime.database).save(context.scope, 1, [{
        "id": "unused", "text": "Inspecting the delegated task.", "created_at": initial.created_at,
    }])
    reader = WorkProgress(runtime)
    value = await reader.read(root)
    assert len(value["subagents"]) == 1
    agent = value["subagents"][0]
    assert agent["session_id"] == child["session_id"] and agent["name"] == child["name"]
    assert agent["status"] == "running"
    assert agent["messages"][-1]["text"] == "Inspecting the delegated task."
    await finish(runtime, context, "The assignment is verified.")
    follow = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Unrelated private follow-up", request_key="later",
    ))
    await runtime.database.claim_next()
    await finish(runtime, await runtime.database.prepare(follow.id), "private-followup-answer")
    value = await reader.read(root)
    assert value["subagents"][0]["messages"][-1]["text"] == "The assignment is verified."
    assert "private-followup" not in json.dumps(value)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, child["session_id"])).deleted = True
    assert (await reader.read(root))["subagents"] == []
