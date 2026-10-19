"""Interactive delegation must honor the same steering fence as other tools."""

import asyncio
import json

import pytest
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_delegation_temporal import dispatching

from moyai.contracts import GuidanceChanged, NewMessage, RunScope
from moyai.delegation import DelegationService, InteractiveDelegation
from moyai.delegation_models import DelegatedSession
from moyai.execution import AuthorizedActivity
from moyai.models import Turn
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("missing_context_identity", [False, True])
async def test_accepted_steering_prevents_stale_child_admission(
    runtime, actor, active, monkeypatch, missing_context_identity,
):
    session, turn, _ = active
    entered, release = asyncio.Event(), asyncio.Event()
    envelopes = []
    execute = AuthorizedActivity.execute_activity

    async def inspect(self, activity_input):
        if activity.info().activity_type == "agent__moyai__capability__moyai_interactive_delegation__start":
            # Assert the actual pinned SDK transport, not the decorated Python
            # method's signature: envelope first, admitted RunScope second.
            envelope, scope = activity_input.args
            assert not isinstance(envelope, RunScope) and isinstance(scope, RunScope)
            metadata = envelope.serialized_run_context["metadata"]
            assert metadata["moyai_input_id"] == turn.id
            assert envelope.arguments["input_id"] == turn.id
            envelopes.append(envelope)
            if missing_context_identity:
                metadata.pop("moyai_input_id")
        return await execute(self, activity_input)

    monkeypatch.setattr(AuthorizedActivity, "execute_activity", inspect)

    async def stream(messages, info):
        prompts = "\n".join(str(part.content) for message in messages for part in message.parts
                            if isinstance(part, UserPromptPart))
        if "NEW GUIDANCE" in prompts:
            yield "Followed the correction without creating a child."
        elif "STALE_ASSIGNMENT" in prompts:
            yield "This obsolete child should never have been admitted."
        else:
            entered.set()
            await release.wait()
            yield {0: DeltaToolCall(
                name="delegate_task", tool_call_id="stale-delegation",
                json_args=json.dumps({"agent_name": "self", "task": "STALE_ASSIGNMENT"}),
            )}

    runtime.models["test"].stream_function = stream
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            try:
                await asyncio.wait_for(entered.wait(), 20)
                correction = await runtime.database.submit(actor, session.id, NewMessage(
                    prompt="NEW GUIDANCE: do not create any subagents.", mode="asap", request_key="correction",
                ))
                await handle.signal(TurnWorkflow.steer, correction.id)
                async with asyncio.timeout(20):
                    while True:
                        async with runtime.database.sessions() as db:
                            if (await db.get(Turn, correction.id)).status == "accepted":
                                break
                        await asyncio.sleep(0.05)
            finally:
                release.set()
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
    async with runtime.database.sessions() as db:
        assert envelopes
        assert not list(await db.scalars(select(DelegatedSession)))
        assert (await db.get(Turn, correction.id)).status == "included"


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("boundary", ["service", "capability"])
async def test_admission_without_input_identity_cannot_bypass_new_guidance(
    runtime, actor, active, accepted, boundary,
):
    session, turn, prepared = active
    correction = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Do not create a child.", request_key="correction", mode="asap",
    ))
    if accepted:
        await runtime.database.prepare_steering(turn.id, correction.id)
        await runtime.database.steering_receipt(correction.id, "accepted-correction")
    if boundary == "service":
        with pytest.raises(GuidanceChanged):
            await DelegationService(runtime.database).start(prepared.scope, "old-call", "Obsolete work")
    else:
        result = await InteractiveDelegation(runtime.database).start(
            prepared.scope, "old-call", "Obsolete work", ctx=RunContext(
                deps=prepared.scope, run_id=turn.id, model=TestModel(), usage=RunUsage(),
            ),
        )
        assert "error" in result
    async with runtime.database.sessions() as db:
        assert not list(await db.scalars(select(DelegatedSession)))


