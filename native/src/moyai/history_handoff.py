"""Settled agent checkpoints between executions of the same Temporal workflow.

Only a completed tool batch may cross this boundary. This is not crash recovery:
an interrupted or uncertain external action is never replayed from a snapshot.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, ToolCallPart, ToolReturnPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, StepEvent, is_provider_valid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamState
from temporalio.exceptions import ApplicationError

from .contracts import PreparedTurn, RunScope
from .deployment_handoff import DeploymentHandoff
from .models import Effect, Event

if TYPE_CHECKING:
    from .runtime import Runtime

VERSION = "moyai-agent-history-handoff-v1"


@dataclass
class HistoryLimits:
    max_bytes: int
    max_events: int


@dataclass
class CheckpointSave:
    """A refused checkpoint leaves the paused agent alive in this execution."""

    reference: str | None = None
    # None identifies recorded results from the original handoff protocol.
    # New saves explicitly distinguish a history-only rollover from an upgrade.
    deployment_allowed: bool | None = None


@dataclass
class AgentCheckpoint:
    prepared: PreparedTurn
    conversation: str
    workspace: dict | None
    run_step: int
    output_retries_used: int
    tool_retries: dict[str, int] = field(default_factory=dict)
    deployment_handoff: DeploymentHandoff | None = None
    allow_history_only: bool = False


@dataclass
class HistoryHandoff:
    checkpoint: str
    stream_state: WorkflowStreamState | None = None
    steered: list[str] = field(default_factory=list)
    pending_steering: list[str] = field(default_factory=list)
    deployment_handoff: DeploymentHandoff | None = None
    pending_deployment_handoff: DeploymentHandoff | None = None
    maintenance_workspace: bool = False


def is_turn_run(run_id: str | None, turn_id: str) -> bool:
    if run_id == turn_id:
        return True
    suffix = (run_id or "").removeprefix(f"{turn_id}:segment:")
    return run_id != suffix and suffix.isascii() and suffix.isdecimal() and int(suffix) > 0


async def turn_run_ids(runtime: Runtime, turn_id: str) -> list[str]:
    """Only server-recorded continuations of this authorized turn."""
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Event).where(
            Event.turn_id == turn_id, Event.kind == "history_segment",
        ).order_by(Event.id)))
    run_ids = [json.loads(runtime.security.unseal(row.content))["run_id"] for row in rows]
    return list(dict.fromkeys([turn_id, *(run_id for run_id in run_ids if is_turn_run(run_id, turn_id))]))


async def latest_turn_snapshot(
    runtime: Runtime, turn_id: str, *, include_interrupted: bool = False,
) -> ContinuableSnapshot | None:
    """The new segment may not have written its first snapshot yet."""
    for run_id in reversed(await turn_run_ids(runtime, turn_id)):
        snapshot = await runtime.stores.steps.latest_snapshot(run_id=run_id, include_interrupted=include_interrupted)
        if snapshot is not None:
            return snapshot
    return None


async def turn_events(runtime: Runtime, turn_id: str) -> list[StepEvent]:
    runs = await turn_run_ids(runtime, turn_id)
    batches = await asyncio.gather(*(runtime.stores.steps.list_events(run_id=run_id) for run_id in runs))
    events = [event for batch in batches for event in batch]
    return sorted(events, key=lambda event: event.timestamp)


async def failed_attempts(runtime: Runtime, turn_id: str, events: list[StepEvent]) -> list[StepEvent]:
    """Recover only failed calls missing from the native lifecycle event log."""
    runs = await turn_run_ids(runtime, turn_id)
    recorded = {event.tool_call_id for event in events if event.tool_call_id}
    # Before-dispatch hooks can reject a call before the SDK emits its lifecycle
    # event. Its saved failed result is still authoritative evidence of an attempt.
    snapshot = await latest_turn_snapshot(runtime, turn_id, include_interrupted=True)
    if snapshot is None:
        return []
    run_id = None
    calls = {}
    missing = []
    for message in snapshot.messages:
        if isinstance(message, ModelResponse):
            run_id = message.run_id if message.run_id in runs else None
            calls = {part.tool_call_id: part.tool_name for part in message.parts if isinstance(part, ToolCallPart)}
        elif isinstance(message, ModelRequest) and run_id:
            for part in message.parts:
                if (
                    isinstance(part, (ToolReturnPart, RetryPromptPart)) and part.tool_call_id
                    and part.tool_call_id not in recorded and part.tool_name
                    and calls.get(part.tool_call_id) == part.tool_name
                    and (isinstance(part, RetryPromptPart) or getattr(part, "outcome", None) == "failed")
                ):
                    missing.append(StepEvent(
                        run_id=run_id, kind="tool_call_failed", step_index=0,
                        tool_call_id=part.tool_call_id, tool_name=part.tool_name,
                        timestamp=message.timestamp or snapshot.timestamp,
                    ))
                    recorded.add(part.tool_call_id)
    return missing


@activity.defn(name="moyai_history_limits_v1")
async def history_limits() -> HistoryLimits:
    from .runtime import current

    settings = current().settings
    return HistoryLimits(settings.history_continue_bytes, settings.history_continue_events)


@activity.defn(name="moyai_save_history_checkpoint_v1")
async def save_history_checkpoint(checkpoint: AgentCheckpoint) -> CheckpointSave:
    from .runtime import current

    runtime = current()
    scope = checkpoint.prepared.scope
    conversation = ConversationTypeAdapter.validate_json(checkpoint.conversation)
    if not is_provider_valid(conversation.messages) or conversation.deferred_tool_requests is not None:
        return CheckpointSave()
    try:
        await runtime.database.authorize_scope(scope)
    except (PermissionError, HTTPException) as error:
        raise ApplicationError(
            "The turn is no longer authorized to continue.",
            type="MoyaiHistoryAuthorizationDenied", non_retryable=True,
        ) from error
    async with runtime.database.sessions() as db:
        pending = await db.scalar(select(Effect.id).where(
            Effect.turn_id == scope.turn_id, Effect.state == "pending",
        ).limit(1))
    if pending is not None:
        return CheckpointSave()
    deployment_allowed = checkpoint.deployment_handoff is not None
    if deployment_allowed:
        # A graph boundary alone does not establish that a gateway attempt or
        # its billing receipt settled. Retain the source deployment if either
        # ledger still has an unresolved external outcome.
        from .inference_usage import InferenceUsage
        from .product_drain import SETTLED_REQUESTS
        from .product_inference import ProductInferenceRequest

        ledgers: list[type[InferenceUsage | ProductInferenceRequest]] = [InferenceUsage]
        if runtime.product_inference is not None:
            ledgers.append(ProductInferenceRequest)
        async with runtime.database.sessions() as db:
            for ledger in ledgers:
                if await db.scalar(select(ledger.id).where(
                    ledger.turn_id == scope.turn_id,
                    ledger.status.not_in(SETTLED_REQUESTS),
                ).limit(1)) is not None:
                    deployment_allowed = False
                    if not checkpoint.allow_history_only:
                        return CheckpointSave()
                    break
    # No plaintext checkpoint or credentials enter the history. The existing
    # immutable encrypted object store also makes activity redelivery idempotent.
    # Control hints are carried separately by HistoryHandoff. A history-only
    # save must not look like an approved deployment in the agent snapshot.
    snapshot = replace(checkpoint, deployment_handoff=None, allow_history_only=False)
    return CheckpointSave(
        await runtime.objects.put(TypeAdapter(AgentCheckpoint).dump_json(snapshot)),
        deployment_allowed=deployment_allowed,
    )


@activity.defn(name="moyai_load_history_checkpoint_v1")
async def load_history_checkpoint(turn_id: str, reference: str) -> AgentCheckpoint:
    from .runtime import current

    runtime = current()
    checkpoint = TypeAdapter(AgentCheckpoint).validate_json(await runtime.objects.get(reference))
    scope = checkpoint.prepared.scope
    if scope.turn_id != turn_id or scope.history_segment < 0:
        raise PermissionError("This history checkpoint belongs to another turn.")
    conversation = ConversationTypeAdapter.validate_json(checkpoint.conversation)
    if conversation.conversation_id != scope.conversation_id or conversation.deferred_tool_requests:
        raise PermissionError("This checkpoint is not a settled continuation of this turn.")
    await runtime.database.authorize_scope(scope)
    scope.history_segment += 1
    await record_segment(runtime, scope)
    return checkpoint


async def record_segment(runtime: Runtime, scope: RunScope) -> None:
    identity = f"history_segment:{scope.root_run_id}"
    try:
        async with runtime.database.sessions.begin() as db:
            db.add(Event(
                session_id=scope.session_id, turn_id=scope.turn_id,
                kind="history_segment", identity=identity,
                content=runtime.security.seal({"run_id": scope.root_run_id}),
            ))
    except IntegrityError:
        async with runtime.database.sessions() as db:
            if await db.scalar(select(Event.id).where(Event.identity == identity)) is None:
                raise


HISTORY_ACTIVITIES = [history_limits, save_history_checkpoint, load_history_checkpoint]
