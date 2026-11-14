"""Validated image consumers; real Modal/app qualification is separate."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import test_product_connections
import test_product_continuation
from fastapi import FastAPI, HTTPException
from pydantic import TypeAdapter

from moyai.contracts import Grant, NewMessage, NewSession, PreparedTurn
from moyai.models import Session, Turn
from moyai.product_continuation_restore import ProductWorkspaceRestorer
from moyai.product_continuation_routes import ProductEnvironment, continuation_routes
from moyai.product_continuation_workspace import (
    ProductWorkspaceCompatibility,
    gitlab_recipe_identity,
)

ENV, BUILD = "c" * 32, "d" * 32
ORIGIN = "https://gitlab.example.test"
PATH = "group/sub/project"
PG = test_product_continuation.PG
continuation = test_product_continuation.continuation
bridge = test_product_connections.bridge


def recipe(**changes):
    return {
        "name": "Validated GitLab project", "repository_provider": "gitlab",
        "repository_origin": ORIGIN, "gitlab_project_id": 42, "repository": PATH,
        "clone_access": "gitlab", **changes,
    }


def selection(**changes):
    return {
        "repo_url": ORIGIN + "/" + PATH, "repository_provider": "gitlab",
        "gitlab_project_id": 42, "environment_id": "auto", **changes,
    }


def grant(**changes):
    return Grant("selected-gitlab", "gitlab", 1, "", policy={"product": {
        "repository_id": 42, "base_url": ORIGIN, **changes,
    }})


async def seed(product, value=None, *, environment=ENV, build=BUILD, resolved=None, **fields):
    value = value or recipe()
    await product.sql("""CREATE TABLE IF NOT EXISTS moyai.environments(
        id text PRIMARY KEY,recipe text,revision int,enabled int,activate_on_ready int,
        active_build text,is_default int,updated_at text)""")
    await product.sql("""CREATE TABLE IF NOT EXISTS moyai.environment_builds(
        id text PRIMARY KEY,environment_id text,revision int,recipe text,resolved_recipe text,
        snapshot_id text,commit_sha text,phase text,sandbox_provider text)""")
    await product.sql("INSERT INTO moyai.environments VALUES(:id,:recipe,1,1,0,:build,1,'now')",
                      id=environment, recipe=json.dumps(value), build=build)
    row = {
        "id": build, "environment_id": environment, "revision": 1,
        "recipe": json.dumps(value), "resolved_recipe": json.dumps(resolved if resolved is not None else value),
        "snapshot_id": "im-validated", "commit_sha": "a" * 40, "phase": "ready",
        "sandbox_provider": "modal", **fields,
    }
    await product.sql(
        "INSERT INTO moyai.environment_builds VALUES(:id,:environment_id,:revision,:recipe,:resolved_recipe,"
        ":snapshot_id,:commit_sha,:phase,:sandbox_provider)", **row,
    )
    return row


@PG
async def test_gitlab_auto_keeps_bare_checkout_when_legacy_environment_store_is_absent(continuation):
    workspace = continuation.runtime.product_workspace
    async with continuation.database.sessions() as db:
        context = await workspace._selection(db, selection(), require_ready=True)
        assert context["environment_build_id"] is None
        assert context["selection"]["environment_id"] == "auto"
        for choices in (selection(environment_id=ENV), selection(environment_build_id=BUILD)):
            with pytest.raises(HTTPException, match="store is unavailable"):
                await workspace._selection(db, choices, require_ready=True)


@PG
@pytest.mark.parametrize("mode", ["auto", "explicit", "pinned"])
@pytest.mark.parametrize("origin", [ORIGIN, "http://127.0.0.1:8929"])
async def test_gitlab_validated_environment_uses_exact_provider_origin_project_and_catalog(continuation, mode, origin):
    product = continuation
    await seed(product, recipe(repository_origin=origin))
    # An unrelated default with the same numeric ID must never be adopted.
    await seed(product, {"name": "GitHub", "repository": "group/project", "repository_id": 42},
               environment="a" * 32, build="b" * 32)
    choices = selection(repo_url=origin + "/" + PATH)
    if mode == "explicit":
        choices["environment_id"] = ENV
    if mode == "pinned":
        choices["environment_build_id"] = BUILD
        await product.sql("UPDATE moyai.environments SET enabled=0,active_build='' WHERE id=:id", id=ENV)
    workspace = product.runtime.product_workspace
    async with product.database.sessions.begin() as db:
        context = await workspace.capture_workspace_context(db, product.principal, choices)
    assert context["environment_build_id"] == BUILD
    assert context["environment_revision"] == 1 and context["commit_sha"] == "a" * 40
    assert context["repository_provider"] == "gitlab" and context["repository_origin"] == origin
    assert context["gitlab_project_id"] == context["repository_id"] == 42
    assert context["pinned_source_build"] is (mode == "pinned")
    assert "github_repository_id" not in context["selection"]
    workspace.authorize_project_grants(context, [grant(base_url=origin)])
    if mode != "pinned":
        rows = await workspace.catalog(product.principal)
        for row in rows:
            assert ProductEnvironment.model_validate(row).model_dump(mode="json") == row
        gitlab = next(row for row in rows if row["id"] == ENV)
        assert gitlab["ready"] and gitlab["repository_id"] is None
        assert gitlab["gitlab_project_id"] == 42 and gitlab["repository_origin"] == origin
        github = next(row for row in rows if row["id"] != ENV)
        assert "repository_provider" not in github and "gitlab_project_id" not in github
        if mode == "auto" and origin == ORIGIN:
            app = FastAPI()
            app.include_router(continuation_routes(
                product.runtime, lambda: SimpleNamespace(principal=product.principal),
            ))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/product/environments")
            assert response.status_code == 200 and response.json() == rows


@PG
@pytest.mark.parametrize("mismatch", [
    {"repository_origin": "https://another.example.test"},
    {"gitlab_project_id": 43},
    {"repository": "group/another/project"},
    {"repository_provider": "github", "repository": "group/project", "repository_id": 42},
])
async def test_gitlab_environment_identity_mismatch_skips_auto_and_refuses_explicit_or_pinned(continuation, mismatch):
    product = continuation
    await seed(product, recipe(**mismatch))
    workspace = product.runtime.product_workspace
    async with product.database.sessions() as db:
        bare = await workspace._selection(db, selection(), require_ready=True)
        assert bare["environment_build_id"] is None
        for choices in (selection(environment_id=ENV), selection(environment_build_id=BUILD)):
            with pytest.raises(HTTPException, match="409"):
                await workspace._selection(db, choices, require_ready=True)
        if mismatch.get("repository_provider") != "github":
            with pytest.raises(HTTPException, match="409"):
                await workspace._selection(db, {
                    "repo_url": "https://github.com/group/project", "github_repository_id": 42, "environment_id": ENV,
                }, require_ready=True)


@pytest.mark.parametrize("changes", [
    {"repository_provider": "github"}, {"repository_origin": "https://user:secret@gitlab.example.test"},
    {"repository_origin": "https://gitlab.example.test/api/v4"}, {"repository_origin": "https://github.com"},
    {"repository_origin": "http://private.example.test"}, {"repository": "group/../project"},
    {"repository": PATH + "?token=secret"}, {"repository": PATH + ".git"},
    {"gitlab_project_id": None}, {"gitlab_project_id": True}, {"gitlab_project_id": -1},
    {"github_repository_id": 42}, {"repository_id": 43}, {"repository_id": True},
    {"clone_access": "github"},
])
def test_gitlab_recipe_requires_unambiguous_provider_identity(changes):
    with pytest.raises(HTTPException, match="409"):
        gitlab_recipe_identity(recipe(**changes))


@PG
@pytest.mark.parametrize("changes", [
    {"resolved_recipe": ""}, {"resolved_recipe": "[]"},
    {"resolved_recipe": json.dumps(recipe(repository_origin="https://another.example.test"))},
    {"resolved_recipe": json.dumps(recipe(gitlab_project_id=43))},
    {"resolved_recipe": json.dumps(recipe(repository="group/other"))},
    {"snapshot_id": ""}, {"commit_sha": "main"}, {"commit_sha": ""},
    {"phase": "failed"}, {"sandbox_provider": "lambda"},
])
async def test_gitlab_environment_requires_immutable_validated_build(continuation, changes):
    product = continuation
    await seed(product, **changes)
    async with product.database.sessions() as db:
        with pytest.raises(HTTPException, match="409"):
            await product.runtime.product_workspace._selection(db, selection(environment_id=ENV), require_ready=True)
    rows = await product.runtime.product_workspace.catalog(product.principal)
    assert rows[0]["ready"] is False


def test_public_gitlab_image_still_requires_selected_exact_grant():
    value = recipe(clone_access="public", repository_id=42)
    url, origin, target = gitlab_recipe_identity(value)
    context = {
        "repo_url": url, "repository_provider": "gitlab", "repository_origin": origin,
        "gitlab_project_id": target, "repository_id": target, "clone_access": "public",
        "environment_build_id": BUILD,
    }
    for grants in ([], [grant(base_url="https://other.example.test")], [grant(repository_id=43)],
                   [Grant("github", "github", 1, "", policy={"product": {"repository_id": 42}})]):
        with pytest.raises(HTTPException, match="403"):
            ProductWorkspaceCompatibility.authorize_project_grants(context, grants)
    ProductWorkspaceCompatibility.authorize_project_grants(context, [grant()])


async def admitted_image(product, choices=None):
    workspace = product.runtime.product_workspace
    selected = grant()
    connections = SimpleNamespace(
        resolve_grants=AsyncMock(return_value=[selected]), require_scope=AsyncMock(),
        check=AsyncMock(), credentials=AsyncMock(side_effect=AssertionError("Images must not fetch tokens")),
        prepare_repository=AsyncMock(side_effect=AssertionError("Images must not run bare bootstrap")),
        repository_receipt=AsyncMock(return_value=None),
    )
    product.runtime.product_connections = product.database.product_connections = connections
    async with product.database.sessions.begin() as db:
        context = await workspace.capture_workspace_context(db, product.principal, choices or selection())
    session = await product.database.create_session(product.actor, NewSession())
    turn = await product.database.submit(product.actor, session.id, NewMessage(
        prompt="Use the validated project", request_key="image", connections=[selected.id],
    ))
    async with product.database.sessions.begin() as db:
        await workspace.bind_automation_turn(db, session, turn, context)
    await product.database.claim_next()
    prepared = await product.database.prepare(turn.id)
    return prepared, context, connections


@PG
@pytest.mark.parametrize("pinned", [False, True])
async def test_gitlab_image_uses_shared_restorer_receipts_and_completed_memory_without_credentials(continuation, pinned):
    product = continuation
    await seed(product)
    if pinned:
        await product.sql("UPDATE moyai.environments SET enabled=0,active_build=''")
    prepared, context, connections = await admitted_image(
        product, selection(environment_build_id=BUILD) if pinned else None,
    )
    transport = SimpleNamespace(native=AsyncMock(return_value=SimpleNamespace(object_id="sb-validated")))
    workspace = product.runtime.product_workspace
    workspace.restorer = ProductWorkspaceRestorer(product.runtime, transport=transport)
    result = await workspace.prepare(prepared)
    assert result.workspace == {"provider": "modal", "id": "sb-validated"}
    materialized = transport.native.await_args.args[1]
    assert materialized == {**context, "environment_snapshot_id": "im-validated", "image_source": "validated_environment"}
    assert "[Prepared project]" in result.prompt
    # Receipt reuse preserves the same image and does not fetch or replace files.
    await workspace.prepare(result)
    transport.native.assert_awaited_once()
    connections.credentials.assert_not_awaited()
    connections.prepare_repository.assert_not_awaited()
    async with product.database.sessions.begin() as db:
        turn = await db.get(Turn, prepared.scope.turn_id)
        turn.preparation = product.runtime.security.seal(TypeAdapter(PreparedTurn).dump_json(result))
        turn.status = "completed"
        session = await db.get(Session, prepared.scope.session_id)
        session.active_turn, session.status = None, "idle"
    scope = await workspace.repository_scope(prepared.scope.session_id, product.principal.grant,
                                             turn_id=prepared.scope.turn_id)
    assert scope["gitlab_project_id"] == 42 and scope["environment_build_id"] == BUILD
    await product.sql("UPDATE moyai.environment_builds SET phase='failed'")
    with pytest.raises(PermissionError, match="build changed"):
        await workspace.repository_scope(prepared.scope.session_id, product.principal.grant,
                                         turn_id=prepared.scope.turn_id)
    await product.sql("UPDATE moyai.environment_builds SET phase='ready'")
    # Current selection is required for historical image-backed memory too.
    connections.require_scope.side_effect = PermissionError("revoked")
    with pytest.raises(PermissionError, match="revoked"):
        await workspace.repository_scope(prepared.scope.session_id, product.principal.grant,
                                         turn_id=prepared.scope.turn_id)


@PG
@pytest.mark.parametrize("change", ["snapshot", "disabled", "phase", "revoked", "during-restore"])
async def test_gitlab_image_rechecks_build_and_authority_before_and_after_materialization(continuation, change):
    product = continuation
    await seed(product)
    prepared, _, connections = await admitted_image(product)
    workspace = product.runtime.product_workspace

    async def native(*args):
        await product.sql("UPDATE moyai.environment_builds SET snapshot_id='im-replaced'")
        return SimpleNamespace(object_id="sb-validated")

    transport = SimpleNamespace(native=AsyncMock(side_effect=native))
    workspace.restorer = ProductWorkspaceRestorer(product.runtime, transport=transport)
    if change == "snapshot":
        await product.sql("UPDATE moyai.environment_builds SET snapshot_id='im-replaced'")
    elif change == "disabled":
        await product.sql("UPDATE moyai.environments SET enabled=0")
    elif change == "phase":
        await product.sql("UPDATE moyai.environment_builds SET phase='failed'")
    elif change == "revoked":
        connections.require_scope.side_effect = PermissionError("revoked")
    with pytest.raises(PermissionError):
        await workspace.prepare(prepared)
    if change != "during-restore":
        transport.native.assert_not_awaited()
    connections.credentials.assert_not_awaited()
    connections.prepare_repository.assert_not_awaited()


def test_github_rename_does_not_reserialize_gitlab_recipes_or_collide_with_project_ids(bridge):
    from app.schema_initializers import initialize_environments

    initialize_environments(bridge.store)
    credentials = {"kind": "github_app", "installation_id": 1, "account_id": 1, "repository_ids": [42]}
    bridge.connectors.save("github", credentials, "group/old")
    github = bridge.connectors.github
    github.remember_repository(
        {"id": 42, "full_name": "group/new", "owner": {"id": 1}}, credentials, alias="group/old",
    )
    raw = json.dumps(recipe(repository_id=42), separators=(",", ":")) + "\n"
    with bridge.store.connect() as conn:
        conn.execute(
            "INSERT INTO environments(id,recipe,revision,updated_by,updated_at) VALUES(?,?,1,'test','now')",
            (ENV, raw),
        )
        conn.execute(
            "INSERT INTO environment_builds(id,environment_id,revision,recipe,resolved_recipe,created_at,actor)"
            " VALUES(?,?,1,?,?,'now','test')", (BUILD, ENV, raw, raw),
        )
    github.migrate_references(credentials)
    assert bridge.store.rows("SELECT recipe FROM environments WHERE id=?", (ENV,))[0]["recipe"] == raw
    row = bridge.store.rows("SELECT recipe,resolved_recipe FROM environment_builds WHERE id=?", (BUILD,))[0]
    assert row["recipe"] == row["resolved_recipe"] == raw


@PG
async def test_legacy_github_environment_context_and_fingerprints_are_byte_compatible(continuation):
    product = continuation
    await seed(product, {
        "name": "Legacy GitHub project", "repository": "Owner/Project", "repository_id": 42,
        "clone_access": "public",
    })
    async with product.database.sessions() as db:
        context = await product.runtime.product_workspace._selection(db, {
            "repo_url": "https://github.com/Owner/Project", "github_repository_id": 42, "environment_id": "auto",
        }, require_ready=True)
    assert context == {
        "version": 1, "source_schema": "moyai",
        "selection": {"repo_url": "https://github.com/Owner/Project", "github_repository_id": 42, "environment_id": "auto"},
        "repo_url": "https://github.com/Owner/Project", "repository_id": 42,
        "environment_id": ENV, "environment_build_id": BUILD, "environment_revision": 1,
        "build_fingerprint": "ea87b1ddec0e6f85d6a8186ae6931893519aa66ca73fe56b88e853a38e6906ba",
        "clone_access": "public", "pinned_source_build": False, "commit_sha": "a" * 40,
        "working_dir": "/workspace/repo",
        "context_fingerprint": "5ec81eff01fc21b20320ffc2233857160e5bbcedee06bb3c224405ffab656700",
    }


@PG
async def test_gitlab_continuation_retains_exact_pinned_environment_and_provider(continuation):
    from moyai.product_continuation_models import ProductContinuation

    product = continuation
    await seed(product)
    await product.sql("ALTER TABLE moyai.runs ADD COLUMN repository_provider text,"
                      "ADD COLUMN repository_origin text,ADD COLUMN gitlab_project_id bigint")
    await product.sql(
        "UPDATE moyai.runs SET repository_provider='gitlab',repository_origin=:origin,"
        "gitlab_project_id=42,repo_url=:url,environment_id=:environment,environment_build_id=:build",
        origin=ORIGIN, url=ORIGIN + "/" + PATH, environment=ENV, build=BUILD,
    )
    await product.sql("UPDATE moyai.environments SET enabled=0,active_build=''")
    connections = SimpleNamespace(
        prepare_repository=AsyncMock(), resolve_grants=AsyncMock(return_value=[grant()]),
    )
    product.runtime.product_connections = product.database.product_connections = connections
    result = await test_product_continuation.admit(product, connections=[grant().id])
    async with product.database.sessions() as db:
        binding = await db.get(ProductContinuation, ("moyai", test_product_continuation.ROOT))
        context = json.loads(product.runtime.security.unseal(binding.workspace))["context"]
    assert binding.first_turn_id == result["turn_id"]
    assert context["pinned_source_build"] and context["environment_build_id"] == BUILD
    assert context["repository_provider"] == "gitlab" and context["repository_origin"] == ORIGIN
    assert context["gitlab_project_id"] == 42 and context["commit_sha"] == "a" * 40
    assert "github_repository_id" not in context["selection"]
