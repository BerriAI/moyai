"""OAuth protocol substitutions supplement real-app/provider consent verification."""

import asyncio
import base64
import hashlib
import html
import json
import time
from dataclasses import asdict, replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import SecretStr
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RunUsage
from sqlalchemy import select

from moyai import oauth_providers, personal_oauth
from moyai.api import create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import Connection
from moyai.oauth_models import OAuthAuthorization, OAuthCredential
from moyai.oauth_providers import OAuthClient, OAuthTokens
from moyai.personal_oauth_tools import PersonalOAuthToolset


@pytest_asyncio.fixture
async def oauth(runtime, monkeypatch):
    runtime.settings.public_url = "https://moyai.test"
    exchanges = []

    async def register(settings, provider):
        return OAuthClient(client_id="moyai-client", redirect_uri=oauth_providers.callback_uri(settings, provider))

    async def exchange(provider, client, **kwargs):
        exchanges.append((provider, client, kwargs))
        return OAuthTokens(
            access_token=SecretStr("private-provider-token"), refresh_token=SecretStr("private-refresh-token"),
            expires_at=time.time() + 3600, client=client,
        )
    monkeypatch.setattr(personal_oauth, "register", register)
    monkeypatch.setattr(personal_oauth, "exchange", exchange)
    app = create_app(runtime, run_dispatcher=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://moyai.test") as client:
        response = await client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
        assert response.status_code == 200
        client.headers["X-Moyai-CSRF"] = response.json()["csrf"]
        yield client, exchanges


async def begin(client, provider="linear"):
    response = await client.post(f"/api/personal-connections/{provider}/oauth")
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()["url"]).query)


async def finish(client, params, **extra):
    return await client.get("/oauth/personal/linear/complete", params={
        "state": params["state"][0], "code": "approved-code", **extra,
    })


async def test_personal_consent_pkce_encrypted_tokens_and_grants(runtime, actor, oauth):
    client, exchanges = oauth
    params = await begin(client)
    assert params["code_challenge_method"] == ["S256"]
    assert "client_secret" not in params and params["resource"] == ["https://mcp.linear.app/mcp"]
    response = await finish(client, params, iss="https://mcp.linear.app")
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


@pytest.mark.parametrize("change", ["cookie", "provider", "issuer", "expired", "cancel"])
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
    if change == "provider":
        response = await client.get("/oauth/personal/notion/complete", params={"state": params["state"][0], "code": "code"})
    else:
        response = await finish(client, params, **(
            {"iss": "https://attacker.test"} if change == "issuer"
            else {"error": "access_denied"} if change == "cancel" else {}
        ))
    assert response.status_code == 303
    assert "personal_connection=success" not in response.headers["location"]
    assert exchanges == []
    assert not any(row["connected"] for row in (await client.get("/api/personal-connections")).json())


