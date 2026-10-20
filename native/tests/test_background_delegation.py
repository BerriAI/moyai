"""Parent and child overlap through real SDK/Temporal runs; controlled models."""

import asyncio
import json
from dataclasses import asdict, replace

import pytest
from pydantic_ai import Conversation, ConversationTypeAdapter, RunContext, RunUsage
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import (
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import select
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_delegation_temporal import dispatching

from moyai.agent import make_agent
from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.delegation import BACKGROUND_VERSION, InteractiveDelegation
from moyai.delegation_models import DelegatedSession
from moyai.models import Turn
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("finish_early,rollover", [
    (False, False), (True, False), ("next_step", False), (True, True),
])
async def test_parent_works_while_child_runs_and_collects_report(runtime, actor, finish_early, rollover):
    child_started, parent_worked, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = []

    async def stream(messages, info):
        prompts = "\n".join(
            str(part.content) for message in messages for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        if "Delegated assignment within the original human request" in prompts:
            child_started.set()
            await release.wait()
            yield "CHILD_EVIDENCE_73"
            return
        requests.append(messages.copy())
        returned = [
            part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
        ]
        if not any(part.tool_name == "spawn_agent" for part in returned):
            yield {0: DeltaToolCall(
                name="spawn_agent", json_args=json.dumps({"task": "Return CHILD_EVIDENCE_73"}),
                tool_call_id="spawn-one",
            )}
        elif not parent_worked.is_set():
            await asyncio.wait_for(child_started.wait(), 30)
            assert not release.is_set()
            parent_worked.set()
            # An actual additional parent model step executes while the child is
            # still inside its first model request. A blocking delegate deadlocks.
            if finish_early is True:
                yield "Premature parent answer"
            elif finish_early == "next_step":
                await release.wait()
                async with asyncio.timeout(30):
                    while True:
                        async with runtime.database.sessions() as db:
                            child = await db.scalar(select(DelegatedSession))
                            initial = await db.get(Turn, child.initial_turn_id)
                            if initial.status == "completed":
                                break
                        await asyncio.sleep(0.05)
                yield {0: DeltaToolCall(name="read_plan", json_args="{}", tool_call_id="own-work")}
            else:
                receipt = next(part for part in returned if part.tool_name == "spawn_agent").content
                if isinstance(receipt, str):
                    receipt = json.loads(receipt)
                yield {0: DeltaToolCall(
                    name="wait_agent",
                    json_args=json.dumps({"agent_ids": [receipt["session_id"]], "timeout_seconds": 60}),
                    tool_call_id="wait-one",
                )}
        else:
            assert "CHILD_EVIDENCE_73" in str(messages)
            yield "Combined parent work with CHILD_EVIDENCE_73."

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
        prompt="Do independent work alongside a child." + (" Context." * 3000 if rollover else ""),
        request_key="parallel-parent",
    ))
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            await asyncio.wait_for(parent_worked.wait(), 45)
            handle = runtime.client.get_workflow_handle(turn.workflow_id)
            async with runtime.database.sessions() as db:
                children = list(await db.scalars(select(DelegatedSession)))
                assert len(children) == 1
                assert (await db.get(Turn, turn.id)).status == "running"
                assert (await db.get(Turn, children[0].initial_turn_id)).status == "running"
            release.set()
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            histories = []
            latest = await handle.fetch_history()
            run_id = latest.events[0].workflow_execution_started_event_attributes.first_execution_run_id
            while run_id:
                history = await runtime.client.get_workflow_handle(
                    turn.workflow_id, run_id=run_id,
                ).fetch_history()
                histories.append(history)
                last = history.events[-1]
                run_id = (
                    last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                    if last.HasField("workflow_execution_continued_as_new_event_attributes") else None
                )
        if rollover:
            assert len(histories) > 1
        for history in histories:
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                interceptors=[ActivityCapacity()],
            ).replay_workflow(history)
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert runtime.security.unseal(saved.result).decode() == "Combined parent work with CHILD_EVIDENCE_73."
    assert len(requests) == 3
    if finish_early == "next_step":
        from moyai.history_handoff import latest_turn_snapshot

        snapshot = await latest_turn_snapshot(runtime, turn.id)
        assert snapshot is not None
        report = next(
            message for message in snapshot.messages
            if any(isinstance(part, UserPromptPart) and "Saved subagent reports" in str(part.content)
                   for part in message.parts)
        )
        assert report.run_id == turn.id and report.conversation_id


