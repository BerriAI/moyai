from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import select
from temporalio.api.common.v1 import Payload

from moyai.contracts import NewMessage, NewSession
from moyai.models import Connection, Message, Session, Turn, User, new_id
from moyai.security import password_hash
from moyai.storage import EncryptedPayloadCodec


async def test_real_sdk_composition_persists_steps_and_workspace(runtime, active):
    session, turn, prepared = active
    result = await runtime.agent.run(
        prepared.prompt, deps=prepared.scope,
        conversation_id=session.id, run_id=turn.id,
    )
    assert result.output == "Task completed."
    snapshot = await runtime.stores.steps.latest_snapshot(run_id=turn.id)
    assert snapshot is not None and snapshot.state == "complete"
    assert snapshot.messages
    assert result.conversation.conversation_id == session.id
    assert result.workspace is not None


async def test_concurrent_duplicate_submission_has_one_turn_and_message(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    body = NewMessage(prompt="Do this once.", request_key="same-request")
    a, b = await asyncio.gather(
        runtime.database.submit(actor, session.id, body),
        runtime.database.submit(actor, session.id, body),
    )
    assert a.id == b.id
    async with runtime.database.sessions() as db:
        assert len((await db.scalars(select(Turn))).all()) == 1
        assert len((await db.scalars(select(Message))).all()) == 1


async def test_request_identity_cannot_change_model_or_connections(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    await runtime.database.submit(actor, session.id, NewMessage(prompt="Task", request_key="same"))
    with pytest.raises(HTTPException) as caught:
        await runtime.database.submit(
            actor, session.id, NewMessage(prompt="Task", request_key="same", mode="asap")
        )
    assert caught.value.status_code == 409


async def test_concurrent_dispatch_claims_only_one_turn_per_session(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    for index in range(2):
        await runtime.database.submit(
            actor, session.id, NewMessage(prompt=f"Task {index}", request_key=str(index))
        )
    claims = await asyncio.gather(runtime.database.claim_next(), runtime.database.claim_next())
    assert len([claim for claim in claims if claim is not None]) == 1


@pytest.mark.parametrize("visibility", [None, "shared", "private"])
async def test_session_visibility_controls_another_admin_access(runtime, actor, visibility):
    body = NewSession() if visibility is None else NewSession(visibility=visibility)
    session = await runtime.database.create_session(actor, body)
    async with runtime.database.sessions.begin() as db:
        other = User(
            id=new_id(), username="another", display_name="Other administrator",
            password=password_hash("another-password-123"), role="admin",
        )
        db.add(other)
    if visibility == "private":
        with pytest.raises(HTTPException) as caught:
            await runtime.database.session(other, session.id)
        assert caught.value.status_code == 404
    else:
        assert session.visibility == "shared"
        assert (await runtime.database.session(other, session.id)).id == session.id


async def test_preparation_retries_keep_original_conversation(runtime, active):
    session, turn, prepared = active
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Session, session.id)
        row.conversation = runtime.security.seal("a later value must not replace the admitted input")
        row.last_actor_id = prepared.scope.actor_id
        row.last_model = prepared.scope.model
    retried = await runtime.database.prepare(turn.id)
    assert retried == prepared


async def test_stop_revokes_authority_before_worker_acknowledgment(runtime, actor, active):
    session, turn, prepared = active
    await runtime.database.authorize_scope(prepared.scope)
    workflow_id = await runtime.database.stop(actor, session.id)
    assert workflow_id == turn.workflow_id
    with pytest.raises(PermissionError):
        await runtime.database.authorize_scope(prepared.scope)
    await runtime.database.submit(actor, session.id, NewMessage(prompt="Next", request_key="next"))
    assert await runtime.database.claim_next() is None
    await runtime.database.stopped(turn.id)
    assert await runtime.database.claim_next() is not None


async def test_revoked_connection_cannot_dispatch(runtime, actor):
    async with runtime.database.sessions.begin() as db:
        connection = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Repository",
            credential=runtime.security.seal("not-a-real-provider-token"),
        )
        db.add(connection)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read the repository", request_key="grant", connections=[connection.id]
    ))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Connection, connection.id)
        saved.enabled = False
        saved.revision += 1
    with pytest.raises(HTTPException):
        await runtime.database.authorize_scope(prepared.scope)


async def test_answer_survives_snapshot_failure(runtime, actor, active):
    session, turn, prepared = active
    conversation = ConversationTypeAdapter.dump_json(Conversation(
        conversation_id=session.id,
        messages=[ModelRequest(parts=[UserPromptPart("Task")]), ModelResponse(parts=[TextPart("Done")])],
    )).decode()
    assert await runtime.database.save_result(
        prepared.scope, conversation=conversation, output="Done",
        workspace=None, usage={"requests": 1},
    )
    await runtime.database.settle(turn.id, warning="Snapshot unavailable.")
    messages = await runtime.database.messages(actor, session.id)
    assert messages[-1]["content"] == "Done"
    saved = await runtime.database.session(actor, session.id)
    assert saved.save_warning == "Snapshot unavailable."


async def test_steering_fences_old_tool_dispatch(runtime, actor, active):
    session, turn, prepared = active
    correction = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read only; don't edit.", mode="asap", request_key="correction"
    ))
    assert correction.parent_turn == turn.id
    with pytest.raises(PermissionError):
        await runtime.database.authorize_scope(prepared.scope, input_id=turn.id)
    await runtime.database.authorize_scope(prepared.scope, input_id=correction.id)


async def test_payload_codec_encrypts_and_offloads_without_plaintext(runtime):
    codec = EncryptedPayloadCodec(runtime.security, runtime.objects, threshold=100)
    original = [
        Payload(metadata={"encoding": b"json/plain"}, data=b'"small secret"'),
        Payload(metadata={"encoding": b"json/plain"}, data=json.dumps("private data " * 1000).encode()),
    ]
    encoded = await codec.encode(original)
    assert all(b"private data" not in item.data and b"small secret" not in item.data for item in encoded)
    assert encoded[1].metadata["encoding"] == b"moyai/encrypted-reference-v1"
    assert await codec.decode(encoded) == original


async def test_object_reference_cannot_escape_storage(runtime):
    with pytest.raises(ValueError):
        await runtime.objects.get("../../private-key")
