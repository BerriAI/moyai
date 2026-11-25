"""OAuth protocol substitutions supplement real-app/provider consent verification."""

import asyncio
import base64
import hashlib
import html
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from app.connectors import Connectors
from app.db import Store
from app.security import Security as ProductSecurity
from fastapi import HTTPException
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage
from sqlalchemy import select
from test_product_identity import settings_for

from moyai import oauth_providers, personal_oauth
from moyai.api import create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import Connection
from moyai.oauth_models import OAuthAuthorization, OAuthCredential
from moyai.personal_oauth_tools import PersonalOAuthToolset
from moyai.product_connections import ProductConnections


@pytest_asyncio.fixture
async def oauth(runtime, monkeypatch, tmp_path):
    runtime.settings.public_url = "https://moyai.test"
    exchanges = []
    settings = settings_for(tmp_path, public_url="https://moyai.test", linear_client_id="moyai-client",
                            linear_client_secret="shared-app-secret", notion_client_id="notion-client",
                            notion_client_secret="shared-notion-secret", github_oauth_client_id="personal-github",
                            github_oauth_client_secret="personal-github-secret")
    store = Store(settings.data_dir, settings.agent_model)
    service = ProductConnections(SimpleNamespace(store_proxy=store, security=ProductSecurity(settings), settings=settings), runtime.database)
    service.connectors.github.save_app({"client_id": "github-client", "client_secret": "shared-github-secret"})

    async def request(self, method, url, **kwargs):
        if "token" in url or "access_token" in url:
            return {"access_token": "private-provider-token", "refresh_token": "private-refresh-token",
                    "expires_in": 3600, "scope": "repo,read:org"}
        if "linear" in url:
            return {"data": {"viewer": {"organization": {"name": "Test workspace"}}}}
        return {"bot": {"workspace_name": "Test workspace"}}

    async def github_request(self, method, path, **kwargs):
        assert path == "/user"
        return {"id": 1, "login": "test-user"}

    from app.github import GitHub
    monkeypatch.setattr(Connectors, "request", request)
    monkeypatch.setattr(GitHub, "request", github_request)
    real_exchange = personal_oauth.exchange

    async def exchange(service, provider, identity, **kwargs):
        exchanges.append((provider, identity, kwargs))
        return await real_exchange(service, provider, identity, **kwargs)

    monkeypatch.setattr(personal_oauth, "exchange", exchange)
    app = create_app(runtime, run_dispatcher=False)
    # The full canonical service is reused; this fixture substitutes provider I/O
    # and login only. Existing-product deployment proof is recorded separately.
    runtime.product_connections = service
    runtime.database.product_connections = service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://moyai.test") as client:
        response = await client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
        assert response.status_code == 200
        client.headers["X-Moyai-CSRF"] = response.json()["csrf"]
        yield client, exchanges
    store.close()


async def begin(client, provider="linear"):
    response = await client.post(f"/api/personal-connections/{provider}/oauth")
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["url"]).query)


async def finish(client, params, provider="linear", **extra):
    return await client.get(f"/oauth/personal/{provider}/complete", params={
        "state": params["state"][0], "code": "approved-code", **extra,
    })


