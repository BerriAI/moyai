from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import select
from temporalio import activity
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_delegation_temporal import dispatching

from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage, NewSession
from moyai.delegation_models import DelegatedSession
from moyai.models import Session, Turn, User, new_id
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
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
        async with make_worker(runtime), dispatching(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            result = await asyncio.wait_for(handle.result(), timeout=45)
            history = await handle.fetch_history()
        assert result["status"] == "completed"
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    plan = await runtime.stores.plan(session.id).get_items()
    assert len(plan) == 1 and plan[0].content == "Verify native execution"
    async with runtime.database.sessions() as db:
        saved = await db.get(Session, session.id)
        reference = TypeAdapter(WorkspaceRef).validate_json(saved.workspace)
    assert (Path(reference.id) / "proof.txt").read_text() == "Written by the native file capability.\n"
    async with runtime.database.sessions() as db:
        children = list(await db.scalars(select(DelegatedSession).where(DelegatedSession.parent_turn_id == turn.id)))
    assert len(children) == 1
    assert len(await runtime.stores.plan(children[0].session_id).get_items()) == 1
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


async def test_snapshot_activity_failure_preserves_native_temporal_result(runtime, active, monkeypatch):
    from moyai.activities import ACTIVITIES, save_computer

    @activity.defn(name="moyai_save_computer_v1")
    async def interrupted_snapshot(turn_id: str, reference: dict | None) -> None:
        raise ApplicationError("Snapshot activity was interrupted.", non_retryable=True)

    replacements = [
        interrupted_snapshot if item is save_computer
        else item for item in ACTIVITIES
    ]
    monkeypatch.setattr("moyai.worker.ACTIVITIES", replacements)
    session, turn, _ = active
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
    assert result["status"] == "completed"
    async with runtime.database.sessions() as db:
        saved_turn = await db.get(Turn, turn.id)
        saved_session = await db.get(Session, session.id)
        assert saved_turn.status == "completed"
        assert runtime.security.unseal(saved_turn.result).decode() == "Task completed."
        assert saved_session.active_turn is None and saved_session.save_warning


@pytest.mark.parametrize("runtime", ["question"], indirect=True)
async def test_shared_snapshot_only_exposes_deferred_requests_to_requester(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Ask a question.", request_key="shared-question"),
    )
    await runtime.database.claim_next()
    async with runtime.database.sessions.begin() as db:
        other = User(id=new_id(), username="viewer", display_name="Viewer", password="unused")
        db.add(other)
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver",
    ) as client:
        for user in (actor, other):
            token, _ = runtime.security.issue_session(user.id, user.auth_version)
            client.cookies.set(COOKIE, token)
            response = await client.get(f"/api/sessions/{session.id}")
            assert response.status_code == 200
            view = response.json()
            assert view["session"]["status"] == "waiting"
            if user.id == actor.id:
                assert view["pending"]["calls"][0]["tool_name"] == "ask_user_question"
            else:
                assert "pending" not in view
