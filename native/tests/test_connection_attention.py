"""Disposable PostgreSQL lifecycle/dispatch tests; external providers are substituted."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import test_google_attention as enrollment
from fastapi import HTTPException
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ApprovalRequired, ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select, text
from test_product_identity import google_cookie

from moyai import connection_attention, connection_attention_discovery
from moyai.api import create_app
from moyai.attention_policy import attention_reads, declared_reads
from moyai.automations.product_automation_models import ProductAutomationBinding as Binding
from moyai.automations.product_automations import Mutation
from moyai.connection_policy import ConnectionPolicy
from moyai.connections import connection_tools
from moyai.contracts import NewMessage, NewSession
from moyai.gateway_config import GatewayCapability, GatewayDiscovery
from moyai.models import Connection, User
from moyai.oauth_models import OAuthCredential
from moyai.personal_oauth_tools import PersonalOAuthToolset
from moyai.runtime import Runtime, configure

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
admitted_product = enrollment.admitted_product
automations = enrollment.automations
canonical_connections = enrollment.canonical_connections
google = enrollment.google


@pytest_asyncio.fixture
async def connected(google):
    """Assemble the real HTTP routes, identity and automation service on disposable PG."""
    ctx = google
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "ALTER TABLE moyai.personal_slack_grants ADD COLUMN revision TEXT NOT NULL DEFAULT 'initial'"
        ))
    runtime = Runtime(ctx.product.settings.model_copy(update={
        "product_mode": "standalone", "public_url": ctx.runtime.settings.public_url,
    }))
    unused = runtime.database
    await unused.close()
    runtime.database = ctx.product.database
    runtime.security = ctx.product.database.security
    runtime.product_identity = ctx.product.adapter
    runtime.product_settings = ctx.service.settings
    runtime.product_connections = ctx.runtime.product_connections
    runtime.product_automations = ctx.service
    original_runtime = ctx.runtime
    ctx.runtime = runtime
    configure(runtime)
    cookie, csrf = google_cookie(ctx.product.adapter, sub=ctx.product.subject)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
            base_url=runtime.settings.public_url, cookies={"workspace_session": cookie},
            headers={"X-Moyai-CSRF": csrf, "X-CSRF-Token": csrf, "Origin": runtime.settings.public_url},
        ) as client:
            original_client = ctx.client
            ctx.client = client
            yield ctx
            ctx.client = original_client
    finally:
        ctx.runtime = original_runtime
        runtime.database = unused
        await runtime.close()


@pytest.fixture
def remote_reads(connected, monkeypatch):
    state = SimpleNamespace(reads=["read_item", "read_other"], failure=False, during=None, calls=0)

    async def list_tools():
        state.calls += 1
        # A real competing source writer must acquire the fence while discovery
        # is in progress: neither CRUD nor toggle may hold it over provider I/O.
        async with connected.product.source.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout='500ms'"))
            await db.execute(text("SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)"))
        if state.during is not None:
            await state.during()
        if state.failure:
            raise RuntimeError("provider exception with synthetic-private-token")
        return [
            *(MCPTool(name=name, inputSchema={"type": "object"}, annotations=ToolAnnotations(readOnlyHint=True))
              for name in state.reads),
            MCPTool(name="write_item", inputSchema={"type": "object"}, annotations=ToolAnnotations(readOnlyHint=False)),
            MCPTool(name="unknown", inputSchema={"type": "object"}),
        ]

    @asynccontextmanager
    async def remote(*_args, **_kwargs):
        yield SimpleNamespace(list_tools=list_tools)

    monkeypatch.setattr(connection_attention_discovery, "remote_mcp_toolset", remote)

    async def gateway_discovery(_self):
        return GatewayDiscovery(
            mcp=GatewayCapability(status="ready", count=len(state.reads) + 2),
            skills=GatewayCapability(status="disabled"),
        )

    monkeypatch.setattr("moyai.gateway.GatewayClient.discover", gateway_discovery)
    return state


def native_input(kind, **policy):
    return {
        "kind": kind, "name": "Synthetic personal account", "credential": "synthetic-private-token",
        "read_only": False, "enabled": True, "shared": False,
        "policy": {
            "use_proactively": True, "require_approval": True,
            **({"mcp": {"url": "https://mcp.example.test/tools"}} if kind == "mcp" else
               {"gateway": {"url": "https://gateway.example.test", "skills": False, "mcp": True}}),
            **policy,
        },
    }


async def native_connect(ctx, kind, **policy):
    response = await ctx.client.post("/api/connections", json=native_input(kind, **policy))
    assert response.status_code == 200, response.text
    assert "synthetic-private-token" not in response.text
    return response.json()


async def ordinary_scope(ctx, identity):
    session = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="private"))
    turn = await ctx.product.database.admission.submit(ctx.product.actor, session.id, NewMessage(
        prompt="An ordinary explicitly approved action", request_key=uuid4().hex, connections=[identity],
    ))
    await ctx.product.database.claim_batch(20)
    return (await ctx.product.database.admission.prepare(turn.id)).scope


@pytest.mark.parametrize("provider", ["github", "linear", "notion", "gmail", "google_calendar"])
async def test_personal_oauth_opt_in_preserves_chat_policy_and_fences_on_opt_out(connected, provider):
    ctx = connected
    initial = await enrollment.connect(ctx, provider, proactive=False)
    async with ctx.product.database.sessions() as db:
        original = await db.get(Connection, initial["connection_id"])
        chat_policy = ConnectionPolicy.model_validate_json(original.policy)
        before = (original.read_only, chat_policy.allowed_tools, chat_policy.require_approval, original.credential)
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0
    assert initial["attention"]["eligible"] and initial["attention_status"] == "disabled"
    await enrollment.attention(ctx, provider, True)
    _, definition, binding, _ = await enrollment.saved(ctx)
    assert definition["connection_ids"] == [initial["connection_id"]] and binding.state == "active"
    _, prepared = await enrollment.admit(ctx)
    async with ctx.product.database.sessions() as db:
        current = await db.get(Connection, initial["connection_id"])
        policy = ConnectionPolicy.model_validate_json(current.policy)
        assert (current.read_only, policy.allowed_tools, policy.require_approval, current.credential) == before
        grants = await ctx.product.database.resolve_grants(db, ctx.product.user_id, [current.id], surface="web")
        assert grants == prepared.scope.grants  # No fabricated read-only replacement grant.
    assert attention_reads(grants[0]) and prepared.scope.attention
    assert not grants[0].read_only if provider in {"github", "linear", "notion"} else grants[0].read_only
    await enrollment.attention(ctx, provider, False)
    assert (await enrollment.saved(ctx))[2].state == "paused"
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)


@pytest.mark.parametrize("provider", ["github", "linear", "notion", "gmail", "google_calendar"])
@pytest.mark.parametrize("action", ["reconnect", "reenable"])
@pytest.mark.parametrize("empty", [False, True])
async def test_oauth_reconnect_and_reenable_never_expand_saved_read_selection(connected, provider, action, empty):
    ctx = connected
    account = await enrollment.connect(ctx, provider)
    async with ctx.product.database.sessions.begin() as db:
        row = await db.get(Connection, account["connection_id"])
        policy = ConnectionPolicy.model_validate_json(row.policy)
        selected = [] if empty else [min(declared_reads(provider, policy))]
        # Empty is a valid persisted reduction when the last selected read is
        # removed from a provider's catalog. Reconnect cannot restore that consent.
        policy.attention_tools = selected
        row.policy = policy.model_dump_json()
        ordinary = row.read_only, policy.allowed_tools, policy.require_approval
    if action == "reconnect":
        started = await ctx.client.post(f"/api/personal-connections/{provider}/oauth")
        assert started.status_code == 200, started.text
        state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
        completed = await ctx.client.get(
            f"/oauth/personal/{provider}/complete", params={"state": state, "code": "synthetic-consent"},
        )
        assert completed.status_code == 303 and "personal_connection=success" in completed.headers["location"]
    else:
        await enrollment.attention(ctx, provider, False)
        current = await enrollment.inventory(ctx, provider)
        resumed = await ctx.client.patch(f"/api/personal-connections/{provider}/attention", json={
            "revision": current["revision"], "use_proactively": True,
        })
        assert resumed.status_code == (422 if empty else 200), resumed.text
    async with ctx.product.database.sessions() as db:
        row = await db.get(Connection, account["connection_id"])
        policy = ConnectionPolicy.model_validate_json(row.policy)
        assert policy.attention_tools == selected
        assert (row.read_only, policy.allowed_tools, policy.require_approval) == ordinary
    status = (await enrollment.inventory(ctx, provider))["attention"]
    assert status["readable_tools"] == selected
    if empty:
        assert not status["eligible"] and status["reason"] == "read_tools_required"


@pytest.mark.parametrize("provider", ["github", "linear", "notion"])
@pytest.mark.parametrize("action", ["reconnect", "disconnect", "workspace_disable"])
async def test_new_oauth_provider_lifecycle_revokes_stale_occurrences(connected, provider, action):
    ctx = connected
    account = await enrollment.connect(ctx, provider)
    _, prepared = await enrollment.admit(ctx)
    if action == "reconnect":
        await enrollment.connect(ctx, provider)
        assert (await enrollment.saved(ctx))[2].state == "active"
    elif action == "disconnect":
        response = await ctx.client.delete(f"/api/personal-connections/{provider}")
        assert response.status_code == 200, response.text
        assert (await enrollment.saved(ctx))[2].state == "paused"
        async with ctx.product.database.sessions() as db:
            assert not (await db.get(OAuthCredential, account["connection_id"])).encrypted
    else:
        async with ctx.product.source.begin() as db:
            await db.execute(text("INSERT INTO moyai.connection_policies VALUES (:provider,0,0,'','')"), {"provider": provider})
        await connection_attention.provider_changed(ctx.runtime, provider)
        assert (await enrollment.saved(ctx))[2].state == "paused"
        assert (await enrollment.inventory(ctx, provider))["attention"]["reason"] == "provider_disabled"
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)


@pytest.mark.parametrize("kind", ["mcp", "litellm"])
async def test_same_save_discovers_reads_enrolls_and_keeps_write_policy(connected, remote_reads, kind):
    ctx = connected
    account = await native_connect(ctx, kind)
    assert account["attention"] == {
        "eligible": True, "reason": None, "use_proactively": True, "status": "pending",
        "readable_tools": ["read_item", "read_other"], "consent_revision": None,
    }
    assert not account["read_only"]
    assert account["policy"]["require_approval"] and account["policy"]["allowed_tools"] is None
    assert account["policy"]["attention_tools"] == ["read_item", "read_other"]
    _, definition, _, _ = await enrollment.saved(ctx)
    assert definition["connection_ids"] == [account["id"]]
    _, prepared = await enrollment.admit(ctx)
    normal = await ordinary_scope(ctx, account["id"])
    assert prepared.scope.grants == normal.grants
    assert not normal.attention and prepared.scope.attention


@pytest.mark.parametrize("kind", ["mcp", "litellm"])
async def test_dynamic_edit_retains_subset_and_disable_invalidates_old_scope(connected, remote_reads, kind):
    ctx = connected
    account = await native_connect(ctx, kind)
    selected = await ctx.client.patch(f"/api/connections/{account['id']}/attention", json={
        "revision": account["revision"], "use_proactively": True, "readable_tools": ["read_item"],
    })
    assert selected.status_code == 200, selected.text
    remote_reads.reads.append("new_read_not_consented")
    body = native_input(kind)
    body["credential"] = ""
    changed = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    assert changed.status_code == 200, changed.text
    assert changed.json()["policy"]["attention_tools"] == ["read_item"]
    _, prepared = await enrollment.admit(ctx)
    body["enabled"] = False
    calls = remote_reads.calls
    disabled = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    assert disabled.status_code == 200, disabled.text
    assert remote_reads.calls == calls
    assert disabled.json()["attention"]["reason"] == "disconnected"
    assert (await enrollment.saved(ctx))[2].state == "paused"
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)
    body["enabled"] = True
    resumed = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["policy"]["attention_tools"] == ["read_item"]
    assert (await enrollment.saved(ctx))[2].state == "active"


@pytest.mark.parametrize("kind", ["mcp", "litellm"])
@pytest.mark.parametrize("failure", ["no_reads", "unreachable", "write_selection"])
async def test_invalid_dynamic_discovery_never_saves_or_enrolls(connected, remote_reads, kind, failure):
    body = native_input(kind)
    if failure == "no_reads":
        remote_reads.reads = []
    elif failure == "unreachable":
        remote_reads.failure = True
    else:
        body["policy"]["attention_tools"] = ["write_item"]
    response = await connected.client.post("/api/connections", json=body)
    assert response.status_code == (502 if failure == "unreachable" else 422), response.text
    assert "synthetic-private-token" not in response.text
    async with connected.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Connection)) == 0
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0


async def test_dynamic_save_cannot_overwrite_edit_during_discovery(connected, remote_reads):
    ctx = connected
    account = await native_connect(ctx, "mcp")

    async def change():
        async with ctx.product.database.sessions.begin() as db:
            row = await db.get(Connection, account["id"])
            row.revision += 1
            row.enabled = False

    remote_reads.during = change
    body = native_input("mcp")
    body["credential"] = ""
    response = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    assert response.status_code == 409, response.text
    async with ctx.product.database.sessions() as db:
        row = await db.get(Connection, account["id"])
        assert not row.enabled and row.revision == account["revision"] + 1


async def test_existing_unopted_connection_stays_unopted_and_explicit_pause_survives(connected, remote_reads):
    ctx = connected
    account = await native_connect(ctx, "mcp", use_proactively=False)
    body = native_input("mcp")
    body["credential"] = ""
    del body["policy"]["use_proactively"]
    updated = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    assert updated.status_code == 200, updated.text
    assert not updated.json()["policy"]["use_proactively"] and remote_reads.calls == 0
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0
    body["policy"]["use_proactively"] = True
    assert (await ctx.client.put(f"/api/connections/{account['id']}", json=body)).status_code == 200
    row, _, _, _ = await enrollment.saved(ctx)
    await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(
        revision=row["revision"], request_key="explicit-pause",
    ))
    assert (await ctx.client.put(f"/api/connections/{account['id']}", json=body)).status_code == 200
    assert (await enrollment.saved(ctx))[2].state == "paused"


@pytest.mark.parametrize("kind", ["mcp", "litellm"])
async def test_existing_native_toggle_discovers_initial_reads_and_preserves_selection(connected, remote_reads, kind):
    ctx = connected
    account = await native_connect(ctx, kind, use_proactively=False)
    path = f"/api/connections/{account['id']}/attention"
    initial = await ctx.client.patch(path, json={"revision": account["revision"], "use_proactively": True})
    assert initial.status_code == 200, initial.text
    assert initial.json()["attention"]["readable_tools"] == ["read_item", "read_other"]
    disabled = await ctx.client.patch(path, json={
        "revision": initial.json()["revision"], "use_proactively": False,
    })
    assert disabled.status_code == 200, disabled.text
    remote_reads.reads = ["read_item", "new_read"]
    resumed = await ctx.client.patch(path, json={
        "revision": disabled.json()["revision"], "use_proactively": True,
    })
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["attention"]["readable_tools"] == ["read_item"]
    _, prepared = await enrollment.admit(ctx)
    assert attention_reads(prepared.scope.grants[0]) == {"read_item"}
    assert not prepared.scope.grants[0].read_only


@pytest.mark.parametrize("kind,read,write", [
    ("github", "list_issues", "create_issue"), ("linear", "issue", "comment"),
    ("notion", "page", "append"), ("mcp", "read_item", "write_item"),
    ("litellm", "read_item", "write_item"),
])
async def test_ordinary_approved_writes_survive_but_attention_cannot_dispatch_them(
    connected, remote_reads, monkeypatch, kind, read, write,
):
    ctx = connected
    account = (
        await enrollment.connect(ctx, kind) if kind in {"github", "linear", "notion"}
        else await native_connect(ctx, kind)
    )
    identity = account.get("connection_id") or account["id"]
    normal = await ordinary_scope(ctx, identity)
    _, observer = await enrollment.admit(ctx)
    calls = []

    def provider():
        tools = FunctionToolset(id="synthetic-provider")

        async def read_item():
            calls.append("read")
            return {"id": "item", "version": "1", "title": "Synthetic request"}

        async def write_item():
            calls.append("write")
            return {"saved": True}

        tools.add_function(read_item, name=read, metadata={"annotations": {"readOnlyHint": True}})
        tools.add_function(write_item, name=write, metadata={"annotations": {"readOnlyHint": False}})
        tools.add_function(write_item, name="unannotated")
        return tools

    # Substitute only the provider tool implementation. SDK filtering, deferred
    # catalog reconstruction, approval and real database authority remain active.
    if kind == "github":
        async def native(_self, **_kwargs):
            return provider()
        monkeypatch.setattr(PersonalOAuthToolset, "native", native)
    elif kind in {"linear", "notion"}:
        monkeypatch.setattr(ctx.runtime.product_connections, "toolset", lambda *_args: provider())
    elif kind == "mcp":
        monkeypatch.setattr("moyai.mcp_remote.remote_mcp_toolset", lambda *_args, **_kwargs: provider())
    else:
        monkeypatch.setattr("moyai.gateway_tools.gateway_toolset", lambda *_args: provider())
    monkeypatch.setattr(ctx.runtime.product_connections.connectors, "policy", lambda _provider: {
        "enabled": True, "read_only": False,
    })
    prefix = f"{kind}_{identity[:8]}_"
    normal_ctx = RunContext(deps=normal, model=TestModel(), usage=RunUsage())
    tools = await connection_tools(ctx.runtime.security).get_toolset().for_run(normal_ctx)
    async with tools:
        await tools.for_run_step(normal_ctx)
        definitions = await tools.get_tools(normal_ctx)
        write_definition = definitions[prefix + write]
        assert write_definition.tool_def.kind == "unapproved"
        with pytest.raises(ApprovalRequired):
            await tools.call_tool(prefix + write, {}, normal_ctx, write_definition)
        assert calls == []
        assert await tools.call_tool(
            prefix + write, {}, replace(normal_ctx, tool_call_approved=True), write_definition,
        ) == {"saved": True}
    restricted_ctx = RunContext(deps=observer.scope, model=TestModel(), usage=RunUsage(), tool_call_approved=True)
    restricted = await connection_tools(ctx.runtime.security, read_only=True).get_toolset().for_run(restricted_ctx)
    async with restricted:
        await restricted.for_run_step(restricted_ctx)
        definitions = await restricted.get_tools(restricted_ctx)
        assert set(definitions) == {prefix + read}
        await restricted.call_tool(prefix + read, {}, restricted_ctx, definitions[prefix + read])
        with pytest.raises((ToolFailed, PermissionError, KeyError)):
            await restricted.call_tool(prefix + write, {}, restricted_ctx, write_definition)
    with pytest.raises(PermissionError, match="Attention cannot write"):
        await ctx.runtime.product_connections.check(observer.scope, observer.scope.grants[0], write=True)
    assert calls == ["write", "read"]
    assert observer.scope.grants == normal.grants and not normal.grants[0].read_only


async def test_historical_policy_and_manual_attention_keep_their_read_scope(connected):
    ctx = connected
    policy = ConnectionPolicy.model_validate_json('{"allowed_tools":["read_item"],"require_approval":true}')
    assert "attention_tools" not in policy.model_dump_json()
    async with ctx.product.database.sessions.begin() as db:
        row = Connection(
            owner_id=ctx.product.user_id, kind="mcp", name="Existing manual attention",
            credential=ctx.runtime.security.seal("synthetic"), read_only=True, shared=False,
            policy=policy.model_dump_json(),
        )
        db.add(row)
        await db.flush()
        grants = await ctx.product.database.dots.grants(db, ctx.product.user_id, [row.id])
        assert attention_reads(grants[0]) == {"read_item"}
        assert not ConnectionPolicy.model_validate(grants[0].policy).use_proactively


@pytest.mark.parametrize("change", ["name", "disable", "remove_reads"])
async def test_manual_attention_edit_keeps_selection_and_fences_prior_execution(connected, change):
    ctx = connected
    body = native_input("mcp", use_proactively=False, allowed_tools=["read_item"])
    body["read_only"] = True
    created = await ctx.client.post("/api/connections", json=body)
    assert created.status_code == 200, created.text
    account = created.json()
    destination = await ctx.product.database.dots.open(ctx.product.actor)
    await enrollment.automation_fixtures.activate(
        ctx, kind="attention", attention_mode="discovery", visibility="private",
        session_id=destination, connection_ids=[account["id"]],
    )
    _, prepared = await enrollment.admit(ctx)
    body["credential"] = ""
    body["name"] = "Renamed manual account"
    if change == "disable":
        body["enabled"] = False
    elif change == "remove_reads":
        body["policy"]["allowed_tools"] = []
    response = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    assert response.status_code == 200, response.text
    _, definition, binding, _ = await enrollment.saved(ctx)
    assert definition["connection_ids"] == [account["id"]]
    assert binding.state == ("active" if change == "name" else "paused")
    assert not response.json()["policy"]["use_proactively"]
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)


@pytest.mark.parametrize("provider", ["github", "gitlab", "linear", "notion", "slack"])
async def test_shared_canonical_grants_have_explicit_exclusion_reason(connected, provider):
    from moyai.contracts import Grant

    ctx = connected
    grant = Grant(id=str(uuid4()), kind=provider, revision=1, credential="", policy={
        "product": {"provider": provider, "personal_owner": None, "access": "organization"},
    })
    async with ctx.product.database.sessions() as db:
        result = await connection_attention.view(
            ctx.service, ctx.product.database, db, ctx.product.user_id, grant, shared=True, enabled=True,
        )
    assert not result.eligible and result.reason == "shared_account"
    assert not attention_reads(grant)


async def personal_slack(ctx, *, epoch="first"):
    from app.connection_authority import new_credentials

    encrypted = ctx.product.adapter.legacy_security.encrypt(json.dumps(new_credentials({
        "access_token": f"synthetic-personal-slack-{epoch}", "user_id": "USYNTHETIC", "team_id": "TSYNTHETIC",
    })))
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.personal_slack_grants(owner_id,encrypted,revision) VALUES (:owner,:encrypted,:revision) "
            "ON CONFLICT(owner_id) DO UPDATE SET encrypted=excluded.encrypted,revision=excluded.revision"
        ), {"owner": ctx.product.user_id, "encrypted": encrypted, "revision": epoch})
    async with ctx.product.database.sessions() as db:
        grants = await ctx.product.database.resolve_grants(db, ctx.product.user_id, ["slack"])
    return next(grant for grant in grants if (grant.policy.get("product") or {}).get("personal_owner"))


async def test_personal_slack_exact_consent_reconnect_disconnect_and_no_workspace_fallback(connected):
    ctx = connected
    # Organization access exists, but may never replace a revoked personal grant.
    await enrollment.automation_fixtures.save_connection(ctx, "slack")
    original = await personal_slack(ctx)
    async with ctx.product.database.sessions() as db:
        before = await connection_attention.view(
            ctx.service, ctx.product.database, db, ctx.product.user_id, original, shared=False, enabled=True,
        )
        assert before.eligible and not before.use_proactively and before.status == "disabled"
        with pytest.raises(HTTPException, match="explicitly allowed"):
            await ctx.product.database.dots.grants(db, ctx.product.user_id, [original.id])
    response = await ctx.client.patch(f"/api/connections/{original.id}/attention", json={
        "revision": original.revision, "consent_revision": before.consent_revision, "use_proactively": True,
    })
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == original.revision
    assert response.json()["attention"]["readable_tools"] == ["channel", "search", "thread"]
    _, prepared = await enrollment.admit(ctx)
    assert prepared.scope.grants == [original]
    async with ctx.product.database.sessions() as db:
        assert await ctx.product.database.resolve_grants(db, ctx.product.user_id, [original.id]) == [original]
        assert await db.get(Connection, original.id) is None  # No synthetic native projection.
        assert connection_attention.slack_consent(await db.get(User, ctx.product.user_id)) == original.id
    replacement = await personal_slack(ctx, epoch="replacement")
    assert replacement.id != original.id
    await connection_attention.provider_changed(ctx.runtime, "slack", owner=ctx.product.user_id)
    assert (await enrollment.saved(ctx))[2].state == "paused"
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)
    # The worker terminalizes the denied occurrence before a later check can run.
    await ctx.product.database.fail(prepared.scope.turn_id, "Personal Slack changed.")
    async with ctx.product.database.sessions() as db:
        state = await connection_attention.view(
            ctx.service, ctx.product.database, db, ctx.product.user_id, replacement, shared=False, enabled=True,
        )
        assert state.eligible and not state.use_proactively
    renewed = await ctx.client.patch(f"/api/connections/{replacement.id}/attention", json={
        "revision": replacement.revision, "consent_revision": state.consent_revision, "use_proactively": True,
    })
    assert renewed.status_code == 200, renewed.text
    _, after = await enrollment.admit(ctx)
    async with ctx.product.source.begin() as db:
        await db.execute(text("DELETE FROM moyai.personal_slack_grants WHERE owner_id=:owner"), {
            "owner": ctx.product.user_id,
        })
    await connection_attention.provider_changed(ctx.runtime, "slack", owner=ctx.product.user_id)
    assert (await enrollment.saved(ctx))[1]["connection_ids"] == []
    assert (await enrollment.saved(ctx))[2].state == "paused"
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(after.scope)
    assert original.read_only and replacement.read_only
    assert attention_reads(original) == declared_reads("slack", ConnectionPolicy.model_validate(original.policy))


async def test_personal_slack_opt_out_revokes_execution_without_changing_canonical_authority(connected):
    ctx = connected
    grant = await personal_slack(ctx)
    path = f"/api/connections/{grant.id}/attention"
    original_enable = {"revision": grant.revision, "consent_revision": 0, "use_proactively": True}
    enabled = await ctx.client.patch(path, json=original_enable)
    assert enabled.status_code == 200
    assert enabled.json()["attention"]["consent_revision"] == 1
    _, prepared = await enrollment.admit(ctx)
    off = await ctx.client.patch(path, json={
        "revision": grant.revision, "consent_revision": 1, "use_proactively": False,
    })
    assert off.status_code == 200 and off.json()["attention"]["status"] == "disabled"
    assert off.json()["attention"]["consent_revision"] == 2
    replay = await ctx.client.patch(path, json=original_enable)
    assert replay.status_code == 409
    assert (await enrollment.saved(ctx))[2].state == "paused"
    async with ctx.product.database.sessions() as db:
        assert await ctx.product.database.resolve_grants(db, ctx.product.user_id, [grant.id]) == [grant]
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)


async def test_slack_consent_cas_is_rechecked_after_discovery(connected, monkeypatch):
    ctx = connected
    grant = await personal_slack(ctx)
    path = f"/api/connections/{grant.id}/attention"
    missing = await ctx.client.patch(path, json={"revision": grant.revision, "use_proactively": True})
    assert missing.status_code == 409
    discover = connection_attention_discovery.discover_reads

    async def opt_out_during_discovery(*args):
        off = await ctx.client.patch(path, json={
            "revision": grant.revision, "consent_revision": 0, "use_proactively": False,
        })
        assert off.status_code == 200
        return await discover(*args)

    monkeypatch.setattr(connection_attention_discovery, "discover_reads", opt_out_during_discovery)
    raced = await ctx.client.patch(path, json={
        "revision": grant.revision, "consent_revision": 0, "use_proactively": True,
    })
    assert raced.status_code == 409
    async with ctx.product.database.sessions() as db:
        user = await db.get(User, ctx.product.user_id)
        assert connection_attention.slack_consent(user) is None
        assert connection_attention.slack_consent_revision(user) == 1
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0


async def test_pending_slack_oauth_choice_cannot_undo_newer_opt_out(connected):
    from test_product_identity import request

    from moyai import personal_slack_attention

    ctx = connected
    grant = await personal_slack(ctx)
    consent_request = request(state={"product_identity_grant": ctx.grant})
    pending = await personal_slack_attention.choice(
        ctx.runtime, consent_request, ctx.product.user_id, "first", True, 0,
    )
    off = await ctx.client.patch(f"/api/connections/{grant.id}/attention", json={
        "revision": grant.revision, "consent_revision": 0, "use_proactively": False,
    })
    assert off.status_code == 200
    with pytest.raises(HTTPException, match="attention changed"):
        await personal_slack_attention.choice(
            ctx.runtime, consent_request, ctx.product.user_id, "first", True, 0,
        )
    await personal_slack(ctx, epoch="oauth-saved")
    with pytest.raises(HTTPException, match="attention changed"):
        await personal_slack_attention.changed(
            ctx.runtime, ctx.product.user_id, request=consent_request,
            revision="oauth-saved", use_proactively=pending.use_proactively,
            consent_revision=pending.consent_revision,
        )
    async with ctx.product.database.sessions() as db:
        user = await db.get(User, ctx.product.user_id)
        assert connection_attention.slack_consent(user) is None
        assert connection_attention.slack_consent_revision(user) == 1
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0


@pytest.mark.parametrize("kind", ["gmail", "google_calendar", "slack"])
async def test_readonly_providers_keep_chat_reads_and_attention_never_acquires_writes(connected, kind):
    """Use canonical tool schemas, not replacement provider tool definitions."""
    ctx = connected
    if kind == "slack":
        original = await personal_slack(ctx)
        enabled = await ctx.client.patch(f"/api/connections/{original.id}/attention", json={
            "revision": original.revision, "consent_revision": 0, "use_proactively": True,
        })
        assert enabled.status_code == 200, enabled.text
        identity = original.id
    else:
        account = await enrollment.connect(ctx, kind)
        identity = account["connection_id"]
    normal = await ordinary_scope(ctx, identity)
    _, observer = await enrollment.admit(ctx)
    assert normal.grants == observer.scope.grants and normal.grants[0].read_only
    for scope in (normal, observer.scope):
        context = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), tool_call_approved=True)
        tools = await connection_tools(ctx.runtime.security).get_toolset().for_run(context)
        async with tools:
            await tools.for_run_step(context)
            definitions = await tools.get_tools(context)
        assert definitions
        assert all(
            tool.tool_def.metadata["annotations"]["readOnlyHint"] is True for tool in definitions.values()
        )
        if scope.attention:
            prefix = f"{kind}_{identity[:8]}_"
            assert set(definitions) == {prefix + name for name in attention_reads(scope.grants[0])}
    with pytest.raises(PermissionError, match="Attention cannot write"):
        await ctx.runtime.product_connections.check(observer.scope, observer.scope.grants[0], write=True)


@pytest.mark.parametrize("selected", [False, True])
async def test_slack_oauth_choice_enrolls_exact_saved_grant_and_preserves_omitted_consent(connected, selected):
    from app.personal_slack import PersonalSlackStatus
    from test_product_identity import request

    from moyai import personal_slack_attention

    ctx = connected
    original = await personal_slack(ctx)
    consent_request = request(state={"product_identity_grant": ctx.grant})
    omitted = await personal_slack_attention.choice(
        ctx.runtime, consent_request, ctx.product.user_id, "first", None,
    )
    assert not omitted.use_proactively and omitted.consent_revision == 0
    explicit = await personal_slack_attention.choice(
        ctx.runtime, consent_request, ctx.product.user_id, "first", selected, 0,
    )
    assert explicit.use_proactively is selected and explicit.consent_revision == 0
    # Represents the successful canonical OAuth save, with a new authority and its receipt.
    saved = await personal_slack(ctx, epoch="oauth-saved")
    await personal_slack_attention.changed(
        ctx.runtime, ctx.product.user_id, request=consent_request,
        revision="oauth-saved", use_proactively=selected, consent_revision=explicit.consent_revision,
    )
    result = await personal_slack_attention.status(ctx.runtime, ctx.product.user_id, PersonalSlackStatus(
        available=True, connected=True, oauth_configured=True, label="Synthetic",
        health="healthy", effective_source="personal", limitation="",
    ))
    assert result.connection_id == saved.id and result.revision == saved.revision
    assert result.use_proactively is selected and result.attention.use_proactively is selected
    assert result.attention.status == ("pending" if selected else "disabled")
    retained = await personal_slack_attention.choice(
        ctx.runtime, consent_request, ctx.product.user_id, "oauth-saved", None,
    )
    assert retained.use_proactively is selected and retained.consent_revision == 1
    assert original.id != saved.id
    async with ctx.product.database.sessions() as db:
        assert await ctx.product.database.resolve_grants(db, ctx.product.user_id, [saved.id]) == [saved]
        assert await db.scalar(select(func.count()).select_from(Binding)) == int(selected)
    if selected:
        _, prepared = await enrollment.admit(ctx)
        assert prepared.scope.grants == [saved]


@pytest.mark.parametrize("boundary", ["changed_grant", "revoked_authority"])
async def test_slack_oauth_callback_cannot_enroll_a_different_grant_or_revoked_owner(connected, boundary):
    from test_product_identity import request

    from moyai import personal_slack_attention

    ctx = connected
    await personal_slack(ctx, epoch="oauth-saved")
    consent_request = request(state={"product_identity_grant": ctx.grant})
    if boundary == "changed_grant":
        await personal_slack(ctx, epoch="other-callback")
    else:
        async with ctx.product.database.sessions.begin() as db:
            (await db.get(User, ctx.product.user_id)).auth_version += 1
    with pytest.raises(HTTPException):
        await personal_slack_attention.changed(
            ctx.runtime, ctx.product.user_id, request=consent_request,
            revision="oauth-saved", use_proactively=True, consent_revision=0,
        )
    async with ctx.product.database.sessions() as db:
        assert connection_attention.slack_consent(await db.get(User, ctx.product.user_id)) is None
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0


@pytest.mark.parametrize("operation", ["create", "edit", "toggle"])
async def test_native_write_rechecks_authority_in_commit_transaction(connected, remote_reads, monkeypatch, operation):
    ctx = connected
    account = await native_connect(ctx, "mcp", use_proactively=False) if operation != "create" else None
    original = connection_attention.authorize_mutation
    revoked = False

    async def revoke_before_commit(runtime, db, request, user):
        nonlocal revoked
        if not revoked:
            revoked = True
            async with runtime.database.sessions.begin() as other:
                (await other.get(User, user.id)).auth_version += 1
        await original(runtime, db, request, user)

    monkeypatch.setattr(connection_attention, "authorize_mutation", revoke_before_commit)
    monkeypatch.setattr("moyai.connection_attention_api.authorize_mutation", revoke_before_commit)
    body = native_input("mcp")
    if operation == "create":
        response = await ctx.client.post("/api/connections", json=body)
    elif operation == "edit":
        body["credential"] = ""
        response = await ctx.client.put(f"/api/connections/{account['id']}", json=body)
    else:
        response = await ctx.client.patch(f"/api/connections/{account['id']}/attention", json={
            "revision": account["revision"], "use_proactively": True,
        })
    assert response.status_code in {401, 403}, response.text
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0
        if account is not None:
            saved = await db.get(Connection, account["id"])
            assert saved.revision == account["revision"]
            assert not ConnectionPolicy.model_validate_json(saved.policy).use_proactively
        else:
            assert await db.scalar(select(func.count()).select_from(Connection)) == 0
