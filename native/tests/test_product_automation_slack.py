"""Verified Slack management captures the owner without changing the raw scope."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
import test_product_automations as fixtures
from fastapi import HTTPException
from sqlalchemy import select, text, update

from moyai.models import Effect, ProductTurnAuthority, Session
from moyai.product_automation_models import ProductAutomationBinding as Binding
from moyai.product_automation_tools import CreateRequest, MigrateRequest
from moyai.product_slack import ProductSlackAuthority

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations
canonical_connections = fixtures.canonical_connections


async def mapped_slack(ctx):
    identity = "slack:TTEST0001:UTEST0001"
    settings = ctx.product.adapter.settings
    settings.slack_bot_enabled = True
    settings.slack_session_users = "UTEST0001"
    settings.slack_signing_secret = "synthetic-local-slack-signature"
    async with ctx.product.source.begin() as db:
        for column in (
            "profile_eligible INTEGER NOT NULL DEFAULT 0", "profile_conflict INTEGER NOT NULL DEFAULT 0",
            "profile_checked_at TEXT NOT NULL DEFAULT ''", "link_status TEXT NOT NULL DEFAULT ''",
        ):
            await db.execute(text("ALTER TABLE moyai.users ADD COLUMN " + column))
        await db.execute(text(
            "INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at,"
            "profile_eligible,profile_checked_at,link_status) VALUES "
            "(:id,'slack','admin@example.test','Synthetic mapped requester','c','u',1,:stamp,'linked')"
        ), {"id": identity, "stamp": datetime.now(UTC).isoformat()})
        await db.execute(text(
            "INSERT INTO moyai.connections(provider,encrypted,label,updated_at) VALUES('slack',:value,'Synthetic','u')"
        ), {"value": ctx.product.adapter.legacy_security.encrypt(json.dumps({
            "bot": {"access_token": "synthetic-bot-token", "bot_user_id": "UBOT00001", "team": {"id": "TTEST0001"}},
        }))})
    ctx.product.adapter.slack_authority = ProductSlackAuthority(ctx.product.adapter)
    # Fixture DDL changes the source row shape; discard cached prepared plans.
    await ctx.product.source.dispose()
    await ctx.product.adapter.source_engine.dispose()
    await ctx.product.database.engine.dispose()
    principal = await ctx.product.adapter.current(identity)
    async with ctx.product.database.sessions.begin() as db:
        actor = await ctx.product.adapter.project_native_user(db, principal)
    return await fixtures.tool_context(ctx, actor=actor, surface="slack")


async def test_mapped_direct_slack_retains_raw_effect_and_captures_canonical_owner(automations, canonical_connections):
    ctx = automations
    caller = await mapped_slack(ctx)
    principal = await ctx.service.authorize_tool(caller.deps, caller.run_id)
    assert principal.grant == ctx.grant
    assert caller.deps.product_grant.user_id == "slack:TTEST0001:UTEST0001"
    create = CreateRequest(
        revision=0, request_key="slack-create-owner", turn_id=caller.deps.turn_id, definition=fixtures.definition(),
    )
    tools, selected, receipt = await fixtures.reserved_call(ctx, caller, "automation_create", create)
    row = await tools.call_tool("automation_create", {"request": create}, caller, selected)
    assert (await ctx.service.preview(principal.grant, row["id"]))["owner_id"] == ctx.grant.user_id
    caller.tool_call_id = "migrate-captured-owner"
    migrate = MigrateRequest(
        automation_id=row["id"], revision=1, request_key="slack-migrate-owner",
        turn_id=caller.deps.turn_id, activate=True,
    )
    tools, selected, _ = await fixtures.reserved_call(ctx, caller, "automation_migrate", migrate)
    row = await tools.call_tool("automation_migrate", {"request": migrate}, caller, selected)
    async with ctx.product.database.sessions() as db:
        effect = await db.get(Effect, receipt)
        binding = await db.get(Binding, row["id"])
        authority = await db.scalar(select(ProductTurnAuthority).where(
            ProductTurnAuthority.turn_id == caller.deps.turn_id,
        ))
        assert effect.state == "confirmed" and effect.actor_id == caller.deps.actor_id
        assert authority.actor_id == caller.deps.actor_id
        assert binding.owner_id == ctx.grant.user_id and binding.authority_version == ctx.grant.authority_version
    # Scheduled admission consumes the captured grant; it must never resolve
    # the current Slack-to-owner relationship to manufacture new authority.
    async def forbidden(*args, **kwargs):
        pytest.fail("Scheduled work attempted fresh individual capture.")
    ctx.product.adapter.individual_for_admission = forbidden
    await ctx.service.reconcile(row["id"])
    async with ctx.product.database.sessions() as db:
        binding = await db.get(Binding, row["id"])
    assert (await fixtures.occurrence(ctx, row, binding))["status"] == "admitted"


@pytest.mark.parametrize("reason", ["stale", "ambiguous", "web", "private", "delegated"])
async def test_slack_management_needs_current_verified_direct_shared_source(automations, canonical_connections, reason):
    ctx = automations
    caller = await mapped_slack(ctx)
    if reason == "private":
        async with ctx.product.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == caller.deps.session_id).values(visibility="private"))
    elif reason in {"web", "delegated"}:
        actor = await ctx.product.database.user(caller.deps.actor_id)
        actor.product_grant = caller.deps.product_grant
        caller = await fixtures.tool_context(
            ctx, actor=actor, surface="web" if reason == "web" else "slack",
            delegate=reason == "delegated",
        )
    else:
        async with ctx.product.source.begin() as db:
            if reason == "stale":
                await db.execute(text("UPDATE moyai.users SET profile_checked_at=:stamp WHERE kind='slack'"), {
                    "stamp": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                })
            else:
                await db.execute(text(
                    "INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at) "
                    "VALUES('google:ambiguous','google','admin@example.test','Synthetic duplicate','c','u')"
                ))
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.service.authorize_tool(caller.deps, caller.run_id)
