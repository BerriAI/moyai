from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.workspaces import WorkspaceRef
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.models import Session, Turn
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


async def test_native_agent_runs_in_real_temporal_server(runtime, active):
    _session, turn, prepared = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=DataConverter(
            payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)
        ),
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            result = await asyncio.wait_for(runtime.client.execute_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            ), timeout=45)
    assert result["status"] == "completed"
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == "completed"
        assert runtime.security.unseal(saved.result).decode() == "Task completed."
    records = await runtime.stores.steps.list_runs(conversation_id=prepared.scope.conversation_id)
    assert len(records) == 1


@pytest.mark.parametrize("runtime", ["tools"], indirect=True)
async def test_native_tools_skills_memory_delegation_and_replay(runtime, active):
    session, turn, prepared = active
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            result = await asyncio.wait_for(handle.result(), timeout=45)
            history = await handle.fetch_history()
        assert result["status"] == "completed"
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    plan = await runtime.stores.plan(session.id).get_items()
    assert len(plan) == 1 and plan[0].content == "Verify native execution"
    async with runtime.database.sessions() as db:
        saved = await db.get(Session, session.id)
        reference = TypeAdapter(WorkspaceRef).validate_json(saved.workspace)
    assert (Path(reference.id) / "proof.txt").read_text() == "Written by the native file capability.\n"
    children = await runtime.stores.steps.list_runs(parent_run_id=turn.id)
    assert len(children) == 1
    memory = await runtime.stores.memory.read(
        f"{prepared.scope.memory_namespace}/main/MEMORY.md", max_chars=1000
    )
    assert memory is not None and "proof succeeded" in memory.content


@pytest.mark.parametrize("runtime", ["question"], indirect=True)
async def test_native_question_pauses_durably(runtime, active):
    session, turn, _prepared = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)),
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            result = await asyncio.wait_for(runtime.client.execute_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            ), timeout=45)
    assert result["status"] == "waiting"
    async with runtime.database.sessions() as db:
        saved = await db.get(Session, session.id)
        assert saved.status == "waiting"