async def test_personal_consent_pkce_encrypted_tokens_and_grants(runtime, actor, oauth):
    client, exchanges = oauth
    params = await begin(client)
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in params and "resource" not in params
    assert params["client_id"] == ["moyai-client"]
    assert params["redirect_uri"] == ["https://moyai.test/oauth/linear/callback"]
    response = await finish(client, params)
    assert response.status_code == 303 and "personal_connection=success" in response.headers["location"]
    verifier = exchanges[0][2]["verifier"]
    assert params["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    ]
    inventory = (await client.get("/api/personal-connections")).json()
    row = next(row for row in inventory if row["provider"] == "linear")
    assert row["connected"]
    accounts = (await client.get("/api/connections")).json()
    account = next(item for item in accounts if item["id"] == row["connection_id"])
    assert account["shared"] is False and account["policy"]["oauth"]
    async with runtime.database.sessions() as db:
        credential = await db.get(OAuthCredential, row["connection_id"])
        connection = await db.get(Connection, row["connection_id"])
        assert "private-provider-token" not in credential.encrypted
        assert runtime.security.unseal(connection.credential) == b""
        grants = await runtime.database.native_grants(db, actor.id, [connection.id])
        assert "private-provider-token" not in json.dumps([asdict(g) for g in grants])
        with pytest.raises(HTTPException, match="private conversation"):
            await runtime.database.native_grants(db, actor.id, [connection.id], shared_session=True)
        with pytest.raises(HTTPException):
            await runtime.database.native_grants(db, "another-user", [connection.id])
    assert "private-provider-token" not in json.dumps(inventory + accounts)
    assert "personal_connection=expired" in (await finish(client, params)).headers["location"]
    assert len(exchanges) == 1


@pytest.mark.parametrize("change", ["cookie", "provider", "client", "expired", "cancel"])
async def test_invalid_or_cancelled_consent_does_not_connect(runtime, oauth, change):
    client, exchanges = oauth
    params = await begin(client)
    if change == "expired":
        async with runtime.database.sessions.begin() as db:
            row = await db.scalar(select(OAuthAuthorization))
            row.expires_at = 0
    if change == "cookie":
        # A new valid session for the same account still cannot consume this flow.
        response = await client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
        client.headers["X-Moyai-CSRF"] = response.json()["csrf"]
    if change == "client":
        runtime.product_connections.settings.linear_client_id = "replacement-app"
    if change == "provider":
        response = await client.get("/oauth/personal/notion/complete", params={"state": params["state"][0], "code": "code"})
    else:
        response = await finish(client, params, **(
            {"error": "access_denied"} if change == "cancel" else {}
        ))
    assert response.status_code == 303
    assert "personal_connection=success" not in response.headers["location"]
    assert len(exchanges) == (1 if change == "client" else 0)
    assert not any(row["connected"] for row in (await client.get("/api/personal-connections")).json())


async def test_cross_site_landing_does_not_need_cookie_or_redeem_code(runtime, oauth):
    client, exchanges = oauth
    params = await begin(client)
    cookies = client.cookies
    client.cookies = httpx.Cookies()
    response = await client.get("/oauth/linear/callback", params={
        "state": params["state"][0], "code": 'code"><script>alert(1)</script>',
    })
    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "no-store" in response.headers["cache-control"]
    assert '<script>alert' not in response.text
    assert 'src="/oauth/personal/continue.js"' in response.text
    assert exchanges == []
    destination = html.unescape(response.text.split('id="continue" href="')[1].split('"')[0])
    assert (await client.get(destination)).status_code == 401
    client.cookies = cookies
    assert "personal_connection=success" in (await client.get(destination)).headers["location"]
    assert len(exchanges) == 1


async def test_disconnect_and_new_start_fence_inflight_callback(runtime, oauth, monkeypatch):
    client, _ = oauth
    entered, release = asyncio.Event(), asyncio.Event()
    exchange = personal_oauth.exchange

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await exchange(*args, **kwargs)

    monkeypatch.setattr(personal_oauth, "exchange", delayed)
    params = await begin(client)
    callback = asyncio.create_task(finish(client, params))
    await entered.wait()
    response = await client.delete("/api/personal-connections/linear")
    assert response.status_code == 200
    release.set()
    assert "personal_connection=expired" in (await callback).headers["location"]
    async with runtime.database.sessions() as db:
        assert (await db.scalar(select(OAuthCredential))).encrypted == ""
    params = await begin(client)
    newer = await begin(client)
    assert "personal_connection=expired" in (await finish(client, params)).headers["location"]
    assert "personal_connection=success" in (await finish(client, newer)).headers["location"]


async def test_native_account_cannot_be_shared_or_replaced_with_a_token(oauth):
    client, _ = oauth
    params = await begin(client)
    await finish(client, params)
    account = (await client.get("/api/connections")).json()[0]
    response = await client.put(f"/api/connections/{account['id']}", json={
        "name": "shared", "kind": "linear", "credential": "replacement", "shared": True,
    })
    assert response.status_code == 409
    response = await client.post("/api/connections", json={
        "name": "forged", "kind": "linear", "credential": "replacement", "policy": {"oauth": True},
    })
    assert response.status_code == 422


async def test_refresh_keeps_grant_authority_and_disconnect_revokes(runtime, actor, oauth, monkeypatch):
    client, exchanges = oauth
    await finish(client, await begin(client))
    account = (await client.get("/api/connections")).json()[0]
    async with runtime.database.sessions.begin() as db:
        credential = await db.get(OAuthCredential, account["id"])
        tokens = json.loads(runtime.security.unseal(credential.encrypted))
        tokens["expires_at"] = time.time() - 1
        credential.encrypted = runtime.security.seal(tokens)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Read my issues", request_key="oauth-refresh", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(turn.id)
    grant = prepared.scope.grants[0]
    assert await personal_oauth.access_token(runtime.database, prepared.scope, grant) == "private-provider-token"
    assert exchanges[-1][2]["previous"] is not None
    assert (await runtime.database.authorize_scope(prepared.scope))[0] == grant
    await client.delete("/api/personal-connections/linear")
    with pytest.raises((PermissionError, HTTPException)):
        await personal_oauth.access_token(runtime.database, prepared.scope, grant)


@pytest.mark.parametrize("provider", ["linear", "notion", "github"])
async def test_shared_app_protocol_and_refresh(runtime, oauth, monkeypatch, provider):
    client, _ = oauth
    service = runtime.product_connections
    sent = []
    original = Connectors.request

    async def request(self, method, url, **kwargs):
        sent.append((url, kwargs))
        return await original(self, method, url, **kwargs)

    monkeypatch.setattr(Connectors, "request", request)
    params = await begin(client, provider)
    assert params["redirect_uri"] == [f"https://moyai.test/oauth/{provider}/callback"]
    assert "client_secret" not in params
    assert ("code_challenge" in params) == (provider != "notion")
    assert "personal_connection=success" in (await finish(client, params, provider)).headers["location"]
    token_request = sent[0][1]
    payload = token_request.get("json", token_request.get("data"))
    if provider == "notion":
        assert token_request["auth"] == ("notion-client", "shared-notion-secret")
    else:
        assert payload["client_secret"] == ("personal-github-secret" if provider == "github" else "shared-app-secret")
        assert payload["code_verifier"]
    identity = await oauth_providers.client_identity(service, provider)
    renewed = await oauth_providers.exchange(service, provider, identity, previous={"refresh_token": "old-refresh"})
    assert renewed["expires_at"] > time.time()
    assert renewed["refresh_token"] == "private-refresh-token"
    assert "shared-app-secret" not in json.dumps(renewed)
    refresh_payload = sent[2][1].get("json", sent[2][1].get("data")) if provider != "github" else sent[1][1]["data"]
    assert refresh_payload["refresh_token"] == "old-refresh"
    assert "code" not in refresh_payload


async def test_slack_uses_only_the_existing_personal_service(runtime, oauth):
    client, _ = oauth
    rows = (await client.get("/api/personal-connections")).json()
    assert {row["provider"] for row in rows} == {"linear", "notion", "github", "gmail", "google_calendar"}
    assert (await client.post("/api/personal-connections/slack/oauth")).status_code == 422
    assert not personal_oauth.personal_callback(httpx.Request("GET", "https://moyai.test/oauth/slack/callback?state=personal-slack:test"))


async def test_github_oauth_app_needs_no_installation_and_never_uses_app_client(runtime, oauth):
    client, _ = oauth
    params = await begin(client, "github")
    assert params["client_id"] == ["personal-github"]
    assert set(params["scope"][0].split()) == {"repo", "read:org"}
    assert "personal_connection=success" in (await finish(client, params, "github")).headers["location"]
    # An existing registered GitHub App is not a personal OAuth fallback.
    runtime.product_connections.settings.github_oauth_client_id = ""
    runtime.product_connections.settings.github_oauth_client_secret = ""
    assert (await client.post("/api/personal-connections/github/oauth")).status_code == 409
    assert await oauth_providers.client_identity(runtime.product_connections, "github") == ""


@pytest.mark.parametrize("scope", ["", "repo", "read:org", "public_repo,read:org"])
async def test_github_insufficient_consent_is_not_saved(runtime, oauth, monkeypatch, scope):
    client, _ = oauth

    async def token(self, *args, **kwargs):
        return {"access_token": "insufficient-token", "scope": scope}

    monkeypatch.setattr(Connectors, "request", token)
    result = await finish(client, await begin(client, "github"), "github")
    assert "personal_connection=failed" in result.headers["location"]
    assert not any(row["connected"] for row in (await client.get("/api/personal-connections")).json())


@pytest.mark.parametrize("scope", ["repo,write:org", "repo admin:org"])
async def test_github_normalized_organization_scope_can_connect(runtime, oauth, monkeypatch, scope):
    client, _ = oauth

    async def token(self, *args, **kwargs):
        return {"access_token": "private-provider-token", "scope": scope}

    monkeypatch.setattr(Connectors, "request", token)
    result = await finish(client, await begin(client, "github"), "github")
    assert "personal_connection=success" in result.headers["location"]


async def test_personal_github_does_not_fall_back_to_installed_bot(runtime, actor, oauth):
    from test_product_connections import github_accounts

    client, _ = oauth
    service = runtime.product_connections
    github_accounts(service, 42)
    # The installation remains usable for explicit shared/automated work.
    assert await service.resolve_grants(actor.id, ["github"], shared_session=True, surface="automation")
    for surface in ("web", "slack"):
        assert not any(row["kind"] == "github" for row in await service.catalog(
            actor.id, surface=surface, shared_session=surface == "slack",
        ))
        with pytest.raises(HTTPException):
            await service.resolve_grants(actor.id, ["github"], surface=surface)
    await finish(client, await begin(client, "github"), "github")
    await client.delete("/api/personal-connections/github")
    assert not any(row["kind"] == "github" for row in await service.catalog(actor.id, surface="slack"))
    service.settings.github_oauth_client_secret = ""  # Partial config is also fail-closed.
    with pytest.raises(HTTPException):
        await service.resolve_grants(actor.id, ["github"])


async def test_slack_alias_requires_current_verified_owner_for_every_token(runtime, actor, oauth, monkeypatch):
    client, _ = oauth
    await finish(client, await begin(client, "github"), "github")
    identity = personal_oauth.connection_id(actor.id, "github")
    linked = {"owner": actor.id}

    async def owner(db, requester):
        assert requester == "slack:TLOCAL001:UALICE01"
        return linked["owner"]

    monkeypatch.setattr(runtime.database, "product_identity", SimpleNamespace(personal_memory_owner=owner))
    async with runtime.database.sessions() as db:
        grants = await runtime.database.native_grants(
            db, "slack:TLOCAL001:UALICE01", [identity], shared_session=True, surface="slack",
        )
        assert len(grants) == 1
        linked["owner"] = "another-work-account"
        with pytest.raises(HTTPException):
            await runtime.database.native_grants(
                db, "slack:TLOCAL001:UALICE01", [identity], shared_session=True, surface="slack",
            )
        linked["owner"] = None
        with pytest.raises(HTTPException):
            await runtime.database.native_grants(
                db, "slack:TLOCAL001:UALICE01", [identity], shared_session=True, surface="slack",
            )


@pytest.mark.parametrize("lifetime", ["3600", None, True, 0, -1, float("inf"), float("nan")])
async def test_invalid_provider_expiry_does_not_connect(runtime, oauth, monkeypatch, lifetime):
    client, _ = oauth

    async def invalid_token(self, method, url, **kwargs):
        return {"access_token": "unusable-token", "expires_in": lifetime}

    monkeypatch.setattr(Connectors, "request", invalid_token)
    params = await begin(client)
    response = await finish(client, params)
    assert "personal_connection=failed" in response.headers["location"]
    inventory = (await client.get("/api/personal-connections")).json()
    assert not any(row["connected"] for row in inventory)
    async with runtime.database.sessions() as db:
        assert all(not row.encrypted for row in await db.scalars(select(OAuthCredential)))


@pytest.mark.parametrize("provider,tool_name", [("linear", "teams"), ("notion", "search")])
async def test_canonical_tools_use_only_personal_token_and_live_workspace_policy(
    runtime, actor, oauth, monkeypatch, provider, tool_name,
):
    client, _ = oauth
    service = runtime.product_connections
    service.connectors.save(provider, {"access_token": "organization-token", "kind": "oauth"}, "Shared account")
    original = service.connectors.connection_revision(provider)
    await finish(client, await begin(client, provider), provider)
    account = next(row for row in (await client.get("/api/connections")).json() if row["policy"].get("oauth"))
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Read my account", request_key=f"canonical-{provider}", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.admission.prepare(turn.id)).scope
    grant = scope.grants[0]
    sent = []

    async def request(self, method, url, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        if provider == "linear":
            return {"data": {"teams": {"nodes": [], "pageInfo": {"hasNextPage": False}}}}
        return {"results": [], "has_more": False, "next_cursor": None}

    monkeypatch.setattr(Connectors, "request", request)
    tools = service.toolset(scope, grant)
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    definition = (await tools.get_tools(ctx))[tool_name]
    args = {"query": "proof"} if provider == "notion" else {}
    await tools.call_tool(tool_name, args, ctx, definition)
    assert sent == ["Bearer private-provider-token"]
    assert service.connectors.connection_revision(provider) == original
    service.store.execute(
        "INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,1,1) "
        "ON CONFLICT(provider) DO UPDATE SET read_only=1", (provider,),
    )
    with pytest.raises(PermissionError, match="read only"):
        await service.credentials(scope, grant, write=True)
    with pytest.raises(PermissionError, match="read only"):
        await service.check(scope, grant, write=True)
    service.store.execute("UPDATE connection_policies SET enabled=0 WHERE provider=?", (provider,))
    with pytest.raises(PermissionError, match="disabled"):
        await tools.call_tool(tool_name, args, ctx, definition)
    assert sent == ["Bearer private-provider-token"]
    assert service.connectors.connection_revision(provider) == original


@pytest.mark.parametrize("surface", ["slack", "automation"])
async def test_personal_grants_require_private_web_surface(runtime, actor, oauth, surface):
    client, _ = oauth
    await finish(client, await begin(client))
    account = next(row for row in (await client.get("/api/connections")).json() if row["policy"].get("oauth"))
    with pytest.raises(HTTPException):
        await runtime.product_connections.resolve_grants(actor.id, [account["id"]], surface=surface)


async def test_personal_tools_approval_live_tokens_and_revocation(runtime, actor, oauth, monkeypatch):
    from pydantic_ai_harness.github import GitHub

    client, _ = oauth
    await finish(client, await begin(client, "github"), "github")
    account = (await client.get("/api/connections")).json()[0]
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Update an issue", request_key="oauth-tools", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.admission.prepare(turn.id)).scope
    calls, tokens = [], []
    fail = False

    def remote(self):
        tokens.append(self.auth)
        tools = FunctionToolset(id="test-provider")

        def change_issue(title: str) -> str:
            calls.append(title)
            if fail:
                raise RuntimeError("private-provider-token")
            return title

        tools.add_function(change_issue, metadata={"annotations": {"readOnlyHint": False}})
        return tools

    monkeypatch.setattr(GitHub, "get_toolset", remote)
    tools = PersonalOAuthToolset(runtime.product_connections, scope, scope.grants[0]).prepared(
        lambda ctx, definitions: [replace(tool, kind="unapproved") for tool in definitions],
    ).approval_required()
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    definition = (await tools.get_tools(ctx))["change_issue"]
    with pytest.raises(ApprovalRequired):
        await tools.call_tool("change_issue", {"title": "Approved title"}, ctx, definition)
    assert not calls
    approved = replace(ctx, tool_call_approved=True)
    assert await tools.call_tool("change_issue", {"title": "Approved title"}, approved, definition) == "Approved title"
    assert calls == ["Approved title"] and set(tokens) == {"private-provider-token"}
    fail = True
    with pytest.raises(ToolFailed, match="operation was not confirmed") as error:
        await tools.call_tool("change_issue", {"title": "Uncertain write"}, approved, definition)
    assert "private-provider-token" not in str(error.value)
    assert calls == ["Approved title", "Uncertain write"]  # Writes are never replayed.
    await client.delete("/api/personal-connections/github")
    count = len(tokens)
    with pytest.raises((PermissionError, ToolFailed, HTTPException)):
        await tools.call_tool("change_issue", {"title": "Revoked"}, approved, definition)
    assert len(tokens) == count and calls == ["Approved title", "Uncertain write"]


async def test_concurrent_token_refresh_rotates_once(runtime, actor, oauth, monkeypatch):
    client, _ = oauth
    await finish(client, await begin(client))
    account = (await client.get("/api/connections")).json()[0]
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(OAuthCredential, account["id"])
        old = json.loads(runtime.security.unseal(saved.encrypted))
        old["expires_at"] = time.time() - 1
        saved.encrypted = runtime.security.seal(old)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Read issues", request_key="oauth-concurrent", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.admission.prepare(turn.id)).scope
    calls = []

    async def rotate(service, provider, identity, *, previous):
        calls.append(previous["refresh_token"])
        await asyncio.sleep(.05)
        return {"access_token": "new-access", "refresh_token": "new-refresh",
                "expires_at": time.time() + 3600, "client_id": identity}

    monkeypatch.setattr(personal_oauth, "exchange", rotate)
    assert await asyncio.gather(*[
        personal_oauth.access_token(runtime.database, scope, scope.grants[0]) for _ in range(3)
    ]) == ["new-access"] * 3
    assert calls == ["private-refresh-token"]
