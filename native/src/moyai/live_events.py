"""A root-only UI projection carried by the SDK's native Workflow Streams."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, is_dataclass

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.durable_exec.temporal import workflow_stream_event_handler
from pydantic_ai.messages import (
    AgentStreamEvent,
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
)
from temporalio.contrib.workflow_streams import WorkflowStream

from .contracts import RunScope

PUBLIC_TOPIC = "moyai-public"
PUBLIC_STREAM_VERSION = "moyai-workflow-public-events-v1"
PUBLIC_UPDATE_PREFIXES = ("Plan", "Delegation", "AskUser", "EnqueuedMessages")
_public_stream: ContextVar[WorkflowStream | None] = ContextVar("moyai_public_stream", default=None)


@asynccontextmanager
async def public_event_sink(stream: WorkflowStream) -> AsyncIterator[None]:
    """Use the workflow's existing SDK stream; never share it across runs."""
    token = _public_stream.set(stream)
    try:
        yield
    finally:
        _public_stream.reset(token)


def public_workflow_event(event: AgentStreamEvent) -> bool:
    return isinstance(event, (FunctionToolCallEvent, FunctionToolResultEvent)) or (
        is_dataclass(event) and not isinstance(event, type)
        and type(event).__name__.startswith(PUBLIC_UPDATE_PREFIXES)
    )


def publish_workflow_event(event: AgentStreamEvent) -> None:
    stream = _public_stream.get()
    if stream is None:
        raise RuntimeError("The workflow's public event stream is not bound.")
    # Public Temporal SDK API: replay rebuilds the same ordered stream without
    # scheduling an activity and signaling its event back to this workflow.
    stream.topic(PUBLIC_TOPIC).publish(event)


def public_model_event(event: AgentStreamEvent) -> bool:
    if isinstance(event, (PartStartEvent, PartEndEvent)):
        return event.part.part_kind == "text"
    return isinstance(event, PartDeltaEvent) and event.delta.part_delta_kind == "text"


@dataclass(kw_only=True)
class ResponseStart(CapabilityEvent, namespace="moyai_ui", name="response_start"):
    run_id: str
    step: int


async def root_events(
    ctx: RunContext[RunScope], stream: AsyncIterable[AgentStreamEvent],
) -> AsyncIterator[AgentStreamEvent]:
    """Child events stay with the child; response indices are scoped by step."""
    started = False
    async for event in stream:
        if ctx.run_id != ctx.deps.root_run_id:
            continue
        if not started and isinstance(event, (PartStartEvent, PartDeltaEvent, PartEndEvent, FinalResultEvent)):
            started = True
            yield ResponseStart(run_id=ctx.deps.turn_id, step=ctx.run_step)
        if (
            not ctx.deps.workflow_public_events
            or public_model_event(event) or public_workflow_event(event)
        ):
            yield event


async def root_event_stream(ctx: RunContext[RunScope], stream: AsyncIterable[AgentStreamEvent]) -> None:
    # Keep the SDK's live model batching and the handler registered for old
    # histories. New workflow-origin events use the direct sink above.
    await workflow_stream_event_handler(PUBLIC_TOPIC)(ctx, root_events(ctx, stream))