async def test_admission_uses_latest_input_but_reporting_can_settle_older_work(runtime, actor, active):
    session, turn, prepared = active
    first = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="First correction.", request_key="first", mode="asap",
    ))
    await runtime.database.prepare_steering(turn.id, first.id)
    await runtime.database.steering_receipt(first.id, "first-accepted")
    service = DelegationService(runtime.database)
    child = await service.start(prepared.scope, "current-call", "Current work", input_id=first.id)
    latest = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="A newer correction.", request_key="latest", mode="asap",
    ))
    await runtime.database.prepare_steering(turn.id, latest.id)
    await runtime.database.steering_receipt(latest.id, "latest-accepted")
    with pytest.raises(GuidanceChanged):
        await service.start(prepared.scope, "stale-call", "Obsolete work", input_id=first.id)
    assert await service.report(prepared.scope, child["session_id"]) is None
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(DelegatedSession)))) == 1


@pytest.mark.parametrize("missing", ["context_identity", "explicit_argument", "history_argument"])
async def test_corrected_delegation_survives_activity_formats_and_replays(runtime, actor, active, monkeypatch, missing):
    session, turn, _ = active
    entered, release = asyncio.Event(), asyncio.Event()
    corrected_inputs = []
    execute = AuthorizedActivity.execute_activity

    async def inspect(self, activity_input):
        if activity.info().activity_type == "agent__moyai__capability__moyai_interactive_delegation__start":
            envelope = activity_input.args[0]
            if envelope.arguments["call_id"] == "corrected-delegation":
                identity = envelope.arguments["input_id"]
                assert envelope.serialized_run_context["metadata"]["moyai_input_id"] == identity
                corrected_inputs.append(identity)
                if missing == "context_identity":
                    envelope.serialized_run_context["metadata"].pop("moyai_input_id")
                elif missing == "explicit_argument":
                    # Simulate a pending activity from the prior revision. The
                    # new worker must recover its accepted input from RunContext.
                    envelope.arguments.pop("input_id")
                else:
                    # Pending starts written before context inheritance still
                    # execute without trying to read omitted activity history.
                    envelope.arguments.pop("messages")
        return await execute(self, activity_input)

    monkeypatch.setattr(AuthorizedActivity, "execute_activity", inspect)

    async def stream(messages, info):
        prompts = "\n".join(str(part.content) for message in messages for part in message.parts
                            if isinstance(part, UserPromptPart))
        if "CURRENT_ASSIGNMENT" in prompts:
            yield "The corrected child completed."
        elif "NEW GUIDANCE" in prompts:
            if any(isinstance(part, ToolReturnPart) and part.tool_call_id == "corrected-delegation"
                   for message in messages for part in message.parts):
                yield "Corrected delegation completed."
            else:
                yield {0: DeltaToolCall(
                    name="delegate_task", tool_call_id="corrected-delegation",
                    json_args=json.dumps({"agent_name": "self", "task": "CURRENT_ASSIGNMENT"}),
                )}
        else:
            entered.set()
            await release.wait()
            yield {0: DeltaToolCall(
                name="delegate_task", tool_call_id="stale-delegation",
                json_args=json.dumps({"agent_name": "self", "task": "STALE_ASSIGNMENT"}),
            )}

    runtime.models["test"].stream_function = stream
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            try:
                await asyncio.wait_for(entered.wait(), 20)
                correction = await runtime.database.submit(actor, session.id, NewMessage(
                    prompt="NEW GUIDANCE: delegate the corrected work.", mode="asap", request_key="correction",
                ))
                await handle.signal(TurnWorkflow.steer, correction.id)
                async with asyncio.timeout(20):
                    while True:
                        async with runtime.database.sessions() as db:
                            if (await db.get(Turn, correction.id)).status == "accepted":
                                break
                        await asyncio.sleep(0.05)
            finally:
                release.set()
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert corrected_inputs == [correction.id]
    async with runtime.database.sessions() as db:
        [child] = list(await db.scalars(select(DelegatedSession)))
        assert child.tool_call_id == "corrected-delegation"
