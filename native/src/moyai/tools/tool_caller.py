"""Classify tool-call failures without failing the agent turn."""

from __future__ import annotations

import asyncio
from builtins import TimeoutError as BuiltinTimeoutError
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

from pydantic_ai.capabilities.abstract import WrapToolExecuteHandler
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
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.workspaces import WorkspaceUnavailableError
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
    CancelledError,
)
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

from ..connections import read_only_tool
from ..presentation import redact


class Outcome(StrEnum):
    RETRY = "retry"
    TO_MODEL = "to_model"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    backoff: timedelta = timedelta(seconds=2)
    escalate: frozenset[str] = frozenset({
        "MoyaiHistoryAuthorizationDenied",
        "WorkspaceUnavailableError",
        "UnexpectedModelBehavior",
        "UsageLimitExceeded",
    })


_DEFAULT_RETRY_POLICY = RetryPolicy()


class ToolCaller:
    """Runs one tool call and decides what a failure means for the turn."""

    def __init__(self, policy: RetryPolicy = _DEFAULT_RETRY_POLICY) -> None:
        self.policy = policy

    async def call(
        self,
        call: ToolCallPart,
        tool: ToolDefinition,
        args: dict[str, Any],
        handler: WrapToolExecuteHandler,
    ) -> Any:
        for attempt in range(1, self.policy.attempts + 1):
            try:
                return await handler(args)
            except asyncio.CancelledError:
                raise
            except (
                ApprovalRequired,
                CallDeferred,
                ModelRetry,
                RunCancelled,
                SkipModelRequest,
                SkipToolExecution,
                SkipToolValidation,
                ToolFailed,
            ):
                raise
            except Exception as error:
                cause = self._root_cause(error)
                if (
                    isinstance(error, ActivityError)
                    and isinstance(cause, ApplicationError)
                    and cause.type == "MoyaiGuidanceChanged"
                ):
                    raise ToolFailed(
                        "New guidance arrived before dispatch; this action did not execute."
                    ) from error

                outcome = self.outcome(cause, tool, attempt)
                if outcome is Outcome.RETRY:
                    await asyncio.sleep(self.policy.backoff.total_seconds() * attempt)
                    continue
                if outcome is Outcome.ESCALATE:
                    raise

                message = str(cause)
                sensitive_args = [
                    value
                    for key, value in args.items()
                    if isinstance(value, str)
                    and any(
                        marker in key.casefold()
                        for marker in ("auth", "credential", "password", "secret", "token", "api_key")
                    )
                ]
                message = str(redact(message, sensitive_args))[:300]
                error_type = (
                    cause.type or type(cause).__name__
                    if isinstance(cause, ApplicationError)
                    else type(cause).__name__
                )
                raise ToolFailed(f"{call.tool_name} failed ({error_type}): {message}") from error

        raise AssertionError("Tool call exited its retry loop without returning or raising.")

    def outcome(self, cause: BaseException, tool: ToolDefinition, attempt: int) -> Outcome:
        root = self._root_cause(cause)
        # These are SDK termination decisions, including exhausted tool retries
        # and limits from a delegated run. Returning ToolFailed would let the
        # parent keep running after the SDK has explicitly stopped execution.
        if isinstance(root, (
            RunCancelled, CancelledError, asyncio.CancelledError, WorkspaceUnavailableError,
            UnexpectedModelBehavior, UsageLimitExceeded,
        )):
            return Outcome.ESCALATE

        error_type = root.type if isinstance(root, ApplicationError) and root.type else type(root).__name__
        if error_type in self.policy.escalate:
            return Outcome.ESCALATE

        if (
            isinstance(root, (TemporalTimeoutError, BuiltinTimeoutError, ConnectionError))
            and read_only_tool(tool)
            and attempt < self.policy.attempts
        ):
            return Outcome.RETRY
        return Outcome.TO_MODEL

    @staticmethod
    def _root_cause(error: BaseException) -> BaseException:
        while isinstance(error, ActivityError) and error.cause is not None:
            error = error.cause
        return error
