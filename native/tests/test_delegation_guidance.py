"""Interactive delegation must honor the same steering fence as other tools."""

import asyncio
import json

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import DeltaToolCall
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment
from test_delegation_temporal import dispatching

from moyai.contracts import NewMessage
from moyai.delegation_models import DelegatedSession
from moyai.models import Turn
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


async def test_accepted_steering_prevents_stale_child_admission(runtime, actor, active):
    session, turn, _ = active
    entered, release = asyncio.Event(), asyncio.Event()

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
        assert not list(await db.scalars(select(DelegatedSession)))
        assert (await db.get(Turn, correction.id)).status == "included"
