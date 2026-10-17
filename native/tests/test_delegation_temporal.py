"""Real SDK and Temporal: three persistent children, steering, replay and resume."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict

from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import select
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.agent import make_agent
from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.delegation_models import DelegatedSession
from moyai.models import Turn
from moyai.storage import encrypted_converter
from moyai.worker import dispatcher, make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@asynccontextmanager
async def dispatching(runtime):
    task = asyncio.create_task(dispatcher(runtime))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_three_children_steer_and_continue_after_worker_replacement(runtime, actor, monkeypatch):
    from moyai import delegation

    entered, release = asyncio.Event(), asyncio.Event()

    def prompts(messages):
        return "\n".join(str(part.content) for message in messages for part in message.parts
                         if isinstance(part, UserPromptPart))

    def respond(messages, info):
        text = prompts(messages)
        if "CHILD_" in text:
            if "FOLLOW_UP" in text:
                assert "CORRECTED" in text
                return ModelResponse(parts=[TextPart("Remembered the correction in this child.")])
            return ModelResponse(parts=[TextPart("Corrected child result" if "CORRECTED" in text else text[-7:])])
        if not any(isinstance(part, ToolReturnPart) and part.tool_name == "delegate_task"
                   for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart(
                "delegate_task", {"agent_name": "self", "task": f"CHILD_{i}"}, tool_call_id=f"child-{i}",
            ) for i in range(3)])
        assert "Corrected child result" in str(messages)
        return ModelResponse(parts=[TextPart("Three children reported.")])

    async def stream(messages, info):
        if "CHILD_0" in prompts(messages) and not release.is_set():
            entered.set()
            await release.wait()
        for i, part in enumerate(respond(messages, info).parts):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {i: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}

    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
    )
    root = await runtime.database.create_session(actor, NewSession(visibility="private"))
    parent = await runtime.database.submit(actor, root.id, NewMessage(prompt="SPAWN_THREE", request_key="root"))
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        # Record the feature's original marker locations without carrying the
        # decision in deps. Replacing the worker must not move those markers.
        current_enabled = delegation.enabled
        monkeypatch.setattr(delegation, "enabled", lambda scope: (
            not workflow.in_workflow() or workflow.patched(delegation.VERSION)
        ))
        async with make_worker(runtime), dispatching(runtime):
            await asyncio.wait_for(entered.wait(), 45)
            async with runtime.database.sessions() as db:
                children = list(await db.scalars(select(DelegatedSession)))
                assert len(children) == 3
                child = next(item for item in children if item.tool_call_id == "child-0")
                initial = await db.get(Turn, child.initial_turn_id)
            correction = await runtime.database.submit(actor, child.session_id, NewMessage(
                prompt="CORRECTED", request_key="human-correction", mode="asap",
            ))
            assert correction.parent_turn == child.initial_turn_id
            async with asyncio.timeout(20):
                while True:
                    async with runtime.database.sessions() as db:
                        row = await db.get(Turn, correction.id)
                        if row.status == "accepted":
                            break
                    await asyncio.sleep(0.05)
            release.set()
            parent_handle = runtime.client.get_workflow_handle(parent.workflow_id)
            assert (await asyncio.wait_for(parent_handle.result(), 45))["status"] == "completed"
            parent_history = await parent_handle.fetch_history()
            child_history = await runtime.client.get_workflow_handle(initial.workflow_id).fetch_history()
        monkeypatch.setattr(delegation, "enabled", current_enabled)
        async with make_worker(runtime), dispatching(runtime):
            follow = await runtime.database.submit(actor, child.session_id, NewMessage(
                prompt="FOLLOW_UP", request_key="human-followup",
            ))
            async with asyncio.timeout(30):
                while True:
                    async with runtime.database.sessions() as db:
                        saved = await db.get(Turn, follow.id)
                        if saved.status == "completed":
                            assert runtime.security.unseal(saved.result).decode().startswith("Remembered")
                            break
                    await asyncio.sleep(0.1)
        for history in (parent_history, child_history):
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                interceptors=[ActivityCapacity()],
            ).replay_workflow(history)
    assert (await runtime.database.session(actor, root.id)).status == "idle"


async def test_pre_feature_delegation_history_replays_without_creating_sessions(runtime, active, monkeypatch):
    from test_bundled_skills_startup import scripted_model
    from test_computer_startup import lazy_local_workspace

    from moyai import delegation

    session, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"
    allocations = []

    def respond(messages, info):
        prompts = "\n".join(str(part.content) for message in messages for part in message.parts
                            if isinstance(part, UserPromptPart))
        returned = {part.tool_name for message in messages for part in message.parts
                    if isinstance(part, ToolReturnPart)}
        if "LEGACY_CHILD" in prompts:
            # The SDK child must inherit the computer acquired by the parent
            # before delegation and keep it through its own tool activities.
            assert allocations == [str(directory)]
            if "shell" not in returned:
                return ModelResponse(parts=[ToolCallPart(
                    "shell", {"command": "printf legacy > legacy.txt"}, tool_call_id="legacy-shell",
                )])
            return ModelResponse(parts=[TextPart("Legacy child wrote its file.")])
        if "delegate_task" not in returned:
            assert not allocations
            return ModelResponse(parts=[ToolCallPart(
                "delegate_task", {"agent_name": "self", "task": "LEGACY_CHILD"},
                tool_call_id="legacy-delegation",
            )])
        return ModelResponse(parts=[TextPart("Legacy delegation completed.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=lazy_local_workspace(directory, allocations),
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    original = workflow.patched
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as old:
            # Only select the historical workflow branch. Activity hooks must
            # receive that decision through the real SDK serialization boundary.
            old.setattr(workflow, "patched", lambda identity: (
                False if identity == delegation.VERSION else original(identity)
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
    async with runtime.database.sessions() as db:
        assert not list(await db.scalars(select(DelegatedSession)))
        saved = await db.get(type(session), session.id)
        assert json.loads(saved.workspace) == {"provider": "local", "id": str(directory)}
    assert allocations == [str(directory)]
    assert (directory / "legacy.txt").read_text() == "legacy"
    assert len(await runtime.stores.steps.list_runs(parent_run_id=turn.id)) == 1


async def test_in_flight_activity_payload_keeps_legacy_computer_setup(runtime, active):
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.workspaces import Workspace
    from pydantic_ai_harness import RepoContext
    from test_computer_startup import lazy_local_workspace

    from moyai.agent import HarnessActivityContext
    from moyai.computer_startup import OnDemandRepository

    _, _, prepared = active
    historical = asdict(prepared.scope)
    historical.pop("interactive_delegation")
    converter = encrypted_converter(runtime.security, runtime.objects)
    # A tool already scheduled by an old worker is decoded without rerunning
    # workflow preparation. Missing version state must never opt it into the
    # new child-session path.
    [restored] = await converter.decode(await converter.encode([historical]), [RunScope])
    assert restored.interactive_delegation is False
    directory = runtime.settings.data_dir.parent / "computer"
    allocations, published = [], []
    ctx = HarnessActivityContext(deps=restored, run_step=1, messages=[])
    backend = lazy_local_workspace(directory, allocations).get_workspace(ctx, ref=None)
    assert backend is not None
    ctx.workspace = Workspace(backend)

    async def publish(scope, reference):
        assert scope == restored
        published.append(reference)

    repository = OnDemandRepository(RepoContext(), publish, deferred=True)
    args = {"agent_name": "self", "task": "Continue historical delegation."}
    await repository.before_tool_execute(
        ctx, call=ToolCallPart("delegate_task", args, tool_call_id="old-call"),
        tool_def=ToolDefinition(name="delegate_task"), args=args,
    )
    assert allocations == [str(directory)]
    assert published == [{"provider": "local", "id": str(directory)}]
