import json
from datetime import UTC, datetime
from types import SimpleNamespace

from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select
from temporalio.exceptions import ActivityError, ApplicationError
from test_application_boundaries import sign_in
from test_sidebar import member_client

from moyai.failure_diagnostics import FAILURE_KIND, RESULT_LIMIT, capture_failure, failure_details
from moyai.models import Event, Session, Turn, User


def test_failure_capture_preserves_exception_and_activity_types_without_messages():
    cause = ApplicationError("private-provider-payload", type="PendingEffectError")
    error = ActivityError(
        "private-transport-message", scheduled_event_id=1, started_event_id=2,
        identity="private-worker-identity", activity_type="moyai_effects_reserve",
        activity_id="16", retry_state=None,
    )
    error.__cause__ = cause
    assert capture_failure(error) == {"exception_chain": [
        {"type": "ActivityError", "activity_type": "moyai_effects_reserve", "activity_id": "16"},
        {"type": "ApplicationError", "application_type": "PendingEffectError"},
    ]}
    cause.__cause__ = error
    assert len(capture_failure(error)["exception_chain"]) == 2


async def test_failure_details_reuse_saved_tool_results_and_redact_private_data(runtime, active, monkeypatch):
    session, turn, _ = active
    runtime.settings.gateway_key = SecretStr("synthetic-gateway-secret")
    timestamp = datetime(2026, 10, 19, 17, 14, tzinfo=UTC)
    messages = [
        ModelResponse(run_id=turn.id, timestamp=timestamp, usage=RequestUsage(input_tokens=120, output_tokens=30), parts=[
            ThinkingPart("private-reasoning"),
            ToolCallPart("browser_open", {"token": "private-input"}, tool_call_id="open"),
            ToolCallPart("credentials_run", {"command": "private-command"}, tool_call_id="private"),
        ]),
        ModelRequest(timestamp=timestamp, parts=[
            ToolReturnPart("browser_open", "Failed: synthetic-gateway-secret", tool_call_id="open", outcome="failed"),
            RetryPromptPart("private-credential-error", tool_name="credentials_run", tool_call_id="private"),
        ]),
        ModelResponse(run_id=f"{turn.id}:segment:1", timestamp=timestamp, parts=[
            ToolCallPart("browser_read", {}, tool_call_id="read"),
        ]),
        ModelRequest(timestamp=timestamp, parts=[ToolReturnPart("browser_read", "Error page", tool_call_id="read")]),
        ModelResponse(run_id="other-turn", parts=[ToolCallPart("shell", {}, tool_call_id="unrelated")]),
        ModelRequest(parts=[RetryPromptPart("unrelated-private-error", tool_name="shell", tool_call_id="unrelated")]),
    ]

    async def snapshot(*_args, **kwargs):
        assert kwargs["include_interrupted"] is True
        return SimpleNamespace(messages=messages)

    monkeypatch.setattr("moyai.failure_diagnostics.latest_turn_snapshot", snapshot)
    await runtime.database.fail(turn.id, "The task failed.", diagnostics={
        "exception_chain": [{"type": "RuntimeError", "message": "private-error-payload"}],
        "secret": "private-secret",
    })
    async with runtime.database.sessions() as db:
        saved_turn = await db.get(Turn, turn.id)
    expected = await failure_details(runtime, saved_turn)
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/turns/{turn.id}/failure")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    details = response.json()
    assert details == expected, "The response schema must preserve every diagnostic service field."
    assert details["runtime"] == {"exception_chain": [{"type": "RuntimeError"}]}
    assert details["last_action"]["tool"] == "browser_read"
    assert details["last_action"]["status"] == "completed"
    assert details["usage"] == {"requests": 2, "input_tokens": 120, "output_tokens": 30}
    assert details["recent_failures"][0]["error"] == "Failed: [redacted]"
    assert details["recent_failures"][1]["restricted"] and details["recent_failures"][1]["error"] is None
    assert all(secret not in response.text for secret in [
        "synthetic-gateway-secret", "private-reasoning", "private-input", "private-command",
        "private-credential-error", "private-error-payload", "private-secret", "unrelated-private-error",
    ])


