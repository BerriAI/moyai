"""Domain attributes for framework spans and the sandbox operations they call."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from opentelemetry import trace
from opentelemetry.trace import INVALID_SPAN, Span, StatusCode
from pydantic import TypeAdapter

from .contracts import PreparedTurn, RunScope
from .models import Session, Turn

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)


def scope_attributes(scope: RunScope) -> dict[str, str]:
    return {
        "gen_ai.agent.name": "moyai",
        "gen_ai.conversation.id": scope.conversation_id,
        "moyai.turn.id": scope.turn_id,
        "metadata": json.dumps({"moyai_session_id": scope.session_id, "moyai_turn_id": scope.turn_id}),
    }


def workspace_attributes(reference: dict[str, Any] | None) -> dict[str, str]:
    return {
        "pydantic_ai.workspace.provider": str(reference["provider"]),
        "pydantic_ai.workspace.id": str(reference["id"]),
    } if reference and reference.get("provider") in {"modal", "lambda", "local"} else {}


def failure(span: Span, error: BaseException) -> None:
    """Never copy exception messages, stacks, URLs or provider payloads."""
    if isinstance(error, asyncio.CancelledError):
        span.set_attribute("moyai.outcome", "cancelled")
        return
    blocked = isinstance(error, PermissionError) or (
        isinstance(error, HTTPException) and error.status_code in {401, 403, 409}
    )
    span.set_attribute("moyai.outcome", "blocked" if blocked else "failed")
    span.set_attribute("error.type", type(error).__name__)
    if isinstance(error, HTTPException):
        span.set_attribute("http.response.status_code", error.status_code)
    span.set_status(StatusCode.ERROR)


@contextmanager
def operation(
    database: Database, name: str, *, attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Use the process-local provider; tracing off remains a true no-op."""
    tracing = database.tracing
    if tracing is None:
        yield INVALID_SPAN
        return
    with tracing.instrumentation.tracer.start_as_current_span(
        name,
        attributes={"gen_ai.agent.name": "moyai", "moyai.outcome": "succeeded", **(attributes or {})},
        record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            failure(span, error)
            raise


async def task_state(database: Database, turn_id: str) -> None:
    """Annotate the existing Temporal activity with committed state, never payloads."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    try:
        async with asyncio.timeout(2):
            async with database.sessions() as db:
                turn = await db.get(Turn, turn_id)
                if turn is None:
                    return
                session = await db.get(Session, turn.session_id)
                attributes: dict[str, Any] = {
                    "metadata": json.dumps({"moyai_session_id": turn.session_id, "moyai_turn_id": turn.id}),
                    "moyai.turn.id": turn.id,
                    "moyai.task.state": turn.status,
                    "moyai.computer.keepalive_eligible": turn.status in {"running", "saving"},
                }
                if turn.preparation:
                    prepared = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(turn.preparation))
                    attributes.update(scope_attributes(prepared.scope))
                if session:
                    attributes["moyai.computer.save_warning"] = bool(session.save_warning)
                    attributes.update(workspace_attributes(json.loads(session.workspace or "null")))
                if turn.status == "waiting" and turn.conversation:
                    from pydantic_ai import ConversationTypeAdapter

                    conversation = ConversationTypeAdapter.validate_json(database.security.unseal(turn.conversation))
                    requests = conversation.deferred_tool_requests
                    tools = {call.tool_name for call in requests.calls} if requests else set()
                    attributes["moyai.wait.reason"] = (
                        "browser_login" if "request_browser_login" in tools else
                        "user_input" if "ask_user_question" in tools else
                        "approval" if requests and requests.approvals else "deferred"
                    )
                if turn.status == "failed":
                    span.set_attribute("moyai.outcome", "failed")
                    span.set_status(StatusCode.ERROR)
                span.set_attributes(attributes)
                span.add_event("moyai.task.state", attributes=attributes)
    except Exception as error:  # noqa: BLE001 - telemetry must not change task outcomes
        logger.warning("Could not record task state (%s).", type(error).__name__)
