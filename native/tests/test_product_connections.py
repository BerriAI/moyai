"""Canonical local stores and native toolsets; no real OAuth/provider calls."""

import asyncio
import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from app.connector_errors import ConnectorError
from app.db import Store
from app.security import Security
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.models.test import TestModel
from test_product_identity import settings_for

from moyai.contracts import RunScope
from moyai.product_connections import ProductConnections


@pytest.fixture
def bridge(tmp_path):
    settings = settings_for(tmp_path)
    store = Store(settings.data_dir, settings.agent_model)
    security = Security(settings)
    with store.connect() as db:
        db.execute(
            "INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            ("google:owner", "google", "owner@example.test", "Owner", "2026-10-15", "2026-10-15"),
        )

    async def authorize(scope):
        assert scope.actor_id == "google:owner"

    service = ProductConnections(
        SimpleNamespace(store_proxy=store, security=security, settings=settings),
        SimpleNamespace(authorize_scope=authorize),
    )
    yield service
    store.close()


def save(service, provider, **extra):
    service.connectors.save(provider, {"access_token": "synthetic-token", "kind": "personal", **extra}, provider)


async def scope_for(bridge, kind):
    grants = await bridge.resolve_grants("google:owner", [kind])
    return RunScope("native-session", "native-turn", "google:owner", "test", grants)


async def test_reference_only_grants_follow_live_disconnect_reconnect_and_policy(bridge):
    save(bridge, "linear")
    scope = await scope_for(bridge, "linear")
    grant = scope.grants[0]
    assert grant.credential == ""
    assert "synthetic-token" not in json.dumps(asdict(scope))
    assert await bridge.resolve_grants(scope.actor_id, [grant.id]) == [grant]
    bridge.store.execute("UPDATE connection_policies SET enabled=0 WHERE provider='linear'")
    # Explicit policy insertion for a store without a preexisting policy row.
    bridge.store.execute(
        "INSERT INTO connection_policies(provider,enabled,read_only) VALUES('linear',0,0) "
        "ON CONFLICT(provider) DO UPDATE SET enabled=0",
    )
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    bridge.store.execute("DELETE FROM connections WHERE provider='linear'")
    save(bridge, "linear")
    bridge.store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='linear'")
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    assert (await scope_for(bridge, "linear")).grants[0].id != grant.id


async def test_refresh_preserves_authority_and_revocation_wins_while_awaiting(bridge, monkeypatch):
    save(bridge, "linear", expires_at=time.time() - 1, refresh_token="synthetic-refresh")
    scope = await scope_for(bridge, "linear")

    async def exchange(provider, **kwargs):
        return {"access_token": "synthetic-refreshed", "expires_at": time.time() + 3600}

    monkeypatch.setattr(bridge.connectors, "exchange", exchange)
    value = await bridge.credentials(scope, scope.grants[0])
    assert value["access_token"] == "synthetic-refreshed"
    await bridge.require_scope(scope)

    save(bridge, "linear", expires_at=time.time() - 1, refresh_token="synthetic-refresh")
    scope = await scope_for(bridge, "linear")

    async def revoked(provider, **kwargs):
        bridge.store.execute("DELETE FROM connections WHERE provider=?", (provider,))
        return {"access_token": "must-not-resurrect"}

    monkeypatch.setattr(bridge.connectors, "exchange", revoked)
    with pytest.raises(ConnectorError, match="changed"):
        await bridge.credentials(scope, scope.grants[0])
    assert not bridge.store.rows("SELECT 1 FROM connections WHERE provider='linear'")


async def test_personal_slack_is_private_and_never_falls_back(bridge):
    save(bridge, "slack")
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))
    scope = await scope_for(bridge, "slack")
    assert scope.grants[0].read_only
    assert scope.grants[0].policy["product"]["personal_owner"] == scope.actor_id
    for shared, surface in [(True, "web"), (False, "slack"), (True, "automation")]:
        with pytest.raises(HTTPException):
            await bridge.resolve_grants(scope.actor_id, ["slack"], shared_session=shared, surface=surface)
    personal.disconnect(scope.actor_id)
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)
    assert (await scope_for(bridge, "slack")).grants[0].policy["product"]["personal_owner"] is None


