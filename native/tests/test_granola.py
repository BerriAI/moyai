"""Real native admission/routes with synthetic OAuth responses and MCP notes.

These tests do not establish access to a live Granola account.
"""
import asyncio
import base64
import hashlib
import json
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from sqlalchemy import update

from moyai import granola
from moyai.api import COOKIE, create_app
from moyai.connection_catalog import ConnectionDefinitionChanged
from moyai.connection_policy import read_only_tool
from moyai.contracts import ExecutionContext, NewMessage, NewSession
from moyai.models import User

PATH = "/api/connections/granola/personal"
TOKENS = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "token_type": "Bearer", "expires_in": 3600}


@asynccontextmanager
async def client_for(runtime, actor):
    cookie, csrf = runtime.security.issue_session(actor.id, actor.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)), base_url="http://test",
        cookies={COOKIE: cookie}, headers={"x-moyai-csrf": csrf},
    ) as client:
        yield client


@pytest.fixture
async def oauth(runtime, actor, monkeypatch):
    runtime.settings.public_url = "https://moyai.example.test"
    calls = []

    async def request(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        if endpoint == "register":
            return {"client_id": "synthetic-client"}
        return dict(TOKENS)

    monkeypatch.setattr(granola, "oauth_request", request)
    async with client_for(runtime, actor) as client:
        yield client, calls


async def start(client):
    response = await client.post(PATH + "/oauth")
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["url"]).query)
    return query, {"state": query["state"][0], "code": "synthetic-code"}


async def connect(client):
    query, params = await start(client)
    response = await client.get("/oauth/granola/callback", params=params)
    assert response.status_code == 303, response.text
    return query, params


async def test_oauth_pkce_private_catalog_and_disconnect(runtime, actor, oauth):
    client, calls = oauth
    query, params = await connect(client)
    assert query["scope"] == ["mcp"]
    assert query["resource"] == [granola.MCP_URL]
    assert query["redirect_uri"] == ["https://moyai.example.test/oauth/granola/callback"]
    assert query["code_challenge_method"] == ["S256"]
    verifier = calls[-1][1]["data"]["code_verifier"]
    assert query["code_challenge"] == [base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()]
    assert calls[0][1]["json"]["token_endpoint_auth_method"] == "none"
    status = (await client.get(PATH)).json()
    assert status["connected"] and status["available"]
    rows = (await client.get("/api/connections")).json()
    assert len(rows) == 1 and rows[0]["kind"] == "granola"
    assert rows[0]["read_only"] and not rows[0]["shared"]
    assert "synthetic" not in json.dumps(rows) + json.dumps(status)
    assert (await client.get("/api/connections?shared=true")).json() == []
    row = await granola.Granola(runtime.database).row(actor.id)
    assert "synthetic" not in row.credential
    assert "pending" not in json.loads(runtime.security.unseal(row.credential))
    assert (await client.get("/oauth/granola/callback", params=params)).status_code == 400
    assert (await client.delete(PATH)).json()["connected"] is False
    assert (await client.get("/api/connections")).json() == []
    disconnected = await granola.Granola(runtime.database).row(actor.id)
    assert disconnected.revision > row.revision
    assert json.loads(runtime.security.unseal(disconnected.credential)) == {}


