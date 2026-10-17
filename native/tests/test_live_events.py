from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from test_delegation_temporal import dispatching

from moyai.live_events import PUBLIC_TOPIC
from moyai.presentation import public_event
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


@pytest.mark.parametrize("runtime", ["stream"], indirect=True)
async def test_native_workflow_stream_scopes_root_text_and_model_rounds(runtime, active):
    _session, turn, _prepared = active
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    )
    seen = []
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime), dispatching(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )

            async def consume():
                async with runtime.durability.stream_agent_events(
                    runtime.client, handle, topic=PUBLIC_TOPIC,
                ) as events:
                    async for event in events:
                        if item := public_event(event):
                            seen.append(item)

            consumer = asyncio.create_task(consume())
            result = await asyncio.wait_for(handle.result(), 45)
            await asyncio.wait_for(consumer, 10)
    assert result["status"] == "completed"
    text = "".join(item["text"] for item in seen if item["type"] in {"text_start", "text_delta"})
    assert "Native capabilities completed." in text
    assert "native-child-result" not in text
    rounds = [item for item in seen if item["type"] == "response_start"]
    assert len(rounds) >= 2
    assert all(item["run_id"] == turn.id for item in rounds)
    assert [item["step"] for item in rounds] == sorted({item["step"] for item in rounds})
    assert any(item["type"] == "tool_call" and item["name"] == "delegate_task" for item in seen)