async def test_failure_metadata_is_immutable_and_exported_only_to_requester(runtime, active, actor, monkeypatch):
    session, turn, _ = active
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
    await runtime.database.fail(turn.id, "The task failed.", diagnostics=capture_failure(ValueError("private-message")))
    await runtime.database.fail(turn.id, "A repeated cleanup.", diagnostics=capture_failure(TypeError("later")))
    async with runtime.database.sessions() as db:
        records = list(await db.scalars(select(Event).where(Event.turn_id == turn.id, Event.kind == FAILURE_KIND)))
    assert len(records) == 1
    assert json.loads(runtime.security.unseal(records[0].content)) == {
        "exception_chain": [{"type": "ValueError"}],
    }
    async with sign_in(runtime) as owner, member_client(runtime) as (member, _):
        path = f"/api/sessions/{session.id}/turns/{turn.id}/failure"
        assert (await member.get(path)).status_code == 403
        exported = (await owner.get(f"/api/sessions/{session.id}/debug-trace")).json()
        failure = next(step for step in exported["steps"] if step.get("extra", {}).get("kind") == "error")
        assert failure["extra"]["diagnostics"]["exception_chain"][0]["type"] == "ValueError"
        shared = (await member.get(f"/api/sessions/{session.id}/debug-trace")).json()
        assert "exception_chain" not in json.dumps(shared)
        assert (await owner.get(f"/api/sessions/not-this-session/turns/{turn.id}/failure")).status_code == 404


async def test_old_failures_keep_basic_context_when_history_is_unavailable(runtime, active, monkeypatch):
    session, turn, _ = active
    await runtime.database.fail(turn.id, "The task failed.")

    async def unavailable(*_args, **_kwargs):
        raise OSError("private-store-error")

    monkeypatch.setattr("moyai.failure_diagnostics.latest_turn_snapshot", unavailable)
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/turns/{turn.id}/failure")
    assert response.status_code == 200
    value = response.json()
    assert value["turn_id"] == turn.id and value["workflow_id"] == turn.workflow_id
    assert value["runtime"] is None and value["recent_failures"] == []
    assert value["notes"] == [
        "Runtime exception details were not captured for this turn.", "Tool history is temporarily unavailable.",
    ]
    assert "private-store-error" not in response.text


async def test_failure_details_bound_history_and_recheck_access_after_loading(runtime, active, actor, monkeypatch):
    session, turn, _ = active
    await runtime.database.fail(turn.id, "The task failed.")
    messages = []
    for index in range(8):
        identity = f"call-{index}"
        messages.extend([
            ModelResponse(run_id=turn.id, parts=[ToolCallPart("shell", {}, tool_call_id=identity)]),
            ModelRequest(parts=[RetryPromptPart("x" * (RESULT_LIMIT + 1), tool_name="shell", tool_call_id=identity)]),
        ])
    revoke = False

    async def snapshot(*_args, **_kwargs):
        if revoke:
            async with runtime.database.sessions.begin() as db:
                (await db.get(User, actor.id)).auth_version += 1
        return SimpleNamespace(messages=messages)

    monkeypatch.setattr("moyai.failure_diagnostics.latest_turn_snapshot", snapshot)
    async with sign_in(runtime) as client:
        path = f"/api/sessions/{session.id}/turns/{turn.id}/failure"
        value = (await client.get(path)).json()
        assert value["failures_limited"] and len(value["recent_failures"]) == 5
        assert value["recent_failures"][0]["id"] == "call-3"
        assert all(row["truncated"] and len(row["error"]) == RESULT_LIMIT for row in value["recent_failures"])
        revoke = True
        assert (await client.get(path)).status_code == 401


async def test_last_action_follows_recorded_order_when_a_call_identity_reappears(runtime, active, monkeypatch):
    session, turn, _ = active
    await runtime.database.fail(turn.id, "The task failed.")
    messages = []
    for identity in ["repeated", "middle", "repeated"]:
        messages.extend([
            ModelResponse(run_id=turn.id, parts=[ToolCallPart("browser_read", {}, tool_call_id=identity)]),
            ModelRequest(parts=[ToolReturnPart("browser_read", "Observed.", tool_call_id=identity)]),
        ])

    async def snapshot(*_args, **_kwargs):
        return SimpleNamespace(messages=messages)

    monkeypatch.setattr("moyai.failure_diagnostics.latest_turn_snapshot", snapshot)
    async with sign_in(runtime) as client:
        details = (await client.get(f"/api/sessions/{session.id}/turns/{turn.id}/failure")).json()
    assert details["last_action"]["id"] == "repeated"
    assert details["last_action"]["status"] == "completed"
