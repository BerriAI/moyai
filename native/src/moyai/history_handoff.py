"""Settled agent checkpoints between executions of the same Temporal workflow.

Only a completed tool batch may cross this boundary. This is not crash recovery:
an interrupted or uncertain external action is never replayed from a snapshot.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, is_provider_valid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from temporalio import activity
from temporalio.contrib.workflow_streams import WorkflowStreamState
from temporalio.exceptions import ApplicationError

from .contracts import PreparedTurn, RunScope
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


@dataclass
class AgentCheckpoint:
    prepared: PreparedTurn
    conversation: str
    workspace: dict | None
    run_step: int
    output_retries_used: int
    tool_retries: dict[str, int] = field(default_factory=dict)


@dataclass
class HistoryHandoff:
    checkpoint: str
    stream_state: WorkflowStreamState | None = None
    steered: list[str] = field(default_factory=list)
    pending_steering: list[str] = field(default_factory=list)


def is_turn_run(run_id: str | None, turn_id: str) -> bool:
    if run_id == turn_id:
        return True
    suffix = (run_id or "").removeprefix(f"{turn_id}:segment:")
    return run_id != suffix and suffix.isascii() and suffix.isdecimal() and int(suffix) > 0


async def latest_turn_snapshot(runtime: Runtime, turn_id: str) -> ContinuableSnapshot | None:
    """The new segment may not have written its first snapshot yet."""
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Event).where(
            Event.turn_id == turn_id, Event.kind == "history_segment",
        ).order_by(Event.id.desc())))
    run_ids = [json.loads(runtime.security.unseal(row.content))["run_id"] for row in rows]
    for run_id in [*run_ids, turn_id]:
        snapshot = await runtime.stores.steps.latest_snapshot(run_id=run_id)
        if snapshot is not None:
            return snapshot
    return None


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
    # No plaintext checkpoint or credentials enter the history. The existing
    # immutable encrypted object store also makes activity redelivery idempotent.
    return CheckpointSave(await runtime.objects.put(TypeAdapter(AgentCheckpoint).dump_json(checkpoint)))


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
