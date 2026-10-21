"""Delivery failures must preserve the workflow's durable, ordered mailbox."""

import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import select
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_delegation_temporal import dispatching

from moyai.agent import make_agent
from moyai.agent_messages import (
    DELIVERED,
    SIGNALLED,
    AgentMessages,
    MailObsolete,
    MailReady,
    MailUnavailable,
    read_agent_mail_v2,
    receipts,
)
from moyai.contracts import NewMessage, NewSession
from moyai.delegation import DelegationService
from moyai.models import Event, Turn
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("error", [
    HTTPException(503, "Authority provider unavailable"),
    HTTPException(403, "Access revoked"),
    PermissionError("Recipient stopped or superseded"),
])
async def test_authority_failure_is_not_evidence_of_consumption(runtime, actor, active, monkeypatch, error):
    _, _, parent = active
    mail = AgentMessages(runtime.database)
    await DelegationService(runtime.database).start(parent.scope, "child", "Work")
    child_turn = await runtime.database.claim_next()
    child = await runtime.database.prepare(child_turn.id)
    receipt = await mail.send(child.scope, "update", "parent", "Private evidence", child_turn.id)
    authorize = runtime.database.authorize_scope

    async def denied(*args, **kwargs):
        raise error

    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    monkeypatch.setattr(runtime.database, "authorize_scope", denied)
    assert isinstance(await read_agent_mail_v2(parent.scope, receipt["message_id"]), MailUnavailable)
    monkeypatch.setattr(runtime.database, "authorize_scope", authorize)
    result = await read_agent_mail_v2(parent.scope, receipt["message_id"])
    assert isinstance(result, MailReady) and result.mail.content == "Private evidence"
    assert isinstance(await read_agent_mail_v2(child.scope, receipt["message_id"]), MailObsolete)
    await mail.acknowledge(parent.scope, [receipt["message_id"]])
    assert isinstance(await read_agent_mail_v2(parent.scope, receipt["message_id"]), MailObsolete)


@pytest.mark.parametrize("lifecycle", ["retry", "restart", "rollover", "stop", "pause"])
async def test_unavailable_read_preserves_mail_across_lifecycle(runtime, actor, monkeypatch, lifecycle):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    unavailable, attempted, continued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    pausing = asyncio.Event()
    unavailable.set()
    reads = []
    original = AgentMessages.read

    async def read(self, scope, message_id):
        if scope.session_id == session.id and unavailable.is_set():
            reads.append((message_id, scope.history_segment))
            attempted.set()
            if len({segment for _, segment in reads}) > 1:
                continued.set()
            raise HTTPException(503, "Access verification is temporarily unavailable.")
        return await original(self, scope, message_id)

    monkeypatch.setattr(AgentMessages, "read", read)

    def call(name, arguments, identity):
        return {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=identity)}

    async def stream(messages, info):
        text = str(messages)
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if "Delegated assignment within the original human request" in text:
            if not any(part.tool_name == "send_message" for part in returns):
                yield call("send_message", {"target": "parent", "message": "RECOVERY_PROGRESS"}, "progress")
            else:
                yield "RECOVERY_COMPLETE"
        elif not any(part.tool_name == "spawn_agent" for part in returns):
            yield call("spawn_agent", {"task": "Send a progress update and finish."}, "spawn")
        elif "RECOVERY_PROGRESS" in text and "RECOVERY_COMPLETE" in text:
            yield "Recovered both messages in order."
        elif pausing.is_set():
            yield call("ask_user_question", {"questions": [{
                "header": "Scope", "question": "Which result should I investigate next?",
                "options": [{"label": "Parent"}, {"label": "Child"}],
            }]}, "scope-question")
        else:
            yield call("wait_agent", {
                "agent_ids": [], "timeout_seconds": 1 if lifecycle in {"rollover", "pause"} else 60,
            }, f"wait-{len(returns)}")

    runtime.models = {"test": FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("unused")]), stream_function=stream,
    )}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
    )
    runtime.settings.worker_shutdown_seconds = 1
    if lifecycle == "rollover":
        runtime.settings.history_continue_bytes = 16384
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Coordinate with one child." + (" Reference." * 3000 if lifecycle == "rollover" else ""),
        request_key="delivery-recovery",
    ))
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        handle = runtime.client.get_workflow_handle(turn.workflow_id)
        async with make_worker(runtime), dispatching(runtime):
            await asyncio.wait_for(attempted.wait(), 30)
            async with asyncio.timeout(15):
                while True:
                    async with runtime.database.sessions() as db:
                        signalled = list(await db.scalars(select(Event).where(
                            Event.turn_id == turn.id, Event.kind == SIGNALLED,
                        ).order_by(Event.id)))
                    if len(signalled) == 2:
                        break
                    await asyncio.sleep(0.05)
            # The completion must stay behind the unavailable progress read.
            await asyncio.sleep(0.2)
            assert {identity for identity, _ in reads} == {signalled[0].id}
            if lifecycle == "rollover":
                await asyncio.wait_for(continued.wait(), 20)
            if lifecycle == "stop":
                await runtime.database.stop(actor, session.id)
                await handle.cancel()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 20)
            elif lifecycle != "restart":
                if lifecycle == "pause":
                    pausing.set()
                else:
                    unavailable.clear()
                expected = "waiting" if lifecycle == "pause" else "completed"
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == expected
        if lifecycle == "restart":
            unavailable.clear()
            async with make_worker(runtime), dispatching(runtime):
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
        if lifecycle != "stop":
            async with runtime.database.sessions() as db:
                saved = await db.get(Turn, turn.id)
                conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(saved.conversation))
                ids, _ = receipts(conversation.messages)
                assert ids == ([] if lifecycle == "pause" else [row.id for row in signalled])
                for row in signalled:
                    assert (await db.get(Event, row.id)).kind == (SIGNALLED if lifecycle == "pause" else DELIVERED)
        latest = await handle.fetch_history()
        run_id = latest.events[0].workflow_execution_started_event_attributes.first_execution_run_id
        while run_id:
            history = await runtime.client.get_workflow_handle(turn.workflow_id, run_id=run_id).fetch_history()
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                interceptors=[ActivityCapacity()],
            ).replay_workflow(history)
            end = history.events[-1]
            run_id = (
                end.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                if end.HasField("workflow_execution_continued_as_new_event_attributes") else None
            )
