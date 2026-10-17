"""Real PostgreSQL skill writes, authority and atomic replay receipts."""

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
import test_product_automations as automation_fixtures
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import text
from test_product_authority import active_turn, demote

from moyai.effects import EffectReceipts, effect_id, requires_receipt
from moyai.models import Effect
from moyai.product_skill_catalog import ProductSkillCatalog
from moyai.product_skill_tools import CreateSkill, DeleteSkill, ProductSkillTools, UpdateSkill, toolset

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Use disposable local PostgreSQL.")
automations = automation_fixtures.automations
admitted_product = automation_fixtures.admitted_product
product_database = automation_fixtures.product_database


@pytest_asyncio.fixture
async def skills(automations):
    ctx = automations
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "CREATE TABLE moyai.skills (id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,"
            "encrypted TEXT NOT NULL,scope TEXT NOT NULL,owner_id TEXT NOT NULL,namespace TEXT NOT NULL,"
            "revision INTEGER NOT NULL,archived INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "client_id TEXT NOT NULL,icon TEXT NOT NULL,UNIQUE(namespace,name),UNIQUE(owner_id,client_id))"
        ))
        await db.execute(text(
            "CREATE TABLE moyai.skill_bundles(skill_id TEXT,revision INTEGER,encrypted TEXT,manifest TEXT,"
            "PRIMARY KEY(skill_id,revision))"
        ))
        await db.execute(text(
            "CREATE TABLE moyai.skill_audit(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            "skill_id TEXT,actor_id TEXT,action TEXT,created_at TEXT)"
        ))
    ctx.catalog = ProductSkillCatalog(ctx.product.adapter)
    ctx.skills = ProductSkillTools(ctx.catalog, ctx.service)
    ctx.tools = toolset(ctx.skills)
    _, _, prepared = await active_turn(ctx.product)
    ctx.scope = prepared.scope
    return ctx


async def call(ctx, name, request, *, scope=None, call_id=None, run_id=None):
    scope = scope or ctx.scope
    context = RunContext(
        deps=scope, model=TestModel(), usage=RunUsage(),
        run_id=run_id or scope.turn_id, tool_call_id=call_id or uuid4().hex,
    )
    receipt = effect_id(context, ToolCallPart(name, {}, tool_call_id=context.tool_call_id))
    await EffectReceipts(ctx.product.database).reserve(
        scope, receipt, name, {"request": request.model_dump(mode="json")},
    )
    result = await ctx.tools.tools[name].function(context, request)
    return result, receipt


def content(**overrides):
    return {
        "name": "release-summary", "description": "Summarize a release.",
        "instructions": "Summarize changes and verification.",
        "supporting_files": {"references/checklist.md": "Tests\n漢🙂\n"}, **overrides,
    }


async def test_crud_uses_canonical_library_preserves_files_and_restores(skills):
    ctx = skills
    listing = await ctx.skills.read(ctx.scope, ctx.scope.turn_id)
    assert listing["can_publish"] and listing["capabilities"]["create"]
    created, receipt = await call(ctx, "skill_create", CreateSkill(**content(), scope="organization"))
    detail = await ctx.skills.read(ctx.scope, ctx.scope.turn_id, skill_id=created["id"])
    assert detail["instructions"] == content()["instructions"]
    assert detail["supporting_files"] == content()["supporting_files"]
    assert (await ctx.catalog.invocation(ctx.grant, "org:release-summary", shared=True))["id"] == created["id"]
    updated, _ = await call(ctx, "skill_update", UpdateSkill(
        **content(instructions="Use the new checklist.", supporting_files=None),
        skill_id=created["id"], revision=1,
    ))
    assert updated["revision"] == 2
    detail = await ctx.skills.read(ctx.scope, ctx.scope.turn_id, skill_id=created["id"])
    assert detail["supporting_files"] == content()["supporting_files"]
    deleted, _ = await call(ctx, "skill_delete", DeleteSkill(skill_id=created["id"], revision=2))
    assert deleted["archived"] and deleted["revision"] == 3
    assert not (await ctx.catalog.listing(ctx.grant))["skills"]
    restored, _ = await call(ctx, "skill_update", UpdateSkill(
        **content(supporting_files={}), skill_id=created["id"], revision=3,
    ))
    assert restored["revision"] == 4 and not restored["archived"]
    assert not (await ctx.skills.read(ctx.scope, ctx.scope.turn_id, skill_id=created["id"]))["supporting_files"]
    async with ctx.product.database.sessions() as db:
        row = await db.get(Effect, receipt)
        assert row.state == "confirmed" and row.resolution
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_audit")) == 4


