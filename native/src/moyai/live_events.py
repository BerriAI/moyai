"""A root-only UI projection carried by the SDK's native Workflow Streams."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass

from pydantic_ai import CapabilityEvent, RunContext
from pydantic_ai.durable_exec.temporal import workflow_stream_event_handler
from pydantic_ai.messages import (
    AgentStreamEvent,
    FinalResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
)

from .contracts import RunScope

PUBLIC_TOPIC = "moyai-public"


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
        if ctx.run_id != ctx.deps.turn_id:
            continue
        if not started and isinstance(event, (PartStartEvent, PartDeltaEvent, PartEndEvent, FinalResultEvent)):
            started = True
            yield ResponseStart(run_id=ctx.deps.turn_id, step=ctx.run_step)
        yield event


async def root_event_stream(ctx: RunContext[RunScope], stream: AsyncIterable[AgentStreamEvent]) -> None:
    # This is the SDK's public event handler, including its activity-to-workflow
    # batching and replay semantics. Moyai supplies only presentation scoping.
    await workflow_stream_event_handler(PUBLIC_TOPIC)(ctx, root_events(ctx, stream))
