"""An expected credential refusal crosses Temporal as a tool result, not a crash."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_bundled_skills_startup import scripted_model
from test_product_credential_execution import request_body

from moyai.agent import make_agent
from moyai.product_credential_tools import capability
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


async def test_refused_credential_request_does_not_crash_the_agent_or_replay_a_write(runtime, active):
    _session, turn, _prepared = active
    service = SimpleNamespace(request=AsyncMock(side_effect=HTTPException(403, "Private server details")))

    def respond(messages, _info):
        results = [part for message in messages for part in message.parts
                   if isinstance(part, ToolReturnPart) and part.tool_name == "credentials_request"]
        if not results:
            return ModelResponse(parts=[ToolCallPart("credentials_request", request_body().model_dump(mode="json"),
                                                    tool_call_id="rejected-request")])
        assert results[-1].outcome == "failed"
        assert "Private server details" not in str(results[-1].content)
        return ModelResponse(parts=[TextPart("I can continue without that credential.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
        product_capabilities=[capability(service)],
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(TurnWorkflow.run, turn.id, id=turn.workflow_id,
                                                         task_queue=runtime.settings.temporal_queue)
            assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
            history = await handle.fetch_history()
        await Replayer(interceptors=[ActivityCapacity()], workflows=[TurnWorkflow],
                       plugins=[PydanticAIPlugin()], data_converter=converter).replay_workflow(history)
    service.request.assert_awaited_once()
