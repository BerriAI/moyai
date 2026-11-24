from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.tools import RunContext

from ..contracts import RunScope
from .base import ContextSource

logger = logging.getLogger(__name__)
SOURCE_TIMEOUT_SECONDS = 20
SOURCE_LABELS = {
    "github": "GitHub repository context",
    "gitlab": "GitLab project context",
    "slack": "Slack conversation context",
    "learned_skills": "relevant learned procedures",
}


class SessionContext(AbstractCapability[RunScope]):
    id = "moyai_session_context"

    def __init__(self, sources: Sequence[ContextSource], *, timeout_seconds: float = SOURCE_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Context source timeout must be positive.")
        self.sources = tuple(sources)
        self.timeout_seconds = timeout_seconds

    def get_instructions(self) -> Callable[[RunContext[RunScope]], Awaitable[str]]:
        async def render(ctx: RunContext[RunScope]) -> str:
            return await self.render_sources(ctx)

        return render

    @durable_operation("render")
    async def render_sources(self, ctx: RunContext[RunScope]) -> str:
        results = await asyncio.gather(
            *(asyncio.wait_for(source.render(ctx.deps), self.timeout_seconds) for source in self.sources),
            return_exceptions=True,
        )
        rendered = tuple(
            result for result in results
            if isinstance(result, str) and result
        )
        failures = tuple(
            (source.name, result)
            for source, result in zip(self.sources, results, strict=True)
            if isinstance(result, BaseException)
        )
        for name, error in failures:
            if not isinstance(error, Exception):
                raise error
            logger.warning("Session context source %s failed (%s)", name, type(error).__name__)
        if not failures:
            return "\n\n".join(rendered)
        # Names come from application code, never provider payloads. Unknown
        # source implementations receive a generic label instead of exposing
        # their exception, identifiers, or configuration to the model.
        unavailable = sorted({SOURCE_LABELS.get(name, "additional context") for name, _ in failures})
        coverage = (
            "Context coverage: unavailable — " + ", ".join(unavailable) + ". "
            "Other context above remains usable. An unavailable source is not evidence that no relevant "
            "information exists. Use authorized read tools to retrieve essential missing evidence; "
            "if it remains unavailable, state the limitation when it affects the answer. "
            "Continue independent work. Never invent the missing content or request secrets in chat."
        )
        return "\n\n".join((*rendered, coverage))
