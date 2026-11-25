"""Dot identity and delivery contracts. Fixtures are supplementary to live proof."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolReturnPart
from sqlalchemy import select, update

from moyai.automations.product_automation_models import ProductAutomationBinding, ProductAutomationTurn
from moyai.contracts import Grant, NewMessage, NewSession
from moyai.database import grant_fingerprint
from moyai.dot import validate_decision
from moyai.dot_models import DotAttentionTurn, DotNotification, PersonalDot
from moyai.models import Connection, Message, Session, Turn, User
from moyai.workspace_recovery import WorkspaceCheckpoint, checkpoint, validate_checkpoint


async def observer(runtime, actor, destination=None, connection=None, automation=None, *, discovery=False):
    database = runtime.database
    destination = destination or await database.dots.open(actor)
    async with database.sessions.begin() as db:
        if connection is None:
            connection = Connection(
                owner_id=actor.id, kind="mcp", name="Reviewed reader", credential=runtime.security.seal("synthetic"),
                read_only=True, policy=json.dumps({"allowed_tools": ["read_item"]}),
            )
            db.add(connection)
            await db.flush()
        if automation is None:
            automation = ProductAutomationBinding(
                id=uuid4().hex, owner_id=actor.id, authority_version="synthetic",
                source_revision=1, generation=1, state="active", definition_digest="a" * 64,
            )
            db.add(automation)
        grants = await database.dots.grants(db, actor.id, [connection.id])
        session = await database.create_session(actor, NewSession(visibility="private"), db=db)
        session.surface = "automation"
        turn = await database.admission.submit(actor, session.id, NewMessage(
            prompt="Check the review deadline.", request_key=uuid4().hex, connections=[connection.id],
        ), db=db, automation_generated=True)
        await database.dots.bind_attention(
            db, turn, automation_id=automation.id, generation=1,
            destination=destination, grants_key=grant_fingerprint(grants),
            discovery=discovery,
        )
    await database.claim_batch(20)
    prepared = await database.admission.prepare(turn.id)
    return destination, connection, automation, prepared


def decision(prepared, *, action="notify", version="2026-11-24T19:00:00Z", quote="Review is due tomorrow."):
    grant = prepared.scope.grants[0]
    item = {"connection_id": grant.id, "source": "event-123", "source_version": version, "quote": quote}
    output = json.dumps({
        "action": action, "message": "Your review is due tomorrow. Shall I prepare the notes?" if action == "notify" else "",
        "evidence": [item] if action == "notify" else [],
    })
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{grant.id[:8]}_read_item", tool_call_id="source",
            content={"id": item["source"], "updated_at": version, "title": quote},
        )]),
    ])).decode()
    return output, conversation


async def finish(runtime, prepared, **values):
    output, conversation = decision(prepared, **values)
    accepted = await runtime.database.save_result(
        prepared.scope, output=output, conversation=conversation, workspace=None, usage={},
    )
    if accepted:
        await runtime.database.settle(prepared.scope.turn_id)
    return accepted


async def test_open_is_private_persistent_and_concurrent(runtime, actor):
    ids = await asyncio.gather(*(runtime.database.dots.open(actor) for _ in range(4)))
    assert len(set(ids)) == 1
    async with runtime.database.sessions.begin() as db:
        session = await db.get(Session, ids[0])
        assert (session.owner_id, session.visibility, session.surface) == (actor.id, "private", "web")
        session.archived = True
    assert await runtime.database.dots.open(actor) == ids[0]
    async with runtime.database.sessions.begin() as db:
        session = await db.get(Session, ids[0])
        session.deleted = True
    assert await runtime.database.dots.open(actor) != ids[0]


async def test_quiet_check_does_not_mutate_dot_or_inherit_user_accounts(runtime, actor):
    destination = await runtime.database.dots.open(actor)
    async with runtime.database.sessions() as db:
        original = await db.get(Session, destination)
        fields = ("revision", "updated_at", "active_turn", "status", "conversation", "last_grants")
        before = {field: getattr(original, field) for field in fields}
    _, _, _, prepared = await observer(runtime, actor, destination)
    assert prepared.scope.session_id != destination and prepared.scope.attention
    assert prepared.conversation is None and prepared.workspace is None
    assert await finish(runtime, prepared, action="quiet")
    async with runtime.database.sessions() as db:
        after = await db.get(Session, destination)
        assert {field: getattr(after, field) for field in fields} == before
        assert (await db.get(DotAttentionTurn, prepared.scope.turn_id)).outcome == "quiet"
    reply = await runtime.database.admission.submit(actor, destination, NewMessage(prompt="What next?", request_key=uuid4().hex))
    await runtime.database.claim_batch(10)
    normal = await runtime.database.admission.prepare(reply.id)
    assert not normal.scope.attention and normal.scope.grants == []
    async with runtime.database.sessions() as db:
        assert await db.get(ProductAutomationTurn, reply.id) is None


async def test_notification_dedupes_source_version_and_survives_concurrent_reply(runtime, actor):
    destination, connection, automation, prepared = await observer(runtime, actor)
    normal_turn = await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="Help with the launch.", request_key=uuid4().hex,
    ))
    await runtime.database.claim_batch(10)
    normal = await runtime.database.admission.prepare(normal_turn.id)
    assert normal.scope.dot_notification_cursor == 0
    assert await finish(runtime, prepared)
    # A normal answer prepared before delivery must not overwrite the new notice.
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelResponse(parts=[TextPart("I can help with the launch.")]),
    ])).decode()
    await runtime.database.save_result(normal.scope, conversation=conversation, output="I can help with the launch.", workspace=None, usage={})
    await runtime.database.settle(normal.scope.turn_id)
    async with runtime.database.sessions() as db:
        session = await db.get(Session, destination)
        assert runtime.security.unseal(session.conversation).decode() == conversation
        assert (await db.get(PersonalDot, actor.id)).checkpoint_cursor == 0
        assert session.last_grants == normal.scope.grants_key
        notifications = list(await db.scalars(select(DotNotification).where(DotNotification.session_id == destination)))
        assert len(notifications) == 1
        message = await db.get(Message, notifications[0].message_id)
        assert message.role == "assistant" and message.author_id is None and message.session_id == destination
    reply = await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="Prepare the review notes.", request_key=uuid4().hex,
    ))
    await runtime.database.claim_batch(10)
    next_reply = await runtime.database.admission.prepare(reply.id)
    assert next_reply.conversation.count("Your review is due tomorrow.") == 1
    assert next_reply.scope.dot_notification_cursor == notifications[0].message_id
    await runtime.database.save_result(
        next_reply.scope, conversation=next_reply.conversation, output="Preparing notes.", workspace=None, usage={},
    )
    await runtime.database.settle(reply.id)
    later = await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="Continue.", request_key=uuid4().hex,
    ))
    await runtime.database.claim_batch(10)
    later_reply = await runtime.database.admission.prepare(later.id)
    assert later_reply.conversation.count("Your review is due tomorrow.") == 1
    _, _, _, duplicate = await observer(runtime, actor, destination, connection, automation)
    assert await finish(runtime, duplicate, quote="Review has the same deadline.")
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, duplicate.scope.turn_id)).outcome == "duplicate"
    _, _, _, changed = await observer(runtime, actor, destination, connection, automation)
    assert await finish(runtime, changed, version="2026-11-24T20:00:00Z")
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, changed.scope.turn_id)).outcome == "notified"


async def test_notification_preserves_saved_computer_checkpoint_and_enters_next_reply(runtime, actor):
    destination, _, _, prepared = await observer(runtime, actor)
    earlier = await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="Save the launch notes.", request_key=uuid4().hex,
    ))
    await runtime.database.claim_batch(10)
    normal = await runtime.database.admission.prepare(earlier.id)
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelResponse(parts=[TextPart("Saved the launch notes on your computer.")]),
    ])).decode()
    reference = {"provider": "modal", "id": "saved-computer"}
    await runtime.database.save_result(
        normal.scope, conversation=conversation, output="Saved the notes.", workspace=reference, usage={},
    )
    await runtime.database.settle(earlier.id)
    async with runtime.database.sessions.begin() as db:
        session = await db.get(Session, destination)
        saved = WorkspaceCheckpoint(
            session_id=destination, workspace=reference,
            scope=normal.scope.workspace_key, turn_id=earlier.id,
            result_fingerprint=(await db.get(Turn, earlier.id)).result_fingerprint,
            conversation_digest=hashlib.sha256(conversation.encode()).hexdigest(), image_id="saved-image",
        ).model_dump_json()
        session.snapshot = saved
    assert await finish(runtime, prepared)
    async with runtime.database.sessions() as db:
        session = await db.get(Session, destination)
        assert session.snapshot == saved
        assert (await validate_checkpoint(
            runtime.database, db, session, normal.scope, reference,
        )).image_id == "saved-image"
    reply = await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="Update the notes for that review.", request_key=uuid4().hex,
    ))
    await runtime.database.claim_batch(10)
    next_reply = await runtime.database.admission.prepare(reply.id)
    assert "Saved the launch notes" in next_reply.conversation
    assert next_reply.conversation.count("Your review is due tomorrow.") == 1
    # Repeated preparation returns the same input even if new mail arrives later.
    assert (await runtime.database.admission.prepare(reply.id)).conversation == next_reply.conversation
    async with runtime.database.sessions() as db:
        session = await db.get(Session, destination)
        assert runtime.security.unseal(session.conversation).decode() == conversation
        assert (await validate_checkpoint(
            runtime.database, db, session, next_reply.scope, reference,
        )).image_id == "saved-image"


async def test_notification_during_computer_capture_does_not_cancel_checkpoint(runtime, actor, monkeypatch):
    destination, _, _, observed = await observer(runtime, actor)
    turn = await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="Save my work.", request_key=uuid4().hex,
    ))
    await runtime.database.claim_batch(10)
    prepared = await runtime.database.admission.prepare(turn.id)
    reference = {"provider": "modal", "id": "sb-original"}
    conversation = ConversationTypeAdapter.dump_json(Conversation()).decode()
    assert await runtime.database.save_result(
        prepared.scope, conversation=conversation, output="Saved.", workspace=reference, usage={},
        checkpoint_workspace=True,
    )

    async def capture(*_args, **_kwargs):
        assert await finish(runtime, observed)
        return "im-checkpoint-with-concurrent-notice"

    monkeypatch.setattr(runtime.database.answers, "snapshots", SimpleNamespace(capture=AsyncMock(side_effect=capture)))
    await runtime.database.answers.save_computer(turn.id, reference, product=False)
    async with runtime.database.sessions() as db:
        session = await db.get(Session, destination)
        assert session.status == "idle" and session.save_warning is None
        assert checkpoint(session.snapshot).image_id == "im-checkpoint-with-concurrent-notice"
        assert await db.scalar(select(DotNotification.message_id).where(DotNotification.session_id == destination))
        assert (await db.get(PersonalDot, actor.id)).checkpoint_cursor == 0


@pytest.mark.parametrize("change", ["pause", "connection", "archived", "owner", "deleted", "mapping"])
async def test_publication_rechecks_authority(runtime, actor, change):
    destination, connection, automation, prepared = await observer(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        if change == "pause":
            await db.execute(update(ProductAutomationBinding).where(ProductAutomationBinding.id == automation.id)
                             .values(generation=2, state="paused"))
        elif change == "connection":
            await db.execute(update(Connection).where(Connection.id == connection.id).values(revision=2))
        elif change == "mapping":
            other = await runtime.database.create_session(actor, NewSession(visibility="private"), db=db)
            (await db.get(PersonalDot, actor.id)).session_id = other.id
        elif change == "owner":
            other = User(username="other", display_name="Other", password="unusable", role="member")
            db.add(other)
            await db.flush()
            (await db.get(Session, destination)).owner_id = other.id
        else:
            setattr(await db.get(Session, destination), change, True)
    assert not await finish(runtime, prepared)
    async with runtime.database.sessions() as db:
        assert await db.get(DotNotification, prepared.scope.turn_id) is None


async def test_attention_evidence_and_worker_input_are_restricted(runtime, actor):
    _, _, _, prepared = await observer(runtime, actor)
    output, conversation = decision(prepared)
    assert validate_decision(output, conversation, prepared.scope.grants)[0].action == "notify"
    with pytest.raises(ValueError):
        validate_decision(output, conversation.replace("_read_item", "_write_item"), prepared.scope.grants)
    with pytest.raises(ValueError):
        validate_decision(output.replace("event-123", "event-invented"), conversation, prepared.scope.grants)
    with pytest.raises(HTTPException, match="immutable"):
        await runtime.database.admission.submit(actor, prepared.scope.session_id, NewMessage(
            prompt="Broaden access", request_key=uuid4().hex,
        ))


def test_evidence_uses_one_record_and_canonical_account_identity():
    accounts = [Grant(id=str(uuid4()), kind="mcp", revision=1, credential="", policy={"allowed_tools": ["read_item"]})
                for _ in range(2)]
    record = {"id": "item-1", "html_url": "https://provider.test/items/1", "updated_at": "v1", "version": "revision-1", "title": "Review due."}

    def resolve(account, *, source="item-1", version="v1", content=record):
        return validate_decision(json.dumps({
            "action": "notify", "message": "Prepare the review.", "evidence": [{
                "connection_id": account.id, "source": source, "source_version": version, "quote": "Review due.",
            }],
        }), ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{account.id[:8]}_read_item", tool_call_id="read",
            content={"content": [{"type": "text", "text": json.dumps(content)}]},
        )])])).decode(), accounts)[1]

    assert resolve(accounts[0]) == resolve(accounts[0], source=record["html_url"])
    assert resolve(accounts[0]) == resolve(accounts[0], version="revision-1")
    assert resolve(accounts[0]) != resolve(accounts[1])
    with pytest.raises(ValueError, match="one connected account"):
        resolve(accounts[0], content=[
            {"id": "item-1", "updated_at": "v1", "title": "Nothing due."},
            {"id": "item-2", "updated_at": "v2", "title": "Review due."},
        ])
    with pytest.raises(ValueError, match="one connected account"):
        resolve(accounts[0], content={"id": "real-item", "body": json.dumps(record)})


def test_focused_evidence_survives_sdk_decoded_mcp_text_blocks():
    account = Grant(id=str(uuid4()), kind="mcp", revision=1, credential="", policy={"allowed_tools": ["read_item"]})
    first = {"id": "item-1", "updated_at": "v1", "title": "Review due."}
    second = {"id": "item-2", "updated_at": "v2", "title": "Another item."}
    decision = json.dumps({
        "action": "notify", "message": "Prepare the review.", "evidence": [{
            "connection_id": account.id, "source": "item-1", "source_version": "v1", "quote": "Review due.",
        }],
    })
    fingerprints = []
    for content in [
        [first, second],
        [[first], [second]],  # SDK decoding of separate JSON-array text blocks.
        {"content": [{"type": "text", "text": json.dumps([row])} for row in (first, second)]},
    ]:
        conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{account.id[:8]}_read_item", tool_call_id="read", content=content,
        )])])).decode()
        fingerprints.append(validate_decision(decision, conversation, [account])[1])
    assert fingerprints[0] is not None and len(set(fingerprints)) == 1


@pytest.mark.parametrize("patch", [{"read_only": False}, {"shared": True}, {"policy": "{}"}])
async def test_attention_rejects_broad_connection_policies(runtime, actor, patch):
    async with runtime.database.sessions.begin() as db:
        connection = Connection(
            owner_id=actor.id, kind="mcp", name="Too broad", credential="synthetic",
            read_only=True, policy=json.dumps({"allowed_tools": ["read_item"]}),
        )
        for field, value in patch.items():
            setattr(connection, field, value)
        db.add(connection)
        await db.flush()
        with pytest.raises(HTTPException):
            await runtime.database.dots.grants(db, actor.id, [connection.id])


async def test_attention_completes_real_temporal_workflow_without_computer_checkpoint(runtime, actor, monkeypatch):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.toolsets import FunctionToolset
    from temporalio.testing import WorkflowEnvironment

    from moyai.storage import encrypted_converter
    from moyai.worker import make_worker
    from moyai.workflow import TurnWorkflow

    destination, connection, _, prepared = await observer(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Connection, connection.id)
        row.policy = json.dumps({"allowed_tools": ["read_item"], "mcp": {"url": "https://example.test/mcp"}})
        # This fixture was already prepared by observer(); update only its
        # synthetic transport descriptor before starting the actual workflow.
        turn = await db.get(Turn, prepared.scope.turn_id)
        turn.preparation = None
    monkeypatch.setattr("moyai.mcp_remote.remote_mcp_toolset", lambda *a, **kw: FunctionToolset())
    runtime.models["test"].function = lambda messages, info: ModelResponse(parts=[
        TextPart('{"action":"quiet","message":"","evidence":[]}'),
    ])
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, prepared.scope.turn_id, id=f"attention-test-{uuid4()}",
                task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 60))["status"] == "completed"
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, prepared.scope.turn_id)).outcome == "quiet"
        assert (await db.get(Session, destination)).conversation is None
