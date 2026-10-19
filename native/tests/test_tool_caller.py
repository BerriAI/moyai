from __future__ import annotations

from builtins import TimeoutError as BuiltinTimeoutError
from datetime import timedelta

import pytest
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    RunCancelled,
    SkipModelRequest,
    SkipToolExecution,
    SkipToolValidation,
    ToolFailed,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.workspaces import WorkspaceUnavailableError
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, TimeoutError

from moyai.tools.tool_caller import Outcome, RetryPolicy, ToolCaller


def tool(*, read_only: bool) -> ToolDefinition:
    return ToolDefinition(
        name="test_tool",
        metadata={"annotations": {"readOnlyHint": read_only}},
    )


def call() -> ToolCallPart:
    return ToolCallPart("test_tool", {}, tool_call_id="test-call")


def activity_error(cause: BaseException) -> ActivityError:
    error = ActivityError(
        "Tool activity failed.",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test-worker",
        activity_type="test_tool",
        activity_id="test-activity",
        retry_state=None,
    )
    error.__cause__ = cause
    return error


@pytest.mark.asyncio
async def test_read_only_transient_failures_retry_until_success():
    caller = ToolCaller(RetryPolicy(backoff=timedelta()))
    attempts = 0

    async def handler(_args):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise BuiltinTimeoutError("temporary outage")
        return "success"

    result = await caller.call(call(), tool(read_only=True), {}, handler)

    assert result == "success"
    assert attempts == 3


@pytest.mark.asyncio
async def test_write_tool_transient_failure_is_not_retried():
    caller = ToolCaller(RetryPolicy(backoff=timedelta()))
    attempts = 0

    async def handler(_args):
        nonlocal attempts
        attempts += 1
        raise BuiltinTimeoutError("temporary outage")

    with pytest.raises(ToolFailed, match="test_tool failed \\(TimeoutError\\)") as raised:
        await caller.call(call(), tool(read_only=False), {}, handler)

    assert attempts == 1
    assert "temporary outage" in str(raised.value)


@pytest.mark.asyncio
async def test_activity_failure_is_returned_to_model_with_tool_and_error_type():
    caller = ToolCaller(RetryPolicy(backoff=timedelta()))
    error = activity_error(ApplicationError("boom secret-token-value", type="GitHubPublishError"))

    async def handler(_args):
        raise error

    with pytest.raises(ToolFailed) as raised:
        await caller.call(
            call(), tool(read_only=False), {"api_token": "secret-token-value"}, handler,
        )

    assert "test_tool" in str(raised.value)
    assert "GitHubPublishError" in str(raised.value)
    assert "secret-token-value" not in str(raised.value)


@pytest.mark.asyncio
async def test_guidance_change_keeps_existing_failure_message():
    caller = ToolCaller()
    error = activity_error(ApplicationError("guidance changed", type="MoyaiGuidanceChanged"))

    async def handler(_args):
        raise error

    with pytest.raises(ToolFailed) as raised:
        await caller.call(call(), tool(read_only=False), {}, handler)

    assert str(raised.value) == "New guidance arrived before dispatch; this action did not execute."


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(
            lambda: ApplicationError(
                "authorization denied", type="MoyaiHistoryAuthorizationDenied", non_retryable=True,
            ),
            id="policy-error-type",
        ),
        pytest.param(lambda: WorkspaceUnavailableError("workspace lost"), id="workspace-unavailable"),
        pytest.param(lambda: RunCancelled("run cancelled"), id="run-cancelled"),
        pytest.param(lambda: CancelledError("workflow cancelled"), id="temporal-cancelled"),
        pytest.param(lambda: UsageLimitExceeded("usage exhausted"), id="usage-limit"),
        pytest.param(lambda: UnexpectedModelBehavior("tool retries exhausted"), id="retry-limit"),
        pytest.param(
            lambda: ApplicationError("usage exhausted", type="UsageLimitExceeded"),
            id="serialized-usage-limit",
        ),
        pytest.param(
            lambda: ApplicationError("tool retries exhausted", type="UnexpectedModelBehavior"),
            id="serialized-retry-limit",
        ),
    ],
)
@pytest.mark.parametrize("wrapped", [False, True], ids=["direct", "activity"])
@pytest.mark.asyncio
async def test_escalated_errors_are_reraised_unchanged(error_factory, wrapped):
    caller = ToolCaller()
    error = error_factory()
    if wrapped:
        error = activity_error(error)
    attempts = 0

    async def handler(_args):
        nonlocal attempts
        attempts += 1
        raise error

    assert caller.outcome(error, tool(read_only=True), attempt=1) is Outcome.ESCALATE
    with pytest.raises(type(error)) as raised:
        await caller.call(call(), tool(read_only=True), {}, handler)

    assert raised.value is error
    assert attempts == 1


@pytest.mark.parametrize(
    "error",
    [
        ToolFailed("already failed"),
        ModelRetry("retry the model"),
        CallDeferred(),
        ApprovalRequired(),
        SkipModelRequest(ModelResponse(parts=[TextPart("skip model")])),
        SkipToolExecution("skip tool"),
        SkipToolValidation({}),
    ],
    ids=[
        "tool-failed",
        "model-retry",
        "call-deferred",
        "approval-required",
        "skip-model-request",
        "skip-tool-execution",
        "skip-tool-validation",
    ],
)
@pytest.mark.asyncio
async def test_pydantic_control_flow_errors_pass_through_unchanged(error):
    caller = ToolCaller()

    async def handler(_args):
        raise error

    with pytest.raises(type(error)) as raised:
        await caller.call(call(), tool(read_only=False), {}, handler)

    assert raised.value is error


@pytest.mark.asyncio
async def test_exhausted_read_only_retries_return_failure_to_model():
    caller = ToolCaller(RetryPolicy(attempts=3, backoff=timedelta()))
    attempts = 0

    async def handler(_args):
        nonlocal attempts
        attempts += 1
        raise BuiltinTimeoutError("still unavailable")

    with pytest.raises(ToolFailed, match="test_tool failed \\(TimeoutError\\)") as raised:
        await caller.call(call(), tool(read_only=True), {}, handler)

    assert attempts == 3
    assert "still unavailable" in str(raised.value)


@pytest.mark.parametrize(
    "error",
    [
        BuiltinTimeoutError("timeout"),
        TimeoutError("temporal timeout", type=None, last_heartbeat_details=[]),
        ConnectionError("connection failed"),
    ],
)
def test_transient_retry_requires_a_safe_read_only_tool(error):
    caller = ToolCaller()

    assert caller.outcome(error, tool(read_only=True), attempt=1) is Outcome.RETRY
    assert caller.outcome(error, tool(read_only=False), attempt=1) is Outcome.TO_MODEL
    assert caller.outcome(error, tool(read_only=True), attempt=3) is Outcome.TO_MODEL


@pytest.mark.asyncio
async def test_retry_backoff_scales_with_attempt_number(monkeypatch):
    caller = ToolCaller(RetryPolicy(backoff=timedelta(seconds=2)))
    delays: list[float] = []
    attempts = 0

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def handler(_args):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError("temporary outage")
        return "success"

    monkeypatch.setattr("moyai.tools.tool_caller.asyncio.sleep", sleep)

    assert await caller.call(call(), tool(read_only=True), {}, handler) == "success"
    assert delays == [2, 4]
