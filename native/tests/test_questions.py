"""Deferred question ordering, reply identity and durable optional deadlines."""

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter, DeferredToolRequests
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from sqlalchemy import select

from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import Event, Message, Session, Turn, User
from moyai.questions import expire_optional_questions, text_results


async def waiting(runtime, actor, *, optional=False, extra=None):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Choose a color.", request_key="first"))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    call = ToolCallPart("ask_user_question", {"questions": [{
        "header": "Color", "question": "Blue or green?",
        "options": [{"label": "Blue"}, {"label": "Green"}],
    }]}, tool_call_id="color")
    pending = DeferredToolRequests(
        calls=[call, *([extra] if extra and extra.tool_name != "write_file" else [])],
        approvals=[extra] if extra and extra.tool_name == "write_file" else [],
        metadata={"color": {"moyai_question_required": False}} if optional else {},
    )
    conversation = Conversation(
        conversation_id=prepared.scope.conversation_id,
        messages=[ModelRequest(parts=[UserPromptPart("Choose a color.")]), ModelResponse(parts=[call])],
        deferred_tool_requests=pending,
    )
    assert await runtime.database.save_result(
        prepared.scope, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        output="", workspace=None, usage={}, deferred=True,
    )
    return session, turn, pending


async def expire_deadline(runtime, turn):
    async with runtime.database.sessions.begin() as db:
        event = await db.scalar(select(Event).where(Event.identity == f"optional-question:{turn.id}"))
        assert event is not None
        event.content = runtime.security.seal({"deadline": "2000-01-01T00:00:00+00:00"})


async def test_answer_is_reserved_before_five_older_followups(runtime, actor):
    session, original, pending = await waiting(runtime, actor)
    followups = [await runtime.database.submit(
        actor, session.id, NewMessage(prompt=f"Follow-up {i}", request_key=f"queued-{i}", mode="when_idle"),
    ) for i in range(5)]
    resumed = await runtime.database.resume(actor, session.id, original.id, "answer", text_results(pending, "Blue"))
    async with runtime.database.sessions() as db:
        assert (await db.get(Session, session.id)).active_turn == resumed.id
    claimed = await runtime.database.claim_batch(10)
    assert [turn.id for turn in claimed] == [resumed.id]
    prepared = await runtime.database.prepare(resumed.id)
    assert prepared.prompt == "" and prepared.deferred_results is not None
    resolved = Conversation(conversation_id=prepared.scope.conversation_id)
    assert await runtime.database.save_result(
        prepared.scope, conversation=ConversationTypeAdapter.dump_json(resolved).decode(),
        output="Blue selected.", workspace=None, usage={},
    )
    await runtime.database.settle(resumed.id)
    next_turn = await runtime.database.claim_next()
    assert next_turn.id == followups[0].id
    assert (await runtime.database.prepare(next_turn.id)).deferred_results is None


async def test_typed_answer_is_an_idempotent_resume_and_a_visible_message(runtime, actor):
    session, original, _ = await waiting(runtime, actor)
    body = NewMessage(prompt="Blue, please.", request_key="typed-answer", reply_to=original.id)
    first = await runtime.database.submit(actor, session.id, body)
    retry = await runtime.database.submit(actor, session.id, body)
    assert first.id == retry.id and first.deferred_results
    async with runtime.database.sessions() as db:
        messages = list(await db.scalars(select(Message).where(Message.turn_id == first.id)))
        assert (await db.get(Turn, original.id)).status == "resumed"
    assert len(messages) == 1 and runtime.security.unseal(messages[0].content).decode() == "Blue, please."
    assert runtime.security.unseal(first.prompt).decode() == ""
    with pytest.raises(HTTPException) as error:
        await runtime.database.submit(actor, session.id, body.model_copy(update={"request_key": "stale"}))
    assert error.value.status_code == 409


async def test_five_concurrent_asap_messages_answer_once_and_preserve_the_rest(runtime, actor):
    session, original, _ = await waiting(runtime, actor)
    turns = await asyncio.gather(*(
        runtime.database.submit(actor, session.id, NewMessage(
            prompt=f"Blue. Additional detail {i}.", mode="asap", request_key=f"burst-{i}",
        )) for i in range(5)
    ))
    answers = [turn for turn in turns if turn.deferred_results]
    assert len(answers) == 1 and len({turn.id for turn in turns}) == 5
    assert [turn.id for turn in await runtime.database.claim_batch(10)] == [answers[0].id]
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, original.id)).status == "resumed"
        assert len(list(await db.scalars(select(Message).where(Message.session_id == session.id)))) == 6


