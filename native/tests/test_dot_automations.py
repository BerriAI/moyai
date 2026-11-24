"""Exercise attention through canonical definitions, admission and generation fences."""
from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest
import test_product_authority as authority_fixtures
import test_product_automations as automation_fixtures
from fastapi import HTTPException
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import select, update
from test_product_automations import activate, occurrence

from moyai.automations.product_automation_tools import CreateRequest, UpdateRequest
from moyai.automations.product_automations import AutomationDefinition, Mutation, definition_payload
from moyai.contracts import NewMessage
from moyai.dot_models import DotAttentionTurn, PersonalDot
from moyai.models import Connection, Session, Turn

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
admitted_product = authority_fixtures.admitted_product
product_database = authority_fixtures.product_database
automations = automation_fixtures.automations
canonical_connections = automation_fixtures.canonical_connections


async def setup(ctx, *, discovery=False):
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
                                  visibility="private", connection_ids=[connection.id],
                                  attention_mode="discovery" if discovery else "focused")
    return destination, connection, row, binding


async def test_discovery_overlap_fences_scheduled_and_manual_admission(automations, canonical_connections):
    ctx = automations
    destination, _, row, binding = await setup(ctx, discovery=True)
    admitted = await occurrence(ctx, row, binding)
    assert admitted["status"] == "admitted"
    assert (await occurrence(ctx, row, binding))["status"] == "skipped"
    with pytest.raises(HTTPException, match="previous discovery"):
        await ctx.service.run_now(ctx.grant, row["id"], Mutation(revision=row["revision"], request_key=uuid4().hex))
    # An ordinary reply is independent of the discovery admission fence.
    assert (await ctx.product.database.admission.submit(ctx.product.actor, destination, NewMessage(
        prompt="What is next?", request_key=uuid4().hex,
    ))).id != admitted["turn_id"]


async def test_only_one_standing_discovery_definition_per_owner(automations, canonical_connections):
    ctx = automations
    destination, connection, _, _ = await setup(ctx, discovery=True)
    with pytest.raises(HTTPException, match="already has a discovery"):
        await automation_fixtures.create(
            ctx, kind="attention", attention_mode="discovery", session_id=destination,
            visibility="private", connection_ids=[connection.id],
        )


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


async def test_attention_history_and_run_now_link_to_dot(automations, canonical_connections):
    ctx = automations
    destination, _, row, _ = await setup(ctx)
    request = Mutation(revision=row["revision"], request_key=uuid4().hex)
    run = await ctx.service.run_now(ctx.grant, row["id"], request)
    assert run["session_id"] == destination
    assert (await ctx.service.run_now(ctx.grant, row["id"], request))["session_id"] == destination
    history = (await ctx.service.history(ctx.grant, row["id"]))["history"]
    assert history and all(item["session_id"] == destination for item in history)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Turn, run["turn_id"])).session_id != destination


@pytest.mark.parametrize("selection", ["subset", "ambiguous", "unselected"])
async def test_conversational_attention_preserves_explicit_account_subset(automations, canonical_connections, selection):
    ctx = automations
    run = await automation_fixtures.tool_context(ctx, visibility="private")
    database = ctx.product.database
    async with database.sessions.begin() as db:
        db.add(PersonalDot(owner_id=run.deps.actor_id, session_id=run.deps.session_id))
        accounts = [Connection(
            owner_id=run.deps.actor_id, kind="mcp", name=f"Reader {i}",
            credential=database.security.seal("synthetic"), read_only=True,
            policy=json.dumps({"allowed_tools": ["read_item"]}),
        ) for i in range(3)]
        db.add_all(accounts)
        await db.flush()
        selected = [row.id for row in accounts[:2]]
        (await db.get(Turn, run.deps.turn_id)).connections = json.dumps(selected)
        run.deps.grants = await database.dots.grants(db, run.deps.actor_id, selected)
    chosen = [accounts[0].id] if selection == "subset" else [] if selection == "ambiguous" else [accounts[2].id]
    body = CreateRequest(
        revision=0, request_key=uuid4().hex, turn_id=run.deps.turn_id,
        definition=automation_fixtures.definition(kind="attention", visibility="private", connection_ids=chosen),
    )
    tools, tool, _ = await automation_fixtures.reserved_call(ctx, run, "automation_create", body)
    if selection != "subset":
        with pytest.raises(ToolFailed):
            await tools.call_tool("automation_create", {"request": body}, run, tool)
        return
    saved = await tools.call_tool("automation_create", {"request": body}, run, tool)
    assert (await ctx.service.preview(ctx.grant, saved["id"]))["definition"]["connection_ids"] == chosen


@pytest.mark.parametrize("selection", ["unchanged", "selected_addition", "unselected_addition"])
async def test_conversational_edits_require_authority_only_for_new_accounts(automations, canonical_connections, selection):
    ctx = automations
    _, original, row, _ = await setup(ctx)
    run = await automation_fixtures.tool_context(ctx, visibility="private")
    database = ctx.product.database
    async with database.sessions.begin() as db:
        other = Connection(
            owner_id=run.deps.actor_id, kind="mcp", name="Another account",
            credential=database.security.seal("synthetic"), read_only=True,
            policy=json.dumps({"allowed_tools": ["read_item"]}),
        )
        db.add(other)
        await db.flush()
        if selection == "selected_addition":
            (await db.get(Turn, run.deps.turn_id)).connections = json.dumps([other.id])
            run.deps.grants = await database.dots.grants(db, run.deps.actor_id, [other.id])
    preview = await ctx.service.preview(ctx.grant, row["id"])
    chosen = [original.id] + ([other.id] if selection != "unchanged" else [])
    definition = AutomationDefinition.model_validate(preview["definition"]).model_copy(update={
        "name": "Updated schedule", "connection_ids": chosen,
    })
    body = UpdateRequest(
        automation_id=row["id"], revision=preview["revision"], request_key=uuid4().hex,
        turn_id=run.deps.turn_id, definition=definition,
    )
    tools, tool, _ = await automation_fixtures.reserved_call(ctx, run, "automation_update", body)
    if selection == "unselected_addition":
        with pytest.raises(ToolFailed):
            await tools.call_tool("automation_update", {"request": body}, run, tool)
        assert (await ctx.service.preview(ctx.grant, row["id"]))["definition"]["connection_ids"] == [original.id]
    else:
        await tools.call_tool("automation_update", {"request": body}, run, tool)
        assert (await ctx.service.preview(ctx.grant, row["id"]))["definition"]["connection_ids"] == sorted(chosen)


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