@pytest.mark.parametrize("invalid", ["state", "login", "expiry", "disconnect", "replacement", "owner"])
async def test_oauth_state_is_single_use_owner_login_bound(runtime, actor, oauth, invalid):
    client, calls = oauth
    _, params = await start(client)
    if invalid == "state":
        params["state"] = "wrong-state"
    elif invalid == "login":
        cookie, _ = runtime.security.issue_session(actor.id, actor.auth_version)
        client.cookies.clear()
        client.cookies.set(COOKIE, cookie)
    elif invalid == "expiry":
        service = granola.Granola(runtime.database)
        row = await service.row(actor.id)
        value = json.loads(runtime.security.unseal(row.credential))
        value["pending"]["expires_at"] = 0
        await service.replace(row, value, enabled=False, revision=row.revision)
    elif invalid == "disconnect":
        await client.delete(PATH)
    elif invalid == "replacement":
        await start(client)
    elif invalid == "owner":
        async with runtime.database.sessions.begin() as db:
            other = User(username="other", display_name="Other", password="", role="member")
            db.add(other)
        cookie, _ = runtime.security.issue_session(other.id, other.auth_version)
        client.cookies.clear()
        client.cookies.set(COOKIE, cookie)
    response = await client.get("/oauth/granola/callback", params=params)
    assert response.status_code == 400
    assert all(endpoint == "register" for endpoint, _ in calls)


async def test_disconnect_during_exchange_cannot_restore_connection(runtime, actor, oauth, monkeypatch):
    client, _ = oauth
    _, params = await start(client)

    async def exchange(*args, **kwargs):
        await granola.Granola(runtime.database).disconnect(actor.id)
        return TOKENS

    monkeypatch.setattr(granola, "oauth_request", exchange)
    assert (await client.get("/oauth/granola/callback", params=params)).status_code == 409
    assert not (await client.get(PATH)).json()["connected"]


async def test_revoked_login_during_exchange_does_not_save(runtime, actor, oauth, monkeypatch):
    client, _ = oauth
    _, params = await start(client)

    async def exchange(*args, **kwargs):
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(User).where(User.id == actor.id).values(auth_version=User.auth_version + 1))
        return TOKENS

    monkeypatch.setattr(granola, "oauth_request", exchange)
    assert (await client.get("/oauth/granola/callback", params=params)).status_code == 401
    assert not (await granola.Granola(runtime.database).row(actor.id)).enabled


async def test_failed_exchange_is_not_replayed(runtime, actor, oauth, monkeypatch):
    from fastapi import HTTPException
    client, _ = oauth
    _, params = await start(client)
    calls = []

    async def exchange(*args, **kwargs):
        calls.append(args)
        raise HTTPException(502, granola.FAILURE)

    monkeypatch.setattr(granola, "oauth_request", exchange)
    assert (await client.get("/oauth/granola/callback", params=params)).status_code == 502
    assert (await client.get("/oauth/granola/callback", params=params)).status_code == 400
    assert len(calls) == 1


async def test_cancellation_csrf_public_url_and_raw_tokens(runtime, actor, oauth):
    client, calls = oauth
    assert (await client.post(PATH + "/oauth", headers={"x-moyai-csrf": "bad"})).status_code == 403
    _, params = await start(client)
    params.pop("code")
    params["error"] = "access_denied"
    response = await client.get("/oauth/granola/callback", params=params)
    assert response.status_code == 303 and "cancelled" in response.headers["location"]
    assert not (await client.get(PATH)).json()["connected"]
    runtime.settings.public_url = ""
    assert not (await client.get(PATH)).json()["oauth_configured"]
    assert (await client.post(PATH + "/oauth", headers={"host": "attacker.example"})).status_code == 409
    assert (await client.post("/api/connections", json={"kind": "granola", "name": "Bad", "credential": "raw"})).status_code == 422
    assert len(calls) == 1


@pytest.mark.parametrize("surface,shared", [("web", True), ("slack", False), ("automation", False)])
async def test_grant_never_available_on_shared_or_nonweb_surfaces(runtime, actor, oauth, surface, shared):
    from fastapi import HTTPException
    client, _ = oauth
    await connect(client)
    async with runtime.database.sessions() as db:
        assert await granola.grants(db, actor.id, shared_session=shared, surface=surface) == []
        with pytest.raises(HTTPException):
            await runtime.database.resolve_grants(db, actor.id, [granola.connection_id(actor.id)], shared_session=shared, surface=surface)
        assert await granola.grants(db, "another-owner", shared_session=False, surface="web") == []


