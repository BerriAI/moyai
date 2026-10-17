"""Real SDK/Temporal regression coverage; live-model proof is separate."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage, UsageLimits
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_bundled_skills_startup import scripted_model

from moyai.agent import make_agent
from moyai.request_limits import VERSION, UnlimitedModelRequests
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow


def loop_model(*, delegated: bool, rounds: int, calls: Counter):
    def respond(messages, info):
        prompts = " ".join(
            str(part.content) for message in messages for part in message.parts
            if isinstance(part, UserPromptPart)
        )
        child = "DELEGATE_REQUEST_LIMIT_PROBE" in prompts
        lane = "child" if child else "parent"
        calls[lane] += 1
        if delegated and not child:
            returned = any(
                isinstance(part, ToolReturnPart) and part.tool_name == "delegate_task"
                for message in messages for part in message.parts
            )
            if not returned:
                return ModelResponse(parts=[ToolCallPart(
                    "delegate_task",
                    {"agent_name": "self", "task": "DELEGATE_REQUEST_LIMIT_PROBE"},
                    tool_call_id="delegate-probe",
                )])
            return ModelResponse(parts=[TextPart("Delegation completed.")])
        if calls[lane] <= rounds:
            return ModelResponse(parts=[ToolCallPart(
                "read_plan", {}, tool_call_id=f"{lane}-read-{calls[lane]}",
            )])
        return ModelResponse(parts=[TextPart("All requested rounds completed.")])

    return scripted_model(respond)


@pytest.mark.parametrize("delegated,rounds", [(False, 101), (True, 51)])
async def test_parent_and_delegate_cross_previous_request_caps(runtime, active, delegated, rounds):
    _, turn, _ = active
    calls = Counter()
    runtime.models = {"test": loop_model(delegated=delegated, rounds=rounds, calls=calls)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
        skills_directory="skills",
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 300))["status"] == "completed"
            history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert calls["child" if delegated else "parent"] == rounds + 1
    if delegated:
        assert calls["parent"] == 2


async def test_non_request_usage_limits_still_stop_execution():
    agent = Agent(
        FunctionModel(lambda messages, info: ModelResponse(
            parts=[TextPart("A response with measured usage.")],
            usage=RequestUsage(input_tokens=10, output_tokens=2),
        )),
        capabilities=[UnlimitedModelRequests()],
    )
    with pytest.raises(UsageLimitExceeded, match="input_tokens_limit"):
        await agent.run("Count the measured usage.", usage_limits=UsageLimits(input_tokens_limit=1))


@pytest.mark.parametrize("delegated", [False, True])
async def test_pre_change_failure_history_replays_with_original_caps(runtime, active, monkeypatch, delegated):
    _, turn, _ = active
    calls = Counter()
    # A shorter configured parent cap keeps the legacy failure/replay case small.
    # Delegates must exercise the SDK's actual implicit 50-request cap.
    runtime.settings.model_request_limit = 3 if not delegated else 100
    runtime.models = {"test": loop_model(delegated=delegated, rounds=51 if delegated else 4, calls=calls)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
        skills_directory="skills",
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    original = workflow.patched
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as previous:
            previous.setattr(workflow, "patched", lambda identity: False if identity == VERSION else original(identity))
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                with pytest.raises(WorkflowFailureError, match="Workflow execution failed"):
                    await asyncio.wait_for(handle.result(), 300)
                history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert calls["child" if delegated else "parent"] == (49 if delegated else 3)
