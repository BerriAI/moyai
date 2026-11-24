"""Private scheduling policy against real isolated persistence; actual-app proof is separate."""

import json
import os
from uuid import uuid4

import pytest
import test_product_automations as fixtures
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai_harness.memory import MemoryOperation
from sqlalchemy import text
from test_product_memory import MEMORY_SCHEMA, note

from moyai.automations.product_automation_models import ProductAutomationTurn
from moyai.automations.product_automation_tools import TargetRequest, toolset
from moyai.automations.product_automations import Mutation, Save
from moyai.models import Connection, Session
from moyai.product_memory import ProductMemoryService
from moyai.product_memory_store import ProductMemoryStore

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations
canonical_connections = fixtures.canonical_connections


async def other_principal(ctx):
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('google:other','google','other@example.test','Other',NULL,'c','u')"
        ))
    return await ctx.product.adapter.current("google:other")


async def test_library_pagination_preview_history_and_admin_pause_never_expose_private_definitions(automations):
    ctx = automations
    other = await other_principal(ctx)
    private = await ctx.service.mutate(other.grant, "create", None, Save(
        revision=0, request_key="other-private", definition=fixtures.definition(visibility="private"),
    ))
    shared = await fixtures.create(ctx, name="Visible shared schedule")
    page = await ctx.service.listing(ctx.grant, shared_library=True, limit=1)
    assert [row["id"] for row in page["automations"]] == [shared["id"]]
    assert page["next_offset"] is None
    # The viewer is an administrator, but personal schedules remain owner-only.
    for action in ("preview", "history", "pause"):
        with pytest.raises(HTTPException) as denied:
            if action == "pause":
                await ctx.service.mutate(ctx.grant, action, private["id"], Mutation(
                    revision=1, request_key="admin-private-pause",
                ))
            else:
                await getattr(ctx.service, action)(ctx.grant, private["id"], shared_library=True)
        assert denied.value.status_code == 404
    own = await ctx.service.preview(other.grant, private["id"], shared_library=True)
    assert own["execution_visibility"] == "private"
    assert own["capabilities"]["update"] and not own["capabilities"]["webhook"]
    assert len((await ctx.service.listing(other.grant, shared_library=True))["automations"]) == 2


@pytest.mark.parametrize("visibility", ["private", "shared"])
async def test_visibility_cannot_be_changed_by_an_owner_edit(automations, visibility):
    ctx = automations
    saved = await fixtures.create(ctx, visibility=visibility)
    with pytest.raises(HTTPException, match="fixed"):
        await ctx.service.mutate(ctx.grant, "update", saved["id"], Save(
            revision=1, request_key="change-audience",
            definition=fixtures.definition(visibility="shared" if visibility == "private" else "private"),
        ))
    assert (await ctx.service.preview(ctx.grant, saved["id"]))["definition"]["visibility"] == visibility


@pytest.mark.parametrize("visibility", ["private", "shared"])
async def test_agent_reads_and_mutations_require_matching_chat_visibility(automations, visibility):
    ctx = automations
    saved = await fixtures.create(ctx, visibility="shared" if visibility == "private" else "private")
    run = await fixtures.tool_context(ctx, visibility=visibility)
    tools = toolset(ctx.service)
    definitions = await tools.get_tools(run)
    listing = await tools.call_tool("automation_list", {}, run, definitions["automation_list"])
    assert listing["automations"] == []
    with pytest.raises(ToolFailed):
        await tools.call_tool("automation_preview", {"automation_id": saved["id"]}, run, definitions["automation_preview"])
    with pytest.raises(HTTPException):
        await ctx.service.access.authorize_history(run.deps, run.run_id, saved["id"])
    body = TargetRequest(
        automation_id=saved["id"], revision=1, request_key="cross-audience-pause", turn_id=run.deps.turn_id,
    )
    tools, selected, _ = await fixtures.reserved_call(ctx, run, "automation_pause", body)
    with pytest.raises(ToolFailed, match="refused before dispatch"):
        await tools.call_tool("automation_pause", {"request": body}, run, selected)


