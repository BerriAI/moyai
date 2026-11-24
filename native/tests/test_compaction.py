"""Supplementary SDK and replay checks; live application evidence is separate."""

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RunUsage

from moyai.compaction import EXCERPT_CHARS, OMISSION, VERSION, EvidenceCompaction
from moyai.contracts import RunScope


def history():
    old = "COMMAND: pytest tests/importer\n" + "ordinary output\n" * 100 + "\nFAILED test_reload; exit code 1"
    recent = {"stdout": "recent output\n" * 100, "exit_code": 0}
    return [
        ModelRequest(parts=[UserPromptPart("Fix the importer; do not publish.")]),
        ModelResponse(parts=[ToolCallPart("shell", {"command": "pytest tests/importer"}, tool_call_id="old")]),
        ModelRequest(parts=[ToolReturnPart("shell", old, tool_call_id="old")]),
        ModelResponse(parts=[TextPart("Checking the follow-up result.")]),
        ModelResponse(parts=[ToolCallPart("shell", {"command": "pytest tests/importer"}, tool_call_id="recent")]),
        ModelRequest(parts=[ToolReturnPart("shell", recent, tool_call_id="recent")]),
        ModelResponse(parts=[TextPart("The recent result is ready.")]),
        ModelRequest(parts=[UserPromptPart("Report the observed outcomes and any remaining failure.")]),
    ]


@pytest.mark.parametrize("legacy", [False, True])
async def test_summary_sees_trailing_failure_and_keeps_raw_recent_results(monkeypatch, legacy):
    from temporalio import workflow

    seen = []

    def summarize(messages, info):
        seen.extend(part.content for msg in messages for part in msg.parts if isinstance(part, UserPromptPart))
        return ModelResponse(parts=[TextPart("Preserved task state.")])

    messages = history()
    old_result, recent_result = messages[2], messages[5]
    ctx = RunContext(
        deps=RunScope("session", "turn", "actor", "test"),
        model=FunctionModel(summarize), usage=RunUsage(),
    )
    with monkeypatch.context() as patch:
        if legacy:
            patch.setattr(workflow, "in_workflow", lambda: True)
            patch.setattr(workflow, "patched", lambda marker: marker != VERSION)
        compacted = await EvidenceCompaction(max_messages=6, keep_messages=4, receipts=True).compact(messages, ctx)
    prompt = "\n".join(seen)
    assert "COMMAND: pytest tests/importer" in prompt
    assert ("FAILED test_reload; exit code 1" in prompt) is not legacy
    assert (OMISSION in prompt) is not legacy
    assert messages[2] is old_result and len(old_result.parts[0].content) > EXCERPT_CHARS
    assert next(message for message in compacted if message is recent_result) is recent_result
    assert isinstance(recent_result.parts[0].content, dict)
    assert any("Fix the importer; do not publish." == part.content
               for message in compacted if isinstance(message, ModelRequest)
               for part in message.parts if isinstance(part, UserPromptPart))
    assert any("compaction" in str(message.parts).lower() for message in compacted)


async def test_no_cutoff_returns_original_messages_without_rewriting_outputs():
    messages = history()
    ctx = RunContext(
        deps=RunScope("session", "turn", "actor", "test"),
        model=FunctionModel(lambda *_: pytest.fail("No summary needed")), usage=RunUsage(),
    )
    result = await EvidenceCompaction(max_messages=100, keep_messages=100).compact(messages, ctx)
    assert len(result) == len(messages)
    assert all(a is b for a, b in zip(result, messages, strict=True))


@pytest.mark.parametrize("legacy", [False, True])
async def test_compaction_replays_with_original_and_evidence_aware_summaries(runtime, active, monkeypatch, legacy):
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
    directory.mkdir(exist_ok=True)
    (directory / "test.log").write_text("COMMAND: check\n" + "progress\n" * 150 + "\nFAILED import; exit code 1")
    summaries = []

    def respond(messages, info):
        if "context summarization assistant" in (info.instructions or ""):
            prompt = "\n".join(str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart))
            summaries.append(prompt)
            return ModelResponse(parts=[TextPart("Task: report the test result. File: test.log. Continue from here.")])
        results = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not results:
            return ModelResponse(parts=[ToolCallPart("read_file", {"path": "test.log"}, tool_call_id="log")])
        if results[-1].tool_call_id == "log":
            return ModelResponse(parts=[ToolCallPart("list_files", {"path": "."}, tool_call_id="files")])
        return ModelResponse(parts=[TextPart("Reported the observed outcome.")])

    monkeypatch.setattr("moyai.agent.EvidenceCompaction",
                        lambda **_: EvidenceCompaction(max_messages=3, keep_messages=2, receipts=True))
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
                saved = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(saved)
    assert summaries
    assert ("FAILED import; exit code 1" in summaries[0]) is not legacy
