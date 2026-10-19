from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter
from pydantic_ai.capabilities import Capability, LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_delegation_temporal import dispatching

from moyai.agent import make_agent
from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage, NewSession
from moyai.delegation_models import DelegatedSession
from moyai.models import Session, Turn, User, new_id
from moyai.storage import EncryptedPayloadCodec
from moyai.tools.tool_caller import ToolCaller
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


async def test_non_retryable_tool_failure_completes_real_temporal_turn(runtime, active, monkeypatch):
    _session, turn, _prepared = active
    observed_failures: list[str] = []
    wrapper_contexts: list[bool] = []
    tool_call = ToolCaller.call

    async def observe_wrapper_context(self, call, tool, args, handler):
        wrapper_contexts.append(workflow.in_workflow())
        return await tool_call(self, call, tool, args, handler)

    monkeypatch.setattr(ToolCaller, "call", observe_wrapper_context)

    async def fail_tool() -> str:
        raise ApplicationError(
            "The provider rejected this operation.", type="GitHubPublishError", non_retryable=True,
        )

    def response(messages, info):
        failed_result = next((
            part for message in messages for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_name == "fail_tool"
        ), None)
        if failed_result is not None:
            assert failed_result.outcome == "failed"
            observed_failures.append(str(failed_result.content))
            return ModelResponse(parts=[TextPart("Recovered from the tool failure.")])
        assert any(tool.name == "fail_tool" for tool in info.function_tools)
        return ModelResponse(parts=[ToolCallPart("fail_tool", {}, tool_call_id="failing-call")])

    async def stream(messages, info):
        for part in response(messages, info).parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(
                    name=part.tool_name, json_args="{}", tool_call_id=part.tool_call_id,
                )}

    runtime.models = {"test": FunctionModel(response, stream_function=stream)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings,
        runtime.database,
        runtime.stores,
        runtime.objects,
        models=runtime.models,
        workspace=LocalWorkspace(Path(runtime.settings.data_dir).parent / "computer"),
        skills_directory="skills",
        product_capabilities=[Capability(id="tool_failure_test", tools=[Tool(fail_tool)])],
    )
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
    assert wrapper_contexts == [True]
    assert len(observed_failures) == 1
    assert "fail_tool failed (GitHubPublishError)" in observed_failures[0]
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == "completed"
        assert runtime.security.unseal(saved.result).decode() == "Recovered from the tool failure."


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


@pytest.mark.parametrize("runtime", ["question", "optional_question"], indirect=True)
async def test_native_question_pauses_durably(runtime, active):
    session, turn, _prepared = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)),
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
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()],
            data_converter=DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)),
        ).replay_workflow(history)
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
                assert view["pending"] is None