async def test_background_tools_hidden_for_legacy_swarm_and_depth_limit(runtime, active, monkeypatch):
    _, turn, prepared = active
    capability = InteractiveDelegation(runtime.database)
    tools = [ToolDefinition(name=name) for name in ("spawn_agent", "wait_agent", "delegate_task", "read_file")]
    for scope, expected in [
        (replace(prepared.scope, interactive_delegation=False), {"delegate_task", "read_file"}),
        (replace(prepared.scope, interactive_delegation=True, swarm_id="swarm"), {"read_file"}),
        (replace(prepared.scope, interactive_delegation=True, delegation_depth=2), {"read_file"}),
        (replace(prepared.scope, interactive_delegation=True, background_delegation=True), {t.name for t in tools}),
    ]:
        ctx = RunContext(deps=scope, run_id=turn.id, model=TestModel(), usage=RunUsage())
        assert {t.name for t in await capability.prepare_tools(ctx, tools)} == expected
    # Old Temporal histories must retain their original tool catalog.
    monkeypatch.setattr(workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(workflow, "patched", lambda name: name != BACKGROUND_VERSION)
    assert {t.name for t in await capability.prepare_tools(ctx, tools)} == {"delegate_task", "read_file"}


async def test_status_report_is_parent_scoped_and_does_not_export_followups(runtime, actor, active):
    from test_delegated_sessions import finish

    _, _, prepared = active
    capability = InteractiveDelegation(runtime.database)
    child = await capability.service.start(prepared.scope, "child", "Bounded assignment")
    assert await capability.reports(prepared.scope, [child["session_id"]]) == [
        {"session_id": child["session_id"], "status": "running"},
    ]
    other = await runtime.database.create_session(actor, NewSession(visibility="private"))
    unavailable = await capability.reports(
        replace(prepared.scope, turn_id="unrelated", session_id=other.id), [child["session_id"]],
    )
    assert unavailable[0]["status"] == "unavailable"
    initial = await runtime.database.claim_next()
    context = await runtime.database.prepare(initial.id)
    await finish(runtime, context, "Original evidence")
    report = (await capability.reports(prepared.scope, [child["session_id"]]))[0]
    assert report["status"] == "completed" and report["output"] == "Original evidence"
    await runtime.database.submit(actor, child["session_id"], NewMessage(
        prompt="Private human follow-up", request_key="private-followup",
    ))
    assert (await capability.reports(prepared.scope, [child["session_id"]]))[0] == report


def test_pending_handles_survive_scope_serialization():
    scope = RunScope(
        session_id="parent", turn_id="turn", actor_id="actor", model="test",
        background_delegation=True, background_agents=["child"],
    )
    restored = RunScope(**json.loads(json.dumps(asdict(scope))))
    assert restored.background_agents == ["child"]
    InteractiveDelegation.acknowledge(restored, [{"session_id": "child"}])
    assert restored.background_agents == []


async def test_spawn_retry_zero_timeout_and_stopped_child(runtime, actor, active):
    _, turn, prepared = active
    scope = replace(prepared.scope, interactive_delegation=True, background_delegation=True)
    capability = InteractiveDelegation(runtime.database)
    ctx = RunContext(
        deps=scope, run_id=turn.id, tool_call_id="spawn-one", model=TestModel(), usage=RunUsage(),
    )
    tools = await capability.tools.get_tools(ctx)
    child = await capability.tools.call_tool("spawn_agent", {"task": "Independent work"}, ctx, tools["spawn_agent"])
    retry = await capability.tools.call_tool("spawn_agent", {"task": "Independent work"}, ctx, tools["spawn_agent"])
    assert retry == child and child["status"] == "accepted"
    assert scope.background_agents == [child["session_id"]]
    args = {"agent_ids": [child["session_id"]], "timeout_seconds": 0}
    status = await capability.tools.call_tool("wait_agent", args, ctx, tools["wait_agent"])
    assert status == {
        "agents": [{"session_id": child["session_id"], "status": "running"}], "timed_out": True,
    }
    assert scope.background_agents == [child["session_id"]]
    await runtime.database.stop(actor, child["session_id"])
    status = await capability.tools.call_tool("wait_agent", args, ctx, tools["wait_agent"])
    assert status["agents"][0]["status"] == "stopped" and not status["timed_out"]
    assert scope.background_agents == []
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(DelegatedSession)))) == 1


