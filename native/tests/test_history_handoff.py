"""Real Temporal/SDK regressions; scripted models are not the live-app demo."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter, ModelRetry
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_bundled_skills_startup import scripted_model
from test_computer_startup import lazy_local_workspace
from test_delegation_temporal import dispatching

from moyai.agent import make_agent
from moyai.contracts import NewMessage
from moyai.history_handoff import AgentCheckpoint, CheckpointSave, is_turn_run, latest_turn_snapshot
from moyai.live_events import PUBLIC_TOPIC, ResponseStart
from moyai.models import Effect, Event, Session, Turn
from moyai.presentation import saved_activity
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("runtime", ["history"], indirect=True)
async def test_rollover_preserves_tool_results_workspace_usage_and_replay(runtime, active):
    session, turn, _ = active
    runtime.settings.history_continue_bytes = 16384
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            first_run = handle.result_run_id
            streamed = []

            async def observe():
                offset = 0
                while True:
                    previous = offset
                    async with runtime.durability.stream_agent_events(
                        runtime.client, handle, topic=PUBLIC_TOPIC, from_offset=offset,
                    ) as events:
                        async for event in events:
                            streamed.append((events.offset, event))
                            offset = events.offset + 1
                            if isinstance(event, AgentRunResultEvent):
                                return
                    if offset == previous:
                        return

            observer = asyncio.create_task(observe())
            result = await asyncio.wait_for(handle.result(), timeout=90)
            await asyncio.wait_for(observer, 15)
            histories = []
            run_id = first_run
            while run_id:
                history = await runtime.client.get_workflow_handle(turn.workflow_id, run_id=run_id).fetch_history()
                histories.append(history)
                last = history.events[-1]
                run_id = (
                    last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                    if last.HasField("workflow_execution_continued_as_new_event_attributes") else None
                )
        assert result["status"] == "completed"
        assert len(histories) >= 3
        for history in histories:
            await Replayer(
                interceptors=[ActivityCapacity()], workflows=[TurnWorkflow],
                plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(history)
        assert (await runtime.client.get_workflow_handle(turn.workflow_id).describe()).status == (
            WorkflowExecutionStatus.COMPLETED
        )
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        workspace = await db.get(Session, session.id)
        effects = list(await db.scalars(select(Effect).where(Effect.turn_id == turn.id)))
        segments = list(await db.scalars(select(Event).where(
            Event.turn_id == turn.id, Event.kind == "history_segment",
        )))
    assert saved.status == "completed"
    assert [offset for offset, _ in streamed] == sorted({offset for offset, _ in streamed})
    steps = [event.step for _, event in streamed if isinstance(event, ResponseStart)]
    assert steps == sorted(set(steps)) and len(steps) == 8
    assert json.loads(saved.usage)["requests"] == 8
    assert len(effects) == 6 and all(row.state == "confirmed" for row in effects)
    assert len(segments) == len(histories) - 1
    reference = TypeAdapter(WorkspaceRef).validate_json(workspace.workspace)
    assert (Path(reference.id) / "handoff-ledger.txt").read_text().splitlines() == [
        "1", "2", "3", "4", "5", "6",
    ]
    snapshot = await latest_turn_snapshot(runtime, turn.id)
    assert snapshot is not None
    calls = saved_activity(snapshot.messages, turn.id)
    assert sum(item.get("type") == "tool_call" for item in calls) == 7


@pytest.mark.parametrize("runtime", ["tools"], indirect=True)
async def test_rollover_preserves_loaded_capabilities_and_persistent_delegation(runtime, active):
    _session, turn, _ = active
    runtime.settings.history_continue_bytes = 16384
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)),
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            result = await asyncio.wait_for(runtime.client.execute_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            ), timeout=90)
    assert result["status"] == "completed"
    snapshot = await latest_turn_snapshot(runtime, turn.id)
    assert snapshot is not None
    assert "native-child-result" in str(snapshot.messages)
    assert any(is_turn_run(message.run_id, turn.id) and message.run_id != turn.id for message in snapshot.messages)


@pytest.mark.parametrize("run_id,expected", [
    ("turn", True), ("turn:segment:1", True), ("turn:segment:100", True),
    ("turn:segment:0", False), ("other:segment:1", False), ("turn:segment:evil", False),
])
def test_segment_identity_is_scoped_to_its_turn(run_id, expected):
    assert is_turn_run(run_id, "turn") is expected


@pytest.mark.parametrize("action", ["steer", "steer_twice", "stop", "failed_steer"])
@pytest.mark.parametrize("runtime", ["history"], indirect=True)
async def test_control_during_checkpoint_commit_is_not_lost(runtime, active, actor, monkeypatch, action):
    from moyai import history_handoff, worker

    entered, release = asyncio.Event(), asyncio.Event()
    save = history_handoff.save_history_checkpoint

    @activity.defn(name="moyai_save_history_checkpoint_v1")
    async def hold_after_commit(checkpoint: AgentCheckpoint) -> CheckpointSave:
        reference = await save(checkpoint)
        messages = ConversationTypeAdapter.validate_json(checkpoint.conversation).messages
        if "history-append-1" in str(messages) and not entered.is_set():
            entered.set()
            await release.wait()
            if action == "failed_steer":
                raise ApplicationError("Checkpoint acknowledgment was lost.", non_retryable=True)
        return reference

    monkeypatch.setattr(worker, "ACTIVITIES", [
        hold_after_commit if item is save else item for item in worker.ACTIVITIES
    ])
    runtime.settings.history_continue_bytes = 16384
    session, turn, _ = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)),
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            await asyncio.wait_for(entered.wait(), 45)
            if action in {"steer", "steer_twice", "failed_steer"}:
                for index in range(2 if action == "steer_twice" else 1):
                    correction = await runtime.database.submit(actor, session.id, NewMessage(
                        prompt=f"NEW GUIDANCE {index}", request_key=f"at-handoff-{index}", mode="asap",
                    ))
                    await runtime.database.signal_attempted(correction.id)
                    await runtime.client.get_workflow_handle(turn.workflow_id).signal(TurnWorkflow.steer, correction.id)
                release.set()
                result = await asyncio.wait_for(handle.result(), 45)
                assert result["status"] == "completed"
                async with runtime.database.sessions() as db:
                    saved = await db.get(Turn, turn.id)
                    received = await db.get(Turn, correction.id)
                assert received.status == "included"
                assert runtime.security.unseal(saved.result).decode() == "Guidance preserved after handoff."
                snapshot = await latest_turn_snapshot(runtime, turn.id)
                dispatched = [part for message in snapshot.messages for part in message.parts
                              if isinstance(part, ToolReturnPart) and part.tool_call_id == "after-new-guidance"]
                assert len(dispatched) == 1 and dispatched[0].outcome == "success"
            else:
                await runtime.client.get_workflow_handle(turn.workflow_id).cancel()
                release.set()
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 45)
                async with runtime.database.sessions() as db:
                    saved = await db.get(Turn, turn.id)
                assert saved.status == "stopped"
    assert (runtime.settings.data_dir.parent / "computer/handoff-ledger.txt").read_text() == "1\n"


@pytest.mark.parametrize("outcome", ["saved", "pending_effect", "storage_error"])
async def test_checkpoint_without_a_computer_and_refused_saves_keep_agent_alive(
    runtime, active, monkeypatch, outcome,
):
    from moyai import history_handoff, worker

    session, turn, prepared = active
    allocations = []
    attempts = 0

    def respond(messages, info):
        count = sum(
            isinstance(part, ToolReturnPart) and part.tool_name == "read_plan"
            for message in messages for part in message.parts
        )
        if count < 3:
            return ModelResponse(parts=[ToolCallPart(
                "read_plan", {}, tool_call_id=f"plan-read-{count}",
            )])
        return ModelResponse(parts=[TextPart("Completed without a computer.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=lazy_local_workspace(runtime.settings.data_dir.parent / "computer", allocations),
    )
    save = history_handoff.save_history_checkpoint

    @activity.defn(name="moyai_save_history_checkpoint_v1")
    async def save_or_refuse(checkpoint: AgentCheckpoint) -> CheckpointSave:
        nonlocal attempts
        attempts += 1
        assert checkpoint.workspace is None
        if outcome == "storage_error":
            raise ApplicationError("Object storage is temporarily unavailable.", non_retryable=True)
        if outcome == "pending_effect" and attempts == 1:
            async with runtime.database.sessions.begin() as db:
                db.add(Effect(
                    id="uncertain-effect", session_id=session.id, turn_id=turn.id,
                    actor_id=prepared.scope.actor_id, tool_name="shell",
                    arguments=runtime.security.seal({}), fingerprint="a" * 64,
                ))
        result = await save(checkpoint)
        if outcome == "pending_effect":
            assert result.reference is None
            async with runtime.database.sessions() as db:
                effect = await db.get(Effect, "uncertain-effect")
                assert effect is not None and effect.state == "pending"
        return result

    monkeypatch.setattr(worker, "ACTIVITIES", [
        save_or_refuse if item is save else item for item in worker.ACTIVITIES
    ])
    runtime.settings.history_continue_bytes = 16384
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            first_run = handle.result_run_id
            result = await asyncio.wait_for(handle.result(), 60)
            final = await runtime.client.get_workflow_handle(turn.workflow_id).describe()
        assert result["status"] == "completed"
        assert (final.run_id != first_run) is (outcome == "saved")
    assert attempts >= 1 and allocations == []
    async with runtime.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        effect = await db.get(Effect, "uncertain-effect")
    assert runtime.security.unseal(saved.result).decode() == "Completed without a computer."
    assert json.loads(saved.usage)["requests"] == 4
    if outcome == "pending_effect":
        # The receipt blocks rollover while the agent is running, then settles
        # as a failed attempt when its final answer is accepted.
        assert effect is not None and effect.state == "failed"
        assert effect.settled_at is not None and effect.resolution is None


@pytest.mark.parametrize("runtime", ["history_retry"], indirect=True)
async def test_rollover_does_not_grant_a_new_tool_retry_budget(runtime, active):
    calls = 0

    @runtime.agent.tool_plain(retries=2)
    def retry_target() -> str:
        nonlocal calls
        calls += 1
        raise ModelRetry("The requested operation cannot be completed.")

    runtime.settings.history_continue_bytes = 16384
    _session, turn, _ = active
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()],
        data_converter=DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects)),
    )
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(runtime.client.execute_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                ), 45)
    assert calls == 3


async def test_pre_handoff_history_replays_without_new_commands(runtime, active, monkeypatch):
    from moyai.history_handoff import VERSION

    _session, turn, _ = active
    original = workflow.patched
    monkeypatch.setattr(workflow, "patched", lambda version: False if version == VERSION else original(version))
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            await asyncio.wait_for(handle.result(), 30)
            history = await handle.fetch_history()
        monkeypatch.setattr(workflow, "patched", original)
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=[TurnWorkflow],
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
