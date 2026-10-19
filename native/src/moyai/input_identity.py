"""Server-authored input identity shared by native tools and child admission."""

from contextvars import ContextVar

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelRequest, TextContent, UserPromptPart

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
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if not isinstance(part, UserPromptPart) or isinstance(part.content, str):
                    continue
                for content in part.content:
                    if (
                        isinstance(content, TextContent) and isinstance(content.metadata, dict)
                        and content.metadata.get("moyai_parent_turn_id") == ctx.deps.turn_id
                    ):
                        identity = content.metadata.get("moyai_input_id", identity)
    return identity


def identified_prompt(content: str | list, turn_id: str, parent_id: str) -> list:
    """Bind identity to the user part: SDK request merging discards request metadata.

    Only the authenticated workflow constructs these parts. Ordinary user text
    cannot supply metadata, and model adapters receive only the original text.
    """
    parts = [content] if isinstance(content, str) else list(content)
    metadata = {"moyai_input_id": turn_id, "moyai_parent_turn_id": parent_id}
    return [TextContent(item, metadata=metadata) if isinstance(item, str) else item for item in parts]