@pytest.mark.parametrize("legacy_token", [False, True])
async def test_personal_metadata_refresh_preserves_inflight_native_grant(bridge, legacy_token):
    from app.connection_authority import AUTHORITY

    save(bridge, "slack")
    personal = bridge.connectors.personal_slack
    owner = "google:owner"
    personal.save(owner, {"access_token": "synthetic-personal"}, "Personal", personal.revision(owner))
    if legacy_token:
        row = personal.row(owner)
        credentials = json.loads(personal.security.decrypt(row["encrypted"]))
        credentials.pop(AUTHORITY)
        bridge.store.execute(
            "UPDATE personal_slack_grants SET encrypted=? WHERE owner_id=?",
            (personal.security.encrypt(json.dumps(credentials)), owner),
        )
    scope = await scope_for(bridge, "slack")
    row = personal.row(owner)
    credentials = json.loads(personal.security.decrypt(row["encrypted"]))
    personal.save(owner, credentials, "Verified personal account", row["revision"], refresh=True)
    assert personal.row(owner)["revision"] != row["revision"]
    await bridge.require_scope(scope)
    assert await bridge.resolve_grants(owner, [grant.id for grant in scope.grants]) == scope.grants
    personal.save(owner, credentials, "Reconnected account", personal.row(owner)["revision"])
    with pytest.raises((HTTPException, PermissionError)):
        await bridge.require_scope(scope)


async def test_canonical_notion_helpers_run_as_native_toolset_and_recheck_after_preflight(bridge, monkeypatch):
    save(bridge, "notion")
    scope = await scope_for(bridge, "notion")
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            bridge.store.execute("DELETE FROM connections WHERE provider='notion'")
            return {"id": "a" * 32, "type": "paragraph"}
        pytest.fail("Revoked Notion mutation reached the provider.")

    monkeypatch.setattr(bridge.connectors, "request", request)
    toolset = bridge.toolset(scope, scope.grants[0])
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    tools = await toolset.get_tools(ctx)
    assert tools["query"].tool_def.metadata["annotations"]["readOnlyHint"]
    assert not tools["update_block"].tool_def.metadata["annotations"]["readOnlyHint"]
    with pytest.raises(HTTPException):
        await toolset.call_tool("update_block", {
            "block_id": "a" * 32, "block": {"type": "paragraph", "rich_text": []},
        }, ctx, tools["update_block"])
    assert len(calls) == 1


async def test_grant_resolution_offloads_canonical_database_wait(bridge, monkeypatch):
    save(bridge, "linear")
    original = bridge._grants
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge, "_grants", blocked)
    task = asyncio.create_task(bridge.resolve_grants("google:owner", ["linear"]))
    await entered.wait()
    await asyncio.sleep(0.01)
    assert not task.done()
    await task


async def test_reversible_policy_toggle_still_revokes_the_original_grant(bridge):
    from app.connection_authority import set_policy

    save(bridge, "linear")
    scope = await scope_for(bridge, "linear")
    set_policy(bridge.store, bridge.security, "linear", False, False)
    set_policy(bridge.store, bridge.security, "linear", True, False)
    with pytest.raises(HTTPException):
        await bridge.require_scope(scope)


async def test_public_metadata_cache_singleflight_copy_and_failed_refresh_evicts(bridge, monkeypatch):
    calls = []
    state = {"private": False, "id": 123}

    async def public(repository="", repository_id=None):
        calls.append((repository, repository_id))
        await asyncio.sleep(.01)
        return {"id": state["id"], "full_name": "owner/public", "private": state["private"]}

    async def source(operation):
        return await operation(SimpleNamespace(github=SimpleNamespace(public_repository=public)))

    monkeypatch.setattr(bridge, "source_call", source)
    values = await asyncio.gather(*(bridge.public_repository(repository_id=123) for _ in range(8)))
    assert calls == [("", 123)]
    values[0]["full_name"] = "caller-mutated"
    assert (await bridge.public_repository(repository_id=123))["full_name"] == "owner/public"
    state["private"] = True
    with pytest.raises(HTTPException):
        await bridge.public_repository(repository_id=123, refresh=True)
    assert 123 not in bridge._public_repositories
    with pytest.raises(HTTPException):
        await bridge.public_repository(repository_id=123)
    assert len(calls) == 3
    state.update(private=False, id=456)
    with pytest.raises(HTTPException):
        await bridge.public_repository(repository_id=123, refresh=True)
    assert not bridge._public_repositories
    state["id"] = 123
    await bridge.public_repository(repository_id=123)
    bridge._public_repositories[123] = (time.monotonic() - 301, values[1])
    await bridge.public_repository(repository_id=123)
    assert len(calls) == 6
    # Names/admission always make a fresh anonymous request, even with an ID cache.
    await bridge.public_repository("owner/public")
    await bridge.public_repository("owner/public")
    assert len(calls) == 8