def test_automatic_report_is_bounded_without_mutating_saved_result():
    report = {"session_id": "child", "status": "completed", "output": "evidence " * 2000}
    message = InteractiveDelegation.report_message([report])
    assert "Truncated" in message.parts[0].content
    assert len(message.parts[0].content) < 7000
    assert report["output"] == "evidence " * 2000


async def test_parent_question_resumes_preserve_children_without_granting_ordinary_followup_access(
    runtime, actor, active,
):
    from test_delegated_sessions import finish

    parent, original_turn, prepared = active
    capability = InteractiveDelegation(runtime.database)
    child = await capability.service.start(prepared.scope, "child", "Original assignment")
    prepared.scope.background_delegation = True
    prepared.scope.background_agents = [child["session_id"]]

    async def pause(context):
        assert await runtime.database.save_result(
            context.scope, conversation=ConversationTypeAdapter.dump_json(Conversation(
                conversation_id=context.scope.conversation_id,
            )).decode(), output="", workspace=None, usage={}, deferred=True,
        )

    await pause(prepared)
    resumed = await runtime.database.resume(actor, parent.id, original_turn.id, "answer-one", {"calls": {}})
    claimed = await runtime.database.claim_batch(10)
    child_turn = next(turn for turn in claimed if turn.session_id == child["session_id"])
    child_context = await runtime.database.prepare(child_turn.id)
    context = await runtime.database.prepare(resumed.id)
    assert context.scope.background_agents == [child["session_id"]]
    assert (await capability.reports(context.scope, [child["session_id"]]))[0]["status"] == "running"
    await pause(context)
    resumed_again = await runtime.database.resume(actor, parent.id, resumed.id, "answer-two", {"calls": {}})
    await runtime.database.claim_next()
    context = await runtime.database.prepare(resumed_again.id)
    assert context.scope.background_agents == [child["session_id"]]
    await finish(runtime, child_context, "Child completed during the question.")
    reports = await capability.reports(context.scope, [child["session_id"]])
    assert reports[0]["status"] == "completed"
    assert reports[0]["output"] == "Child completed during the question."
    capability.acknowledge(context.scope, reports)
    await finish(runtime, context, "Combined answer after human input.")
    async with runtime.database.sessions() as db:
        # Resume access never rewrites the identity of the original admission.
        link = await db.get(DelegatedSession, child["session_id"])
        assert link.parent_turn_id == original_turn.id
    followup = await runtime.database.submit(actor, parent.id, NewMessage(
        prompt="An unrelated later task", request_key="ordinary-followup",
    ))
    await runtime.database.claim_next()
    later = await runtime.database.prepare(followup.id)
    assert not later.scope.background_agents
    assert (await capability.reports(later.scope, [child["session_id"]]))[0]["status"] == "unavailable"