async def test_duplicate_delivery_has_one_write_and_conflicting_revision_is_refused(skills):
    ctx = skills
    request = CreateSkill(**content(), scope="organization")
    call_id = uuid4().hex
    first, _ = await call(ctx, "skill_create", request, call_id=call_id)
    second, _ = await call(ctx, "skill_create", request, call_id=call_id)
    assert first == second
    async with ctx.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skills")) == 1
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_audit")) == 1
    with pytest.raises(ToolFailed, match="already exists"):
        await call(ctx, "skill_create", request)
    # A refused write must not poison the next valid write.
    await call(ctx, "skill_update", UpdateSkill(**content(), skill_id=first["id"], revision=1))
    with pytest.raises(ToolFailed, match="changed"):
        await call(ctx, "skill_delete", DeleteSkill(skill_id=first["id"], revision=1))
    assert (await ctx.catalog.detail(ctx.grant, first["id"]))["revision"] == 2


async def test_competing_deliveries_commit_only_once(skills):
    ctx = skills
    results = await asyncio.gather(*[
        call(ctx, "skill_create", CreateSkill(**content(), scope="organization"), call_id="same-delivery")
        for _ in range(2)
    ])
    assert results[0] == results[1]
    async with ctx.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_audit")) == 1


async def test_private_skills_never_release_into_shared_chat_even_to_admin(skills):
    ctx = skills
    _, _, private = await active_turn(ctx.product, private=True)
    created, _ = await call(ctx, "skill_create", CreateSkill(**content()), scope=private.scope)
    assert not (await ctx.skills.read(private.scope, private.scope.turn_id))["can_publish"]
    assert not (await ctx.skills.read(ctx.scope, ctx.scope.turn_id))["skills"]
    with pytest.raises(HTTPException) as denied:
        await ctx.skills.read(ctx.scope, ctx.scope.turn_id, skill_id=created["id"])
    assert denied.value.status_code == 404
    with pytest.raises(ToolFailed, match="not found"):
        await call(ctx, "skill_delete", DeleteSkill(skill_id=created["id"], revision=1))
    with pytest.raises(ToolFailed, match="shared conversation"):
        await call(ctx, "skill_create", CreateSkill(**content(name="private-export"), scope="organization"),
                   scope=private.scope)
    assert (await ctx.skills.read(private.scope, private.scope.turn_id, skill_id=created["id"]))["id"] == created["id"]


@pytest.mark.parametrize("kind", ["delegated", "slack", "forged_visibility", "revoked", "stopped"])
async def test_untrusted_or_stale_execution_cannot_write(skills, kind):
    ctx = skills
    scope, run_id = ctx.scope, ctx.scope.turn_id
    context = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), run_id=run_id, tool_call_id="refused")
    receipt = effect_id(context, ToolCallPart("skill_create", {}, tool_call_id=context.tool_call_id))
    await EffectReceipts(ctx.product.database).reserve(scope, receipt, "skill_create", {})
    if kind == "delegated":
        run_id = "delegate"
    elif kind == "slack":
        scope = replace(scope, surface="slack")
    elif kind == "forged_visibility":
        scope = replace(scope, visibility="private")
    elif kind == "revoked":
        await demote(ctx.product)
    elif kind == "stopped":
        async with ctx.product.database.sessions.begin() as db:
            await db.execute(text("UPDATE moyai_turns SET status='stopped' WHERE id=:id"), {"id": scope.turn_id})
    with pytest.raises((HTTPException, PermissionError)):
        await ctx.skills.mutate(scope, run_id, receipt, "skill_create", CreateSkill(**content(), scope="organization"))
    async with ctx.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skills")) == 0


async def test_member_cannot_write_organization_skills(skills):
    ctx = skills
    await demote(ctx.product)
    principal = await ctx.product.adapter.current(ctx.product.user_id)
    async with ctx.product.database.sessions.begin() as db:
        ctx.product.actor = await ctx.product.adapter.project_native_user(db, principal)
    _, _, prepared = await active_turn(ctx.product)
    with pytest.raises(ToolFailed, match="administrator"):
        await call(ctx, "skill_create", CreateSkill(**content(), scope="organization"), scope=prepared.scope)


async def test_each_mutation_is_receipted_and_read_tools_are_read_only(skills):
    assert set(skills.tools.tools) == {"skill_list", "skill_read", "skill_create", "skill_update", "skill_delete"}
    for name, tool in skills.tools.tools.items():
        assert requires_receipt(tool.tool_def) == (name not in {"skill_list", "skill_read"})
        assert tool.metadata["annotations"]["readOnlyHint"] == (name in {"skill_list", "skill_read"})


@pytest.mark.parametrize("files", [
    {"../outside": "x"}, {"/absolute": "x"}, {".hidden": "x"},
    {"ok": "\x00"}, {"big": "x" * 262_145}, {f"file-{index}": "" for index in range(51)},
])
def test_invalid_bundles_are_rejected_before_dispatch(files):
    with pytest.raises(ValidationError):
        CreateSkill(**content(supporting_files=files))
