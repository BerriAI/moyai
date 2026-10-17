"""Real Temporal/SDK launch and replay; controlled model, not live-provider proof."""

import asyncio

from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from sqlalchemy import func, select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_swarm_launch import streaming_model

from moyai.agent import make_agent
from moyai.contracts import NewMessage, NewSession
from moyai.storage import encrypted_converter
from moyai.swarm_contracts import SwarmControl
from moyai.swarm_models import SwarmMember, SwarmMission, SwarmOperation
from moyai.worker import dispatch_once, make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


async def test_chat_launch_executes_as_native_tool_and_replays_without_another_team(runtime, actor):
    def respond(messages, info):
        if "start_swarm" not in {tool.name for tool in info.function_tools}:
            return ModelResponse(parts=[TextPart("Member ready.")])
        if any(isinstance(part, ToolReturnPart) and part.tool_name == "start_swarm"
               for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("Launched the persistent team.")])
        return ModelResponse(parts=[ToolCallPart(
            "start_swarm", {"goal": "Investigate onboarding.", "workers": 2, "budget_seconds": 7200},
            tool_call_id="durable-launch",
        )])

    runtime.models = {"test": streaming_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
    )
    parent = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, parent.id, NewMessage(
        prompt="Start a two-worker swarm for two hours.", request_key="human-launch",
    ))
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            await dispatch_once(runtime)
            handle = runtime.client.get_workflow_handle(turn.workflow_id)
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            history = await handle.fetch_history()
        async with runtime.database.sessions() as db:
            mission = await db.scalar(select(SwarmMission))
            assert mission.budget_seconds == 7200
            assert await db.scalar(select(func.count()).select_from(SwarmMember)) == 3
            assert await db.scalar(select(func.count()).select_from(SwarmOperation)) == 1
        async with make_worker(runtime):
            await runtime.database.swarms.control(
                actor, mission.id, SwarmControl(action="pause", revision=0),
            )
            await dispatch_once(runtime)
        await Replayer(
            workflows=[TurnWorkflow], interceptors=[ActivityCapacity()],
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 1
