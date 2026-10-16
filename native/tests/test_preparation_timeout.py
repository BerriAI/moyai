"""Recorded preparation activity policy and compatibility with pre-change history."""

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.models import Turn
from moyai.product_continuation_errors import MESSAGES, PREPARATION_BLOCKED_TYPE
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import PREPARE_VERSION, TurnWorkflow


@pytest.mark.parametrize("old_history", [False, True])
async def test_preparation_policy_and_old_history_replay(runtime, active, monkeypatch, old_history):
    _, turn, _ = active
    original_patched = workflow.patched
    if old_history:
        # Execute the exact former scheduling branch without recording its new marker.
        def before_preparation_change(identity, **kwargs):
            return False if identity == PREPARE_VERSION else original_patched(identity, **kwargs)
        monkeypatch.setattr(workflow, "patched", before_preparation_change)

    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
            history = await handle.fetch_history()
        preparation = [
            event.activity_task_scheduled_event_attributes for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
            and event.activity_task_scheduled_event_attributes.activity_type.name == "moyai_prepare_turn_v1"
        ]
        assert len(preparation) == 1
        recorded = preparation[0]
        assert recorded.start_to_close_timeout.seconds == (60 if old_history else 1200)
        assert recorded.schedule_to_close_timeout.seconds == (180 if old_history else 1200)
        assert recorded.heartbeat_timeout.seconds == (0 if old_history else 30)
        assert recorded.retry_policy.maximum_attempts == 3
        monkeypatch.setattr(workflow, "patched", original_patched)
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)


@pytest.mark.parametrize("old_history", [False, True])
@pytest.mark.parametrize("known", [False, True])
async def test_preparation_notice_is_allowlisted_and_replay_compatible(runtime, active, monkeypatch, old_history, known):
    _, turn, _ = active
    code = "recovery_collection_incomplete" if known else "untrusted-provider-code"
    async def blocked(_turn_id):
        raise ApplicationError("private-provider-detail", code, type=PREPARATION_BLOCKED_TYPE, non_retryable=True)
    monkeypatch.setattr(runtime.database, "prepare", blocked)
    original_patched = workflow.patched
    if old_history:
        def before_notice(identity, **kwargs):
            return False if identity == "moyai-public-preparation-errors-v1" else original_patched(identity, **kwargs)
        monkeypatch.setattr(workflow, "patched", before_notice)
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 15)
            history = await handle.fetch_history()
        async with runtime.database.sessions() as db:
            row = await db.get(Turn, turn.id)
            assert row.status == "failed"
            if known and not old_history:
                assert row.error == MESSAGES[code]
            else:
                assert row.error == (
                    "The agent could not complete this turn. "
                    "Inspect the saved activity before repeating a possibly completed action."
                )
            assert "private-provider-detail" not in row.error
        scheduled = [
            event.activity_task_scheduled_event_attributes.activity_type.name for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
        ]
        assert scheduled.count("moyai_prepare_turn_v1") == 1
        assert not any("model_request" in name for name in scheduled)
        monkeypatch.setattr(workflow, "patched", original_patched)
        await Replayer(
            interceptors=[ActivityCapacity()],
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
