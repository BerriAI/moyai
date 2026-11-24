"""Supplemental SDK checks; real-model application evidence is separate."""

from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem

from moyai.agent import WorkflowPlanning
from moyai.completion import VERSION, CompletionReview
from moyai.contracts import RunScope


async def run_case(respond, store, prompt="Implement and verify the change.", *, history=None):
    capability = CompletionReview(SimpleNamespace(plan=lambda _: store))
    return await Agent(
        FunctionModel(respond), deps_type=RunScope,
        capabilities=[WorkflowPlanning(store=store, enable_subtasks=True), capability],
    ).run(prompt, deps=RunScope("session", "turn", "actor", "test"), run_id="turn", message_history=history)


def reviewed(messages):
    return any(isinstance(message, ModelRequest) and (message.metadata or {}).get(VERSION) for message in messages)


@pytest.mark.parametrize("final_status", ["completed", "blocked"])
async def test_unfinished_current_plan_continues_to_completion_or_a_real_blocker(final_status):
    store = InMemoryPlanStore()
    attempts = []

    def respond(messages, info):
        attempts.append(1)
        returns = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"id": "step-one", "content": "Verify the change", "status": "in_progress"},
            ]})])
        if not reviewed(messages):
            return ModelResponse(parts=[TextPart("I will verify the change.")])
        if returns[-1].tool_name == "write_plan":
            return ModelResponse(parts=[ToolCallPart("update_task_status", {
                "task_id": "step-one", "status": final_status,
            })])
        return ModelResponse(parts=[TextPart("Verified." if final_status == "completed" else "Blocked: test service unavailable.")])

    result = await run_case(respond, store)
    assert len(attempts) == 4
    assert result.output == ("Verified." if final_status == "completed" else "Blocked: test service unavailable.")
    assert (await store.get_items())[0].status == final_status
    assert sum(bool((message.metadata or {}).get(VERSION)) for message in result.all_messages()) == 1


async def test_plan_only_answer_is_reviewed_once_without_forcing_execution():
    store = InMemoryPlanStore()
    calls = []

    def respond(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"content": "Proposed implementation", "status": "pending"},
            ]})])
        return ModelResponse(parts=[TextPart("Here is the proposed plan; no implementation was requested.")])

    result = await run_case(respond, store, "Only propose a plan. Do not implement it.")
    assert len(calls) == 3
    assert result.output.startswith("Here is the proposed plan")
    assert (await store.get_items())[0].status == "pending"


async def test_old_pending_plan_does_not_force_tools_or_reopen_work_for_a_question():
    store = InMemoryPlanStore()
    await store.set_items([PlanItem(content="Old unfinished work")])
    calls = []

    def respond(messages, info):
        calls.append(1)
        return ModelResponse(parts=[TextPart("Four.")])

    result = await run_case(respond, store, "What is two plus two?", history=[
        ModelResponse(run_id="older-turn", parts=[ToolCallPart("write_plan", {})]),
        ModelRequest(run_id="older-turn", parts=[ToolReturnPart("write_plan", "Plan updated.")]),
    ])
    assert result.output == "Four."
    assert len(calls) == 1


async def test_already_completed_plan_needs_no_review():
    store = InMemoryPlanStore()
    calls = []

    def respond(messages, info):
        calls.append(1)
        if len(calls) == 1:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"content": "Verified result", "status": "completed"},
            ]})])
        return ModelResponse(parts=[TextPart("Done.")])

    result = await run_case(respond, store)
    assert result.output == "Done."
    assert len(calls) == 2


@pytest.mark.parametrize("legacy", [False, True])
async def test_completion_review_replays_and_preserves_old_workflow_decisions(runtime, active, monkeypatch, legacy):
    import asyncio

    from pydantic_ai.capabilities import LocalWorkspace
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from temporalio import workflow
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer
    from test_bundled_skills_startup import scripted_model

    from moyai.agent import make_agent
    from moyai.storage import encrypted_converter
    from moyai.worker import make_worker
    from moyai.worker_capacity import ActivityCapacity
    from moyai.workflow import TurnWorkflow

    _, turn, _ = active
    directory = runtime.settings.data_dir.parent / "computer"

    def respond(messages, info):
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("write_plan", {"items": [
                {"id": "proof-step", "content": "Write proof.txt", "status": "in_progress"},
            ]}, tool_call_id="plan")])
        if not reviewed(messages):
            return ModelResponse(parts=[TextPart("I will write the file.")])
        if results[-1].tool_name == "write_plan":
            return ModelResponse(parts=[ToolCallPart("write_file", {
                "path": "proof.txt", "content": "Observed after completion review.",
            }, tool_call_id="write-proof")])
        if results[-1].tool_name == "write_file":
            return ModelResponse(parts=[ToolCallPart("update_task_status", {
                "task_id": "proof-step", "status": "completed",
            }, tool_call_id="complete-plan")])
        return ModelResponse(parts=[TextPart("The requested file was written.")])

    runtime.models = {"test": scripted_model(respond)}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models, workspace=LocalWorkspace(directory), skills_directory="skills",
    )
    converter = encrypted_converter(runtime.security, runtime.objects)
    original = workflow.patched
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        with monkeypatch.context() as old:
            if legacy:
                old.setattr(workflow, "patched", lambda marker: False if marker == VERSION else original(marker))
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 40))["status"] == "completed"
                history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    assert (directory / "proof.txt").exists() is not legacy
