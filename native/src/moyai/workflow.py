"""One durable execution owner. Pydantic AI owns every agent step."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    # Modal's import-time client setup belongs to the worker process. Workspace
    # I/O still runs in the native SDK's activities, never in workflow code.
    import modal  # noqa: F401
    from pydantic import TypeAdapter
    from pydantic_ai import (
        Conversation,
        ConversationTypeAdapter,
        DeferredToolRequests,
        DeferredToolResults,
        RunCancelled,
    )
    from pydantic_ai.agent import AbstractAgent
    from pydantic_ai.durable_exec.temporal import AgentEventStream, PydanticAIWorkflow
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.run import AgentRun
    from pydantic_ai.usage import UsageLimits
    from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError

    from .contracts import CompletedTurn, PreparedTurn, RunScope, SteeringInput
    from .images import prompt_content
    from .product_continuation_errors import public_preparation_message
    from .product_drain import DRAIN_VERSION, DrainDecision
    from .request_limits import unlimited_model_requests
    from .runtime import current
    from .workspace_recovery import VERSION as WORKSPACE_CHECKPOINT_VERSION


CONTROL = {
    "start_to_close_timeout": timedelta(seconds=60),
    "schedule_to_close_timeout": timedelta(minutes=3),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}
PREPARE_VERSION = "moyai-turn-preparation-timeout-v1"
PREPARE = {
    **CONTROL,
    "start_to_close_timeout": timedelta(minutes=20),
    "schedule_to_close_timeout": timedelta(minutes=20),
    "heartbeat_timeout": timedelta(seconds=30),
}


@workflow.defn(name="MoyaiPydanticTurnV1")
class TurnWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__: Sequence[AbstractAgent[Any, Any]] = ()

    @workflow.init
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.events = AgentEventStream(drain_timeout=timedelta(seconds=1))
        self.agent_run: AgentRun[RunScope, str | DeferredToolRequests] | None = None
        self.finished = False
        self.steered: set[str] = set()
        self.maintenance_notice = 0

    @workflow.signal
    def maintenance_changed(self, generation: int) -> None:
        # A signal is only a wake-up hint; the activity rechecks persisted CAS
        # intent. Duplicate, delayed or future hints never authorize execution.
        if type(generation) is int and generation >= 0:
            self.maintenance_notice += 1

    async def maintenance_checkpoint(self, turn_id: str, boundary: str) -> None:
        while True:
            notice = self.maintenance_notice
            decision = await workflow.execute_activity(
                "moyai_product_drain_checkpoint_v1", args=[turn_id, boundary],
                result_type=DrainDecision, **CONTROL,
            )
            if not decision.paused:
                return
            await workflow.wait_condition(lambda notice=notice: self.maintenance_notice != notice)

    @workflow.signal
    async def steer(self, turn_id: str) -> None:
        if turn_id in self.steered:
            return
        self.steered.add(turn_id)
        try:
            value = await workflow.execute_activity(
                "moyai_prepare_steering_v1",
                args=[self.turn_id, turn_id], result_type=SteeringInput, **CONTROL,
            )
        except ActivityError:
            self.steered.discard(turn_id)
            workflow.logger.warning("Steering preparation did not settle; the saved input remains pending.")
            return
        await workflow.wait_condition(lambda: self.agent_run is not None or self.finished)
        enqueue_id = None
        if not self.finished and self.agent_run is not None:
            try:
                enqueue_id = self.agent_run.enqueue(
                    ModelRequest(
                        parts=[UserPromptPart(content=prompt_content(value.prompt, value.images) or "")],
                        metadata={
                            "moyai_input_id": value.turn_id, "moyai_parent_turn_id": self.turn_id,
                        },
                    ),
                    priority=value.mode,
                )
            except UserError:
                pass  # Native final drain won the race; preserve it as a queued follow-up.
        await workflow.execute_activity(
            "moyai_steering_receipt_v1", args=[turn_id, enqueue_id], **CONTROL
        )

    @workflow.run
    async def run(self, turn_id: str) -> dict:
        prepared: PreparedTurn | None = None
        try:
            checkpoint_workspace = workflow.patched(WORKSPACE_CHECKPOINT_VERSION)
            maintenance = workflow.patched(DRAIN_VERSION)
            if maintenance:
                await self.maintenance_checkpoint(turn_id, "before_prepare")
            prepared = await workflow.execute_activity(
                "moyai_prepare_turn_v1",
                args=[turn_id, True] if checkpoint_workspace else [turn_id], result_type=PreparedTurn,
                **(PREPARE if workflow.patched(PREPARE_VERSION) else CONTROL),
            )
            if prepared is None:
                raise ValueError("Turn preparation returned no authorized input.")
            runtime = current()
            conversation = (
                ConversationTypeAdapter.validate_json(prepared.conversation)
                if prepared.conversation else Conversation(conversation_id=prepared.scope.conversation_id)
            )
            workspace = (
                TypeAdapter(WorkspaceRef).validate_python(prepared.workspace)
                if prepared.workspace else None
            )
            deferred_results = (
                TypeAdapter(DeferredToolResults).validate_python(prepared.deferred_results)
                if prepared.deferred_results else None
            )
            async with self.events, runtime.agent.iter(
                prompt_content(prepared.prompt, prepared.images),
                conversation=conversation,
                run_id=turn_id,
                model=runtime.models[prepared.scope.model],
                deps=prepared.scope,
                deferred_tool_results=deferred_results,
                workspace=workspace,
                usage_limits=UsageLimits(
                    request_limit=None if unlimited_model_requests() else runtime.settings.model_request_limit,
                ),
            ) as agent_run:
                self.agent_run = agent_run
                # This is the public SDK iterator, not another model/tool dispatch loop.
                boundary = 0
                async for _ in agent_run:
                    # This is an SDK graph boundary, not another agent loop.
                    # A running delegation batch settles before this returns.
                    # Completed answers/deferrals still finish their bookkeeping.
                    if maintenance and agent_run.result is None:
                        await self.maintenance_checkpoint(turn_id, f"native_node:{boundary}")
                        boundary += 1
                result = agent_run.result
                if result is None:
                    raise RuntimeError("The SDK ended without a run result.")
            self.finished = True
            await workflow.wait_condition(lambda: workflow.all_handlers_finished())
            waiting = isinstance(result.output, DeferredToolRequests)
            reference = (
                TypeAdapter(WorkspaceRef).dump_python(result.workspace.ref, mode="json")
                if result.workspace.ref else None
            )
            accepted = await workflow.execute_activity(
                "moyai_save_answer_v1",
                CompletedTurn(
                    scope=prepared.scope,
                    conversation=ConversationTypeAdapter.dump_json(result.conversation).decode(),
                    output="" if waiting else str(result.output),
                    workspace=reference,
                    usage=asdict(result.usage),
                    deferred=waiting,
                    checkpoint_workspace=checkpoint_workspace,
                ),
                result_type=bool,
                **CONTROL,
            )
            if not accepted:
                await workflow.execute_activity(
                    "moyai_stopped_turn_v1",
                    args=[turn_id, ConversationTypeAdapter.dump_json(result.conversation).decode()],
                    **CONTROL,
                )
                return {"turn_id": turn_id, "status": "stopped"}
            if accepted and (not waiting or checkpoint_workspace):
                try:
                    await workflow.execute_activity(
                        "moyai_save_computer_v1", args=[turn_id, reference],
                        start_to_close_timeout=timedelta(
                            minutes=17 if reference and reference.get("provider") == "lambda" else 3,
                        ),
                        retry_policy=RetryPolicy(maximum_attempts=1),
                    )
                except ActivityError:
                    # Worker loss/timeouts bypass the activity's own error handler.
                    # The answer is already committed; only snapshot settlement failed.
                    await workflow.execute_activity(
                        "moyai_settle_answer_v1", turn_id, **CONTROL,
                    )
            return {"turn_id": turn_id, "status": "waiting" if waiting else "completed"}
        except RunCancelled as error:
            self.finished = True
            conversation = ConversationTypeAdapter.dump_json(Conversation(
                messages=error.all_messages(), usage=error.usage,
                conversation_id=prepared.scope.conversation_id if prepared else turn_id,
            )).decode()
            await workflow.execute_activity(
                "moyai_stopped_turn_v1", args=[turn_id, conversation], **CONTROL
            )
            return {"turn_id": turn_id, "status": "stopped"}
        except asyncio.CancelledError as error:
            self.finished = True
            cancelled = RunCancelled.from_cancellation(error)
            conversation = None
            if cancelled is not None and prepared is not None:
                conversation = ConversationTypeAdapter.dump_json(Conversation(
                    messages=cancelled.all_messages(), usage=cancelled.usage,
                    conversation_id=prepared.scope.conversation_id,
                )).decode()
            await asyncio.shield(workflow.execute_activity(
                "moyai_stopped_turn_v1", args=[turn_id, conversation], **CONTROL
            ))
            # Temporal remains the cancellation and recovery authority.
            raise
        except Exception as error:
            self.finished = True
            message = (
                "The conversation's computer is no longer available. "
                "Your saved answers and downloads are preserved."
            ) if isinstance(error, WorkspaceUnavailableError) else (
                "The agent could not complete this turn. "
                "Inspect the saved activity before repeating a possibly completed action."
            )
            if prepared is None and workflow.patched("moyai-public-preparation-errors-v1"):
                message = public_preparation_message(error) or message
            await workflow.execute_activity(
                "moyai_fail_turn_v1",
                args=[turn_id, message],
                **CONTROL,
            )
            if isinstance(error, ApplicationError):
                raise
            raise ApplicationError(
                "The agent turn failed; inspect its recorded activity before retrying.",
                type=type(error).__name__, non_retryable=True,
            ) from error

