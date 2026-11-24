"""Give the native summarizer both ends of old, long tool results."""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelRequestPart, ToolReturnPart
from pydantic_ai_harness.compaction import SummarizingCompaction
from temporalio import workflow

from .contracts import RunScope

VERSION = "moyai-compaction-tool-evidence-v1"
EXCERPT_CHARS = 495  # The SDK reserves five of its 500 characters for "[...]".
OMISSION = "\n[Middle omitted; full tool result remains in saved history.]\n"


def excerpt(text: str) -> str:
    """Keep the original budget, with room for commands and trailing outcomes."""
    if len(text) <= EXCERPT_CHARS:
        return text
    # The shell's process/log-location footer itself is about 280 characters.
    # Reserve enough tail to include the observed outcome immediately above it.
    head = 80
    tail = EXCERPT_CHARS - head - len(OMISSION)
    return text[:head] + OMISSION + text[-tail:]


class EvidenceCompaction(SummarizingCompaction[RunScope]):
    """Use the SDK's summarization, tool pairing, receipts and durable operation.

    Only the summarizer's view is excerpted. Recent messages returned unchanged
    by the SDK are mapped back to their originals, including structured content,
    files, tool metadata and exact raw output. Persisted history is never edited.
    """

    async def compact(
        self, messages: list[ModelMessage], ctx: RunContext[RunScope],
    ) -> list[ModelMessage]:
        if workflow.in_workflow() and not workflow.patched(VERSION):
            return await super().compact(messages, ctx)
        originals: dict[int, ModelMessage] = {}
        prepared: list[ModelMessage] = []
        for message in messages:
            if not isinstance(message, ModelRequest):
                prepared.append(message)
                continue
            parts: list[ModelRequestPart] = []
            changed = False
            for part in message.parts:
                if isinstance(part, ToolReturnPart):
                    # The public serializer renders file references rather than
                    # binary bytes; leave short/structured results untouched.
                    content, _ = part.model_response_str_and_user_content(wrap_if_error=False)
                    if len(content) > 500:
                        # The SDK reads the first 500 characters for its summary.
                        # Retain the remainder here so its receipt token estimate
                        # still accounts for the size of the dropped result.
                        part = replace(part, content=excerpt(content) + content[EXCERPT_CHARS:])
                        changed = True
                parts.append(part)
            view = replace(message, parts=parts) if changed else message
            originals[id(view)] = message
            prepared.append(view)
        compacted = await super().compact(prepared, ctx)
        return [originals.get(id(message), message) for message in compacted]
