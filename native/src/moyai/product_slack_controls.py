"""Signed Slack answers use the SDK's existing deferred-result/resume protocol."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter, DeferredToolResults
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse, ask_user_result
from sqlalchemy import select

from .models import Event, Session, Turn, User
from .product_requester import answer_actor
from .slack_questions import SlackQuestions


def question_results(pending, value: str | dict[str, Any]) -> dict[str, Any]:
    calls = [call for call in pending.calls if call.tool_name == "ask_user_question"]
    requests = [AskUserRequest.from_tool_call(call) for call in calls]
    headers = [question.header for request in requests for question in request.questions]
    if not headers:
        raise HTTPException(409, "There is no ordinary question to answer. Use the secure form for credential requests.")
    if len(headers) != len(set(headers)):
        raise HTTPException(422, "These questions repeat a heading; answer them in the Moyai session.")
    if isinstance(value, dict):
        answers = value
    elif len(headers) == 1 and not value.lstrip().startswith("{"):
        answers = {headers[0]: value.strip()}
    else:
        try:
            answers = json.loads(value)
        except (ValueError, TypeError):
            raise HTTPException(422, "For multiple questions, reply /answer with a JSON object keyed by question heading.") from None
    if not isinstance(answers, dict) or set(answers) != set(headers):
        raise HTTPException(422, "Supply one answer for each current question heading.")
    result = {}
    for request in requests:
        response = []
        for question in request.questions:
            value = answers[question.header]
            labels = {option.label for option in question.options}
            if isinstance(value, str) and value.strip():
                choice = {"selected": [value]} if value in labels else {"custom_answer": value}
            elif isinstance(value, list) and value and all(isinstance(item, str) and item in labels for item in value):
                choice = {"selected": value}
            else:
                raise HTTPException(422, "Choose current option labels or a nonempty written answer.")
            response.append({"header": question.header, **choice})
        try:
            result[request.id] = ask_user_result(request, TypeAdapter(AskUserResponse).validate_python({"answers": response}))
        except ValueError:
            raise HTTPException(422, "The reply does not match the current question choices.") from None
    return result


def deferred(runtime, turn: Turn):
    if not turn.conversation:
        raise HTTPException(409, "This task is not waiting for an answer.")
    value = ConversationTypeAdapter.validate_json(runtime.security.unseal(turn.conversation)).deferred_tool_requests
    if value is None:
        raise HTTPException(409, "This task has no current deferred request.")
    return value


async def stored_replies(runtime, db, turn: Turn) -> tuple[dict, dict]:
    pending = deferred(runtime, turn)
    fingerprint = hashlib.sha256(TypeAdapter(Any).dump_json(pending)).hexdigest()
    row = await db.scalar(select(Event).where(Event.identity == "product-slack-replies:" + turn.id))
    if row is None:
        return {}, {}
    value = json.loads(runtime.security.unseal(row.content))
    if value.get("fingerprint") != fingerprint or value.get("actor_id") != turn.actor_id:
        raise HTTPException(409, "The saved answers belong to an earlier request.")
    return value.get("calls", {}), value.get("approvals", {})


async def record_replies(runtime, actor: User, turn: Turn, *, calls=None, approvals=None) -> None:
    pending = deferred(runtime, turn)
    fingerprint = hashlib.sha256(TypeAdapter(Any).dump_json(pending)).hexdigest()
    async with runtime.database.sessions.begin() as db:
        session = await db.scalar(select(Session).where(Session.id == turn.session_id).with_for_update())
        current = await db.get(Turn, turn.id)
        if (
            session is None or session.active_turn != turn.id or current is None or current.status != "waiting"
            or actor.id != current.actor_id
        ):
            raise HTTPException(409, "This request is no longer waiting for that reply.")
        await runtime.database._actor_product_grant(db, actor)
        existing_calls, existing_approvals = await stored_replies(runtime, db, current)
        value = {"fingerprint": fingerprint, "actor_id": actor.id,
                 "calls": {**existing_calls, **(calls or {})}, "approvals": {**existing_approvals, **(approvals or {})}}
        row = await db.scalar(select(Event).where(Event.identity == "product-slack-replies:" + turn.id))
        if row:
            row.content = runtime.security.seal(value)
        else:
            db.add(Event(session_id=turn.session_id, turn_id=turn.id, kind="slack_input",
                         identity="product-slack-replies:" + turn.id, content=runtime.security.seal(value)))


async def resume_replies(runtime, actor: User, turn: Turn) -> bool:
    pending = deferred(runtime, turn)
    async with runtime.database.sessions.begin() as db:
        calls, approvals = await stored_replies(runtime, db, turn)
        vault = getattr(runtime, "product_secrets", None)
        if vault is not None:
            calls.update(await vault.execution.resolved_calls(db, turn, actor))
    try:
        results = pending.build_results(calls=calls, approvals=approvals)
        if pending.remaining(results) is not None:
            return False
    except (ValueError, KeyError):
        return False
    await runtime.database.resume(
        actor, turn.session_id, turn.id, "slack-deferred:" + turn.id,
        TypeAdapter(DeferredToolResults).dump_python(results, mode="json"),
    )
    return True


async def answer(slack, sender: User, thread, value: str, *, continuing: bool = False) -> bool:
    async with slack.runtime.database.sessions() as db:
        session = await slack.runtime.database.allowed_session(db, thread.session_id, sender)
        turn = await db.get(Turn, session.active_turn) if session.active_turn else None
    if turn is None or turn.status != "waiting":
        raise HTTPException(409, "This session is not waiting for an answer.")
    actor = await answer_actor(slack.runtime, sender, turn)
    if not continuing:
        await record_replies(slack.runtime, actor, turn, calls=question_results(deferred(slack.runtime, turn), value))
    return await resume_replies(slack.runtime, actor, turn)


async def submit_choices(runtime, actor: User, turn: Turn, pending, state: dict[str, Any]) -> str | None:
    try:
        requests = SlackQuestions.requests(pending)
        if not requests:
            raise HTTPException(409, "There is no question to answer.")
        answers = SlackQuestions.answers(requests, state)
        calls = question_results(pending, answers)
        actor = await answer_actor(runtime, actor, turn)
        await record_replies(runtime, actor, turn, calls=calls)
        resumed = await resume_replies(runtime, actor, turn)
    except HTTPException as error:
        if error.status_code >= 500:
            raise
        return str(error.detail)
    if resumed:
        return None
    return "Your answer was saved. Complete the remaining approval or secure request to continue."
