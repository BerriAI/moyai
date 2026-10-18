"""Supplementary SDK/Temporal proof; no live model or Modal calls."""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.agent import PACKAGE_ROOT, Authority, HarnessActivityContext, StartupSkills, make_agent
from moyai.bundled_skills import bundled_skill_catalog
from moyai.context import ContextSource, SessionContext
from moyai.contracts import RunScope
from moyai.settings_runtime import settings_capability
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


class TeamContextSource(ContextSource):
    name: ClassVar[str] = "test"

    async def render(self, scope: RunScope) -> str:
        return await asyncio.to_thread(lambda: "Connected team context is available.")


def scripted_model(respond):
    async def stream(messages, info):
        for index, part in enumerate(respond(messages, info).parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}

    return FunctionModel(respond, stream_function=stream)


@pytest.mark.parametrize("load_skill", [False, True])
async def test_default_agent_reaches_temporal_model_without_computer(runtime, active, monkeypatch, load_skill):
    _, turn, _ = active
    allocations = AsyncMock(side_effect=AssertionError("Text/skill startup must not allocate Modal."))
    attachments = AsyncMock(side_effect=AssertionError("Text/skill startup must not attach to Modal."))
    monkeypatch.setattr("modal.App.lookup.aio", allocations)
    monkeypatch.setattr("modal.Sandbox.create.aio", allocations)
    monkeypatch.setattr("modal.Sandbox.from_id.aio", attachments)
    requests = []

    def respond(messages, info):
        allocations.assert_not_awaited()
        attachments.assert_not_awaited()
        requests.append(messages)
        assert "Connected team context is available." in info.instructions
        loaded = any(
            isinstance(part, ToolReturnPart) and part.tool_name == "load_capability"
            for message in messages for part in message.parts
        )
        if not loaded:
            assert "coding" in info.instructions
            if load_skill:
                loader = next(tool for tool in info.function_tools if tool.name == "load_capability")
                key = next(iter(loader.parameters_json_schema["properties"]))
                return ModelResponse(parts=[ToolCallPart(
                    "load_capability", {key: "coding"}, tool_call_id="load-bundled-coding",
                )])
        else:
            assert any(
                isinstance(part, ToolReturnPart)
                and "Do not replace a missing integration with a mock" in str(part.content)
                for message in messages for part in message.parts
            )
        return ModelResponse(parts=[TextPart("Ready without a computer.")])

    models: dict[str, Model[Any]] = {"test": scripted_model(respond)}
    runtime.models = models
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        product_capabilities=[SessionContext(sources=[TeamContextSource()])],
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
            result = await asyncio.wait_for(handle.result(), 30)
            history = await handle.fetch_history()
        assert result["status"] == "completed"
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert len(requests) == (2 if load_skill else 1)
    allocations.assert_not_awaited()
    attachments.assert_not_awaited()
    scheduled = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events if event.HasField("activity_task_scheduled_event_attributes")
    ]
    assert not any("__workspace__" in name for name in scheduled)
    assert (await runtime.database.session(await runtime.database.user(turn.actor_id), turn.session_id)).workspace is None


async def test_make_agent_includes_session_context_in_model_request(runtime, active, tmp_path):
    _, turn, prepared = active
    requests: list[str] = []

    def respond(messages, info):
        requests.append(info.instructions)
        return ModelResponse(parts=[TextPart("Used the team context.")])

    models: dict[str, Model[Any]] = {"test": scripted_model(respond)}
    agent, _ = make_agent(
        runtime.settings,
        runtime.database,
        runtime.stores,
        runtime.objects,
        models=models,
        workspace=LocalWorkspace(tmp_path),
        product_capabilities=[SessionContext(sources=[TeamContextSource()])],
    )
    result = await agent.run(prepared.prompt, deps=prepared.scope, run_id=turn.id)

    assert result.output == "Used the team context."
    assert len(requests) == 1
    assert "Connected team context is available." in requests[0]


