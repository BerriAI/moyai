"""Persisted owner grants and automation claims; no delegated self-authorization."""

from __future__ import annotations

import asyncio
import os

import pytest
import test_product_automations as fixtures
from app.automation_definitions import Claim
from fastapi import HTTPException
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import update

from moyai.effects import requires_receipt
from moyai.models import Effect, Session, Turn
from moyai.product_automation_models import ProductAutomationBinding as Binding
from moyai.product_automation_tools import GrantRequest, ScopedUpdate
from moyai.product_automations import AUTOMATION_MUTATIONS

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations


def test_scoped_mutations_are_registered_with_native_effect_receipts():
    for name in AUTOMATION_MUTATIONS:
        assert requires_receipt(ToolDefinition(name=name))


async def invoke(ctx, run_ctx, action, body):
    tools, selected, receipt = await fixtures.reserved_call(ctx, run_ctx, action, body)
    async with asyncio.timeout(5):
        result = await tools.call_tool(action, {"request": body}, run_ctx, selected)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, receipt)).state == "confirmed"
    return result


async def automation_context(ctx, row, binding):
    result = await fixtures.occurrence(ctx, row, binding)
    await ctx.product.database.claim_next()
    prepared = await ctx.product.database.prepare(result["turn_id"])
    from pydantic_ai import RunContext, RunUsage
    from pydantic_ai.models.test import TestModel
    return RunContext(
        deps=prepared.scope, model=TestModel(), usage=RunUsage(),
        run_id=result["turn_id"], tool_call_id="synthetic-access",
    )


async def test_persistent_self_management_is_narrow_live_and_preserves_original_authority(automations):
    ctx = automations
    row, binding = await fixtures.activate(ctx, self_management={"instructions": True})
    run_ctx = await automation_context(ctx, row, binding)
    view = await ctx.service.access.read(run_ctx.deps)
    assert view["automations"][0]["permissions"] == ["instructions"]
    with pytest.raises(HTTPException):
        await ctx.service.authorize_tool(run_ctx.deps, run_ctx.run_id)
    changed = await invoke(ctx, run_ctx, "automation_self_update", ScopedUpdate(
        automation_id=row["id"], revision=2, request_key="self-edit-prompt", turn_id=run_ctx.deps.turn_id,
        prompt="Revised synthetic automation instructions.",
    ))
    assert changed["native_state"] == "active" and changed["revision"] == 3
    async with ctx.product.database.sessions() as db:
        current = await db.get(Binding, row["id"])
        assert current.authority_version == binding.authority_version
        assert current.generation == binding.generation + 1
    run_ctx.tool_call_id = "forbidden-grant"
    request = GrantRequest(
        automation_id=row["id"], revision=3, request_key="no-self-grant", turn_id=run_ctx.deps.turn_id,
        permissions=["schedule"],
    )
    tools, selected, receipt = await fixtures.reserved_call(ctx, run_ctx, "automation_grant", request)
    with pytest.raises(ToolFailed, match="refused before dispatch"):
        await tools.call_tool("automation_grant", {"request": request}, run_ctx, selected)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, receipt)).state == "not_dispatched"


async def test_human_turn_grant_allows_delegated_narrow_edits_then_expires_on_turn_change(automations):
    ctx = automations
    row, _ = await fixtures.activate(ctx)
    run_ctx = await fixtures.tool_context(ctx)
    await invoke(ctx, run_ctx, "automation_grant", GrantRequest(
        automation_id=row["id"], revision=2, request_key="grant-instructions", turn_id=run_ctx.deps.turn_id,
        permissions=["instructions"],
    ))
    run_ctx.run_id = run_ctx.deps.turn_id + ":delegated"
    run_ctx.tool_call_id = "delegated-edit"
    await invoke(ctx, run_ctx, "automation_self_update", ScopedUpdate(
        automation_id=row["id"], revision=2, request_key="delegated-instructions", turn_id=run_ctx.deps.turn_id,
        prompt="Changed only under the current human grant.",
    ))
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == run_ctx.deps.session_id).values(active_turn=None))
        await db.execute(update(Turn).where(Turn.id == run_ctx.deps.turn_id).values(status="completed"))
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.service.access.read(run_ctx.deps)


async def test_claim_item_deduplicates_across_native_occurrences_even_after_failure(automations):
    ctx = automations
    row, binding = await fixtures.activate(ctx)
    first = await automation_context(ctx, row, binding)
    result = await invoke(ctx, first, "automation_claim_item", Claim(item_key="SYNTHETIC-123"))
    assert result["claimed"]
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(update(Turn).where(Turn.id == first.deps.turn_id).values(status="failed"))
        await db.execute(update(Session).where(Session.id == first.deps.session_id).values(active_turn=None))
    second = await automation_context(ctx, row, binding)
    result = await invoke(ctx, second, "automation_claim_item", Claim(item_key="synthetic-123"))
    assert not result["claimed"] and result["session_id"] == first.deps.session_id
    assert (await ctx.service.access.authorize_history(
        second.deps, second.run_id, row["id"],
    )).user_id == ctx.grant.user_id
    other, _ = await fixtures.activate(ctx)
    with pytest.raises(HTTPException):
        await ctx.service.access.authorize_history(second.deps, second.run_id, other["id"])
