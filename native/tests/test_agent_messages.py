"""Two-way model-visible mail through the real Pydantic/Temporal runtime."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Conversation, ConversationTypeAdapter, RunContext
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextContent, TextPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage
from sqlalchemy import select
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import ExecuteActivityInput, Replayer
from test_delegation_temporal import dispatching

from moyai.agent import make_agent
from moyai.agent_messages import DELIVERED, PENDING, SIGNALLED, AgentMessages, MailDispatch, dispatch_mail
from moyai.contracts import NewMessage, NewSession
from moyai.delegation import DelegationService, InteractiveDelegation
from moyai.delegation_models import DelegatedSession
from moyai.execution import AuthorizedActivity
from moyai.models import Event, Turn
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("rollover", [False, True])
async def test_child_pushes_progress_parent_guides_and_completion_arrives(runtime, actor, rollover, monkeypatch):
    progress_seen, guidance_seen, finish_allowed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    send = AgentMessages.send
    attempts = []

    async def lose_first_reply(self, scope, call_id, target, message, input_id):
        result = await send(self, scope, call_id, target, message, input_id)
        attempts.append(call_id)
        if not rollover and len(attempts) == 1:
            raise RuntimeError("Lost activity reply after message commit.")
        return result

    monkeypatch.setattr(AgentMessages, "send", lose_first_reply)
    if rollover:
        finish_allowed.set()
    else:
        runtime.settings.worker_shutdown_seconds = 1

    async def stream(messages, info):
        text = str(messages)
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        def call(name, arguments, identity):
            return {0: DeltaToolCall(name=name, json_args=json.dumps(arguments), tool_call_id=identity)}

        if "Delegated assignment within the original human request" in text:
            if not any(p.tool_name == "send_message" for p in returns):
                yield call("send_message", {"target": "parent", "message": "DISCOVERY_GREEN"}, "child-update")
            elif "GUIDANCE_BLUE" not in text:
                yield call("wait_agent", {"agent_ids": [], "timeout_seconds": 30}, "child-wait")
            else:
                guidance_seen.set()
                await finish_allowed.wait()
                yield "FINAL_BLUE"
            return
        spawned = next((p for p in returns if p.tool_name == "spawn_agent"), None)
        if spawned is None:
            yield call("spawn_agent", {"task": "Report a discovery and await guidance."}, "spawn-one")
        elif "DISCOVERY_GREEN" not in text:
            yield call("wait_agent", {"agent_ids": [], "timeout_seconds": 30}, "parent-wait-update")
        elif not any(p.tool_name == "send_message" for p in returns):
            async with runtime.database.sessions() as db:
                link = await db.scalar(select(DelegatedSession))
                assert (await db.get(Turn, link.initial_turn_id)).status == "running"
            progress_seen.set()
            receipt = json.loads(spawned.content) if isinstance(spawned.content, str) else spawned.content
            yield call("send_message", {
                "target": receipt["session_id"], "message": "GUIDANCE_BLUE",
            }, "parent-guidance")
        elif "FINAL_BLUE" not in text:
            # No explicit final-result polling: finalization must wait for pushed mail.
            yield "Waiting for the child's verified result."
        else:
            assert progress_seen.is_set() and guidance_seen.is_set()
            yield "Combined result: FINAL_BLUE"

    runtime.models = {"test": FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("unused")]), stream_function=stream,
    )}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
    )
    if rollover:
        runtime.settings.history_continue_bytes = 16384
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Coordinate with a child." + (" Reference." * 3000 if rollover else ""),
        request_key="message-exchange",
    ))
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            await asyncio.wait_for(progress_seen.wait(), 45)
            if not rollover:
                await asyncio.wait_for(guidance_seen.wait(), 30)
                async with runtime.database.sessions() as db:
                    progress = await db.scalar(select(Event).where(Event.kind == DELIVERED).order_by(Event.id))
                # Duplicate delivery after consumption must not append the update twice.
                await runtime.client.get_workflow_handle(turn.workflow_id).signal("agent_message", progress.id)
            else:
                await asyncio.wait_for(runtime.client.get_workflow_handle(turn.workflow_id).result(), 60)
        # A replacement worker reconstructs its mailbox from Temporal history.
        finish_allowed.set()
        async with make_worker(runtime), dispatching(runtime):
            handle = runtime.client.get_workflow_handle(turn.workflow_id)
            assert (await asyncio.wait_for(handle.result(), 60))["status"] == "completed"
            async with runtime.database.sessions() as db:
                link = await db.scalar(select(DelegatedSession))
                child = await db.get(Turn, link.initial_turn_id)
            histories = []
            for workflow_id in [turn.workflow_id, child.workflow_id]:
                latest = await runtime.client.get_workflow_handle(workflow_id).fetch_history()
                run_id = latest.events[0].workflow_execution_started_event_attributes.first_execution_run_id
                while run_id:
                    history = await runtime.client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
                    histories.append(history)
                    end = history.events[-1]
                    run_id = (
                        end.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                        if end.HasField("workflow_execution_continued_as_new_event_attributes") else None
                    )
        for history in histories:
            assert not any(
                event.activity_task_scheduled_event_attributes.activity_type.name.endswith("__reports")
                for event in history.events if event.HasField("activity_task_scheduled_event_attributes")
            ), "Message waits must wake on notifications, not poll reports."
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                interceptors=[ActivityCapacity()],
            ).replay_workflow(history)
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Event).where(Event.kind == DELIVERED)))
        assert len(rows) == 3  # Two progress messages and one automatic final result.
        for turn_id in [turn.id, child.id]:
            saved = await db.get(Turn, turn_id)
            conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(saved.conversation))
            received = [
                content.metadata["moyai_mail_id"]
                for message in conversation.messages for part in message.parts
                if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
                for content in part.content if isinstance(content, TextContent)
                and content.metadata and "moyai_mail_id" in content.metadata
            ]
            assert len(received) == len(set(received))
        assert runtime.security.unseal((await db.get(Turn, turn.id)).result).decode() == "Combined result: FINAL_BLUE"
    if rollover:
        assert len(histories) > 2
    else:
        assert attempts.count("child-update") == 2


async def test_message_receipts_are_idempotent_and_unrelated_followups_cannot_message(runtime, actor, active):
    from test_delegated_sessions import finish

    _, parent, prepared = active
    service, mail = DelegationService(runtime.database), AgentMessages(runtime.database)
    child = await service.start(prepared.scope, "child", "Assignment")
    child_turn = await runtime.database.claim_next()
    child_context = await runtime.database.prepare(child_turn.id)
    first = await mail.send(prepared.scope, "guidance", child["session_id"], "Keep the evidence.", parent.id)
    assert await mail.send(
        prepared.scope, "guidance", child["session_id"], "Keep the evidence.", parent.id,
    ) == first
    with pytest.raises(PermissionError):
        await mail.send(prepared.scope, "guidance", child["session_id"], "Changed", parent.id)
    returned = await mail.send(child_context.scope, "progress", "parent", "Working.", child_turn.id)
    assert returned["target"] == prepared.scope.session_id
    assert await mail.read(child_context.scope, returned["message_id"]) is None
    assert (await mail.read(prepared.scope, returned["message_id"])).content == "Working."
    await finish(runtime, child_context, "Done.")
    later = await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Private later work", request_key="later",
    ))
    await runtime.database.claim_next()
    context = await runtime.database.prepare(later.id)
    with pytest.raises(PermissionError):
        await mail.send(context.scope, "leak", "parent", "Private unrelated content", later.id)
    with pytest.raises(PermissionError):
        await mail.send(replace(prepared.scope, turn_id=later.id), "wrong", child["session_id"], "No", later.id)


async def test_lost_signal_reply_retries_in_order_without_starving_other_recipients(runtime, actor, active):
    _, parent, prepared = active
    mail = AgentMessages(runtime.database)
    child = await DelegationService(runtime.database).start(prepared.scope, "child", "Work")
    child_turn = await runtime.database.claim_next()
    child_context = await runtime.database.prepare(child_turn.id)
    sent = [
        await mail.send(prepared.scope, f"guidance-{i}", child["session_id"], f"Update {i}", parent.id)
        for i in range(8)
    ]
    with pytest.raises(ValueError, match="eight unread"):
        await mail.send(prepared.scope, "too-many", child["session_id"], "Overflow", parent.id)
    other = await DelegationService(runtime.database).start(prepared.scope, "other-child", "Other work")
    other_turn = await runtime.database.claim_next()
    await runtime.database.prepare(other_turn.id)
    independent = await mail.send(prepared.scope, "independent", other["session_id"], "Unblocked", parent.id)
    # Simulate acceptance by Temporal followed by a lost RPC acknowledgment.
    signal = AsyncMock(side_effect=[RPCError("lost reply", RPCStatusCode.UNAVAILABLE, b""), None])
    runtime.client = SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(signal=signal))
    state = MailDispatch()
    await dispatch_mail(runtime, state, limit=1)
    async with runtime.database.sessions() as db:
        assert (await db.get(Event, sent[0]["message_id"])).kind == PENDING
    await dispatch_mail(runtime, state, limit=1)
    assert signal.call_args.args == ("agent_message", independent["message_id"])
    signal.side_effect = None
    for _ in range(12):
        await dispatch_mail(runtime, state, limit=1)
    async with runtime.database.sessions() as db:
        rows = await db.scalars(select(Event).where(Event.id.in_([item["message_id"] for item in sent])))
        assert {row.kind for row in rows} == {SIGNALLED}
    assert sum(call.args == ("agent_message", sent[0]["message_id"]) for call in signal.call_args_list) == 2
    before = signal.call_count
    # A fresh dispatcher does not repeatedly signal a durably accepted message.
    await dispatch_mail(runtime, MailDispatch())
    assert signal.call_count == before
    await mail.acknowledge(child_context.scope, [sent[0]["message_id"]])
    assert await mail.read(child_context.scope, sent[0]["message_id"]) is None
    await mail.send(prepared.scope, "capacity-freed", child["session_id"], "Next", parent.id)
    runtime.client = None


async def test_answer_continuations_receive_pending_mail_in_both_directions(runtime, actor, active):
    from test_delegated_sessions import finish

    parent, parent_turn, prepared = active
    prepared.scope.background_delegation = True
    prepared.scope.background_agents = []
    mail = AgentMessages(runtime.database)
    child = await DelegationService(runtime.database).start(prepared.scope, "child", "Work")
    prepared.scope.background_agents.append(child["session_id"])
    child_turn = await runtime.database.claim_next()
    child_context = await runtime.database.prepare(child_turn.id)
    progress = await mail.send(child_context.scope, "progress", "parent", "Finding", child_turn.id)
    guidance = await mail.send(prepared.scope, "guidance", child["session_id"], "Direction", parent_turn.id)
    signal = AsyncMock()
    runtime.client = SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(signal=signal))
    await dispatch_mail(runtime, MailDispatch())

    async def pause(context):
        assert await runtime.database.save_result(
            context.scope, conversation=ConversationTypeAdapter.dump_json(Conversation(
                conversation_id=context.scope.conversation_id,
            )).decode(), output="", workspace=None, usage={}, deferred=True,
        )

    await pause(prepared)
    await pause(child_context)
    parent_answer = await runtime.database.resume(actor, parent.id, parent_turn.id, "parent-answer", {"calls": {}})
    child_answer = await runtime.database.resume(
        actor, child["session_id"], child_turn.id, "child-answer", {"calls": {}},
    )
    await runtime.database.claim_batch(10)
    parent_context = await runtime.database.prepare(parent_answer.id)
    child_context = await runtime.database.prepare(child_answer.id)
    async with runtime.database.sessions() as db:
        assert (await db.get(Event, progress["message_id"])).kind == PENDING
        assert (await db.get(Event, guidance["message_id"])).kind == PENDING
    assert (await mail.read(parent_context.scope, progress["message_id"])).content == "Finding"
    assert (await mail.read(child_context.scope, guidance["message_id"])).content == "Direction"
    assert (await mail.send(child_context.scope, "next-progress", "parent", "Next finding", child_answer.id))[
        "target"
    ] == parent.id
    await mail.send(parent_context.scope, "next-guidance", child["session_id"], "Next direction", parent_answer.id)
    await finish(runtime, parent_context, "Done")
    later = await runtime.database.submit(actor, parent.id, NewMessage(prompt="New work", request_key="new-work"))
    await runtime.database.claim_next()
    later_context = await runtime.database.prepare(later.id)
    assert await mail.read(later_context.scope, progress["message_id"]) is None
    with pytest.raises(PermissionError, match="different assignment"):
        await mail.send(child_context.scope, "late-progress", "parent", "Must stay private", child_answer.id)
    runtime.client = None


async def test_stopped_sender_recovers_receipt_but_cannot_send_more_or_wake_stopped_child(
    runtime, actor, active, monkeypatch,
):
    parent, turn, prepared = active
    mail = AgentMessages(runtime.database)
    child = await DelegationService(runtime.database).start(prepared.scope, "child", "Work")
    sent = await mail.send(prepared.scope, "guidance", child["session_id"], "Direction", turn.id)
    await runtime.database.stop(actor, parent.id)
    monkeypatch.setattr("moyai.execution.activity.info", lambda: SimpleNamespace(
        activity_type="agent__moyai__capability__moyai_interactive_delegation__send_message",
    ))

    async def recover(_):
        return await InteractiveDelegation(runtime.database).send(
            prepared.scope, "guidance", child["session_id"], "Direction", turn.id,
        )

    interceptor = AuthorizedActivity(SimpleNamespace(execute_activity=recover), runtime.database)
    request = ExecuteActivityInput(fn=lambda: None, args=[prepared.scope], executor=None, headers={})
    assert await interceptor.execute_activity(request) == sent
    with pytest.raises(PermissionError, match="stopped or superseded"):
        await mail.send(prepared.scope, "new-guidance", child["session_id"], "New direction", turn.id)
    await runtime.database.stop(actor, child["session_id"])
    signal = AsyncMock()
    runtime.client = SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(signal=signal))
    await dispatch_mail(runtime, MailDispatch())
    signal.assert_not_called()
    runtime.client = None


async def test_depth_limited_children_can_message_but_cannot_delegate(runtime, active):
    _, turn, prepared = active
    capability = InteractiveDelegation(runtime.database)
    tools = [ToolDefinition(name=name) for name in ("spawn_agent", "delegate_task", "send_message", "wait_agent")]
    scope = replace(
        prepared.scope, interactive_delegation=True, background_delegation=True,
        agent_messages=True, delegation_depth=2,
    )
    ctx = RunContext(deps=scope, run_id=turn.id, model=TestModel(), usage=RunUsage())
    assert {tool.name for tool in await capability.prepare_tools(ctx, tools)} == {"send_message", "wait_agent"}
    scope.swarm_id = "fixed-roster"
    assert not await capability.prepare_tools(ctx, tools)


async def test_fast_completion_follows_committed_progress_without_duplicating_report(runtime, actor, active):
    from test_delegated_sessions import finish

    _, _, prepared = active
    prepared.scope.agent_messages = True
    await DelegationService(runtime.database).start(prepared.scope, "child", "Work")
    child_turn = await runtime.database.claim_next()
    child_context = await runtime.database.prepare(child_turn.id)
    mail = AgentMessages(runtime.database)
    progress = await mail.send(child_context.scope, "progress", "parent", "Discovery", child_turn.id)
    await finish(runtime, child_context, "Final evidence")
    signal = AsyncMock()
    runtime.client = SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(signal=signal))
    await dispatch_mail(runtime, MailDispatch())
    await dispatch_mail(runtime, MailDispatch())
    assert signal.call_count == 2
    assert signal.call_args_list[0].args == ("agent_message", progress["message_id"])
    completion = await mail.read(prepared.scope, signal.call_args_list[1].args[1])
    assert completion.kind == "completion" and completion.content == "Final evidence"
    runtime.client = None
