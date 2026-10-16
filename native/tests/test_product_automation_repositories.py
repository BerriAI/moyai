"""Canonical repository preflight uses stable IDs without holding a writer lock."""

from __future__ import annotations

import json
import os

import pytest
import test_product_automations as fixtures
from app.connector_errors import ConnectorError
from fastapi import HTTPException
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import func, select, text

from moyai.contracts import Grant
from moyai.models import Effect, Turn
from moyai.product_automation_context_models import ProductAutomationExecutionContext as Context
from moyai.product_automation_models import ProductAutomationBinding as Binding
from moyai.product_automation_models import ProductAutomationOperation as Operation
from moyai.product_automation_tools import CreateRequest
from moyai.product_automations import Mutation, Save, definition_digest

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations


class RepositoryMetadata:
    """Only provider metadata is controlled; canonical mutation/locks are real."""

    def __init__(self, ctx):
        self.ctx, self.calls = ctx, []
        self.target, self.name = 42, "synthetic/renamed"
        self.grants = [Grant("synthetic-github-grant", "github", 1, "", policy={"product": {"repository_id": 42}})]
        self.during_read = None

    async def resolve_repository(self, grant, repository="", repository_id=None, *, selected):
        self.calls.append((repository, repository_id, selected))
        async with self.ctx.product.database.sessions.begin() as db:
            assert await db.scalar(text(
                "SELECT pg_try_advisory_xact_lock(726941, CAST(to_regnamespace(:schema) AS oid)::int)"
            ), {"schema": self.ctx.service.schema}), "Provider I/O was attempted under the canonical writer fence."
        if self.during_read:
            await self.during_read()
        return {"repository_id": self.target, "repository": self.name,
                "repo_url": "https://github.com/" + self.name,
                "connection_id": "synthetic-github-grant" if selected else None, "public": not selected}

    async def resolve_grants(self, *args, **kwargs):
        assert kwargs.get("db") is not None
        return self.grants


def setup(ctx):
    adapter = RepositoryMetadata(ctx)
    ctx.service.connections = adapter
    return adapter


async def source(ctx, identity):
    async with ctx.product.source.connect() as db:
        return dict((await db.execute(text("SELECT * FROM moyai.automations WHERE id=:id"), {
            "id": identity,
        })).mappings().one())


async def test_save_pins_permanent_identity_and_known_retry_never_requires_new_provider_lookup(automations):
    ctx = automations
    adapter = setup(ctx)
    body = Save(revision=0, request_key="pin-stable-repository", definition=fixtures.definition(
        repo_url="https://github.com/synthetic/original",
    ))
    result = await ctx.service.mutate(ctx.grant, "create", None, body)
    row = await source(ctx, result["id"])
    saved = json.loads(row["definition"])
    assert saved["github_repository_id"] == 42 and saved["repo_url"] == "https://github.com/synthetic/renamed"
    assert adapter.calls == [("synthetic/original", None, False)]
    assert row["paused"] == 1

    async def unavailable():
        pytest.fail("Known committed retry performed provider lookup.")
    adapter.during_read = unavailable
    assert await ctx.service.mutate(ctx.grant, "create", None, body) == result


async def test_saved_permanent_identity_cannot_be_replaced_by_url_lookup(automations):
    ctx = automations
    adapter = setup(ctx)
    saved = await fixtures.create(ctx, repo_url="https://github.com/synthetic/original")
    adapter.target = 99
    with pytest.raises(HTTPException, match="permanent repository identity changed"):
        await ctx.service.mutate(ctx.grant, "update", saved["id"], Save(
            revision=1, request_key="reject-new-repository",
            definition=fixtures.definition(repo_url="https://github.com/synthetic/renamed", github_repository_id=42),
        ))
    row = await source(ctx, saved["id"])
    assert row["revision"] == 1 and json.loads(row["definition"])["github_repository_id"] == 42


