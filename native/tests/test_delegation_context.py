"""Delegation carries the parent's evidence without adopting its live computer."""

from copy import deepcopy

from pydantic_ai import Agent, ConversationTypeAdapter, RunContext, RunUsage
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.step_persistence import is_provider_valid

from moyai.delegation import DelegationService, InteractiveDelegation
from moyai.work_progress import WorkProgress


async def test_child_model_receives_frozen_parent_evidence_without_reexecuting_pending_calls(runtime, active):
    _, _, prepared = active
    messages = [
        ModelRequest(parts=[UserPromptPart("Investigate the checkout in BerriAI/moyai at commit 123abc.")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "src/settings.py"}, tool_call_id="read")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "The setting is inherited_access=True.", tool_call_id="read")]),
        ModelResponse(parts=[
            ToolCallPart("delegate_task", {"agent_name": "self", "task": "Check the setting."}, tool_call_id="delegate"),
            ToolCallPart("shell", {"command": "touch parent-only"}, tool_call_id="pending"),
        ], workspace_ref={"provider": "local", "id": "parent-computer"}),
    ]
    original = deepcopy(messages)
    ctx = RunContext(deps=prepared.scope, model=TestModel(), usage=RunUsage(), messages=messages)
    child = await InteractiveDelegation(runtime.database).start(
        prepared.scope, "delegate", "Check the setting.", messages=messages, ctx=ctx,
    )
    messages.append(ModelRequest(parts=[UserPromptPart("LATER_PRIVATE_CONTEXT")]))
    # A lost response must recover the admitted snapshot, not refresh it from a newer parent.
    assert await InteractiveDelegation(runtime.database).start(
        prepared.scope, "delegate", "Check the setting.", messages=messages, ctx=ctx,
    ) == child
    turn = await runtime.database.claim_next()
    saved = await runtime.database.prepare(turn.id)
    assert saved.conversation is not None
    conversation = ConversationTypeAdapter.validate_json(saved.conversation)
    assert is_provider_valid(conversation.messages)
    assert saved.workspace is None
    assert all(not message.workspace_ref for message in conversation.messages if isinstance(message, ModelResponse))
    assert messages[:-1] == original
    seen = []

    def respond(history, info):
        seen.append(history)
        text = str(history)
        assert "BerriAI/moyai" in text and "123abc" in text and "inherited_access=True" in text
        assert "LATER_PRIVATE_CONTEXT" not in text
        return ModelResponse(parts=[TextPart("Verified inherited_access=True in the parent's checkout evidence.")])

    result = await Agent(FunctionModel(respond)).run(saved.prompt, conversation=conversation)
    assert result.output.startswith("Verified") and len(seen) == 1
    assert not (runtime.settings.data_dir.parent / "computer" / "parent-only").exists()


async def test_failure_before_first_response_is_visible_in_parent_report_and_progress(runtime, active):
    _, parent, prepared = active
    child = await DelegationService(runtime.database).start(prepared.scope, "fails", "Inspect the repository.")
    turn = await runtime.database.claim_next()
    await runtime.database.prepare(turn.id)
    await runtime.database.fail(turn.id, "Activity task failed", diagnostics={"exception_chain": [
        {"type": "ActivityError", "activity_type": "agent__moyai__capability__memory__load_snapshot"},
        {"type": "PermissionError", "message": "SECRET_DIAGNOSTIC_MUST_NOT_LEAK"},
    ]})
    report = await DelegationService(runtime.database).report(prepared.scope, child["session_id"])
    assert report["status"] == "failed"
    assert "PermissionError" in report["output"] and "memory" in report["output"]
    progress = await WorkProgress(runtime).read(parent)
    failed = progress["subagents"][0]
    assert failed["status"] == "failed" and failed["messages"]
    assert "PermissionError" in failed["messages"][-1]["text"]
    assert "SECRET_DIAGNOSTIC_MUST_NOT_LEAK" not in str((report, progress))