async def test_bundled_catalog_does_not_replace_skill_files_in_execution_workspace(runtime, active):
    _, turn, prepared = active
    computer = runtime.settings.data_dir.parent / "computer"
    reads = []
    requests = []

    class TrackedBackend(LocalWorkspaceBackend):
        async def read_bytes(self, path: str) -> bytes:
            assert requests, "The execution workspace was read before the first model request."
            reads.append(path)
            return await super().read_bytes(path)

    class TrackedWorkspace(LocalWorkspace[RunScope]):
        def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None):
            backend = TrackedBackend(self.working_dir)
            return backend if ref is None or ref == backend.ref else None

    def respond(messages, info):
        requests.append(messages)
        results = {
            part.tool_name: part for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart)
        }
        if "load_capability" not in results:
            assert not reads
            loader = next(tool for tool in info.function_tools if tool.name == "load_capability")
            key = next(iter(loader.parameters_json_schema["properties"]))
            return ModelResponse(parts=[ToolCallPart("load_capability", {key: "coding"})])
        if "read_file" not in results:
            assert not reads
            assert "Use the native file and shell tools" in str(results["load_capability"].content)
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "skills/coding/SKILL.md"})])
        assert "Use the native file and shell tools" in str(results["read_file"].content)
        return ModelResponse(parts=[TextPart("Bundled instructions and computer files agree.")])

    models: dict[str, Model[Any]] = {"test": scripted_model(respond)}
    agent, _ = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=models, workspace=TrackedWorkspace(computer),
    )
    result = await agent.run(prepared.prompt, deps=prepared.scope, run_id=turn.id)
    assert result.output == "Bundled instructions and computer files agree."
    assert reads == [str(computer / "skills" / "coding" / "SKILL.md")]
    assert (computer / "skills" / "coding" / "SKILL.md").read_bytes() == (
        PACKAGE_ROOT / "skills" / "coding" / "SKILL.md"
    ).read_bytes()


@pytest.mark.parametrize("old_history", [False, True])
async def test_bundled_catalog_version_preserves_old_workspace_reads(tmp_path, monkeypatch, old_history):
    source = tmp_path / "skills" / "example"
    source.mkdir(parents=True)
    skill = source / "SKILL.md"
    skill.write_text("---\nname: example\ndescription: Packaged description\n---\nPackaged body.\n")
    catalog = bundled_skill_catalog(source.parent, str(source.parent))
    skill.write_text("---\nname: example\ndescription: Computer description\n---\nComputer body.\n")
    marker = []
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)

    def patched(identity):
        marker.append(identity)
        return not old_history

    monkeypatch.setattr(workflow, "patched", patched)

    def respond(messages, info):
        expected = "Computer description" if old_history else "Packaged description"
        assert expected in info.instructions
        return ModelResponse(parts=[TextPart("Selected the compatible catalog.")])

    agent = Agent(
        FunctionModel(respond),
        deps_type=RunScope,
        capabilities=[
            LocalWorkspace(tmp_path),
            StartupSkills(str(source.parent), catalog=catalog),
        ],
    )
    result = await agent.run("Inspect the skill directory.", deps=RunScope(
        session_id="session", turn_id="turn", actor_id="actor", model="test",
    ))
    assert result.output == "Selected the compatible catalog."
    assert marker == ["moyai-bundled-skill-catalog-v1"]


@pytest.mark.parametrize("old_history", [False, True])
async def test_catalog_patch_replays_real_temporal_workspace_history(runtime, active, monkeypatch, old_history):
    _, turn, _ = active
    computer = runtime.settings.data_dir.parent / "computer"
    skills = StartupSkills(
        "skills", catalog=bundled_skill_catalog(computer / "skills", str(computer / "skills")),
    )
    runtime.durability = TemporalDurability(
        models=runtime.models, run_context_type=HarnessActivityContext,
    )
    runtime.agent = Agent(
        runtime.models["test"], name="moyai", deps_type=RunScope,
        capabilities=[
            LocalWorkspace(computer), skills, Authority(runtime.database),
            settings_capability(runtime.database), runtime.durability,
        ],
    )
    original_patched = workflow.patched
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as prior:
            if old_history:
                prior.setattr(workflow, "patched", lambda identity: (
                    False if identity == "moyai-bundled-skill-catalog-v1" else original_patched(identity)
                ))
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
                history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    calls = [
        event for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name.endswith("__workspace__call")
    ]
    assert len(calls) > 1 if old_history else len(calls) == 1