async def test_source_revision_is_rechecked_after_offloaded_repository_lookup(automations):
    ctx = automations
    adapter = setup(ctx)
    saved = await fixtures.create(ctx, repo_url="https://github.com/synthetic/original")
    async def competing_pause():
        await ctx.service.mutate(ctx.grant, "pause", saved["id"], Mutation(
            revision=1, request_key="concurrent-owner-pause",
        ))
    adapter.during_read = competing_pause
    with pytest.raises(HTTPException, match="automation changed"):
        await ctx.service.mutate(ctx.grant, "update", saved["id"], Save(
            revision=1, request_key="stale-repository-edit", definition=fixtures.definition(
                repo_url="https://github.com/synthetic/original", prompt="An edit which must not overwrite the pause.",
            ),
        ))
    row = await source(ctx, saved["id"])
    assert row["revision"] == 2 and json.loads(row["definition"])["prompt"] == fixtures.definition().prompt


async def test_selected_authority_is_rechecked_inside_writer_after_lookup(automations):
    ctx = automations
    adapter = setup(ctx)
    async def disconnected():
        adapter.grants = []
    adapter.during_read = disconnected
    with pytest.raises(HTTPException, match="authority changed before"):
        await fixtures.create(ctx, repo_url="https://github.com/synthetic/original", plugins=["github"])
    async with ctx.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 0


async def test_github_event_identity_is_pinned_without_injecting_execution_tools(automations):
    ctx = automations
    adapter = setup(ctx)
    row = await fixtures.create(ctx, triggers=[{"id": "github-event", "event": {
        "provider": "github", "event": "issues", "repository": "synthetic/original",
    }}])
    saved = json.loads((await source(ctx, row["id"]))["definition"])
    assert saved["triggers"][0]["event"]["repository_id"] == 42
    assert saved["triggers"][0]["event"]["repository"] == "synthetic/renamed"
    assert saved["plugins"] == [] and adapter.calls == [("synthetic/original", None, True)]


async def test_repository_lookup_refusal_leaves_no_false_pending_automation_effect(automations):
    ctx = automations
    adapter = setup(ctx)
    async def unavailable():
        raise ConnectorError("Synthetic metadata unavailable.")
    adapter.during_read = unavailable
    caller = await fixtures.tool_context(ctx)
    body = CreateRequest(
        revision=0, request_key="refused-repo-lookup", turn_id=caller.deps.turn_id,
        definition=fixtures.definition(repo_url="https://github.com/synthetic/original"),
    )
    tools, selected, receipt = await fixtures.reserved_call(ctx, caller, "automation_create", body)
    with pytest.raises(ToolFailed, match="refused before dispatch"):
        await tools.call_tool("automation_create", {"request": body}, caller, selected)
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Effect, receipt)).state == "not_dispatched"
        assert await db.scalar(select(Operation).where(Operation.request_key == body.request_key)) is None


@pytest.mark.parametrize("unverified_identity", [None, 0, True])
async def test_old_unpinned_capture_cannot_resolve_a_new_repository_at_occurrence_time(automations, unverified_identity):
    ctx = automations
    row, binding = await fixtures.activate(ctx)
    saved = json.loads((await source(ctx, row["id"]))["definition"])
    saved["repo_url"] = "https://github.com/synthetic/original"
    definition = fixtures.AutomationDefinition.model_validate(saved)
    async with ctx.product.source.begin() as db:
        await db.execute(text("UPDATE moyai.automations SET definition=:definition,revision=revision+1 WHERE id=:id"), {
            "id": row["id"], "definition": definition.model_dump_json(),
        })
    async with ctx.product.database.sessions.begin() as db:
        current = await db.get(Binding, binding.id)
        current.definition_digest = definition_digest(definition)
        current.generation += 1
        current.synced_generation = current.generation
        current.source_revision += 1
        workspace = {"repo_url": definition.repo_url, "repository_id": unverified_identity}
        # Seed an older-version capture as a new immutable row. Never weaken or
        # disable the database's protection against rewriting admitted authority.
        db.add(Context(
            automation_id=binding.id, generation=current.generation, owner_id=binding.owner_id,
            authority_version=binding.authority_version, connection_ids="[]", workspace_context=json.dumps(workspace),
            fingerprint=ctx.service._context_fingerprint(
                binding.id, current.generation, binding.owner_id, binding.authority_version, [], workspace,
            ),
        ))
        binding = current
    row["revision"] += 1

    class Workspace:
        async def validate_workspace_context(self, *args):
            pytest.fail("An unverified saved identity reached fresh workspace selection.")
    ctx.service.workspace = Workspace()
    result = await fixtures.occurrence(ctx, row, binding)
    assert result["status"] == "blocked" and result["turn_id"] is None
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
