import json
from datetime import UTC, datetime
from types import SimpleNamespace

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from test_application_boundaries import sign_in
from test_sidebar import member_client

from moyai.contracts import NewMessage, NewSession
from moyai.debug_trace import debug_trace
from moyai.models import Message, Turn, new_id
from moyai.runtime import Runtime


def _messages(turn_id: str) -> list[ModelRequest | ModelResponse]:
    timestamp = datetime(2026, 6, 20, tzinfo=UTC)
    return [
        ModelRequest(
            instructions="Follow the system guidance.",
            parts=[
                SystemPromptPart("Follow the system guidance."),
                UserPromptPart("Prompt delivered with repository context."),
            ],
            timestamp=timestamp,
            run_id=turn_id,
        ),
        ModelResponse(
            parts=[
                ThinkingPart("Check the relevant source before answering."),
                TextPart("I checked the source."),
                ToolCallPart(
                    "credentials_run", {"command": "sensitive command"}, tool_call_id="private-call",
                ),
                ToolCallPart("shell", {"command": "pwd"}, tool_call_id="shell-call"),
            ],
            model_name="test-model",
            timestamp=timestamp,
            usage=RequestUsage(input_tokens=120, output_tokens=30, cache_read_tokens=10),
            run_id=turn_id,
        ),
        ModelRequest(
            instructions="Follow the system guidance.",
            parts=[
                SystemPromptPart("Follow the system guidance."),
                UserPromptPart("Check the working directory."),
                ToolReturnPart(
                    "credentials_run", "private command output", tool_call_id="private-call",
                ),
                RetryPromptPart(
                    "The command needs correction.", tool_name="shell", tool_call_id="shell-call",
                ),
                RetryPromptPart(
                    "Provider request failed.", tool_name="missing", tool_call_id="missing-call",
                ),
            ],
            timestamp=timestamp,
            run_id=turn_id,
        ),
        ModelResponse(
            parts=[TextPart("This message belongs to another run.")],
            model_name="test-model",
            timestamp=timestamp,
            run_id="another-turn",
        ),
    ]


async def test_debug_trace_exports_atif_with_requester_activity_and_redaction(runtime, actor, monkeypatch, tmp_path):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Inspect the project.", request_key="debug-trace"),
    )
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Turn, turn.id)
        assert saved is not None
        saved.status = "failed"
        saved.error = "A tool failed."
        saved.usage = json.dumps({"cost": 0.25})

    async def snapshot(*, run_id: str):
        assert run_id == turn.id
        return SimpleNamespace(messages=_messages(turn.id))

    monkeypatch.setattr(runtime.stores.steps, "latest_snapshot", snapshot)
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/debug-trace")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="moyai-trace-{session.id}.json"'
    assert response.headers["cache-control"] == "no-store"

    trace = response.json()
    sample = tmp_path / "debug-trace.json"
    sample.write_text(json.dumps(trace, indent=2) + "\n")
    assert trace["schema_version"] == "ATIF-v1.8"
    assert trace["agent"]["name"] == "moyai"
    assert trace["agent"]["version"] == "0.1.0"
    steps = trace["steps"]
    assert [step["step_id"] for step in steps] == list(range(1, len(steps) + 1))
    requester = steps[0]
    assert requester["source"] == "user"
    assert requester["message"] == "Inspect the project."
    assert requester["extra"] == {
        "turn_id": turn.id,
        "turn_status": "failed",
        "author": "requester",
        "prompt_received": "Prompt delivered with repository context.",
    }

    system = [
        step
        for step in steps
        if step["source"] == "system" and step["message"] == "Follow the system guidance."
    ]
    assert len(system) == 1
    agent = next(step for step in steps if step["source"] == "agent")
    assert agent["message"] == "I checked the source."
    assert agent["reasoning_content"] == "Check the relevant source before answering."
    assert agent["model_name"] == "test-model"
    assert agent["metrics"] == {"prompt_tokens": 120, "completion_tokens": 30, "cached_tokens": 10}
    assert next(step for step in steps if step.get("extra") == {"kind": "steering"})["message"] == "Check the working directory."
    calls = {call["tool_call_id"]: call for call in agent["tool_calls"]}
    assert calls["private-call"]["arguments"] == {}
    assert calls["private-call"]["extra"] == {"redacted": True}
    assert calls["shell-call"]["arguments"] == {"command": "pwd"}
    observations = {item["source_call_id"]: item for item in agent["observation"]["results"]}
    assert observations["private-call"]["content"] is None
    assert observations["private-call"]["extra"] == {"failed": False}
    assert observations["shell-call"]["content"] == "The command needs correction."
    assert observations["shell-call"]["extra"] == {"failed": True}
    assert any(step["source"] == "system" and step.get("extra") == {"kind": "retry"} for step in steps)
    assert any(
        step["source"] == "system" and step.get("extra") == {"kind": "error", "turn_id": turn.id}
        for step in steps
    )
    assert "sensitive command" not in json.dumps(trace)
    assert "private command output" not in json.dumps(trace)
    assert "another run" not in json.dumps(trace)
    assert trace["final_metrics"] == {
        "total_prompt_tokens": 120,
        "total_completion_tokens": 30,
        "total_cached_tokens": 10,
        "total_steps": len(steps),
        "total_cost_usd": 0.25,
    }


