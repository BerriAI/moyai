from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import RunContext

from ..contracts import RunScope
from .base import ContextSource

logger = logging.getLogger(__name__)


class SessionContext(AbstractCapability[RunScope]):
    def __init__(self, sources: Sequence[ContextSource]) -> None:
        self.sources = tuple(sources)

    def get_instructions(self) -> Callable[[RunContext[RunScope]], Awaitable[str]]:
        async def render(ctx: RunContext[RunScope]) -> str:
            results = await asyncio.gather(
                *(source.render(ctx.deps) for source in self.sources),
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
                logger.warning("Session context source %s failed: %s", name, error)
            return "\n\n".join(rendered)

        return render