async def test_cross_site_landing_does_not_need_cookie_or_redeem_code(runtime, oauth):
    client, exchanges = oauth
    params = await begin(client)
    cookies = client.cookies
    client.cookies = httpx.Cookies()
    response = await client.get("/oauth/personal/linear/callback", params={
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
        tokens = OAuthTokens.model_validate_json(runtime.security.unseal(credential.encrypted))
        tokens.expires_at = time.time() - 1
        credential.encrypted = runtime.security.seal(tokens.stored())
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read my issues", request_key="oauth-refresh", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    grant = prepared.scope.grants[0]
    assert await personal_oauth.access_token(runtime.database, prepared.scope, grant) == "private-provider-token"
    assert exchanges[-1][2]["previous"] is not None
    assert (await runtime.database.authorize_scope(prepared.scope))[0] == grant
    await client.delete("/api/personal-connections/linear")
    with pytest.raises((PermissionError, HTTPException)):
        await personal_oauth.access_token(runtime.database, prepared.scope, grant)


@pytest.mark.parametrize("provider", ["linear", "notion", "github", "slack"])
async def test_provider_protocol_never_sends_secret_to_authorization_url(runtime, monkeypatch, provider):
    runtime.settings.public_url = "https://moyai.test/prefix"
    runtime.settings.personal_github_client_id = "github-app"
    runtime.settings.personal_github_client_secret = SecretStr("private-client-secret")
    runtime.settings.personal_slack_client_id = "slack-app"
    runtime.settings.personal_slack_client_secret = SecretStr("private-client-secret")
    sent = []

    async def request(url, **kwargs):
        sent.append((url, kwargs))
        return {"client_id": "registered-client"} if url.endswith("/register") else {
            "access_token": "access-token", "refresh_token": "new-refresh", "expires_in": 3600,
            "token_type": "user" if provider == "slack" else "bearer",
        }
    monkeypatch.setattr(oauth_providers, "provider_request", request)
    client = await oauth_providers.register(runtime.settings, provider)
    assert client.redirect_uri == f"https://moyai.test/prefix/oauth/personal/{provider}/callback"
    url = oauth_providers.authorization_url(provider, client, "state", "verifier")
    assert "private-client-secret" not in url
    tokens = await oauth_providers.exchange(provider, client, code="code", verifier="verifier")
    assert sent[-1][1]["data"]["code_verifier"] == "verifier"
    assert tokens.expires_at > time.time()
    await oauth_providers.exchange(provider, client, previous=tokens)
    assert sent[-1][1]["data"]["refresh_token"] == "new-refresh"
    assert "code" not in sent[-1][1]["data"]


@pytest.mark.parametrize("provider,token_type", [("slack", "bot"), ("linear", "user"), ("github", "user")])
async def test_provider_token_type_does_not_admit_a_different_credential(monkeypatch, provider, token_type):
    async def request(url, **kwargs):
        return {"access_token": "access-token", "token_type": token_type}

    monkeypatch.setattr(oauth_providers, "provider_request", request)
    client = OAuthClient(client_id="client", redirect_uri="https://moyai.test/callback")
    with pytest.raises(oauth_providers.OAuthFailure, match="invalid token"):
        await oauth_providers.exchange(provider, client, code="code", verifier="verifier")


async def test_personal_tools_approval_live_tokens_and_revocation(runtime, actor, oauth, monkeypatch):
    from moyai import personal_oauth_tools

    client, _ = oauth
    await finish(client, await begin(client))
    account = (await client.get("/api/connections")).json()[0]
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Update an issue", request_key="oauth-tools", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.database.prepare(turn.id)).scope
    calls, tokens = [], []
    fail = False

    def remote(config, token, allowed, **kwargs):
        tokens.append(token)
        tools = FunctionToolset(id="test-provider")

        def change_issue(title: str) -> str:
            calls.append(title)
            if fail:
                raise RuntimeError("private-provider-token")
            return title

        tools.add_function(change_issue, metadata={"annotations": {"readOnlyHint": False}})
        return tools

    monkeypatch.setattr(personal_oauth_tools, "remote_mcp_toolset", remote)
    tools = PersonalOAuthToolset(runtime.database, scope, scope.grants[0]).prepared(
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
    await client.delete("/api/personal-connections/linear")
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
        old = OAuthTokens.model_validate_json(runtime.security.unseal(saved.encrypted))
        old.expires_at = time.time() - 1
        saved.encrypted = runtime.security.seal(old.stored())
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read issues", request_key="oauth-concurrent", connections=[account["id"]],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.database.prepare(turn.id)).scope
    calls = []

    async def rotate(provider, client, *, previous):
        calls.append(previous.refresh_token.get_secret_value())
        await asyncio.sleep(.05)
        return OAuthTokens(access_token=SecretStr("new-access"), refresh_token=SecretStr("new-refresh"),
                           expires_at=time.time() + 3600, client=client)

    monkeypatch.setattr(personal_oauth, "exchange", rotate)
    assert await asyncio.gather(*[
        personal_oauth.access_token(runtime.database, scope, scope.grants[0]) for _ in range(3)
    ]) == ["new-access"] * 3
    assert calls == ["private-refresh-token"]
