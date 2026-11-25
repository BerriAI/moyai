"""Real disposable Postgres enrollment boundaries; provider consent is substituted."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
import pytest_asyncio
import test_product_automations as automation_fixtures
from app.connector_errors import ConnectorError
from fastapi import FastAPI, HTTPException, Request
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import func, select, text
from temporalio.testing import WorkflowEnvironment
from test_product_identity import google_cookie

from moyai import google_attention, personal_oauth
from moyai.automations.product_automation_context_models import (
    ProductAutomationExecutionContext as ExecutionContext,
)
from moyai.automations.product_automation_models import ProductAutomationBinding as Binding
from moyai.automations.product_automations import Mutation, Save, native_schedule_id
from moyai.connection_policy import ConnectionPolicy
from moyai.dot_discovery_agent import DiscoveryBounds
from moyai.dot_models import DotAttentionTurn, DotNotification, PersonalDot
from moyai.dot_sources import GITHUB_MCP
from moyai.mcp_remote import RemoteMCPConfig
from moyai.models import Connection, Session, User
from moyai.oauth_models import OAuthAuthorization, OAuthCredential
from moyai.runtime import Runtime

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
admitted_product = automation_fixtures.admitted_product
automations = automation_fixtures.automations
canonical_connections = automation_fixtures.canonical_connections


@pytest_asyncio.fixture
async def google(automations, canonical_connections, monkeypatch):
    """Use the canonical product identity, source tables and native PG services."""
    ctx = automations
    product = ctx.product
    runtime = SimpleNamespace(
        settings=product.settings.model_copy(update={"public_url": product.adapter.settings.public_url}),
        database=product.database, security=product.database.security,
        product_identity=product.adapter, product_connections=canonical_connections,
        product_automations=ctx.service,
    )

    async def actor(request: Request):
        principal = await product.adapter.authenticate(
            request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"},
        )
        async with product.database.sessions.begin() as db:
            user = await product.adapter.project_native_user(db, principal)
        request.state.product_principal = principal
        return user

    async def client_identity(service, provider):
        return "synthetic-google-client"

    async def authorization_url(service, provider, state, verifier):
        return "https://accounts.example.test/authorize?" + urlencode({"state": state})

    async def exchange(service, provider, identity, **kwargs):
        return {
            "access_token": "synthetic-only", "refresh_token": "synthetic-refresh-only",
            "subject": "synthetic-google-account", "client_id": identity,
            "label": "owner@example.test",
        }

    # Only provider I/O is replaced. Identity, consent state, transactions,
    # enrollment, context capture, schedule reconciliation and fencing are real.
    monkeypatch.setattr(personal_oauth, "client_identity", client_identity)
    monkeypatch.setattr(personal_oauth, "authorization_url", authorization_url)
    monkeypatch.setattr(personal_oauth, "exchange", exchange)
    app = FastAPI()
    app.include_router(personal_oauth.routes(cast(Runtime, runtime), actor))
    cookie, csrf = google_cookie(product.adapter, sub=product.subject)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=runtime.settings.public_url,
        cookies={"workspace_session": cookie},
        headers={"X-Moyai-CSRF": csrf, "X-CSRF-Token": csrf, "Origin": runtime.settings.public_url},
    ) as client:
        ctx.client, ctx.runtime = client, runtime
        yield ctx


async def inventory(ctx, provider):
    response = await ctx.client.get("/api/personal-connections")
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["provider"] == provider)


async def connect(ctx, provider, *, proactive=True):
    started = await ctx.client.post(
        f"/api/personal-connections/{provider}/oauth", json={"use_proactively": proactive},
    )
    assert started.status_code == 200, started.text
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
    completed = await ctx.client.get(
        f"/oauth/personal/{provider}/complete", params={"state": state, "code": "synthetic-consent"},
    )
    assert completed.status_code == 303, completed.text
    assert "personal_connection=success" in completed.headers["location"]
    return await inventory(ctx, provider)


async def attention(ctx, provider, enabled):
    account = await inventory(ctx, provider)
    response = await ctx.client.patch(f"/api/personal-connections/{provider}/attention", json={
        "revision": account["revision"], "use_proactively": enabled,
    })
    assert response.status_code == 200, response.text
    return response.json()


async def saved(ctx):
    async with ctx.product.database.sessions() as db:
        row = (await db.execute(text("SELECT * FROM moyai.automations"))).mappings().one()
        binding = await db.get(Binding, row["id"])
        assert binding is not None
        context = await db.get(ExecutionContext, (row["id"], binding.generation))
        assert context is not None
        return dict(row), json.loads(row["definition"]), binding, context


async def admit(ctx):
    row, definition, binding, _ = await saved(ctx)
    await ctx.service.reconcile(row["id"])
    trigger = definition["triggers"][0]["id"]
    stamp = datetime.now(UTC).isoformat()
    receipt = await ctx.service.admit(
        row["id"], binding.generation, row["revision"], trigger,
        f"native:{row['id']}:{trigger}:{stamp}", stamp,
    )
    assert receipt["status"] == "admitted", receipt
    await ctx.product.database.claim_batch(20)
    prepared = await ctx.product.database.admission.prepare(receipt["turn_id"])
    return receipt, prepared


async def test_connect_creates_one_private_hourly_attention_and_second_account_reuses_it(google):
    ctx = google
    gmail = await connect(ctx, "gmail")
    first, definition, binding, context = await saved(ctx)
    assert gmail["connected"] and gmail["use_proactively"] and gmail["attention_status"] == "pending"
    assert binding.state == "active" and binding.sync_state == "pending"
    assert definition["kind"] == "attention" and definition["attention_mode"] == "discovery"
    assert definition["connection_ids"] == [gmail["connection_id"]]
    assert definition["visibility"] == "private" and definition["environment_id"] == "none"
    assert len(definition["triggers"]) == 1
    assert definition["triggers"][0]["schedule"]["frequency"] == "hourly"
    assert json.loads(context.connection_ids) == [gmail["connection_id"]]
    async with ctx.product.database.sessions() as db:
        dot = await db.get(PersonalDot, ctx.product.user_id)
        destination = await db.get(Session, dot.session_id)
        assert definition["session_id"] == dot.session_id
        assert destination.visibility == "private" and destination.owner_id == ctx.product.user_id
        account = await db.get(Connection, gmail["connection_id"])
        assert account.enabled and account.read_only and not account.shared
    await ctx.service.reconcile(first["id"])
    calendar = await connect(ctx, "google_calendar")
    second, changed, binding, context = await saved(ctx)
    assert second["id"] == first["id"] and second["revision"] > first["revision"]
    assert binding.state == "active"
    assert changed["triggers"] == definition["triggers"]
    expected = sorted([gmail["connection_id"], calendar["connection_id"]])
    assert changed["connection_ids"] == json.loads(context.connection_ids) == expected
    await ctx.service.reconcile(second["id"])
    assert len(ctx.remote.schedules) == 1
    assert list(ctx.remote.schedules) == [native_schedule_id(first["id"], definition["triggers"][0]["id"])]
    assert sum(action == "create" for action, _ in ctx.remote.calls) == 1
    # The canonical legacy row stays paused; only its existing native scheduler runs.
    assert second["paused"] == 1
    assert (await inventory(ctx, "gmail"))["attention_status"] == "active"


async def test_opt_out_removes_account_scope_and_last_opt_out_pauses(google):
    ctx = google
    await connect(ctx, "gmail")
    calendar = await connect(ctx, "google_calendar")
    before, _, _, _ = await saved(ctx)
    await attention(ctx, "gmail", False)
    row, definition, binding, context = await saved(ctx)
    assert row["id"] == before["id"] and binding.state == "active"
    assert definition["connection_ids"] == json.loads(context.connection_ids) == [calendar["connection_id"]]
    gmail = await inventory(ctx, "gmail")
    assert gmail["connected"] and not gmail["use_proactively"] and gmail["attention_status"] == "disabled"
    await attention(ctx, "google_calendar", False)
    row, definition, binding, _ = await saved(ctx)
    assert definition["connection_ids"] == [] and binding.state == "paused"
    await ctx.service.reconcile(row["id"])
    assert all(schedule.state.paused for schedule in ctx.remote.schedules.values())
    assert (await inventory(ctx, "google_calendar"))["attention_status"] == "disabled"


async def test_consent_opt_out_creates_no_automation_until_owner_enables_attention(google):
    ctx = google
    account = await connect(ctx, "gmail", proactive=False)
    assert account["connected"] and not account["use_proactively"]
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 0
        assert await db.scalar(select(func.count()).select_from(Binding)) == 0
    assert not ctx.remote.calls
    await attention(ctx, "gmail", True)
    _, definition, binding, _ = await saved(ctx)
    assert binding.state == "active" and definition["connection_ids"] == [account["connection_id"]]


async def test_explicit_pause_survives_adding_another_account(google):
    ctx = google
    await connect(ctx, "gmail")
    row, _, _, _ = await saved(ctx)
    await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(
        revision=row["revision"], request_key="owner-pauses-attention",
    ))
    await connect(ctx, "google_calendar")
    row, definition, binding, _ = await saved(ctx)
    assert len(definition["connection_ids"]) == 2
    assert binding.state == "paused"
    assert (await inventory(ctx, "google_calendar"))["attention_status"] == "paused"
    await ctx.service.reconcile(row["id"])
    assert all(schedule.state.paused for schedule in ctx.remote.schedules.values())


@pytest.mark.parametrize("change", ["disconnect", "opt_out"])
async def test_account_removal_does_not_recreate_retired_attention(google, change):
    ctx = google
    await connect(ctx, "gmail")
    calendar = await connect(ctx, "google_calendar")
    original, _, _, _ = await saved(ctx)
    await ctx.service.reconcile(original["id"])
    await ctx.service.mutate(ctx.grant, "retire", original["id"], Mutation(
        revision=original["revision"], request_key="owner-retires-google-attention",
    ))
    await ctx.service.reconcile(original["id"])
    assert not ctx.remote.schedules

    if change == "disconnect":
        response = await ctx.client.delete("/api/personal-connections/gmail")
        await assert_disconnected(ctx, "gmail", response)
    else:
        await attention(ctx, "gmail", False)
        assert not (await inventory(ctx, "gmail"))["use_proactively"]

    async with ctx.product.database.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automations")) == 1
        assert await db.scalar(select(func.count()).select_from(Binding)) == 1
        retired = await db.get(Binding, original["id"])
        assert retired is not None and retired.state == "deleted"
        remaining = await db.get(Connection, calendar["connection_id"])
        assert remaining.enabled and ConnectionPolicy.model_validate_json(remaining.policy).use_proactively
    await ctx.service.reconcile(original["id"])
    assert not ctx.remote.schedules
    assert (await inventory(ctx, "google_calendar"))["attention_status"] == "paused"


@pytest.mark.parametrize("pause_before_opt_out", [True, False])
async def test_explicit_pause_survives_emptying_and_reenabling_account_scope(google, pause_before_opt_out):
    ctx = google
    await connect(ctx, "gmail")
    if pause_before_opt_out:
        row, _, _, _ = await saved(ctx)
        await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(
            revision=row["revision"], request_key="owner-pauses-before-opt-out",
        ))
    await attention(ctx, "gmail", False)
    assert (await saved(ctx))[1]["connection_ids"] == []
    if not pause_before_opt_out:
        row, _, _, _ = await saved(ctx)
        await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(
            revision=row["revision"], request_key="owner-pauses-after-opt-out",
        ))
    await attention(ctx, "gmail", True)
    assert (await saved(ctx))[2].state == "paused"


async def test_automatic_empty_scope_pause_resumes_when_account_opts_in(google):
    ctx = google
    account = await connect(ctx, "gmail")
    await attention(ctx, "gmail", False)
    assert (await saved(ctx))[2].state == "paused"
    await attention(ctx, "gmail", True)
    _, definition, binding, context = await saved(ctx)
    assert binding.state == "active"
    assert definition["connection_ids"] == json.loads(context.connection_ids) == [account["connection_id"]]


async def test_stale_attention_edit_cannot_overwrite_newer_owner_choice(google):
    ctx = google
    old = await connect(ctx, "gmail")
    await attention(ctx, "gmail", False)
    stale = await ctx.client.patch("/api/personal-connections/gmail/attention", json={
        "revision": old["revision"], "use_proactively": True,
    })
    assert stale.status_code == 409
    assert not (await inventory(ctx, "gmail"))["use_proactively"]
    assert (await saved(ctx))[2].state == "paused"


async def test_attention_status_requires_account_membership_in_active_definition(google):
    ctx = google
    await connect(ctx, "gmail")
    calendar = await connect(ctx, "google_calendar")
    row, definition, _, _ = await saved(ctx)
    definition["connection_ids"] = [calendar["connection_id"]]
    changed = await ctx.service.mutate(ctx.grant, "update", row["id"], Save(
        revision=row["revision"], request_key="owner-selects-calendar-only",
        definition=automation_fixtures.definition(**definition),
    ))
    await ctx.service.mutate(ctx.grant, "enable", row["id"], Mutation(
        revision=changed["revision"], request_key="owner-enables-calendar-only",
    ))
    await ctx.service.reconcile(row["id"])
    gmail = await inventory(ctx, "gmail")
    assert gmail["connected"] and gmail["use_proactively"]
    assert gmail["attention_status"] == "paused"
    assert (await inventory(ctx, "google_calendar"))["attention_status"] == "active"


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
async def test_unsuccessful_reconnect_preserves_live_attention_authority(
    google, monkeypatch, provider, outcome,
):
    ctx = google
    account = await connect(ctx, provider)
    initial, _, _, _ = await saved(ctx)
    await ctx.service.reconcile(initial["id"])
    original, definition, binding, context = await saved(ctx)
    original_grants = await ctx.runtime.product_connections.resolve_grants(
        ctx.product.user_id, [account["connection_id"]], surface="automation",
    )
    async with ctx.product.database.sessions() as db:
        credential = await db.get(OAuthCredential, account["connection_id"])
        encrypted, credential_revision = credential.encrypted, credential.revision

    async def failed_exchange(_service, _provider, _identity, **_kwargs):
        assert outcome == "failed", "Cancelled consent must not redeem the code."
        raise ConnectorError("Synthetic token exchange rejection.", code="invalid_grant")

    monkeypatch.setattr(personal_oauth, "exchange", failed_exchange)
    started = await ctx.client.post(
        f"/api/personal-connections/{provider}/oauth", json={"use_proactively": True},
    )
    assert started.status_code == 200, started.text
    assert await ctx.runtime.product_connections.resolve_grants(
        ctx.product.user_id, [account["connection_id"]], surface="automation",
    ) == original_grants
    state = parse_qs(urlsplit(started.json()["url"]).query)["state"][0]
    completed = await ctx.client.get(f"/oauth/personal/{provider}/complete", params={
        "state": state, "code": "synthetic-consent",
        **({"error": "access_denied"} if outcome == "cancelled" else {}),
    })
    assert completed.status_code == 303, completed.text
    assert f"personal_connection={outcome}" in completed.headers["location"]

    async with ctx.product.database.sessions() as db:
        current = await db.get(Connection, account["connection_id"])
        credential = await db.get(OAuthCredential, account["connection_id"])
        assert current.enabled and current.revision == account["revision"]
        assert credential.encrypted == encrypted and credential.revision == credential_revision
        assert await db.scalar(select(OAuthAuthorization)) is None
    current, current_definition, current_binding, current_context = await saved(ctx)
    assert current["revision"] == original["revision"] and current_definition == definition
    assert current_binding.generation == binding.generation and current_binding.state == "active"
    assert current_context.fingerprint == context.fingerprint
    assert (await inventory(ctx, provider))["attention_status"] == "active"
    _, prepared = await admit(ctx)
    assert prepared.scope.grants == original_grants
    await ctx.product.database.authorize_scope(prepared.scope)


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("owner_action", [None, "pause", "retire"], ids=["active", "owner-paused", "retired"])
async def test_reconnect_after_dot_deletion_retargets_live_attention_and_preserves_owner_state(
    google, provider, owner_action,
):
    ctx = google
    account = await connect(ctx, provider)
    original, _, _, _ = await saved(ctx)
    await ctx.service.reconcile(original["id"])
    if owner_action is not None:
        await ctx.service.mutate(ctx.grant, owner_action, original["id"], Mutation(
            revision=original["revision"], request_key=f"owner-{owner_action}-before-dot-deletion",
        ))
        await ctx.service.reconcile(original["id"])
    original, definition, original_binding, _ = await saved(ctx)
    await ctx.product.database.delete_session(ctx.product.actor, definition["session_id"])

    reconnected = await connect(ctx, provider)
    assert reconnected["connected"] and reconnected["revision"] > account["revision"]
    row, changed, binding, _ = await saved(ctx)
    assert row["id"] == original["id"]
    async with ctx.product.database.sessions() as db:
        dot = await db.get(PersonalDot, ctx.product.user_id)
        assert dot is not None and dot.session_id != definition["session_id"]
        destination = await ctx.product.database.dots.own_session(db, ctx.product.user_id, dot.session_id)
        assert (await db.get(Session, definition["session_id"])).deleted
        assert await db.scalar(select(func.count()).select_from(Binding)) == 1
    expected_state = {None: "active", "pause": "paused", "retire": "deleted"}[owner_action]
    assert binding.state == expected_state
    if owner_action == "retire":
        assert changed == definition
        assert row["revision"] == original["revision"]
        assert binding.generation == original_binding.generation
    else:
        assert changed["session_id"] == destination.id
        assert changed["connection_ids"] == [account["connection_id"]]
        assert row["revision"] > original["revision"]
        assert binding.generation > original_binding.generation
    await ctx.service.reconcile(row["id"])
    if owner_action == "retire":
        assert not ctx.remote.schedules
    else:
        schedule_id = native_schedule_id(row["id"], changed["triggers"][0]["id"])
        assert list(ctx.remote.schedules) == [schedule_id]
        assert ctx.remote.schedules[schedule_id].state.paused == (owner_action == "pause")
    assert sum(action == "create" for action, _ in ctx.remote.calls) == 1
    if owner_action is None:
        receipt, prepared = await admit(ctx)
        await ctx.product.database.authorize_scope(prepared.scope)
        async with ctx.product.database.sessions() as db:
            attention_turn = await db.get(DotAttentionTurn, receipt["turn_id"])
            assert attention_turn is not None and attention_turn.destination_session_id == destination.id


async def test_disconnect_fences_old_turns_and_keeps_remaining_account_active(google):
    ctx = google
    gmail = await connect(ctx, "gmail")
    calendar = await connect(ctx, "google_calendar")
    original, _, binding, _ = await saved(ctx)
    receipt, prepared = await admit(ctx)
    assert {grant.id for grant in prepared.scope.grants} == {gmail["connection_id"], calendar["connection_id"]}
    assert prepared.scope.discovery and prepared.scope.attention
    assert (await ctx.client.delete("/api/personal-connections/gmail")).status_code == 200
    _, definition, current_binding, context = await saved(ctx)
    assert current_binding.generation > binding.generation
    assert current_binding.state == "active"
    assert definition["connection_ids"] == json.loads(context.connection_ids) == [calendar["connection_id"]]
    with pytest.raises((PermissionError, HTTPException)):
        await ctx.product.database.authorize_scope(prepared.scope)
    assert not await ctx.product.database.save_result(
        prepared.scope, output='{"items":[],"notify_observation":null,"message":""}',
        conversation='{"messages":[]}', workspace=None, usage={},
    )
    async with ctx.product.database.sessions() as db:
        assert not (await db.get(Connection, gmail["connection_id"])).enabled
        assert (await db.get(OAuthCredential, gmail["connection_id"])).encrypted == ""
        assert await db.scalar(select(func.count()).select_from(DotNotification)) == 0
    stamp = datetime.now(UTC).isoformat()
    stale = await ctx.service.admit(
        original["id"], binding.generation, original["revision"], "attention",
        f"native:{original['id']}:attention:{stamp}", stamp,
    )
    assert stale["status"] == "skipped"
    assert receipt["turn_id"]
    assert (await ctx.client.delete("/api/personal-connections/google_calendar")).status_code == 200
    assert (await saved(ctx))[2].state == "paused"


async def restricted_github_attention(ctx):
    destination = await ctx.product.database.dots.open(ctx.product.actor)
    async with ctx.product.database.sessions.begin() as db:
        account = Connection(
            owner_id=ctx.product.user_id, kind="mcp", name="Reviewed GitHub",
            credential=ctx.product.database.security.seal("synthetic-github"),
            read_only=True, shared=False, policy=ConnectionPolicy(
                allowed_tools=["list_pull_requests"], mcp=RemoteMCPConfig(url=GITHUB_MCP),
            ).model_dump_json(),
        )
        db.add(account)
        await db.flush()
    sources = [{"connection_id": account.id, "source": "https://github.com/BerriAI/moyai"}]
    original, _ = await automation_fixtures.activate(
        ctx, kind="attention", attention_mode="discovery", visibility="private",
        session_id=destination, connection_ids=[account.id], attention_sources=sources,
    )
    return account, sources, original


async def assert_disconnected(ctx, provider, response):
    """Inspect durable revocation even when the endpoint reports an error."""
    identity = personal_oauth.connection_id(ctx.product.user_id, provider)
    async with ctx.product.database.sessions() as db:
        account = await db.get(Connection, identity)
        credential = await db.get(OAuthCredential, identity)
        assert not account.enabled, (
            f"Disconnect returned HTTP {response.status_code} and left {provider} enabled: {response.text}"
        )
        assert not credential.encrypted, "Disconnect retained the OAuth credential."
        assert await db.scalar(select(func.count()).select_from(OAuthAuthorization).where(
            OAuthAuthorization.owner_id == ctx.product.user_id,
            OAuthAuthorization.provider == provider,
        )) == 0
    assert response.status_code == 200, response.text


async def test_disconnect_commits_revocation_and_pauses_full_manual_discovery(google):
    ctx = google
    await connect(ctx, "gmail")
    calendar = await connect(ctx, "google_calendar")
    original, definition, _, _ = await saved(ctx)
    async with ctx.product.database.sessions.begin() as db:
        manual_accounts = [
            Connection(
                owner_id=ctx.product.user_id, kind="mcp", name=f"Manual account {index}",
                credential=ctx.product.database.security.seal(f"synthetic-manual-{index}"),
                read_only=True, shared=False, policy=ConnectionPolicy(
                    allowed_tools=["list_pull_requests"], mcp=RemoteMCPConfig(url=GITHUB_MCP),
                ).model_dump_json(),
            )
            for index in range(4)
        ]
        db.add_all(manual_accounts)
        await db.flush()
        selected = sorted(account.id for account in manual_accounts)
    changed = await ctx.service.mutate(ctx.grant, "update", original["id"], Save(
        revision=original["revision"], request_key="owner-selects-four-manual-accounts",
        definition=automation_fixtures.definition(**{**definition, "connection_ids": selected}),
    ))
    await ctx.service.mutate(ctx.grant, "enable", original["id"], Mutation(
        revision=changed["revision"], request_key="owner-enables-four-manual-accounts",
    ))
    await ctx.service.reconcile(original["id"])
    _, _, active_binding, _ = await saved(ctx)
    assert active_binding.state == "active"
    assert (await inventory(ctx, "gmail"))["use_proactively"]
    assert (await inventory(ctx, "google_calendar"))["use_proactively"]
    pending = await ctx.client.post("/api/personal-connections/gmail/oauth", json={"use_proactively": True})
    assert pending.status_code == 200, pending.text

    response = await ctx.client.delete("/api/personal-connections/gmail")
    await assert_disconnected(ctx, "gmail", response)
    row, definition, binding, _ = await saved(ctx)
    assert row["id"] == original["id"]
    assert definition["connection_ids"] == selected
    assert binding.state == "paused" and binding.generation > active_binding.generation
    async with ctx.product.database.sessions() as db:
        remaining = await db.get(Connection, calendar["connection_id"])
        assert remaining.enabled and ConnectionPolicy.model_validate_json(remaining.policy).use_proactively
    await ctx.service.reconcile(row["id"])
    assert len(ctx.remote.schedules) == 1
    assert all(schedule.state.paused for schedule in ctx.remote.schedules.values())


@pytest.mark.parametrize("destination_state", ["archived", "deleted"])
@pytest.mark.parametrize("owner_paused", [False, True])
async def test_disconnect_revokes_even_when_dot_destination_is_unavailable(
    google, destination_state, owner_paused,
):
    ctx = google
    account = await connect(ctx, "gmail")
    remaining = await connect(ctx, "google_calendar")
    original, definition, _, _ = await saved(ctx)
    if owner_paused:
        await ctx.service.mutate(ctx.grant, "pause", original["id"], Mutation(
            revision=original["revision"], request_key="owner-pauses-before-disconnect",
        ))
    # A pending reconnect must also be cancelled by the disconnect.
    pending = await ctx.client.post("/api/personal-connections/gmail/oauth", json={"use_proactively": True})
    assert pending.status_code == 200, pending.text
    if destination_state == "deleted":
        await ctx.product.database.delete_session(ctx.product.actor, definition["session_id"])
    else:
        async with ctx.product.database.sessions.begin() as db:
            destination = await db.get(Session, definition["session_id"])
            destination.archived = True
            destination.revision += 1
    response = await ctx.client.delete("/api/personal-connections/gmail")
    await assert_disconnected(ctx, "gmail", response)
    _, changed, binding, _ = await saved(ctx)
    assert binding.state == "paused"
    assert changed["connection_ids"] == [remaining["connection_id"]]
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Connection, account["connection_id"])).revision > account["revision"]
        assert (await db.get(Connection, remaining["connection_id"])).enabled
        dot = await db.get(PersonalDot, ctx.product.user_id)
        assert dot.session_id == definition["session_id"]
        destination = await db.get(Session, dot.session_id)
        assert getattr(destination, destination_state), "Disconnect must not reopen the unavailable Dot."


@pytest.mark.parametrize("other_account", ["github", "google_calendar", "calendar_read_policy"])
@pytest.mark.parametrize("owner_paused", [False, True])
async def test_disconnect_revokes_even_when_another_selected_account_is_revoked(
    google, other_account, owner_paused,
):
    ctx = google
    if other_account == "github":
        other, _, _ = await restricted_github_attention(ctx)
        other_id = other.id
    else:
        other_id = (await connect(ctx, "google_calendar"))["connection_id"]
    account = await connect(ctx, "gmail")
    original, _, _, _ = await saved(ctx)
    if owner_paused:
        await ctx.service.mutate(ctx.grant, "pause", original["id"], Mutation(
            revision=original["revision"], request_key="owner-pauses-before-other-account-revoked",
        ))
    async with ctx.product.database.sessions.begin() as db:
        other = await db.get(Connection, other_id)
        if other_account == "calendar_read_policy":
            policy = ConnectionPolicy.model_validate_json(other.policy)
            policy.allowed_tools = []
            other.policy = policy.model_dump_json()
        else:
            other.enabled = False
            if other_account == "google_calendar":
                credential = await db.get(OAuthCredential, other_id)
                credential.encrypted = ""
                credential.revision += 1
        other.revision += 1
    response = await ctx.client.delete("/api/personal-connections/gmail")
    await assert_disconnected(ctx, "gmail", response)
    _, changed, binding, _ = await saved(ctx)
    assert binding.state == "paused"
    assert account["connection_id"] not in changed["connection_ids"]
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Connection, account["connection_id"])).revision > account["revision"]
        other = await db.get(Connection, other_id)
        if other_account == "calendar_read_policy":
            assert not ConnectionPolicy.model_validate_json(other.policy).allowed_tools
        else:
            assert not other.enabled, "Disconnect must not restore another account's authority."


async def test_google_enrollment_preserves_restricted_github_repository_scope(google):
    ctx = google
    account, sources, original = await restricted_github_attention(ctx)
    google_account = await connect(ctx, "gmail")
    row, definition, binding, context = await saved(ctx)
    assert row["id"] == original["id"] and binding.state == "active"
    assert definition["attention_sources"] == sources
    assert set(json.loads(context.connection_ids)) == {account.id, google_account["connection_id"]}
    _, prepared = await admit(ctx)
    assert prepared.scope.discovery_repositories == {account.id: [("berriai", "moyai")]}
    bounds = DiscoveryBounds()
    call = ToolCallPart(f"mcp_{account.id[:8]}_list_pull_requests", {}, "read")
    with pytest.raises(ModelRetry, match="outside"):
        await bounds.before_tool_execute(
            RunContext(deps=prepared.scope, model=TestModel(), usage=RunUsage()),
            call=call, tool_def=ToolDefinition(name=call.tool_name),
            args={"owner": "BerriAI", "repo": "another-repository"},
        )


async def test_concurrent_connection_callbacks_still_create_one_canonical_attention(google):
    ctx = google
    accounts = await asyncio.gather(connect(ctx, "gmail"), connect(ctx, "google_calendar"))
    _, definition, binding, context = await saved(ctx)
    expected = sorted(account["connection_id"] for account in accounts)
    assert definition["connection_ids"] == json.loads(context.connection_ids) == expected
    assert binding.state == "active"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Binding)) == 1
        assert await db.scalar(select(func.count()).select_from(PersonalDot)) == 1


async def test_disabled_shared_nonreadonly_and_other_owner_accounts_are_not_enrolled(google):
    ctx = google
    account = await connect(ctx, "gmail")
    async with ctx.product.database.sessions.begin() as db:
        other = User(username="other@example.test", display_name="Other", password="unused")
        db.add(other)
        await db.flush()
        for change in [
            {"enabled": False}, {"shared": True}, {"read_only": False},
            {"owner_id": other.id}, {"use_proactively": False},
        ]:
            values = {
                "owner_id": ctx.product.user_id, "kind": "google_calendar",
                "name": "Ineligible", "credential": ctx.product.database.security.seal(""),
                "read_only": True, "shared": False, "enabled": True,
            }
            proactive = change.pop("use_proactively", True)
            values.update(change)
            db.add(Connection(**values, policy=ConnectionPolicy(
                oauth=True, allowed_tools=["events"], use_proactively=proactive,
            ).model_dump_json()))
        await db.flush()
        await google_attention.configure(ctx.service, db, ctx.grant)
    assert (await saved(ctx))[1]["connection_ids"] == [account["connection_id"]]


async def test_enrollment_uses_one_real_temporal_schedule_across_accounts(google):
    """The actual existing Temporal reconciler owns creation, updates and pausing."""
    ctx = google
    async with await WorkflowEnvironment.start_local() as temporal:
        async def connect_temporal():
            return temporal.client
        ctx.service.connect = connect_temporal
        await connect(ctx, "gmail")
        first, definition, _, _ = await saved(ctx)
        await ctx.service.reconcile(first["id"])
        await connect(ctx, "google_calendar")
        await ctx.service.reconcile(first["id"])
        schedules = [schedule async for schedule in await temporal.client.list_schedules()]
        expected = native_schedule_id(first["id"], definition["triggers"][0]["id"])
        assert [schedule.id for schedule in schedules] == [expected]
        description = await temporal.client.get_schedule_handle(expected).describe()
        assert not description.schedule.state.paused
        await attention(ctx, "gmail", False)
        await attention(ctx, "google_calendar", False)
        await ctx.service.reconcile(first["id"])
        description = await temporal.client.get_schedule_handle(expected).describe()
        assert description.schedule.state.paused
        assert len([schedule async for schedule in await temporal.client.list_schedules()]) == 1
