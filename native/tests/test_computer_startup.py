"""Real SDK/Temporal execution with a lazy local computer, supplementary to Modal E2E."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic_ai import RunContext
from pydantic_ai.capabilities import LocalWorkspace, Toolset
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef
from sqlalchemy import select, update
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_bundled_skills_startup import scripted_model

from moyai.agent import make_agent
from moyai.computer_startup import STARTUP_VERSION
from moyai.contracts import NewMessage, RunScope
from moyai.models import Effect, User
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


def lazy_local_workspace(directory: Path, allocations: list[str]) -> LocalWorkspace[RunScope]:
    class Backend(LocalWorkspaceBackend):
        def __init__(self, *, attached: bool):
            super().__init__(directory)
            self.attached = attached

        @property
        def ref(self) -> WorkspaceRef | None:
            return super().ref if self.attached else None

        async def working_dir(self) -> str:
            if not self.attached:
                self.attached = True
                allocations.append(str(directory))
            return await super().working_dir()

    class Computer(LocalWorkspace[RunScope]):
        def get_workspace(self, ctx: RunContext[RunScope], *, ref: WorkspaceRef | None):
            expected = WorkspaceRef(provider="local", id=str(directory))
            return Backend(attached=ref is not None) if ref is None or ref == expected else None

    return Computer(directory)


@pytest.mark.parametrize("repository_instructions", [False, True])
async def test_cold_computer_starts_at_tool_use_and_reuses_warm_reference(
    runtime, active, actor, repository_instructions,
):
    session, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"
    instructions = "REPOSITORY_CONTEXT_SEEN: write proof.txt with the content authorized."
    if repository_instructions:
        (directory / "AGENTS.md").write_text(instructions)
    allocations = []
    requests = []

    def respond(messages, info):
        requests.append(messages)
        if "FOLLOWUP_WITHOUT_TOOLS" in str(messages):
            assert allocations == [str(directory)]
            return ModelResponse(parts=[TextPart("The same computer is available.")])
        returns = [
            part for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "shell"
        ]
        if not returns:
            assert not allocations
            # A second computer operation in this batch must also wait for the
            # model to see newly loaded instructions before doing any work.
            return ModelResponse(parts=[
                ToolCallPart("shell", {"command": "printf authorized > proof.txt"}, tool_call_id="first-shell"),
                *([ToolCallPart(
                    "write_file", {"path": "forbidden.txt", "content": "must never be written"},
                    tool_call_id="same-step-write",
                )] if repository_instructions else []),
            ])
        if repository_instructions and len(returns) == 1:
            assert instructions in str(returns[0].content)
            assert not (directory / "proof.txt").exists()
            assert not (directory / "forbidden.txt").exists()
            return ModelResponse(parts=[ToolCallPart(
                "shell", {"command": "printf authorized > proof.txt"}, tool_call_id="informed-shell",
            )])
        assert (directory / "proof.txt").read_text() == "authorized"
        return ModelResponse(parts=[TextPart("Computer work completed.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=lazy_local_workspace(directory, allocations),
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
            assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
            history = await handle.fetch_history()
            saved = await runtime.database.session(actor, session.id)
            assert json.loads(saved.workspace) == {"provider": "local", "id": str(directory)}
            followup = await runtime.database.submit(
                actor, session.id, NewMessage(prompt="FOLLOWUP_WITHOUT_TOOLS", request_key="warm-followup"),
            )
            await runtime.database.claim_next()
            warm = await runtime.client.start_workflow(
                TurnWorkflow.run, followup.id, id=followup.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(warm.result(), 30))["status"] == "completed"
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert allocations == [str(directory)]
    assert len(requests) == (4 if repository_instructions else 3)
    assert not (directory / "forbidden.txt").exists()
    async with runtime.database.sessions() as db:
        effects = list(await db.scalars(select(Effect).where(Effect.session_id == session.id)))
    assert len(effects) == 1 and effects[0].state == "confirmed"


async def test_full_agent_replays_pre_lazy_startup_commands(runtime, active, monkeypatch):
    _, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"
    allocations = []
    model_calls = []

    def respond(messages, info):
        assert allocations == [str(directory)]
        model_calls.append(messages)
        return ModelResponse(parts=[TextPart("Legacy startup completed.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=lazy_local_workspace(directory, allocations), skills_directory="skills",
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    original = workflow.patched
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as previous:
            previous.setattr(workflow, "patched", lambda identity: (
                False if identity == STARTUP_VERSION else original(identity)
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
    assert len(model_calls) == 1


async def test_prefixed_github_checkout_acquires_and_persists_first_workspace(runtime, active, actor):
    session, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"
    allocations = []
    tools = FunctionToolset[RunScope](id="checkout-startup-test")

    @tools.tool(name="github_11111111_checkout", metadata={"annotations": {"readOnlyHint": True}})
    async def checkout(ctx: RunContext[RunScope]) -> str:
        assert ctx.workspace.ref is not None
        await ctx.workspace.write_bytes(str(directory / "checkout-started.txt"), b"workspace acquired")
        return "checkout acquired its workspace"

    def respond(messages, info):
        returned = [part for message in messages for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.tool_name == "github_11111111_checkout"]
        if not returned:
            assert allocations == []
            return ModelResponse(parts=[ToolCallPart("github_11111111_checkout", {}, tool_call_id="first-checkout")])
        assert (directory / "checkout-started.txt").read_bytes() == b"workspace acquired"
        return ModelResponse(parts=[TextPart("Checkout completed.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=lazy_local_workspace(directory, allocations), product_capabilities=[Toolset(tools)],
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
            assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
            history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    saved = await runtime.database.session(actor, session.id)
    assert json.loads(saved.workspace) == {"provider": "local", "id": str(directory)}
    assert allocations == [str(directory)]


@pytest.mark.parametrize("revocation", ["stop", "disable"])
async def test_revoked_turn_cannot_allocate_its_first_computer(runtime, active, actor, revocation):
    session, turn, _ = active
    allocations = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def stream(messages, info):
        entered.set()
        await release.wait()
        yield {0: DeltaToolCall(
            name="shell", json_args=json.dumps({"command": "printf forbidden > forbidden.txt"}),
            tool_call_id="revoked-shell",
        )}

    runtime.models = {"test": FunctionModel(stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=lazy_local_workspace(runtime.settings.data_dir.parent / "computer", allocations),
    )
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            try:
                await asyncio.wait_for(entered.wait(), 10)
                if revocation == "stop":
                    await runtime.database.stop(actor, session.id)
                else:
                    async with runtime.database.sessions.begin() as db:
                        await db.execute(update(User).where(User.id == actor.id).values(enabled=False))
            finally:
                release.set()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 30)
    assert not allocations
    async with runtime.database.sessions() as db:
        assert not list(await db.scalars(select(Effect).where(Effect.session_id == session.id)))


async def test_first_delegation_acquires_only_the_child_computer(runtime, active, actor):
    from test_delegation_temporal import dispatching

    from moyai.delegation_models import DelegatedSession

    session, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"
    allocations = []

    def respond(messages, info):
        prompts = " ".join(
            str(part.content) for message in messages for part in message.parts if isinstance(part, UserPromptPart)
        )
        returned = {
            part.tool_name for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
        }
        if "CHILD_FIRST_COMPUTER" in prompts:
            if "shell" not in returned:
                assert not allocations
                return ModelResponse(parts=[ToolCallPart(
                    "shell", {"command": "printf child > child.txt"}, tool_call_id="child-shell",
                )])
            assert allocations == [str(directory)]
            return ModelResponse(parts=[TextPart("Child wrote its file.")])
        if "delegate_task" not in returned:
            assert not allocations
            return ModelResponse(parts=[ToolCallPart(
                "delegate_task", {"agent_name": "self", "task": "CHILD_FIRST_COMPUTER"},
                tool_call_id="first-delegation",
            )])
        return ModelResponse(parts=[TextPart("Child computer work completed.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=lazy_local_workspace(directory, allocations),
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
            history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert allocations == [str(directory)]
    assert (directory / "child.txt").read_text() == "child"
    saved = await runtime.database.session(actor, session.id)
    assert saved.workspace is None
    async with runtime.database.sessions() as db:
        child = await db.scalar(select(DelegatedSession).where(DelegatedSession.parent_turn_id == turn.id))
    saved_child = await runtime.database.session(actor, child.session_id)
    assert json.loads(saved_child.workspace) == {"provider": "local", "id": str(directory)}
