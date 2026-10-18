"""Failure-boundary regressions; live model/Modal evidence is recorded separately."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.usage import RunUsage
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace, WorkspaceUnavailableError
from pydantic_ai_harness import Shell
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_bundled_skills_startup import scripted_model

from moyai.agent import make_agent
from moyai.effects import SHELL_RECOVERY_VERSION, EffectReceipts, effect_id
from moyai.models import Effect
from moyai.shell_recovery import ReadyShellToolset
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


async def shell_call(runtime, active, *, versioned=True):
    _, turn, prepared = active
    ctx = RunContext(
        deps=prepared.scope, model=runtime.models["test"], usage=RunUsage(),
        run_id=turn.id, tool_call_id="checked-command", tool_name="shell",
        workspace=Workspace(LocalWorkspaceBackend(runtime.settings.data_dir.parent / "computer")),
    )
    native = Shell(tools=["shell"]).get_toolset()
    tools = ReadyShellToolset(native, runtime.database)
    tool = (await tools.get_tools(ctx))["shell"]
    tool = replace(tool, tool_def=replace(
        tool.tool_def, metadata={SHELL_RECOVERY_VERSION: True} if versioned else None,
    ))
    arguments = {"command": "git status --short"}
    call = ToolCallPart("shell", arguments, tool_call_id="checked-command")
    identity = effect_id(ctx, call)
    await EffectReceipts(runtime.database).reserve(
        prepared.scope, identity, "shell", arguments, shell_readiness=versioned,
    )
    return ctx, tools, tool, identity


async def receipt(runtime, identity):
    async with runtime.database.sessions() as db:
        row = await db.get(Effect, identity)
        return row.state, json.loads(runtime.security.unseal(row.resolution)) if row.resolution else None


async def test_unavailable_before_dispatch_is_known_not_executed(runtime, active, monkeypatch):
    ctx, tools, tool, identity = await shell_call(runtime, active)
    monkeypatch.setattr(ctx.workspace, "working_dir", AsyncMock(side_effect=WorkspaceUnavailableError("expired")))
    dispatch = AsyncMock()
    monkeypatch.setattr(tools.native, "call_tool", dispatch)
    with pytest.raises(ToolFailed, match="command did not run"):
        await tools.call_tool("shell", {}, ctx, tool)
    dispatch.assert_not_awaited()
    assert await receipt(runtime, identity) == ("not_dispatched", {"shell_phase": "not_dispatched"})
    # A new legitimate operation is no longer blocked by this refused command.
    await EffectReceipts(runtime.database).reserve(ctx.deps, "next-action", "shell", {"command": "git log"})


async def test_transient_readiness_retries_only_the_probe(runtime, active, monkeypatch):
    ctx, tools, tool, identity = await shell_call(runtime, active)
    probe = AsyncMock(side_effect=[TimeoutError("temporary"), "/workspace"])
    dispatch = AsyncMock(return_value="done")
    monkeypatch.setattr(ctx.workspace, "working_dir", probe)
    monkeypatch.setattr(tools.native, "call_tool", dispatch)
    assert await tools.call_tool("shell", {}, ctx, tool) == "done"
    assert probe.await_count == 2
    dispatch.assert_awaited_once()
    assert await receipt(runtime, identity) == ("pending", {"shell_phase": "dispatched"})


async def test_repeated_probe_failure_never_dispatches(runtime, active, monkeypatch):
    ctx, tools, tool, identity = await shell_call(runtime, active)
    probe = AsyncMock(side_effect=TimeoutError("temporary"))
    dispatch = AsyncMock()
    monkeypatch.setattr(ctx.workspace, "working_dir", probe)
    monkeypatch.setattr(tools.native, "call_tool", dispatch)
    with pytest.raises(ToolFailed, match="two readiness checks"):
        await tools.call_tool("shell", {}, ctx, tool)
    assert probe.await_count == 2
    dispatch.assert_not_awaited()
    assert (await receipt(runtime, identity))[0] == "not_dispatched"


async def test_same_error_after_dispatch_stays_uncertain_and_cannot_repeat(runtime, active, monkeypatch):
    ctx, tools, tool, identity = await shell_call(runtime, active)
    dispatch = AsyncMock(side_effect=WorkspaceUnavailableError("expired after starting"))
    monkeypatch.setattr(tools.native, "call_tool", dispatch)
    with pytest.raises(WorkspaceUnavailableError):
        await tools.call_tool("shell", {}, ctx, tool)
    assert await receipt(runtime, identity) == ("pending", {"shell_phase": "dispatched"})
    with pytest.raises(ToolFailed, match="already has an execution claim"):
        await tools.call_tool("shell", {}, ctx, tool)
    dispatch.assert_awaited_once()
    with pytest.raises(PermissionError, match="reconciliation"):
        await EffectReceipts(runtime.database).reserve(ctx.deps, "another-id", "shell", {})


@pytest.mark.parametrize("cancel", [False, True])
async def test_duplicate_delivery_cannot_dispatch_or_clear_live_claim(runtime, active, monkeypatch, cancel):
    ctx, tools, tool, identity = await shell_call(runtime, active)
    entered, release = asyncio.Event(), asyncio.Event()

    async def checking():
        entered.set()
        await release.wait()
        return "/workspace"

    monkeypatch.setattr(ctx.workspace, "working_dir", checking)
    dispatch = AsyncMock(return_value="done")
    monkeypatch.setattr(tools.native, "call_tool", dispatch)
    first = asyncio.create_task(tools.call_tool("shell", {}, ctx, tool))
    await entered.wait()
    with pytest.raises(ToolFailed, match="already has an execution claim"):
        await tools.call_tool("shell", {}, ctx, tool)
    _, phase = await receipt(runtime, identity)
    assert phase is not None and phase["shell_phase"] == "checking"
    if cancel:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (await receipt(runtime, identity))[0] == "not_dispatched"
        dispatch.assert_not_awaited()
    else:
        release.set()
        assert await first == "done"
        dispatch.assert_awaited_once()


async def test_legacy_tool_definition_keeps_old_activity_behavior(runtime, active, monkeypatch):
    ctx, tools, tool, identity = await shell_call(runtime, active, versioned=False)
    probe = AsyncMock(side_effect=AssertionError("No new probe in an old activity."))
    dispatch = AsyncMock(return_value="legacy")
    monkeypatch.setattr(ctx.workspace, "working_dir", probe)
    monkeypatch.setattr(tools.native, "call_tool", dispatch)
    assert await tools.call_tool("shell", {}, ctx, tool) == "legacy"
    assert await receipt(runtime, identity) == ("pending", None)
    probe.assert_not_awaited()


async def test_native_temporal_agent_receives_failure_and_completes_without_review(runtime, active, actor):
    session, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"
    expired = False
    model_calls = 0

    class Backend(LocalWorkspaceBackend):
        async def working_dir(self):
            if expired:
                raise WorkspaceUnavailableError("Computer reached its idle timeout.")
            return await super().working_dir()

    class Computer(LocalWorkspace):
        def get_workspace(self, ctx, *, ref):
            return Backend(directory)

    def respond(messages, info):
        nonlocal expired, model_calls
        model_calls += 1
        results = [
            part for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "shell"
        ]
        if not results:
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "printf ready"}, tool_call_id="ready",
            )])
        if len(results) == 1:
            expired = True
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "git status --short"}, tool_call_id="git-check",
            )])
        assert "command did not run" in str(results[-1].content)
        return ModelResponse(parts=[TextPart("The computer expired before the Git check. No action review is needed.")])

    models: dict[str, Model] = {"test": scripted_model(respond)}
    runtime.models = models
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=models, workspace=Computer(directory),
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            history = await handle.fetch_history()
        count = model_calls
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
        assert model_calls == count
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Effect).where(Effect.session_id == session.id).order_by(Effect.created_at)))
        assert [row.state for row in rows] == ["confirmed", "not_dispatched"]
    assert (await runtime.database.session(actor, session.id)).status == "idle"
