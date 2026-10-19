"""Lazy action details expose only the requester's selected tool call and result."""
from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, StepEvent, StepPersistence
from test_application_boundaries import sign_in
from test_sidebar import member_client
from test_workspace_overview import native_run

from moyai.contracts import NewMessage
from moyai.delegation import DelegationService
from moyai.models import Session, User
from moyai.work_progress import WorkProgress


async def saved_action(runtime, run_id, *, name="shell", status="completed", output="README.md\nnative/", call_id="call"):
    await runtime.stores.steps.register_run(RunRecord(run_id=run_id, agent_name="moyai"))
    await runtime.stores.steps.append_event(StepEvent(
        run_id=run_id, kind="tool_call_started", step_index=1, tool_name=name, tool_call_id=call_id,
    ))
    messages = [
        ModelResponse(run_id="unrelated", parts=[ToolCallPart(name, {"private": "inherited input"}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(name, "inherited output", tool_call_id=call_id)]),
        ModelResponse(run_id=run_id, parts=[ThinkingPart("hidden reasoning"), ToolCallPart(name, {"command": "ls synthetic-gateway-secret"}, tool_call_id=call_id)]),
    ]
    if status != "running":
        await runtime.stores.steps.append_event(StepEvent(
            run_id=run_id, kind=f"tool_call_{status}", step_index=2, tool_name=name, tool_call_id=call_id,
            error="private provider metadata",
        ))
        result_type = RetryPromptPart if status == "failed" else ToolReturnPart
        messages.append(ModelRequest(parts=[result_type(tool_name=name, content=output, tool_call_id=call_id)]))
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(run_id=run_id, step_index=2, messages=messages))


@pytest.mark.parametrize("status", ["running", "completed", "failed"])
async def test_actual_input_result_redaction_and_lifecycle(runtime, active, status):
    session, turn, _ = active
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    await saved_action(runtime, turn.id, status=status, output={"text": "synthetic-gateway-secret", "ok": True} if status == "completed" else "Command failed")
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/progress/{turn.id}/actions/call")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == status
    assert json.loads(result["input"]) == {"command": "ls [redacted]"}
    assert result["output"] is None if status == "running" else result["output"]
    if status == "completed":
        assert json.loads(result["output"]) == {"text": "[redacted]", "ok": True}
    assert not any(value in response.text for value in ("hidden reasoning", "inherited", "private provider", "synthetic-gateway-secret"))


async def test_details_read_real_harness_snapshots(runtime, active):
    session, turn, _ = active

    def respond(messages, info):
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("Finished.")])
        return ModelResponse(parts=[ToolCallPart("inspect_file", {"path": "README.md"}, tool_call_id="real-call")])

    agent = Agent(FunctionModel(respond), capabilities=[StepPersistence(store=runtime.stores.steps)])

    @agent.tool_plain
    def inspect_file(path: str) -> str:
        return f"Contents of {path}"

    await agent.run("Inspect the file", run_id=turn.id)
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/progress/{turn.id}/actions/real-call")
    assert response.status_code == 200, response.text
    assert json.loads(response.json()["input"]) == {"path": "README.md"}
    assert response.json()["output"] == "Contents of README.md"


@pytest.mark.parametrize("name", ["load_capability", "read_memory", "credentials_get"])
async def test_private_actions_keep_payloads_private(runtime, active, name):
    session, turn, _ = active
    await saved_action(runtime, turn.id, name=name)
    async with sign_in(runtime) as client:
        result = (await client.get(f"/api/sessions/{session.id}/progress/{turn.id}/actions/call")).json()
    assert result["restricted"] and result["input"] is None and result["output"] is None