async def test_debug_trace_hides_teammate_work_and_denies_nonmembers(runtime, actor, monkeypatch):
    shared = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    snapshot_calls: list[str] = []

    async def unexpected_snapshot(runtime: Runtime, run_id: str) -> None:
        snapshot_calls.append(run_id)

    monkeypatch.setattr("moyai.debug_trace.latest_turn_snapshot", unexpected_snapshot)
    async with member_client(runtime) as (client, teammate):
        turn = await runtime.database.submit(
            teammate, shared.id, NewMessage(prompt="Private teammate request.", request_key="teammate"),
        )
        async with runtime.database.sessions.begin() as db:
            saved = await db.get(Turn, turn.id)
            assert saved is not None
            saved.status = "completed"
            db.add(Message(
                session_id=shared.id, turn_id=turn.id, author_id=None, role="assistant",
                content=runtime.security.seal("Teammate answer."),
                identity=f"debug-trace-assistant:{new_id()}",
            ))
        async with sign_in(runtime) as owner:
            response = await owner.get(f"/api/sessions/{shared.id}/debug-trace")
        assert response.status_code == 200
        steps = response.json()["steps"]
        assert steps[0]["extra"]["author"] == "teammate"
        assert steps[0]["message"] == "Private teammate request."
        assistant = next(step for step in steps if step["source"] == "agent")
        assert assistant["message"] == "Teammate answer."
        assert assistant["extra"] == {"details_hidden": True}
        assert "reasoning_content" not in assistant
        assert "tool_calls" not in assistant
        assert snapshot_calls == []

        private = await runtime.database.create_session(actor, NewSession(visibility="private"))
        denied = await client.get(f"/api/sessions/{private.id}/debug-trace")
        assert denied.status_code == 404


async def test_debug_trace_uses_saved_assistant_when_no_snapshot_exists(runtime, actor, monkeypatch):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="A queued task.", request_key="queued-debug-trace"),
    )
    async with runtime.database.sessions.begin() as db:
        db.add(Message(
            session_id=session.id, turn_id=turn.id, author_id=None, role="assistant",
            content=runtime.security.seal("Saved answer."),
            identity=f"debug-trace-assistant:{new_id()}",
        ))

    async def no_snapshot(runtime: Runtime, turn_id: str) -> None:
        return

    monkeypatch.setattr("moyai.debug_trace.latest_turn_snapshot", no_snapshot)
    trace = await debug_trace(runtime, actor, session.id)
    assistant = next(step for step in trace["steps"] if step["source"] == "agent")
    assert assistant["message"] == "Saved answer."
    assert assistant["extra"] == {"turn_id": turn.id}
