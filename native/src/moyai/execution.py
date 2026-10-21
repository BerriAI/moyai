"""Application authority at the Temporal activity execution boundary."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

from .contracts import GuidanceChanged, RunScope
from .database import Database
from .inference_usage import INFERENCE_ACTIVITY, InferenceActivity
from .performance import computer_timing


class AuthorizedActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, database: Database):
        super().__init__(next)
        self.database = database

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        name = info.activity_type
        # These are SDK-persisted activity names, fixed by our pinned agent/capability IDs.
        # Evidence must still settle after Stop; business operations must recheck authority.
        evidence = (
            "__capability__step_persistence__" in name
            or name in {
                "agent__moyai__capability__moyai_effects__confirm",
                "agent__moyai__capability__moyai_effects__record_pr_publication",
                "agent__moyai__capability__moyai_effects__not_dispatched",
                "agent__moyai__capability__moyai_effects__browser_state",
            }
        )
        scope = None
        if name.startswith("agent__moyai__") and not evidence:
            scope = next((arg for arg in input.args if isinstance(arg, RunScope)), None)
            if scope is None:
                raise ApplicationError(
                    "The SDK activity did not carry an authorized run scope.",
                    type="MoyaiMissingExecutionScope", non_retryable=True,
                )
            input_id = None
            # Delegation enforces guidance in admission with an explicit input
            # identity. Ordinary SDK tools carry it in their saved context.
            if input.args and name.endswith("__call_tool"):
                context = getattr(input.args[0], "serialized_run_context", {})
                input_id = (context.get("metadata") or {}).get("moyai_input_id")
            try:
                # Admission owns both original-identity receipt recovery and
                # live authorization for NEW work. Prechecking a retry here
                # would discard a child committed before steering or Stop.
                if name not in {
                    "agent__moyai__capability__moyai_interactive_delegation__start",
                    "agent__moyai__capability__moyai_interactive_delegation__send_message",
                }:
                    await self.database.authorize_scope(scope, input_id=input_id)
            except (PermissionError, HTTPException) as error:
                raise ApplicationError(
                    "Execution authority changed before this activity attempt.",
                    type="MoyaiGuidanceChanged" if isinstance(error, GuidanceChanged) else "MoyaiPermissionDenied",
                    non_retryable=True,
                ) from error
        if scope is not None and name.endswith(("__model_request", "__model_request_stream")):
            serialized = getattr(input.args[0], "serialized_run_context", {}) if input.args else {}
            usage = InferenceActivity(
                self.database, scope,
                f"{info.workflow_namespace}/{info.workflow_id}/{info.workflow_run_id}/{info.activity_id}/{info.attempt}",
                # A workflow retry/reset changes run_id, but does not authorize
                # paying again for this activity of the same admitted turn.
                logical_identity=f"{info.workflow_namespace}/{info.workflow_id}/{scope.root_run_id}/{info.activity_id}",
                foreground=isinstance(serialized, dict) and serialized.get("run_id") == scope.root_run_id,
            )
            token = INFERENCE_ACTIVITY.set(usage)
            try:
                result = await self.next.execute_activity(input)
                await usage.result(result)
                return result
            except Exception as error:
                if usage.admission_error is not None:
                    raise ApplicationError(
                        "Product inference admission refused; no new model request was dispatched.",
                        type="MoyaiInferenceAdmissionDenied", non_retryable=True,
                    ) from error
                raise
            finally:
                INFERENCE_ACTIVITY.reset(token)
        if scope is not None and name.endswith("__workspace__call") and input.args:
            wire = input.args[0]
            serialized = getattr(wire, "serialized_run_context", {})
            if (
                getattr(getattr(wire, "call", None), "method", None) == "ensure"
                and isinstance(serialized, dict) and serialized.get("run_id") == scope.root_run_id
            ):
                async with computer_timing(
                    self.database, scope, "reuse" if getattr(wire, "ref", None) else "cold", ensure=True,
                ) as timing:
                    result = await self.next.execute_activity(input)
                    if getattr(result, "error", None) is not None:
                        timing.status = "failed"
                    return result
        return await self.next.execute_activity(input)


class ExecutionAuthority(Interceptor):
    """Run on every attempt, including infrastructure retries on another worker."""

    def __init__(self, database: Database):
        self.database = database

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return AuthorizedActivity(next, self.database)