@pytest.mark.parametrize("selection", ["native", "slack"])
async def test_private_schedules_retain_personal_connections_and_recheck_revocation(
    automations, canonical_connections, selection,
):
    ctx = automations
    if selection == "native":
        connection_id = str(uuid4())
        async with ctx.product.database.sessions.begin() as db:
            db.add(Connection(
                id=connection_id, owner_id=ctx.product.actor.id, name="Personal MCP", kind="mcp",
                credential=ctx.product.database.security.seal("synthetic-token"), shared=False,
            ))
    else:
        from app.connection_authority import new_credentials
        credentials = new_credentials({"access_token": "synthetic-personal-token", "kind": "personal"})
        async with ctx.product.source.begin() as db:
            await db.execute(text(
                "INSERT INTO moyai.personal_slack_grants VALUES (:owner,:encrypted)"
            ), {"owner": ctx.product.actor.id, "encrypted": ctx.product.adapter.legacy_security.encrypt(json.dumps(credentials))})
        async with ctx.product.database.sessions() as db:
            grants = await canonical_connections.resolve_grants(
                ctx.product.actor.id, ["slack"], shared_session=False, surface="web", db=db,
            )
        connection_id = grants[0].id
    saved, binding = await fixtures.activate(ctx, visibility="private", connection_ids=[connection_id])
    first = await fixtures.occurrence(ctx, saved, binding)
    assert first["status"] == "admitted"
    assert (await ctx.product.database.claim_next()).id == first["turn_id"]
    prepared = await ctx.product.database.admission.prepare(first["turn_id"])
    assert prepared.scope.authority.visibility == "private" and prepared.scope.authority.surface == "automation"
    assert [grant.id for grant in prepared.scope.grants] == [connection_id]
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Session, first["session_id"])).visibility == "private"
        assert (await db.get(ProductAutomationTurn, first["turn_id"])).visibility == "private"
    assert (await ctx.service.history(ctx.grant, saved["id"]))["history"][0]["session_id"] == first["session_id"]
    if selection == "native":
        async with ctx.product.database.sessions.begin() as db:
            (await db.get(Connection, connection_id)).enabled = False
    else:
        async with ctx.product.source.begin() as db:
            await db.execute(text("DELETE FROM moyai.personal_slack_grants"))
    assert (await fixtures.occurrence(ctx, saved, binding))["status"] == "blocked"
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.product.database.authorize_scope(prepared.scope)


async def test_private_scheduled_run_recalls_private_memory_but_cannot_write_it(automations):
    ctx = automations
    async with ctx.product.source.begin() as db:
        for statement in MEMORY_SCHEMA:
            await db.execute(text(statement))
    memory = ProductMemoryService(ctx.product.database, ctx.product.adapter)
    async with memory.writer(ctx.grant) as (db, owner):
        await memory.save_in(db, owner, note(content="Start my brief with Maple brief."),
                             source={"type": "manual", "visibility": "private"})
    saved, binding = await fixtures.activate(ctx, visibility="private")
    admitted = await fixtures.occurrence(ctx, saved, binding)
    await ctx.product.database.claim_next()
    prepared = await ctx.product.database.admission.prepare(admitted["turn_id"])
    store = ProductMemoryStore(memory, prepared.scope, admitted["turn_id"])
    assert "Maple brief" in (await store.read(store.prefix + "MEMORY.md", max_chars=10000)).content
    with pytest.raises(ToolFailed, match="cannot mutate"):
        await store.write(store.prefix + "personal-forbidden.md", json.dumps({
            "key": "forbidden", "title": "Forbidden note", "content": "Synthetic scheduled observation.",
            "kind": "preference", "repo_url": "", "revision": 0,
            "source_message_id": 1, "source_quote": "Synthetic scheduled observation.",
        }), expected_version=None, operation=MemoryOperation("scheduled-write", "a" * 64))


def test_private_definitions_cannot_enable_shared_event_receivers_or_dynamic_provider_selection():
    for extra in (
        {"triggers": [{"id": "event", "event": {"provider": "webhook", "event": "*"}}]},
        {"plugins": ["slack"]},
    ):
        with pytest.raises(ValidationError):
            fixtures.definition(visibility="private", **extra)
