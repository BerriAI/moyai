"""A second synthetic provider exercises shared admission, not a GitHub webhook."""

import json

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from moyai.contracts import NewMessage, NewSession
from moyai.inbound_events import InboundEvent
from moyai.message_sources import memory_text
from moyai.models import Message, MessageSource, Turn, Upload


async def test_github_event_uses_shared_admission_context_attachments_and_receipts(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    raw = b"representative issue attachment"
    digest = await runtime.objects.put(raw)
    async with runtime.database.sessions.begin() as db:
        upload = Upload(owner_id=actor.id, name="issue.txt", digest=digest, media_type="text/plain", size=len(raw))
        db.add(upload)
        await db.flush()
    event = InboundEvent(
        source="github", sender_id=actor.id, deduplication_key="issue-comment-123",
        text="Please fix this issue.", attachments=(upload.id,),
        provenance={"repository": "example/repo", "comment_id": "123", "internal": "private provider metadata"},
    )
    body = NewMessage.from_event(event).with_model_input(event.text + "\nUntrusted issue context.")
    turn = await runtime.database.submit(actor, session.id, body)
    assert (await runtime.database.submit(actor, session.id, body)).id == turn.id
    rows = await runtime.database.messages(actor, session.id)
    assert len(rows) == 1 and rows[0]["content"] == event.text and rows[0]["is_self"]
    assert rows[0]["attachments"][0]["id"] == upload.id
    assert "private provider metadata" not in json.dumps(rows)
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    assert "Untrusted issue context." in prepared.prompt
    async with runtime.database.sessions() as db:
        assert (await db.get(Upload, upload.id)).session_id == session.id
        message = await db.scalar(select(Message).where(Message.turn_id == turn.id))
        assert await memory_text(runtime.database, db, message) == event.text
        source = await db.get(MessageSource, message.identity)
        assert source.kind == "github" and not source.mirror
        assert json.loads(runtime.security.unseal(source.data))["repository"] == "example/repo"
    changed = NewMessage.from_event(event.model_copy(update={"text": "A different request"}))
    with pytest.raises(HTTPException) as conflict:
        await runtime.database.submit(actor, session.id, changed)
    assert conflict.value.status_code == 409


@pytest.mark.parametrize("mutation", ["sender", "key", "prompt", "attachments"])
async def test_event_cannot_change_admitted_identity_or_payload(runtime, actor, mutation):
    session = await runtime.database.create_session(actor, NewSession())
    event = InboundEvent(source="github", sender_id=actor.id, deduplication_key="comment-1", text="Fix issue")
    if mutation == "sender":
        event = event.model_copy(update={"sender_id": "another-actor"})
    body = NewMessage.from_event(event)
    if mutation == "key":
        body.request_key = "github-message:comment-2"
    elif mutation == "prompt":
        body.prompt = "Changed prompt"
    elif mutation == "attachments":
        body.attachments = ["unverified-file"]
    with pytest.raises(ValueError, match="Inbound event does not match"):
        await runtime.database.submit(actor, session.id, body)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0


async def test_event_does_not_grant_access_to_a_private_chat(runtime, actor):
    from test_sidebar import member_client
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    async with member_client(runtime) as (_, other):
        body = NewMessage.from_event(InboundEvent(
            source="github", sender_id=other.id, deduplication_key="comment-1", text="Read private chat",
        ))
        with pytest.raises(HTTPException) as denied:
            await runtime.database.submit(other, session.id, body)
        assert denied.value.status_code == 404