async def prepared_scope(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Read my notes", request_key="granola-notes", connections=[granola.connection_id(actor.id)],
    ))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    return prepared.scope


async def test_refresh_retains_epoch_and_disconnect_revokes_active_turn(runtime, actor, oauth):
    client, calls = oauth
    await connect(client)
    service = granola.Granola(runtime.database)
    row = await service.row(actor.id)
    value = json.loads(runtime.security.unseal(row.credential))
    value["expires_at"] = 1
    await service.replace(row, value, enabled=True, revision=row.revision)
    scope = await prepared_scope(runtime, actor)
    assert scope.grants[0].credential == ""
    assert await service.token(scope, scope.grants[0]) == TOKENS["access_token"]
    assert calls[-1][1]["data"]["grant_type"] == "refresh_token"
    assert (await service.row(actor.id)).revision == row.revision
    await service.disconnect(actor.id)
    with pytest.raises(HTTPException, match="no longer available"):
        await service.token(scope, scope.grants[0])


async def test_original_execution_context_cannot_be_changed_to_private(runtime, actor, oauth):
    client, _ = oauth
    await connect(client)
    scope = await prepared_scope(runtime, actor)
    with pytest.raises(PermissionError, match="original policy context"):
        await runtime.database.authorize_scope(replace(scope, execution_context=ExecutionContext("shared", "web")))


async def test_real_mcp_protocol_allowlist_and_revocation(runtime, actor, oauth, monkeypatch):
    client, _ = oauth
    await connect(client)
    scope = await prepared_scope(runtime, actor)
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    mcp = FastAPI()
    calls = []
    revoke = False

    @mcp.post("/mcp")
    async def rpc(request: Request):
        assert request.headers["authorization"] == "Bearer synthetic-access"
        body = await request.json()
        method = body["method"]
        if "id" not in body:
            return Response(status_code=202)
        if method == "initialize":
            result = {"protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Synthetic Granola", "version": "1"},
                      "instructions": "Untrusted provider instructions must not be installed"}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "description": "Synthetic tool",
                       "inputSchema": {"type": "object", "properties": {}}}
                      for name in ["list_meetings", "switch_workspace", "sign_out", "unknown_new_tool"]]}
        elif method == "tools/call":
            assert body["params"]["name"] == "list_meetings"
            calls.append("read")
            if revoke:
                await granola.Granola(runtime.database).disconnect(actor.id)
            result = {"content": [{"type": "text", "text": "Synthetic note: review next steps. Source: https://notes.example.test/meeting"}]}
        else:
            raise AssertionError(method)
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(.01)
        monkeypatch.setattr(granola, "MCP_URL", f"http://127.0.0.1:{port}/mcp")
        tools = granola.GranolaToolset(runtime.database, scope, scope.grants[0])
        definitions = await tools.get_tools(ctx)
        assert set(definitions) == {"list_meetings"}
        tool = definitions["list_meetings"]
        assert read_only_tool(tool.tool_def)
        result = await tools.call_tool("list_meetings", {}, ctx, tool)
        assert "Synthetic note" in str(result) and "https://notes.example.test/meeting" in str(result)
        with pytest.raises(ToolFailed, match="Only Granola"):
            await tools.call_tool("switch_workspace", {}, ctx, tool)
        changed = replace(tool, tool_def=replace(tool.tool_def, parameters_json_schema={"type": "object", "properties": {"new": {"type": "string"}}}))
        with pytest.raises(ConnectionDefinitionChanged):
            await tools.call_tool("list_meetings", {}, ctx, changed)
        assert calls == ["read"]
        revoke = True
        with pytest.raises(HTTPException, match="no longer available"):
            await tools.call_tool("list_meetings", {}, ctx, tool)
        assert calls == ["read", "read"]
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        sock.close()