async def test_details_require_requester_and_authorized_lineage(runtime, active):
    session, turn, _ = active
    await saved_action(runtime, turn.id)
    child = await native_run(runtime, turn.id, terminal=None)
    # Save a real call under this child without changing its parent registration.
    snapshot = ContinuableSnapshot(run_id=child, step_index=3, messages=[
        ModelResponse(run_id=child, parts=[ToolCallPart("shell", {"command": "pwd"}, tool_call_id="private-tool-call")]),
        ModelRequest(parts=[ToolReturnPart("shell", "/workspace", tool_call_id="private-tool-call")]),
    ])
    await runtime.stores.steps.save_snapshot(snapshot)
    unrelated = await native_run(runtime, "another-root")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
    path = f"/api/sessions/{session.id}/progress/{turn.id}/actions/"
    async with sign_in(runtime) as owner, member_client(runtime) as (member, _):
        assert (await member.get(path + "call")).status_code == 403
        response = await owner.get(path + "private-tool-call", params={"agent_id": child})
        assert response.status_code == 200 and response.json()["output"] == "/workspace"
        assert (await owner.get(path + "private-tool-call", params={"agent_id": unrelated})).status_code == 404
        assert (await owner.get(path + "missing")).status_code == 404
        assert (await owner.get(path.replace(turn.id, "missing") + "call")).status_code == 404


async def test_delegated_details_exclude_later_chat_and_deleted_session(runtime, active, actor):
    from test_delegated_sessions import finish

    session, root, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "one", "Inspect the task.")
    initial = await runtime.database.claim_next()
    context = await runtime.database.prepare(initial.id)
    await saved_action(runtime, initial.id)
    path = f"/api/sessions/{session.id}/progress/{root.id}/actions/"
    async with sign_in(runtime) as client:
        response = await client.get(path + f"{initial.id}:call", params={"agent_id": initial.id})
        assert response.status_code == 200, response.text
        await finish(runtime, context, "Done.")
        follow = await runtime.database.submit(actor, child["session_id"], NewMessage(prompt="Private followup", request_key="later"))
        await runtime.database.claim_next()
        await saved_action(runtime, follow.id)
        assert (await client.get(path + f"{follow.id}:call", params={"agent_id": initial.id})).status_code == 404
        async with runtime.database.sessions.begin() as db:
            (await db.get(Session, child["session_id"])).deleted = True
        assert (await client.get(path + f"{initial.id}:call", params={"agent_id": initial.id})).status_code == 404


@pytest.mark.parametrize("revoke", ["auth", "session"])
async def test_revocation_during_detail_read_drops_payload(runtime, active, actor, monkeypatch, revoke):
    session, turn, _ = active
    await saved_action(runtime, turn.id)
    entered, release = asyncio.Event(), asyncio.Event()
    original = WorkProgress.action

    async def delayed(self, *args):
        result = await original(self, *args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(WorkProgress, "action", delayed)
    async with sign_in(runtime) as client:
        request = asyncio.create_task(client.get(f"/api/sessions/{session.id}/progress/{turn.id}/actions/call"))
        await asyncio.wait_for(entered.wait(), 5)
        async with runtime.database.sessions.begin() as db:
            if revoke == "auth":
                (await db.get(User, actor.id)).auth_version += 1
            else:
                (await db.get(Session, session.id)).deleted = True
        release.set()
        response = await request
    assert response.status_code in {401, 403, 404}
    assert "README.md" not in response.text


async def test_history_segments_keep_actions_and_missing_dispatch_failures(runtime, active):
    from dataclasses import replace

    from moyai.history_handoff import record_segment

    session, turn, prepared = active
    scope = replace(prepared.scope, history_segment=2)
    await record_segment(runtime, scope)
    await saved_action(runtime, turn.id, call_id="first")
    await saved_action(runtime, scope.root_run_id, call_id="last")
    await runtime.stores.steps.append_event(StepEvent(
        run_id=scope.root_run_id, kind="model_request_completed", step_index=3,
    ))
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=scope.root_run_id, step_index=4,
        messages=[
            ModelResponse(run_id=scope.root_run_id, parts=[ToolCallPart("browser_open", {"url": "https://example.test"}, tool_call_id="refused")]),
            ModelRequest(parts=[ToolReturnPart("browser_open", "Guidance changed", tool_call_id="refused", outcome="failed")]),
        ],
    ))
    async with sign_in(runtime) as client:
        base = f"/api/sessions/{session.id}/progress/{turn.id}"
        progress = (await client.get(base + "?include_activity=true")).json()
        assert {tool["id"] for tool in progress["tools"]} == {"first", "last", "refused"}
        for identity in ["first", "last", "refused"]:
            result = await client.get(base + "/actions/" + identity)
            assert result.status_code == 200, result.text
        assert result.json()["status"] == "failed"
        assert result.json()["output"] == "Guidance changed"
