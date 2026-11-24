"""Exercise attention through canonical definitions, admission and generation fences."""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
import test_product_authority as authority_fixtures
import test_product_automations as automation_fixtures
from fastapi import HTTPException
from sqlalchemy import select, update
from test_product_automations import activate, occurrence

from moyai.automations.product_automation_tools import CreateRequest
from moyai.automations.product_automations import AutomationDefinition, Mutation, definition_payload
from moyai.contracts import NewMessage
from moyai.dot_models import DotAttentionTurn
from moyai.models import Connection, Session, Turn

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
admitted_product = authority_fixtures.admitted_product
product_database = authority_fixtures.product_database
automations = automation_fixtures.automations
canonical_connections = automation_fixtures.canonical_connections


async def setup(ctx):
    database = ctx.product.database
    destination = await database.dots.open(ctx.product.actor)
    async with database.sessions.begin() as db:
        connection = Connection(
            owner_id=ctx.product.actor.id, kind="mcp", name="Reviewed read account",
            credential=database.security.seal("synthetic"), read_only=True,
            policy=json.dumps({"allowed_tools": ["read_item"]}),
        )
        db.add(connection)
        await db.flush()
    row, binding = await activate(ctx, kind="attention", session_id=destination,
                                  visibility="private", connection_ids=[connection.id])
    return destination, connection, row, binding


async def test_attention_uses_separate_session_and_pins_selection(automations, canonical_connections):
    ctx = automations
    destination, connection, row, binding = await setup(ctx)
    database = ctx.product.database
    # A later human reply with no accounts must not change scheduled authority.
    await database.admission.submit(ctx.product.actor, destination, NewMessage(
        prompt="Help me plan.", request_key=uuid4().hex,
    ))
    admitted = await occurrence(ctx, row, binding)
    assert admitted["status"] == "admitted" and admitted["session_id"] != destination
    async with database.sessions() as db:
        worker = await db.get(Session, admitted["session_id"])
        turn = await db.get(Turn, admitted["turn_id"])
        record = await db.get(DotAttentionTurn, turn.id)
        assert worker.surface == "automation" and worker.visibility == "private"
        assert record.destination_session_id == destination
        assert json.loads(turn.connections) == [connection.id]
    await database.claim_batch(10)
    prepared = await database.admission.prepare(admitted["turn_id"])
    assert prepared.scope.attention and prepared.workspace is None
    await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(revision=row["revision"], request_key=uuid4().hex))
    with pytest.raises((PermissionError, HTTPException)):
        await database.authorize_scope(prepared.scope)


async def test_changed_connection_requires_owner_reauthorization(automations, canonical_connections):
    ctx = automations
    _, connection, row, binding = await setup(ctx)
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(update(Connection).where(Connection.id == connection.id).values(revision=2))
    assert (await occurrence(ctx, row, binding))["status"] == "blocked"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(DotAttentionTurn.turn_id)) is None


def test_task_digests_and_late_current_chat_binding_remain_compatible():
    definition = AutomationDefinition.model_validate({
        "name": "Prepare meetings", "prompt": "Read my upcoming meetings.",
        "triggers": [{"id": "morning", "schedule": {"frequency": "daily"}}],
        "environment_id": "none", "visibility": "private",
    })
    assert "kind" not in definition_payload(definition)
    attention = definition.model_copy(update={"kind": "attention"})
    request = CreateRequest(
        turn_id=str(uuid4()), revision=0, request_key="create-attention", definition=attention, destination="current_chat",
    )
    assert request.definition.session_id is None  # The authenticated tool supplies it.
    assert definition_payload(attention)["kind"] == "attention"
