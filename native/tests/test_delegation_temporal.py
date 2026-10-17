"""Real SDK and Temporal: three persistent children, steering, replay and resume."""

import asyncio
import json
from contextlib import asynccontextmanager, suppress

from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.agent import make_agent
from moyai.contracts import NewMessage, NewSession
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


async def test_three_children_steer_and_continue_after_worker_replacement(runtime, actor):
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
    from scenarios import integration_model

    from moyai import delegation

    _, turn, _ = active
    runtime.models = {"test": integration_model("stream")}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as old:
            old.setattr(delegation, "enabled", lambda: False)
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
    assert len(await runtime.stores.steps.list_runs(parent_run_id=turn.id)) == 1
