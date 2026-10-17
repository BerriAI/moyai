"""Real Temporal/SDK execution with a controlled model; not live-provider proof."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import select
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.models import Turn
from moyai.storage import EncryptedPayloadCodec
from moyai.swarm_contracts import NewSwarm, SwarmControl
from moyai.swarm_models import SwarmTurn
from moyai.swarm_runtime import SwarmWorkflow
from moyai.worker import dispatch_once, make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("runtime", ["swarm"], indirect=True)
async def test_native_swarm_tools_execute_replay_and_survive_worker_replacement(runtime, actor):
    root, _ = await runtime.database.swarms.start(actor, NewSwarm(
        prompt="Check the test evidence together.", workers=2, request_key="temporal-team",
    ))
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            await dispatch_once(runtime)
            async with runtime.database.sessions() as db:
                turns = list(await db.scalars(select(Turn).join(
                    SwarmTurn, SwarmTurn.turn_id == Turn.id,
                ).where(SwarmTurn.mission_id == root.id)))
            results = await asyncio.wait_for(asyncio.gather(*(
                runtime.client.get_workflow_handle(turn.workflow_id).result() for turn in turns
            )), timeout=60)
            assert all(result["status"] == "completed" for result in results)
            child = next(turn for turn in turns if turn.session_id != root.id)
            child_history = await runtime.client.get_workflow_handle(child.workflow_id).fetch_history()
        # No application-held task or in-memory team survives this boundary.
        async with make_worker(runtime):
            before = await runtime.database.swarms.view(actor, root.id)
            assert before["leader_id"] is not None
            assert len([post for post in before["posts"] if post["kind"] == "finding"]) == 2
            await runtime.database.swarms.control(
                actor, root.id, SwarmControl(action="pause", revision=before["revision"]),
            )
            await dispatch_once(runtime)
            supervisor = runtime.client.get_workflow_handle(f"moyai-swarm-{root.id}-1")
            await asyncio.wait_for(supervisor.result(), timeout=15)
            history = await supervisor.fetch_history()
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=[TurnWorkflow, SwarmWorkflow],
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(child_history)
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=[SwarmWorkflow],
            data_converter=converter,
        ).replay_workflow(history)
    after = await runtime.database.swarms.view(actor, root.id)
    assert after["status"] == "paused" and after["leader_id"] == before["leader_id"]
