"""SDK behavior checks; live-model proof is recorded separately."""

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from moyai.commentary import Commentary, saved_commentary
from moyai.contracts import RunScope


def text(content, phase):
    return TextPart(content, provider_name="openai", provider_details={"phase": phase})


@pytest.mark.parametrize("with_tool", [False, True])
async def test_commentary_continues_and_remains_in_native_history(with_tool):
    count, calls = 0, []

    def inspect() -> str:
        """Inspect the test item."""
        calls.append("inspect")
        return "Observed."

    def model(messages, info):
        nonlocal count
        count += 1
        if count == 1:
            return ModelResponse(parts=[
                text("I will inspect it.", "commentary"),
                *([ToolCallPart("inspect", {}, tool_call_id="inspect")] if with_tool else []),
            ])
        return ModelResponse(parts=[text("Verified result.", "final_answer")])

    result = await Agent(FunctionModel(model), tools=[inspect], capabilities=[Commentary()]).run(
        "Inspect", deps=RunScope("s", "t", "u", "test"),
    )
    assert count == 2 and result.output == "Verified result."
    assert calls == (["inspect"] if with_tool else [])
    assert any(
        isinstance(part, TextPart) and part.provider_details == {"phase": "commentary"}
        for message in result.all_messages() for part in message.parts
    )


async def test_same_response_keeps_commentary_out_of_final_output_without_rewriting_history():
    response = ModelResponse(parts=[text("Checking.", "commentary"), text("Result.", "final_answer")])
    result = await Agent(FunctionModel(lambda messages, info: response), capabilities=[Commentary()]).run(
        "Inspect", deps=RunScope("s", "t", "u", "test"),
    )
    assert result.output == "Result."
    saved = result.all_messages()[-1]
    assert len(saved.parts) == 2
    assert saved.parts[0].content == "Checking."


async def test_unphased_final_answers_keep_native_behavior():
    result = await Agent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("Legacy answer.")])),
        capabilities=[Commentary()],
    ).run("Inspect", deps=RunScope("s", "t", "u", "test"))
    assert result.output == "Legacy answer."


def test_saved_progress_omits_private_reasoning_children_and_final_text():
    messages = [
        ModelResponse(run_id="root", parts=[
            ThinkingPart("private reasoning"), text("Checking.", "commentary"), text("Result.", "final_answer"),
        ]),
        ModelResponse(run_id="child", parts=[text("Child-private progress.", "commentary")]),
        ModelResponse(run_id="root", parts=[TextPart("Legacy progress."), ToolCallPart("read", {})]),
        ModelResponse(run_id="root", parts=[TextPart("Legacy final.")]),
    ]
    assert [item["text"] for item in saved_commentary(messages, "root")] == ["Checking.", "Legacy progress."]


async def test_commentary_only_loop_is_bounded_by_native_retry_budget():
    calls = []

    def model(messages, info):
        calls.append(1)
        return ModelResponse(parts=[text("Still checking.", "commentary")])

    with pytest.raises(UnexpectedModelBehavior):
        await Agent(FunctionModel(model), capabilities=[Commentary()], retries=2).run(
            "Inspect", deps=RunScope("s", "t", "u", "test"),
        )
    assert len(calls) == 3


async def test_saved_progress_api_keeps_requester_boundary_and_redacts_secrets(runtime, active):
    from pydantic import SecretStr
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord
    from test_application_boundaries import sign_in
    from test_sidebar import member_client

    from moyai.models import Session

    session, turn, _ = active
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
    await runtime.stores.steps.register_run(RunRecord(run_id=turn.id, agent_name="moyai"))
    await runtime.stores.steps.save_snapshot(ContinuableSnapshot(
        run_id=turn.id, step_index=1, messages=[
            ModelResponse(run_id=turn.id, parts=[text("Checking synthetic-gateway-secret", "commentary")]),
            ModelResponse(run_id="child", parts=[text("Child-private text", "commentary")]),
        ],
    ))
    path = f"/api/sessions/{session.id}/progress/{turn.id}"
    async with sign_in(runtime) as owner, member_client(runtime) as (member, _):
        response = await owner.get(path)
        assert response.status_code == 200
        assert [item["text"] for item in response.json()["messages"]] == ["Checking [redacted]"]
        assert (await member.get(path)).status_code == 403
        assert (await owner.get(f"/api/sessions/{session.id}/progress/missing")).status_code == 404


def test_public_events_preserve_phase_but_never_thinking():
    from pydantic_ai.messages import PartEndEvent, PartStartEvent

    from moyai.presentation import public_event

    for event_type in (PartStartEvent, PartEndEvent):
        assert public_event(event_type(index=0, part=text("Update", "commentary")))["phase"] == "commentary"
        assert public_event(event_type(index=0, part=ThinkingPart("private"))) is None


async def test_public_projection_is_idempotent_and_survives_context_compaction(runtime, active, monkeypatch):
    from sqlalchemy import func, select

    from moyai.models import Event
    from moyai.progress import turn_progress

    _, turn, prepared = active
    capability = Commentary(runtime.database)
    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[
            text("Checking the inputs.", "commentary") if calls == 1 else text("Done.", "final_answer")
        ])

    result = await Agent(FunctionModel(model), capabilities=[capability]).run(
        "Inspect", deps=prepared.scope, run_id=turn.id,
    )
    assert result.output == "Done."
    await capability.save(prepared.scope, 1, [{"id": "ignored", "text": "Checking the inputs.", "phase": "commentary"}])

    async def snapshot(**kwargs):
        pytest.fail("New public progress should not depend on the compacted model context.")

    monkeypatch.setattr(runtime.stores.steps, "latest_snapshot", snapshot)
    assert [item["text"] for item in await turn_progress(runtime, turn)] == ["Checking the inputs."]
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Event).where(Event.kind == "commentary")) == 1
