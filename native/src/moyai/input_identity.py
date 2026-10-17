"""Server-authored input identity shared by native tools and child admission."""

from contextvars import ContextVar

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelRequest

from .contracts import RunScope

DELEGATED_INPUT: ContextVar[str | None] = ContextVar("moyai_delegated_input", default=None)


def input_identity(ctx: RunContext[RunScope]) -> str:
    identity = DELEGATED_INPUT.get() or ctx.deps.turn_id
    for message in ctx.messages:
        if (
            isinstance(message, ModelRequest) and message.metadata
            and message.metadata.get("moyai_parent_turn_id") == ctx.deps.turn_id
        ):
            identity = message.metadata.get("moyai_input_id", identity)
    return identity
