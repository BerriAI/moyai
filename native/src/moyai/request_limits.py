"""Remove request-count caps without changing other native usage controls."""

from __future__ import annotations

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from temporalio import workflow

from .contracts import RunScope

VERSION = "moyai-unlimited-model-requests-v1"


def unlimited_model_requests() -> bool:
    # Existing histories must retain their original termination decisions.
    return not workflow.in_workflow() or workflow.patched(VERSION)


class UnlimitedModelRequests(AbstractCapability[RunScope]):
    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        # Coder's self-delegations do not pass the parent's UsageLimits to the
        # pinned SDK. for_run runs after its default limits are resolved and
        # receives the same limits object used by the agent graph.
        if unlimited_model_requests() and ctx.usage_limits is not None:
            ctx.usage_limits.request_limit = None
        return self