async def test_optional_deadline_survives_delayed_dispatch_and_never_selects_an_option(runtime, actor):
    session, original, _ = await waiting(runtime, actor, optional=True)
    await expire_optional_questions(runtime.database)
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, original.id)).status == "waiting"
    await expire_deadline(runtime, original)
    await expire_optional_questions(runtime.database)
    await expire_optional_questions(runtime.database)
    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn).where(Turn.session_id == session.id)))
        resumed = next(turn for turn in turns if turn.id != original.id)
        assert (await db.get(Session, session.id)).active_turn == resumed.id
    assert len(turns) == 2
    result = json.loads(runtime.security.unseal(resumed.deferred_results))
    assert "did not answer in time" in result["calls"]["color"]
    assert "Blue" not in json.dumps(result) and "Green" not in json.dumps(result)


@pytest.mark.parametrize("extra", [
    None,
    ToolCallPart("write_file", {"path": "private"}, tool_call_id="approval"),
    ToolCallPart("credentials_request", {}, tool_call_id="secret"),
    ToolCallPart("ask_user_question", {"questions": [{
        "header": "Required", "question": "Which account?",
        "options": [{"label": "A"}, {"label": "B"}],
    }]}, tool_call_id="required"),
])
async def test_required_questions_and_mixed_requests_never_timeout(runtime, actor, extra):
    session, original, _ = await waiting(runtime, actor, optional=extra is not None, extra=extra)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(Event).where(Event.identity == f"optional-question:{original.id}")) is None
    await expire_optional_questions(runtime.database)
    async with runtime.database.sessions() as db:
        assert (await db.get(Session, session.id)).active_turn == original.id
        assert (await db.get(Turn, original.id)).status == "waiting"


@pytest.mark.parametrize("extra", [
    ToolCallPart("write_file", {}, tool_call_id="approval"),
    ToolCallPart("credentials_request", {}, tool_call_id="secret"),
])
async def test_chat_cannot_resolve_approvals_or_credentials(runtime, actor, extra):
    session, original, _ = await waiting(runtime, actor, extra=extra)
    queued = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="yes", mode="asap", request_key="not-an-approval"),
    )
    assert queued.deferred_results is None
    with pytest.raises(HTTPException):
        await runtime.database.submit(actor, session.id, NewMessage(
            prompt="yes", reply_to=original.id, request_key="explicit-not-an-approval",
        ))
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, original.id)).status == "waiting"


async def test_timeout_racing_a_reply_produces_one_resume(runtime, actor):
    session, original, _ = await waiting(runtime, actor, optional=True)
    await expire_deadline(runtime, original)
    outcomes = await asyncio.gather(
        expire_optional_questions(runtime.database),
        runtime.database.submit(actor, session.id, NewMessage(
            prompt="Blue", reply_to=original.id, request_key="human",
        )), return_exceptions=True,
    )
    assert all(not isinstance(value, BaseException) or (
        isinstance(value, HTTPException) and value.status_code == 409
    ) for value in outcomes)
    async with runtime.database.sessions() as db:
        turns = list(await db.scalars(select(Turn).where(Turn.session_id == session.id)))
    assert len(turns) == 2 and sum(turn.deferred_results is not None for turn in turns) == 1


async def test_stop_wins_over_an_expired_question(runtime, actor):
    session, original, _ = await waiting(runtime, actor, optional=True)
    await expire_deadline(runtime, original)
    await runtime.database.stop(actor, session.id)
    await expire_optional_questions(runtime.database)
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, original.id)).status == "stopped"
        assert len(list(await db.scalars(select(Turn).where(Turn.session_id == session.id)))) == 1


async def test_http_reply_is_requester_only_and_exposes_a_safe_composer_target(runtime, actor):
    session, original, _ = await waiting(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        other = User(username="viewer", display_name="Viewer", password="unused")
        db.add(other)
        await db.flush()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)), base_url="http://local",
    ) as client:
        for user in (other, actor):
            cookie, csrf = runtime.security.issue_session(user.id, user.auth_version)
            client.cookies.set(COOKIE, cookie)
            client.headers["x-moyai-csrf"] = csrf
            view = (await client.get(f"/api/sessions/{session.id}")).json()
            assert view.get("can_reply_with_text", False) is (user.id == actor.id)
            response = await client.post(f"/api/sessions/{session.id}/messages", json={
                "prompt": "Blue", "request_key": "typed-http", "reply_to": original.id,
            })
            assert response.status_code == (202 if user.id == actor.id else 403)
