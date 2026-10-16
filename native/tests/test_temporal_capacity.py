"""Real Temporal replay and activity-capacity isolation; no provider claims."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from moyai.activities import ACTIVITIES
from moyai.agent import HarnessActivityContext, make_agent
from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.execution import ExecutionAuthority
from moyai.product_drain import drain_checkpoint
from moyai.runtime import configure
from moyai.settings_runtime import SETTINGS_ACTIVITIES, SETTINGS_WORKFLOWS
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ROUTING_VERSION, ActivityCapacity, activity_pool
from moyai.workflow import TurnWorkflow


class CapacityScenario:
    def __init__(self):
        self.tool_started = asyncio.Event()
        self.release_tool = asyncio.Event()
        self.model_calls = 0
        self.computer: Path | None = None

    async def respond(self, messages, info):
        self.model_calls += 1
        prompt = next(
            str(part.content) for message in messages for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        returned = any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts)
        if prompt == "slow" and not returned:
            if self.computer is not None:
                return ModelResponse(parts=[ToolCallPart("shell", {
                    "command": "touch capacity-started; while [ ! -f capacity-release ]; do sleep .05; done",
                    "timeout": 25,
                }, tool_call_id="one-wait")])
            return ModelResponse(parts=[ToolCallPart("wait_tool", {}, tool_call_id="one-wait")])
        return ModelResponse(parts=[TextPart("Completed.")])

    async def stream(self, messages, info):
        for index, part in enumerate((await self.respond(messages, info)).parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}

    async def wait_tool(self) -> str:
        self.tool_started.set()
        await self.release_tool.wait()
        return "Tool released."

    async def wait_started(self):
        if self.computer is None:
            await self.tool_started.wait()
        else:
            while not (self.computer / "capacity-started").exists():
                await asyncio.sleep(.02)

    def release(self):
        self.release_tool.set()
        if self.computer is not None:
            (self.computer / "capacity-release").touch()

    def install(self, runtime, *, default_capabilities=False):
        model = FunctionModel(self.respond, stream_function=self.stream)
        runtime.models = {"test": model}
        if default_capabilities:
            self.computer = runtime.settings.data_dir.parent / "computer"
            runtime.agent, runtime.durability = make_agent(
                runtime.settings, runtime.database, runtime.stores, runtime.objects,
                models=runtime.models, workspace=LocalWorkspace(self.computer),
            )
            return
        runtime.durability = TemporalDurability(
            models=runtime.models, run_context_type=HarnessActivityContext,
            activity_config={
                "start_to_close_timeout": timedelta(seconds=30),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            },
        )
        tools = FunctionToolset(id="capacity")
        tools.add_function(self.wait_tool)
        runtime.agent = Agent(
            model, name="moyai", deps_type=RunScope, toolsets=[tools], capabilities=[runtime.durability],
        )


async def queued(runtime, actor, prompt):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt=prompt, request_key=prompt),
    )
    assert (await runtime.database.claim_next()).id == turn.id
    return turn


def scheduled_queues(history):
    return [
        event.activity_task_scheduled_event_attributes.task_queue.name
        for event in history.events if event.HasField("activity_task_scheduled_event_attributes")
    ]


def routed_marker(history):
    return any(
        ROUTING_VERSION.encode() in event.marker_recorded_event_attributes.SerializeToString()
        for event in history.events if event.HasField("marker_recorded_event_attributes")
    )


@pytest.mark.parametrize("default_capabilities", [False, True])
async def test_blocked_tool_cannot_consume_model_or_control_capacity_and_new_history_replays(
    runtime, actor, default_capabilities,
):
    scenario = CapacityScenario()
    scenario.install(runtime, default_capabilities=default_capabilities)
    runtime.settings.temporal_worker_activities = 1
    runtime.settings.temporal_workflow_cache_size = 0
    slow, fast = await queued(runtime, actor, "slow"), await queued(runtime, actor, "fast")
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        pools = make_worker(runtime)
        assert len(pools.workers) == 4
        for worker in pools.workers:
            assert worker.config()["max_concurrent_activities"] == 1
            assert worker.config()["max_cached_workflows"] == 0
        async with pools:
            slow_handle = await runtime.client.start_workflow(
                TurnWorkflow.run, slow.id, id=slow.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            try:
                await asyncio.wait_for(scenario.wait_started(), 15)
                fast_handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, fast.id, id=fast.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                try:
                    result = await asyncio.wait_for(fast_handle.result(), 10)
                except TimeoutError as error:
                    pending = await fast_handle.fetch_history()
                    scheduled = [
                        (event.activity_task_scheduled_event_attributes.activity_type.name,
                         event.activity_task_scheduled_event_attributes.task_queue.name)
                        for event in pending.events if event.HasField("activity_task_scheduled_event_attributes")
                    ]
                    raise AssertionError(f"Fast turn blocked after scheduling: {scheduled}") from error
                assert result["status"] == "completed"
                assert not scenario.release_tool.is_set()
                if default_capabilities:
                    fast_history = await fast_handle.fetch_history()
                    scheduled = {
                        event.activity_task_scheduled_event_attributes.activity_type.name:
                        event.activity_task_scheduled_event_attributes.task_queue.name
                        for event in fast_history.events
                        if event.HasField("activity_task_scheduled_event_attributes")
                    }
                    for name in (
                        "agent__moyai__capability__planning__read_plan",
                        "agent__moyai__capability__moyai_authority__check",
                        "agent__moyai__capability__memory__load_snapshot",
                    ):
                        assert scheduled[name] == runtime.settings.temporal_queue + "-control"
            finally:
                scenario.release()
            assert (await asyncio.wait_for(slow_handle.result(), 15))["status"] == "completed"
            history = await slow_handle.fetch_history()
        assert set(scheduled_queues(history)) == {
            f"{runtime.settings.temporal_queue}-{pool}" for pool in ("model", "tools", "control")
        }
        assert routed_marker(history)
        calls = scenario.model_calls
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
        assert scenario.model_calls == calls
        assert not pools.is_running


def test_workspace_receipt_and_no_computer_settlement_use_control_capacity():
    assert activity_pool("agent__moyai__capability__moyai_authority__save_workspace") == "control"
    assert activity_pool("moyai_save_computer_v1", ["turn", None]) == "control"
    assert activity_pool("moyai_save_computer_v1", ["turn", {"provider": "local"}]) == "control"
    assert activity_pool("moyai_save_computer_v1", ["turn", {"provider": "modal"}]) == "tools"


async def test_existing_unsplit_native_history_replays_with_versioned_routing(runtime, actor):
    scenario = CapacityScenario()
    scenario.install(runtime)
    turn = await queued(runtime, actor, "slow")
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        configure(runtime)
        TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
        # Exact 3ae6c5d worker/TurnWorkflow routing: no outbound interceptor,
        # one queue for SDK activities and application activities.
        async with Worker(
            runtime.client, task_queue=runtime.settings.temporal_queue,
            workflows=[TurnWorkflow, *SETTINGS_WORKFLOWS],
            activities=[*ACTIVITIES, drain_checkpoint, *SETTINGS_ACTIVITIES],
            interceptors=[ExecutionAuthority(runtime.database)],
            max_concurrent_activities=32, max_concurrent_workflow_tasks=16,
        ):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            try:
                await asyncio.wait_for(scenario.tool_started.wait(), 15)
                pending_history = await handle.fetch_history()
                calls = scenario.model_calls
                await Replayer(
                    workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
                    interceptors=[ActivityCapacity()],
                ).replay_workflow(pending_history)
                assert scenario.model_calls == calls
            finally:
                scenario.release()
            assert (await asyncio.wait_for(handle.result(), 15))["status"] == "completed"
            history = await handle.fetch_history()
        assert set(scheduled_queues(history)) == {runtime.settings.temporal_queue}
        assert not routed_marker(history)
        calls = scenario.model_calls
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
        assert scenario.model_calls == calls


async def test_split_workers_register_one_owner_before_polling_and_all_become_ready(runtime):
    async with await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()]) as environment:
        runtime.client = environment.client
        pools = make_worker(runtime)

        async def register():
            assert not any(worker.is_running for worker in pools.workers)

        pools.register = AsyncMock(side_effect=register)
        async with pools:
            async with asyncio.timeout(5):
                while not pools.is_running:
                    await asyncio.sleep(.01)
            pools.register.assert_awaited_once()
        assert not pools.is_running


async def test_repeated_cancellation_retires_all_real_temporal_pollers(runtime):
    async with await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()]) as environment:
        runtime.client = environment.client
        pools = make_worker(runtime)
        ready = asyncio.Event()
        released = []

        async def lifetime():
            try:
                async with pools:
                    async with asyncio.timeout(5):
                        while not pools.is_running:
                            await asyncio.sleep(.01)
                    ready.set()
                    await asyncio.Event().wait()
            finally:
                released.append(tuple(worker.is_running for worker in pools.workers))

        task = asyncio.create_task(lifetime())
        await asyncio.wait_for(ready.wait(), 5)
        for _ in range(5):
            task.cancel()
            await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert released == [(False, False, False, False)]
