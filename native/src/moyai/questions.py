"""Clarification answers and optional deadlines around the native deferred protocol."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter, DeferredToolRequests, DeferredToolResults
from pydantic_ai_harness.ask_user import (
    TIMED_OUT,
    AskUserAnswer,
    AskUserRequest,
    AskUserResponse,
    ask_user_result,
)
from sqlalchemy import select

from .models import Event, Session, Turn, User, now

if TYPE_CHECKING:
    from .database import Database

OPTIONAL_EVENT = "optional_question"


def pending_questions(database: Database, turn: Turn) -> DeferredToolRequests | None:
    if not turn.conversation:
        return None
    return ConversationTypeAdapter.validate_json(database.security.unseal(turn.conversation)).deferred_tool_requests


def ordinary_questions(pending: DeferredToolRequests | None) -> bool:
    return bool(
        pending and pending.calls and not pending.approvals
        and all(call.tool_name == "ask_user_question" for call in pending.calls)
    )


def can_reply_with_text(pending: DeferredToolRequests | None) -> bool:
    return bool(
        ordinary_questions(pending) and pending
        and sum(len(AskUserRequest.from_tool_call(call).questions) for call in pending.calls) == 1
    )


def text_results(pending: DeferredToolRequests, value: str) -> dict[str, Any]:
    if not can_reply_with_text(pending):
        raise HTTPException(409, "Answer the questions or approval requests in their forms.")
    request = AskUserRequest.from_tool_call(pending.calls[0])
    response = AskUserResponse(answers=(AskUserAnswer(header=request.questions[0].header, custom_answer=value),))
    try:
        result = pending.build_results(calls={request.id: ask_user_result(request, response)})
    except ValueError:
        raise HTTPException(422, "Enter a nonempty text answer without control characters.") from None
    return TypeAdapter(DeferredToolResults).dump_python(result, mode="json")


def optional_questions(pending: DeferredToolRequests | None) -> bool:
    return bool(
        ordinary_questions(pending) and pending
        and all((pending.metadata.get(call.tool_call_id) or {}).get("moyai_question_required") is False
                for call in pending.calls)
    )


def record_optional_deadline(database: Database, db: Any, turn: Turn) -> None:
    if not optional_questions(pending_questions(database, turn)):
        return
    deadline = (datetime.fromisoformat(now()) + timedelta(
        seconds=database.settings.optional_question_timeout_seconds,
    )).isoformat()
    db.add(Event(
        session_id=turn.session_id, turn_id=turn.id, kind=OPTIONAL_EVENT,
        identity=f"optional-question:{turn.id}", content=database.security.seal({"deadline": deadline}),
    ))


async def expire_optional_questions(database: Database, *, after: int = 0) -> int:
    """A persisted deadline survives restarts; ordinary resume performs all live fences."""
    async with database.sessions() as db:
        pairs = (await db.execute(select(Event, Turn).join(Turn, Turn.id == Event.turn_id).join(
            Session, Session.active_turn == Turn.id,
        ).where(
            Event.kind == OPTIONAL_EVENT, Event.id > after,
            Turn.status == "waiting", Session.status == "waiting",
            Session.deleted.is_(False),
        ).order_by(Event.id).limit(100))).all()
    for event, turn in pairs:
        value = json.loads(database.security.unseal(event.content))
        if value["deadline"] > now():
            continue
        pending = pending_questions(database, turn)
        if not optional_questions(pending) or pending is None:
            continue
        try:
            async with database.sessions() as db:
                actor = await db.get(User, turn.actor_id)
                if actor is None or not actor.enabled:
                    continue
                grant = await database._saved_product_grant(db, turn)
                if grant is not None:
                    cast(Any, actor).product_grant = grant
            result = pending.build_results(calls={call.tool_call_id: TIMED_OUT for call in pending.calls})
            await database.resume(
                actor, turn.session_id, turn.id, f"question-timeout:{turn.id}",
                TypeAdapter(DeferredToolResults).dump_python(result, mode="json"),
            )
        except PermissionError:
            continue
        except HTTPException as error:
            if error.status_code not in {401, 403, 404, 409}:
                raise
            # A reply, Stop, changed permission or paused swarm wins over timeout.
            continue
    # Old paused or revoked requests must not starve newer eligible deadlines.
    return pairs[-1][0].id if len(pairs) == 100 else 0