async def test_onboarding_reuses_canonical_auth_csrf_and_provider_routes(bridge, monkeypatch):
    import httpx
    from fastapi import FastAPI, Response

    from moyai.product_connections import routes

    async def authorized(*args, **kwargs):
        return None

    bridge.product.identity = SimpleNamespace(
        authenticate=authorized, require_request_current=authorized,
    )
    bridge.settings.linear_client_id = "synthetic-client"
    bridge.settings.linear_client_secret = "synthetic-client-secret"

    async def verify(provider, credentials):
        return "Local " + provider

    monkeypatch.setattr(bridge.connectors, "verify", verify)
    app = FastAPI()
    app.include_router(routes(bridge))
    response = Response()
    sid = bridge.security.new_session(response)
    cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=bridge.settings.public_url) as client:
        assert (await client.get("/api/product/connections")).status_code == 401
        client.cookies.set("workspace_session", cookie)
        assert (await client.get("/api/product/connections")).status_code == 200
        assert (await client.post("/api/connections/linear", json={"token": "local-token"})).status_code == 403
        client.headers.update({"Origin": bridge.settings.public_url, "X-CSRF-Token": bridge.security.csrf(sid)})
        connected = await client.post("/api/connections/linear", json={"token": "local-token"})
        assert connected.status_code == 200 and "local-token" not in connected.text
        scope = await scope_for(bridge, "linear")
        oauth = await client.post("/api/connections/linear/oauth")
        assert oauth.status_code == 200 and "linear.app/oauth/authorize?" in oauth.json()["url"]
        assert (await client.delete("/api/connections/linear")).status_code == 200
        assert not bridge.store.rows("SELECT 1 FROM oauth_states WHERE provider='linear'")
        with pytest.raises(HTTPException):
            await bridge.require_scope(scope)


async def test_personal_slack_keeps_bot_operations_separate_and_never_uses_user_token(bridge, monkeypatch):
    from dataclasses import replace

    from moyai.effects import requires_receipt

    save(bridge, "slack", expires_at=time.time() - 1, bot={
        "access_token": "synthetic-bot-only", "bot_user_id": "UBOT00001",
        "team": {"id": "TTEST0001"}, "scope": "chat:write",
    })
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))
    scope = await scope_for(bridge, "slack")
    assert {g.policy["product"]["access"] for g in scope.grants} == {"personal_reads", "bot"}
    shared = await bridge.resolve_grants(scope.actor_id, ["slack"], shared_session=True, surface="slack")
    assert len(shared) == 1 and shared[0].policy["product"]["access"] == "bot"
    bot = next(g for g in scope.grants if g.policy["product"]["access"] == "bot")
    calls = []

    async def request(method, url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer synthetic-bot-only"
        assert url.endswith("/chat.postMessage")
        assert kwargs["json"]["text"].startswith("Owner: ")
        calls.append(url)
        return {"ok": True, "channel": "CTEST0001", "ts": "1792100000.001"}

    monkeypatch.setattr(bridge.connectors, "request", request)
    tools = bridge.toolset(scope, bot)
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    definitions = await tools.get_tools(ctx)
    assert "search" not in definitions and "send" in definitions
    assert requires_receipt(replace(definitions["send"].tool_def, name=f"slack_{bot.id[:8]}_send"))
    result = await tools.call_tool("send", {"channel": "CTEST0001", "text": "Local-only output."}, ctx, definitions["send"])
    assert result["complete"] and len(calls) == 1


def test_repository_metadata_migration_preserves_frozen_source_runs(bridge):
    credentials = {"kind": "github_app", "installation_id": 1, "account_id": 1, "repository_ids": [7]}
    bridge.connectors.save("github", credentials, "org/old")
    github = bridge.connectors.github
    github.remember_repository(
        {"id": 7, "full_name": "org/new", "owner": {"id": 1}}, credentials, alias="org/old",
    )
    run = bridge.store.create_run("Preserved history", "https://github.com/org/old", "demo", ["github"],
                                  chat_enabled=True, user_id="google:owner")
    github.source_run_mutable = lambda connection, identity: identity != run["id"]
    github.migrate_references(credentials)
    assert bridge.store.run(run["id"])["repo_url"] == "https://github.com/org/old"
    assert github.repository_name(7, credentials) == "org/new"
