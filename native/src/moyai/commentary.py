"""Presentation and completion of native assistant text; never private reasoning."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import ModelMessage, ModelResponse, NativeToolCallPart, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.output import OutputContext
from sqlalchemy.exc import IntegrityError
from temporalio import workflow

from .contracts import RunScope
from .models import Event

if TYPE_CHECKING:
    from .database import Database

Phase = Literal["commentary", "final_answer"]


def enabled() -> bool:
    # Existing workflows keep their recorded completion decisions on replay.
    return not workflow.in_workflow() or workflow.patched("moyai-commentary-v1")


def text_phase(details: dict[str, Any] | None) -> Phase | None:
    phase = (details or {}).get("phase")
    return phase if phase in ("commentary", "final_answer") else None


class Commentary(AbstractCapability[RunScope]):
    """Use SDK lifecycle hooks without replacing its model/tool loop or history."""

    id = "moyai_commentary"

    def __init__(self, database: Database | None = None):
        self.database = database

    @durable_operation("save")
    async def save(self, scope: RunScope, step: int, messages: list[dict[str, str]]) -> None:
        """An idempotent public projection; SDK conversations remain unmodified."""
        if self.database is None:
            return
        for index, message in enumerate(messages):
            identity = f"commentary:{scope.turn_id}:{step}:{index}"
            try:
                async with self.database.sessions.begin() as db:
                    db.add(Event(
                        session_id=scope.session_id, turn_id=scope.turn_id,
                        kind="commentary", identity=identity,
                        content=self.database.security.seal({**message, "id": identity}),
                    ))
            except IntegrityError:
                # A completed native activity may be retried after a lost result.
                # Only a confirmed duplicate is success; other integrity errors fail.
                from sqlalchemy import select
                async with self.database.sessions() as db:
                    if await db.scalar(select(Event.id).where(Event.identity == identity)) is None:
                        raise

    async def after_model_request(
        self, ctx: RunContext[RunScope], *,
        request_context: ModelRequestContext, response: ModelResponse,
    ) -> ModelResponse:
        if not enabled():
            return response
        if self.database is not None and ctx.run_id == ctx.deps.turn_id:
            progress = saved_commentary([replace(response, run_id=ctx.deps.turn_id)], ctx.deps.turn_id)
            if progress:
                await self.save(ctx.deps, ctx.run_step, progress)
        text = [part for part in response.parts if isinstance(part, TextPart) and part.content.strip()]
        work = any(isinstance(part, (ToolCallPart, NativeToolCallPart)) for part in response.parts)
        if text and not work and all(text_phase(part.provider_details) == "commentary" for part in text):
            # The SDK retains the response in history and bounds this continuation
            # by its normal output retry budget. Never report a preamble as success.
            raise ModelRetry(
                "That was an intermediate progress update. Continue with the necessary tools, "
                "or provide your completed answer marked final_answer."
            )
        return response

    async def after_output_process(
        self, ctx: RunContext[RunScope], *, output_context: OutputContext, output: Any,
    ) -> Any:
        if not enabled() or not isinstance(output, str):
            return output
        response = next((item for item in reversed(ctx.messages) if isinstance(item, ModelResponse)), None)
        if response is None:
            return output
        text = [part for part in response.parts if isinstance(part, TextPart)]
        if any(text_phase(part.provider_details) == "commentary" for part in text):
            # Only the rendered answer changes. Native messages (and their phase
            # metadata) remain intact for continuation, compaction and replay.
            return "\n\n".join(
                part.content for part in text
                if text_phase(part.provider_details) != "commentary" and part.content
            )
        return output


def saved_commentary(messages: list[ModelMessage], run_id: str) -> list[dict[str, str]]:
    """Read public progress from native snapshots, including unphased providers."""
    result: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, ModelResponse) or message.run_id != run_id:
            continue
        has_tools = any(isinstance(part, (ToolCallPart, NativeToolCallPart)) for part in message.parts)
        for part_index, part in enumerate(message.parts):
            if not isinstance(part, TextPart) or not part.content.strip():
                continue
            phase = text_phase(part.provider_details)
            if phase == "commentary" or (phase is None and has_tools):
                result.append({
                    "id": f"{run_id}:{message.provider_response_id or index}:{part_index}",
                    "text": part.content, "phase": "commentary",
                    "created_at": message.timestamp.isoformat(),
                })
    return result
