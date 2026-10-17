"""An admitted command survives a lost reply; it does not admit new work."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import ExecuteActivityInput, Replayer
from test_delegation_temporal import dispatching

from moyai.contracts import NewMessage
from moyai.delegation import DelegationService, InteractiveDelegation
from moyai.delegation_models import DelegatedSession
from moyai.execution import AuthorizedActivity
from moyai.models import Turn, User
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

START = "agent__moyai__capability__moyai_interactive_delegation__start"


@pytest.mark.parametrize("change", ["guidance", "stop", "delete", "disable"])
async def test_retry_recovers_only_the_original_receipt_through_activity_authorization(
    runtime, actor, active, monkeypatch, change,
):
    session, turn, prepared = active
    database = runtime.database
    service = DelegationService(database)
    child = await service.start(prepared.scope, "call", "Original work")
    if change == "guidance":
        correction = await database.submit(actor, session.id, NewMessage(
            prompt="New guidance", request_key="correction", mode="asap",
        ))
        await database.steering_receipt(correction.id, "enqueue")
    elif change == "stop":
        await database.stop(actor, session.id)
    elif change == "delete":
        await database.delete_session(actor, session.id)
    else:
        async with database.sessions.begin() as db:
            (await db.get(User, actor.id)).enabled = False

    info = SimpleNamespace(activity_type=START)
    monkeypatch.setattr("moyai.execution.activity.info", lambda: info)

    class Next:
        async def execute_activity(self, _):
            return await InteractiveDelegation(database).start(
                prepared.scope, "call", "Original work", ctx=RunContext(
                    deps=prepared.scope, run_id=turn.id, model=TestModel(), usage=RunUsage(),
                ),
            )

    interceptor = AuthorizedActivity(Next(), database)
    request = ExecuteActivityInput(fn=lambda: None, args=[prepared.scope], executor=None, headers={})
    assert await interceptor.execute_activity(request) == child
    with pytest.raises((PermissionError, HTTPException)):
        await service.start(prepared.scope, "new-call", "New work")
    with pytest.raises(ValueError):
        await service.start(prepared.scope, "call", "Changed work")
    with pytest.raises(PermissionError):
        await service.start(replace(prepared.scope, session_id="other"), "call", "Original work")
    # Returning an ID/name receipt must not authorize model execution or content
    # reads after Stop/deletion/revocation.
    if change != "guidance":
        info.activity_type = "agent__moyai__capability__moyai_interactive_delegation__report"
        with pytest.raises(ApplicationError):
            await interceptor.execute_activity(request)
    async with database.sessions() as db:
        assert len(list(await db.scalars(select(DelegatedSession)))) == 1


async def test_temporal_retries_a_lost_admission_reply_after_new_guidance_and_replays(runtime, actor, active, monkeypatch):
    session, turn, _ = active
    service_start = DelegationService.start
    admitted, release = asyncio.Event(), asyncio.Event()
    receipts = []
    attempts = []

    async def lose_first_reply(self, scope, call_id, task, **kwargs):
        attempts.append(call_id)
        result = await service_start(self, scope, call_id, task, **kwargs)
        receipts.append(result)
        if len(receipts) == 1:
            admitted.set()
            await release.wait()
            raise RuntimeError("Simulated lost activity reply after the admission committed.")
        return result

    monkeypatch.setattr(DelegationService, "start", lose_first_reply)

    async def stream(messages, info):
        prompts = "\n".join(str(part.content) for message in messages for part in message.parts
                            if isinstance(part, UserPromptPart))
        if "RECOVERED_ASSIGNMENT" in prompts:
            yield "Child completed its admitted work."
        elif any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield "Recovered the original child."
        else:
            yield {0: DeltaToolCall(
                name="delegate_task", tool_call_id="recover-call",
                json_args=json.dumps({"agent_name": "self", "task": "RECOVERED_ASSIGNMENT"}),
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
                await asyncio.wait_for(admitted.wait(), 20)
                correction = await runtime.database.submit(actor, session.id, NewMessage(
                    prompt="Continue waiting for the already admitted child. Do not create another.",
                    request_key="correction", mode="asap",
                ))
                await handle.signal(TurnWorkflow.steer, correction.id)
                async with asyncio.timeout(20):
                    while True:
                        async with runtime.database.sessions() as db:
                            if (await db.get(Turn, correction.id)).status == "accepted":
                                break
                        await asyncio.sleep(.05)
            finally:
                release.set()
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert attempts == ["recover-call", "recover-call"]
    assert len(receipts) == 2 and receipts[0] == receipts[1]
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(DelegatedSession)))) == 1