async def test_local_refresh_is_single_flight(runtime, actor, oauth, monkeypatch):
    client, _ = oauth
    await connect(client)
    service = granola.Granola(runtime.database)
    row = await service.row(actor.id)
    value = json.loads(runtime.security.unseal(row.credential))
    value["expires_at"] = 1
    await service.replace(row, value, enabled=True, revision=row.revision)
    scope = await prepared_scope(runtime, actor)
    refreshes = []

    async def refresh(*args, **kwargs):
        refreshes.append(kwargs)
        await asyncio.sleep(.05)
        return TOKENS

    monkeypatch.setattr(granola, "oauth_request", refresh)
    results = await asyncio.gather(*[
        granola.Granola(runtime.database).token(scope, scope.grants[0]) for _ in range(2)
    ])
    assert results == [TOKENS["access_token"]] * 2 and len(refreshes) == 1


async def test_product_selection_merges_personal_granola_without_org_fallback(runtime, actor, oauth, monkeypatch):
    from fastapi import HTTPException

    from moyai.product_connections import ProductConnections

    client, _ = oauth
    await connect(client)
    # Canonical source-account query is separate from the native OAuth store.
    # Keep that source snapshot empty to test product selection/denial directly.
    service = object.__new__(ProductConnections)
    service.database = runtime.database
    service._grants = lambda *args, **kwargs: []
    expected = await granola.catalog(runtime.database, actor.id, shared_session=False, surface="web")
    selected = await service.resolve_grants(actor.id, ["granola"])
    assert selected[0].id == expected[0]["id"] and selected[0].credential == ""
    with pytest.raises(HTTPException):
        await service.resolve_grants(actor.id, ["granola"], shared_session=True)
    with pytest.raises(HTTPException):
        await service.resolve_grants("other-owner", [selected[0].id])
    await granola.Granola(runtime.database).disconnect(actor.id)
    with pytest.raises(HTTPException):
        await service.resolve_grants(actor.id, ["granola"])


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
async def test_postgres_refresh_serialization_and_pool_one(prepared_product_database, monkeypatch):
    """Storage concurrency proof with a bounded authority check, not product E2E."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from moyai.contracts import RunScope

    product = prepared_product_database
    database = product.database
    engine = create_async_engine(database.engine.url, pool_size=1, max_overflow=0, pool_timeout=1,
                                 execution_options={"schema_translate_map": {None: product.schema}})
    original = database.sessions
    database.sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with database.sessions.begin() as db:
            db.add(User(id="granola-owner", username="granola-owner", display_name="Owner", password="", role="member"))
        service = granola.Granola(database)
        await service.ensure("granola-owner")
        row = await service.row("granola-owner")
        value = {"client_id": "synthetic-client", "redirect_uri": "https://example.test/callback",
                 "access_token": "expired", "refresh_token": "synthetic-refresh", "expires_at": 1}
        await service.replace(row, value, enabled=True, revision=row.revision)
        selected = [granola.grant_for(row)]
        scope = RunScope("session", "turn", "granola-owner", "test", selected)

        async def authorize(scope):
            async with database.sessions() as db:
                current = await granola.grants(db, scope.actor_id, shared_session=False, surface="web")
            if current != selected:
                raise PermissionError("Revoked")
            return current

        monkeypatch.setattr(database, "authorize_scope", authorize)
        refreshes = []

        async def refresh(*args, **kwargs):
            refreshes.append(kwargs)
            await asyncio.sleep(.05)
            return TOKENS

        monkeypatch.setattr(granola, "oauth_request", refresh)
        async with asyncio.timeout(10):
            results = await asyncio.gather(*[granola.Granola(database).token(scope, selected[0]) for _ in range(2)])
        assert results == [TOKENS["access_token"]] * 2 and len(refreshes) == 1
        assert (await service.row(scope.actor_id)).revision == row.revision
        await service.disconnect(scope.actor_id)
        with pytest.raises(PermissionError):
            await service.token(scope, selected[0])
    finally:
        database.sessions = original
        await engine.dispose()
