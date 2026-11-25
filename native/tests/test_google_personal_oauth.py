"""Google SDK HTTP substitution through Moyai's actual personal OAuth routes."""
import asyncio
import base64
import hashlib
import json
import time
from copy import copy
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
import requests
from agentchat.channels.slack.host import SlackHost
from app import google_apps
from app.connector_errors import ConnectorError
from app.connectors import TOOLS
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.models.test import TestModel
from sqlalchemy import select
from test_personal_oauth import begin, finish
from test_personal_oauth import oauth as oauth  # noqa: PLC0414 - explicit pytest fixture re-export

from moyai import oauth_providers, personal_oauth
from moyai.connection_policy import ConnectionPolicy, read_only_tool
from moyai.contracts import ExecutionContext, NewMessage, NewSession
from moyai.delegation_authority import DelegatedAuthority
from moyai.dot_models import DotSlackConversation, PersonalDot
from moyai.goal_models import Goal, GoalTurn
from moyai.models import Connection, Session, SlackThread, Turn
from moyai.oauth_models import OAuthAuthorization, OAuthCredential
from moyai.preferences.settings_models import SettingAutomation
from moyai.preferences.settings_runtime import admit_automation
from moyai.product_slack_events import SlackEvents, SlackMessage

CLIENT_ID = "google-test-client"
ACCESS = "synthetic-google-access-token"
REFRESH = "synthetic-google-refresh-token"
SUBJECT = "google-subject-123"


@pytest_asyncio.fixture
async def google_oauth(runtime, oauth, monkeypatch):
    runtime.product_automations = None
    settings = runtime.product_connections.settings
    settings.google_apps_client_id = CLIENT_ID
    settings.google_apps_client_secret = "synthetic-client-secret"
    backend = SimpleNamespace(queue=[], requests=[])

    def send(session, request, **kwargs):
        backend.requests.append(request)
        assert request.url in {google_apps.TOKEN_URL, google_apps.USERINFO_URL}
        payload = backend.queue.pop(0)
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    yield oauth[0], backend


def authorize(backend, provider, *, subject=SUBJECT, refresh: str | None = REFRESH, email="owner@example.test"):
    tokens = {
        "access_token": ACCESS, "token_type": "Bearer", "expires_in": 3600,
        "scope": " ".join([*google_apps.IDENTITY_SCOPES, *google_apps.SCOPES[provider]]),
    }
    if refresh:
        tokens["refresh_token"] = refresh
    backend.queue.extend([tokens, {"sub": subject, "email": email, "email_verified": True}])


async def connect(client, backend, provider, *, proactive=True):
    started = await client.post(
        f"/api/personal-connections/{provider}/oauth", json={"use_proactively": proactive},
    )
    assert started.status_code == 200, started.text
    params = parse_qs(urlsplit(started.json()["url"]).query)
    authorize(backend, provider)
    response = await finish(client, params, provider)
    assert response.status_code == 303 and "personal_connection=success" in response.headers["location"]
    return params


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("proactive", [False, True])
async def test_google_consent_saves_private_readonly_account_with_explicit_tools(
    runtime, actor, google_oauth, provider, proactive,
):
    client, backend = google_oauth
    params = await connect(client, backend, provider, proactive=proactive)
    body = parse_qs(backend.requests[0].body)
    verifier = body["code_verifier"][0]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
    ]
    identity = personal_oauth.connection_id(actor.id, provider)
    async with runtime.database.sessions() as db:
        account = await db.get(Connection, identity)
        saved = await db.get(OAuthCredential, identity)
        assert account.enabled and not account.shared and account.read_only
        assert account.owner_id == actor.id and account.kind == provider
        policy = ConnectionPolicy.model_validate_json(account.policy)
        assert policy.oauth and policy.use_proactively is proactive
        assert policy.oauth_account == personal_oauth.digest(f"{CLIENT_ID}:{SUBJECT}")
        assert policy.allowed_tools
        assert set(policy.allowed_tools) == {
            name.removeprefix(provider + "_") for name, spec in TOOLS.items() if spec[0] == provider and not spec[1]
        }
        tokens = json.loads(runtime.security.unseal(saved.encrypted))
        assert tokens["subject"] == SUBJECT and tokens["client_id"] == CLIENT_ID
        assert ACCESS not in saved.encrypted and runtime.security.unseal(account.credential) == b""
        assert await db.scalar(select(OAuthAuthorization)) is None
        grants = await runtime.database.native_grants(db, actor.id, [identity])
        assert grants[0].read_only and ACCESS not in json.dumps([asdict(g) for g in grants])
    row = next(item for item in (await client.get("/api/personal-connections")).json() if item["provider"] == provider)
    assert row["connected"] and row["use_proactively"] is proactive
    assert row["attention_status"] == ("unavailable" if proactive else "disabled")
    accounts = (await client.get("/api/connections")).json()
    assert all(secret not in json.dumps(accounts) for secret in (ACCESS, REFRESH, SUBJECT))
    assert "personal_connection=expired" in (await finish(client, params, provider)).headers["location"]
    assert len(backend.requests) == 2


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_google_grants_are_owner_private_on_web_and_automation(runtime, actor, google_oauth, provider):
    client, backend = google_oauth
    await connect(client, backend, provider)
    identity = personal_oauth.connection_id(actor.id, provider)
    service = runtime.product_connections
    for surface in ("web", "automation"):
        grants = await service.resolve_grants(actor.id, [identity], shared_session=False, surface=surface)
        assert len(grants) == 1 and grants[0].kind == provider and grants[0].read_only
        async with runtime.database.sessions() as db:
            with pytest.raises(HTTPException):
                await runtime.database.native_grants(db, "another-user", [identity], surface=surface)
            with pytest.raises(HTTPException):
                await runtime.database.native_grants(db, actor.id, [identity], shared_session=True, surface=surface)
    async with runtime.database.sessions() as db:
        with pytest.raises(HTTPException):
            await runtime.database.native_grants(db, actor.id, [identity], surface="slack")
    # Native token update endpoints cannot bypass the OAuth-only account route.
    response = await client.put(f"/api/connections/{identity}", json={
        "name": "Shared Google", "kind": provider, "credential": "replacement", "shared": True,
    })
    assert response.status_code in {409, 422}
    async with runtime.database.sessions() as db:
        account = await db.get(Connection, identity)
        assert not account.shared and account.read_only


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("change", ["disconnect", "new_start"])
async def test_inflight_google_callback_cannot_overwrite_newer_authority(
    runtime, actor, google_oauth, monkeypatch, provider, change,
):
    client, backend = google_oauth
    entered, release = asyncio.Event(), asyncio.Event()
    original = personal_oauth.exchange

    async def delayed(*args, **kwargs):
        value = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return value

    monkeypatch.setattr(personal_oauth, "exchange", delayed)
    params = await begin(client, provider)
    authorize(backend, provider)
    task = asyncio.create_task(finish(client, params, provider))
    newer = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        if change == "disconnect":
            assert (await client.delete(f"/api/personal-connections/{provider}")).status_code == 200
        else:
            newer = await begin(client, provider)
        release.set()
        response = await asyncio.wait_for(task, timeout=5)
        assert "personal_connection=expired" in response.headers["location"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    identity = personal_oauth.connection_id(actor.id, provider)
    async with runtime.database.sessions() as db:
        assert not (await db.get(Connection, identity)).enabled
        assert (await db.get(OAuthCredential, identity)).encrypted == ""
    if change == "new_start":
        assert newer is not None
        authorize(backend, provider)
        response = await finish(client, newer, provider)
        assert "personal_connection=success" in response.headers["location"]


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("replacement_completed", [False, True])
async def test_replaced_inflight_reconnect_preserves_live_account_and_newest_consent(
    runtime, actor, google_oauth, monkeypatch, provider, replacement_completed,
):
    client, backend = google_oauth
    await connect(client, backend, provider)
    identity = personal_oauth.connection_id(actor.id, provider)
    original_grants = await runtime.product_connections.resolve_grants(actor.id, [identity])
    entered, release = asyncio.Event(), asyncio.Event()
    exchange = personal_oauth.exchange

    async def delayed(*args, **kwargs):
        value = await exchange(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(personal_oauth, "exchange", delayed)
    older = await begin(client, provider)
    authorize(backend, provider, subject="superseded-google-subject")
    callback = asyncio.create_task(finish(client, older, provider))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        newer = await begin(client, provider)
        assert await runtime.product_connections.resolve_grants(actor.id, [identity]) == original_grants
        if replacement_completed:
            authorize(backend, provider, subject="newest-google-subject")
            completed = await finish(client, newer, provider)
            assert completed.status_code == 303
            assert "personal_connection=success" in completed.headers["location"]
        expected_grants = await runtime.product_connections.resolve_grants(actor.id, [identity])
        async with runtime.database.sessions() as db:
            credential = await db.get(OAuthCredential, identity)
            encrypted, credential_revision = credential.encrypted, credential.revision

        release.set()
        stale = await asyncio.wait_for(callback, timeout=5)
        assert stale.status_code == 303 and "personal_connection=expired" in stale.headers["location"]
        assert await runtime.product_connections.resolve_grants(actor.id, [identity]) == expected_grants
        async with runtime.database.sessions() as db:
            account = await db.get(Connection, identity)
            credential = await db.get(OAuthCredential, identity)
            assert account.enabled
            assert credential.encrypted == encrypted and credential.revision == credential_revision
            tokens = json.loads(runtime.security.unseal(credential.encrypted))
            assert tokens["subject"] == ("newest-google-subject" if replacement_completed else SUBJECT)
            pending = list(await db.scalars(select(OAuthAuthorization)))
            assert [entry.state_hash for entry in pending] == (
                [] if replacement_completed else [personal_oauth.digest(newer["state"][0])]
            )
        if not replacement_completed:
            authorize(backend, provider, subject="newest-google-subject")
            completed = await finish(client, newer, provider)
            assert completed.status_code == 303
            assert "personal_connection=success" in completed.headers["location"]
        current_grants = await runtime.product_connections.resolve_grants(actor.id, [identity])
        assert current_grants[0].revision > original_grants[0].revision
        assert current_grants[0].policy["oauth_account"] == personal_oauth.digest(
            f"{CLIENT_ID}:newest-google-subject",
        )
        async with runtime.database.sessions() as db:
            assert await db.scalar(select(OAuthAuthorization)) is None
    finally:
        release.set()
        if not callback.done():
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_google_refresh_rejects_changed_subject_and_preserves_stored_credentials(
    runtime, actor, google_oauth, provider,
):
    client, backend = google_oauth
    await connect(client, backend, provider)
    identity = personal_oauth.connection_id(actor.id, provider)
    async with runtime.database.sessions.begin() as db:
        credential = await db.get(OAuthCredential, identity)
        tokens = json.loads(runtime.security.unseal(credential.encrypted))
        tokens["expires_at"] = time.time() - 1
        credential.encrypted = runtime.security.seal(tokens)
        before_encrypted, before_revision = credential.encrypted, credential.revision
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.admission.submit(actor, session.id, NewMessage(
        prompt="Read my Google account", request_key="google-refresh-subject", connections=[identity],
    ))
    await runtime.database.claim_next()
    scope = (await runtime.admission.prepare(turn.id)).scope
    tools = await runtime.product_connections.toolset(scope, scope.grants[0]).get_tools(
        RunContext(deps=scope, model=TestModel(), usage=RunUsage()),
    )
    assert tools and set(tools) == set(scope.grants[0].policy["allowed_tools"])
    assert all(read_only_tool(tool.tool_def) for tool in tools.values())
    authorize(backend, provider, subject="different-google-subject", email="another@example.test")
    with pytest.raises(PermissionError, match="could not be renewed"):
        await personal_oauth.credentials(runtime.database, scope, scope.grants[0])
    async with runtime.database.sessions() as db:
        credential = await db.get(OAuthCredential, identity)
        assert credential.encrypted == before_encrypted and credential.revision == before_revision
    authorize(backend, provider, refresh=None, email="renamed@example.test")
    tokens = await personal_oauth.credentials(runtime.database, scope, scope.grants[0])
    assert tokens["subject"] == SUBJECT and tokens["refresh_token"] == REFRESH
    assert tokens["label"] == "renamed@example.test"
    assert (await runtime.database.authorize_scope(scope))[0] == scope.grants[0]
    assert (await client.delete(f"/api/personal-connections/{provider}")).status_code == 200
    with pytest.raises((PermissionError, HTTPException)):
        await personal_oauth.credentials(runtime.database, scope, scope.grants[0])


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_google_client_identity_change_fences_exchange_before_and_after_http(
    runtime, google_oauth, monkeypatch, provider,
):
    _, backend = google_oauth
    service = runtime.product_connections
    service.settings.google_apps_client_id = "changed-client"
    with pytest.raises(ConnectorError, match="configuration changed"):
        await oauth_providers.exchange(service, provider, CLIENT_ID, code="code", verifier="v" * 64)
    assert not backend.requests
    service.settings.google_apps_client_id = CLIENT_ID
    authorize(backend, provider)
    # The execution helper binds a new connector object, so intercept its class
    # method while still executing the actual provider verification.
    from app.connectors import Connectors
    real_verify = Connectors.verify

    async def verify(self, *args, **kwargs):
        label = await real_verify(self, *args, **kwargs)
        service.settings.google_apps_client_id = "changed-client"
        return label

    monkeypatch.setattr(Connectors, "verify", verify)
    with pytest.raises(ConnectorError, match="configuration changed"):
        await oauth_providers.exchange(service, provider, CLIENT_ID, code="code", verifier="v" * 64)
    assert len(backend.requests) == 2


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("change", ["session", "wrong_provider", "expired", "cancelled"])
async def test_google_callback_state_is_bound_and_not_redeemed_when_invalid(
    runtime, google_oauth, provider, change,
):
    client, backend = google_oauth
    params = await begin(client, provider)
    if change == "session":
        login = await client.post("/api/login", json={"username": "admin", "password": "test-password-12345"})
        assert login.status_code == 200
        client.headers["X-Moyai-CSRF"] = login.json()["csrf"]
    elif change == "expired":
        async with runtime.database.sessions.begin() as db:
            pending = await db.scalar(select(OAuthAuthorization))
            pending.expires_at = 0
    target = ("gmail" if provider == "google_calendar" else "google_calendar") if change == "wrong_provider" else provider
    response = await finish(
        client, params, target, **({"error": "access_denied"} if change == "cancelled" else {}),
    )
    assert response.status_code == 303
    assert "personal_connection=success" not in response.headers["location"]
    assert not backend.requests
    async with runtime.database.sessions() as db:
        assert not any(item.encrypted for item in await db.scalars(select(OAuthCredential)))


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_explicit_reconnect_can_change_subject_but_invalidates_old_grants(
    runtime, actor, google_oauth, provider,
):
    client, backend = google_oauth
    await connect(client, backend, provider)
    identity = personal_oauth.connection_id(actor.id, provider)
    before = (await runtime.product_connections.resolve_grants(actor.id, [identity]))[0]
    params = await begin(client, provider)
    authorize(backend, provider, subject="new-approved-subject", email="new@example.test")
    response = await finish(client, params, provider)
    assert "personal_connection=success" in response.headers["location"]
    after = (await runtime.product_connections.resolve_grants(actor.id, [identity]))[0]
    assert after.id == before.id and after.revision > before.revision
    assert after.policy["oauth_account"] != before.policy["oauth_account"]
    assert after.policy["oauth_account"] == personal_oauth.digest(f"{CLIENT_ID}:new-approved-subject")


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_google_click_connect_uses_deployment_app_without_personal_client_credentials(
    runtime, actor, google_oauth, provider,
):
    client, backend = google_oauth
    inventory = (await client.get("/api/personal-connections")).json()
    entry = next(row for row in inventory if row["provider"] == provider)
    assert entry["configured"] is True and entry["connected"] is False
    assert entry["use_proactively"] is True
    params = await begin(client, provider)  # Empty request body: one Connect action.
    assert params["client_id"] == [CLIENT_ID]
    assert params["prompt"] == ["consent select_account"]
    assert not {"client_secret", "access_token", "refresh_token"} & params.keys()
    authorize(backend, provider, email="personal@gmail.test")
    response = await finish(client, params, provider)
    assert "personal_connection=success" in response.headers["location"]
    async with runtime.database.sessions() as db:
        identity = personal_oauth.connection_id(actor.id, provider)
        account = await db.get(Connection, identity)
        policy = ConnectionPolicy.model_validate_json(account.policy)
        tokens = json.loads(runtime.security.unseal((await db.get(OAuthCredential, identity)).encrypted))
        assert account.owner_id == actor.id and not account.shared and account.read_only
        assert policy.use_proactively is True and tokens["label"] == "personal@gmail.test"


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("missing", ["client_id", "client_secret", "both"])
async def test_missing_deployment_registration_does_not_start_or_persist_personal_consent(
    runtime, actor, google_oauth, provider, missing,
):
    client, backend = google_oauth
    settings = runtime.product_connections.settings
    if missing in {"client_id", "both"}:
        settings.google_apps_client_id = ""
    if missing in {"client_secret", "both"}:
        settings.google_apps_client_secret = ""
    inventory = (await client.get("/api/personal-connections")).json()
    entry = next(row for row in inventory if row["provider"] == provider)
    assert entry["configured"] is False and entry["connected"] is False
    response = await client.post(f"/api/personal-connections/{provider}/oauth")
    assert response.status_code == 409 and "not enabled on this Moyai deployment" in response.json()["detail"]
    assert not backend.requests
    async with runtime.database.sessions() as db:
        identity = personal_oauth.connection_id(actor.id, provider)
        assert await db.get(Connection, identity) is None
        assert await db.get(OAuthCredential, identity) is None
        assert await db.scalar(select(OAuthAuthorization)) is None


@pytest_asyncio.fixture
async def google_dot(runtime, actor, google_oauth):
    client, backend = google_oauth
    for provider in ("gmail", "google_calendar"):
        await connect(client, backend, provider)
    dot_id = await runtime.database.dots.open(actor)
    # Slack Dot conversations deliberately use private/web execution authority.
    # Only the persisted root mapping distinguishes them from ordinary chats.
    root = await runtime.database.create_session(actor, NewSession(visibility="private"))
    async with runtime.database.sessions.begin() as db:
        db.add(DotSlackConversation(session_id=root.id, dot_session_id=dot_id))
    return SimpleNamespace(
        dot=dot_id, slack=root.id, client=client, backend=backend,
        ids={provider: personal_oauth.connection_id(actor.id, provider) for provider in ("gmail", "google_calendar")},
    )


@pytest.mark.parametrize("target", ["dot", "slack"])
async def test_direct_dot_and_private_slack_root_default_accounts_reach_execution(
    runtime, actor, google_dot, target,
):
    session_id = getattr(google_dot, target)
    expected = set(google_dot.ids.values())
    async with runtime.database.sessions() as db:
        session = await db.get(Session, session_id)
        assert await runtime.database.dots.is_conversation(db, actor.id, session)
        assert not await runtime.database.dots.is_conversation(db, actor.id, None)
        assert set(await runtime.database.dots.connected_accounts(db, actor.id, session)) == expected
    # Explicit selection and defaults are deduplicated without losing either.
    turn = await runtime.admission.submit(actor, session_id, NewMessage(
        prompt="Read the accounts connected to my Dot.", request_key="dot-google-defaults",
        connections=[google_dot.ids["gmail"], google_dot.ids["gmail"]],
    ), dot_accounts=True)
    assert len(json.loads(turn.connections)) == 2
    assert set(json.loads(turn.connections)) == expected
    claimed = await runtime.database.claim_next()
    assert claimed.id == turn.id
    prepared = await runtime.admission.prepare(turn.id)
    assert {grant.id for grant in prepared.scope.grants} == expected
    assert all(grant.read_only for grant in prepared.scope.grants)
    assert prepared.scope.authority == ExecutionContext("private", "web")


@pytest.mark.parametrize("manual", [False, True])
async def test_ordinary_chat_requires_explicit_google_selection(runtime, actor, google_dot, manual):
    ordinary = await runtime.database.create_session(actor, NewSession(visibility="private"))
    selected = [google_dot.ids["google_calendar"]] if manual else []
    async with runtime.database.sessions() as db:
        assert not await runtime.database.dots.is_conversation(db, actor.id, ordinary)
        assert await runtime.database.dots.connected_accounts(db, actor.id, ordinary) == []
    turn = await runtime.admission.submit(actor, ordinary.id, NewMessage(
        prompt="Read only my selected accounts.", request_key="ordinary-google-selection", connections=selected,
    ), dot_accounts=True)
    assert json.loads(turn.connections) == selected
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(turn.id)
    assert [grant.id for grant in prepared.scope.grants] == selected


@pytest.mark.parametrize("target", ["dot", "slack"])
@pytest.mark.parametrize("manual", [False, True])
async def test_ordinary_caller_preserves_selection_even_in_dot(runtime, actor, google_dot, target, manual):
    selected = [google_dot.ids["google_calendar"]] if manual else []
    turn = await runtime.admission.submit(actor, getattr(google_dot, target), NewMessage(
        prompt="Use only the caller's saved account selection.", request_key="ordinary-caller-dot",
        connections=selected,
    ))
    assert json.loads(turn.connections) == selected
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(turn.id)
    assert [grant.id for grant in prepared.scope.grants] == selected


@pytest.mark.parametrize("target", ["dot", "slack", "ordinary"])
async def test_http_user_message_enables_defaults_only_for_dot_sessions(runtime, actor, google_dot, target):
    if target == "ordinary":
        session_id = (await runtime.database.create_session(actor, NewSession(visibility="private"))).id
    else:
        session_id = getattr(google_dot, target)
    response = await google_dot.client.post(f"/api/sessions/{session_id}/messages", json={
        "prompt": "Read my connected accounts.", "request_key": "http-dot-google-defaults",
    })
    assert response.status_code == 202, response.text
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, response.json()["id"])
        expected = set() if target == "ordinary" else set(google_dot.ids.values())
        assert set(json.loads(turn.connections)) == expected


async def change_google_account(runtime, client, identity, change, provider="gmail"):
    if change == "off":
        async with runtime.database.sessions() as db:
            revision = (await db.get(Connection, identity)).revision
        response = await client.patch(f"/api/personal-connections/{provider}/attention", json={
            "revision": revision, "use_proactively": False,
        })
        assert response.status_code == 200, response.text
    elif change == "disabled":
        async with runtime.database.sessions.begin() as db:
            account = await db.get(Connection, identity)
            account.enabled = False
            account.revision += 1
    else:
        assert change == "disconnected"
        response = await client.delete(f"/api/personal-connections/{provider}")
        assert response.status_code == 200


@pytest.mark.parametrize("target", ["dot", "slack"])
@pytest.mark.parametrize("change", ["off", "disabled", "disconnected"])
async def test_dot_defaults_follow_account_state_and_manual_selection_keeps_its_policy(
    runtime, actor, google_dot, target, change,
):
    identity = google_dot.ids["gmail"]
    await change_google_account(runtime, google_dot.client, identity, change)
    session_id = getattr(google_dot, target)
    async with runtime.database.sessions() as db:
        session = await db.get(Session, session_id)
        assert await runtime.database.dots.connected_accounts(db, actor.id, session) == [
            google_dot.ids["google_calendar"],
        ]
    turn = await runtime.admission.submit(actor, session_id, NewMessage(
        prompt="Check my connected accounts.", request_key="dot-after-account-change",
    ), dot_accounts=True)
    assert json.loads(turn.connections) == [google_dot.ids["google_calendar"]]
    manual = NewMessage(prompt="Use Gmail explicitly.", request_key="dot-manual-google", connections=[identity])
    if change == "off":
        manual_turn = await runtime.admission.submit(actor, session_id, manual, dot_accounts=True)
        assert set(json.loads(manual_turn.connections)) == set(google_dot.ids.values())
    else:
        with pytest.raises(HTTPException):
            await runtime.admission.submit(actor, session_id, manual, dot_accounts=True)


@pytest.mark.parametrize("target,change", [
    ("dot", "off"), ("dot", "all_off"), ("dot", "disabled"), ("dot", "disconnected"),
    ("slack", "off"), ("slack", "all_off"), ("slack", "disabled"), ("slack", "disconnected"),
    ("ordinary", "ordinary_manual"), ("stale_slack", "stale_manual"),
])
async def test_private_slack_receipt_preserves_explicit_accounts_and_current_dot_consent(
    runtime, actor, google_dot, monkeypatch, target, change,
):
    # Keep one explicit non-Google account to ensure the receipt does not discard
    # the existing selection wholesale when refreshing implicit Google accounts.
    params = await begin(google_dot.client, "linear")
    response = await finish(google_dot.client, params, "linear")
    assert "personal_connection=success" in response.headers["location"]
    linear_id = personal_oauth.connection_id(actor.id, "linear")
    selected = [linear_id]
    if target == "ordinary":
        session_id = (await runtime.database.create_session(actor, NewSession(visibility="private"))).id
    else:
        session_id = google_dot.slack if target == "stale_slack" else getattr(google_dot, target)
    if target in {"ordinary", "stale_slack"}:
        selected = [linear_id, *google_dot.ids.values()]
    if target == "stale_slack":
        async with runtime.database.sessions.begin() as db:
            previous_dot = await db.get(Session, google_dot.dot)
            previous_dot.deleted = True
        assert await runtime.database.dots.open(actor) != google_dot.dot
    previous = await runtime.admission.submit(actor, session_id, NewMessage(
        prompt="Read my connected accounts.", request_key="slack-before-consent-change", connections=selected,
    ), dot_accounts=True)
    assert set(json.loads(previous.connections)) == {linear_id, *google_dot.ids.values()}
    if target in {"dot", "slack"}:
        affected = google_dot.ids.items() if change == "all_off" else [("gmail", google_dot.ids["gmail"])]
        for provider, identity in affected:
            await change_google_account(
                runtime, google_dot.client, identity, "off" if change == "all_off" else change, provider,
            )
    async with runtime.database.sessions.begin() as db:
        thread = SlackThread(
            session_id=session_id, team_id="TLOCAL001", channel_id="DLOCAL001",
            thread_ts="1795300000.001", slack_app="dot",
        )
        db.add(thread)
    event = {
        "type": "message", "channel_type": "im", "channel": "DLOCAL001", "user": "UACTUAL01",
        "ts": "1795300001.001", "thread_ts": thread.thread_ts, "text": "Follow up on that.",
    }
    payload = {"type": "event_callback", "event_id": "ELOCAL001", "team_id": thread.team_id, "event": event}
    message = SlackMessage.parse(payload, event, SlackHost(bot_user_id="UBOT00001", workspace_id=thread.team_id))
    assert message is not None
    private_actor = copy(actor)
    private_actor.slack_web_grant = object()
    original_submit = runtime.admission.submit
    admitted = []

    async def admit(sender, session_id, body, **kwargs):
        # Substitute only verified Slack identity projection: the receipt query,
        # account filtering, native admission, and grant resolution all run.
        assert sender is private_actor
        admitted.append((list(body.connections), kwargs["dot_accounts"]))
        return await original_submit(actor, session_id, body, **kwargs)

    monkeypatch.setattr(runtime.admission, "submit", admit)
    slack: Any = SimpleNamespace(runtime=runtime)
    events = SlackEvents(slack)
    body = NewMessage(prompt=message.prompt, request_key=message.key)
    turn = await events._submit(private_actor, thread, body, message, [])
    assert [(set(ids), opt_in) for ids, opt_in in admitted] == [(set(selected), True)]
    expected = set(selected)
    if target in {"dot", "slack"} and change != "all_off":
        expected.add(google_dot.ids["google_calendar"])
    assert set(json.loads(turn.connections)) == expected
    retry = await events._submit(private_actor, thread, body, message, [])
    assert retry.id == turn.id


@pytest.mark.parametrize("target", ["dot", "slack"])
@pytest.mark.parametrize("boundary", ["foreign_owner", "shared", "slack_surface", "missing_dot"])
async def test_dot_defaults_require_current_private_web_ownership(
    runtime, actor, google_dot, target, boundary,
):
    async with runtime.database.sessions.begin() as db:
        session = await db.get(Session, getattr(google_dot, target))
        owner = actor.id
        if boundary == "foreign_owner":
            owner = "another-user"
        elif boundary == "shared":
            session.visibility = "shared"
        elif boundary == "slack_surface":
            session.surface = "slack"
        else:
            await db.delete(await db.get(PersonalDot, actor.id))
            await db.flush()
        assert not await runtime.database.dots.is_conversation(db, owner, session)
        assert await runtime.database.dots.connected_accounts(db, owner, session) == []


async def test_slack_root_for_previous_dot_does_not_inherit_new_dot_defaults(runtime, actor, google_dot):
    async with runtime.database.sessions.begin() as db:
        previous = await db.get(Session, google_dot.dot)
        previous.deleted = True
    replacement = await runtime.database.dots.open(actor)
    assert replacement != google_dot.dot
    async with runtime.database.sessions() as db:
        root = await db.get(Session, google_dot.slack)
        assert not await runtime.database.dots.is_conversation(db, actor.id, root)
        assert await runtime.database.dots.connected_accounts(db, actor.id, root) == []
    turn = await runtime.admission.submit(actor, google_dot.slack, NewMessage(
        prompt="Continue the old Slack conversation.", request_key="stale-slack-dot",
    ), dot_accounts=True)
    assert json.loads(turn.connections) == []


@pytest.mark.parametrize("policy_change", ["shared", "writable", "not_oauth", "no_tools", "missing_credential"])
async def test_dot_default_accounts_require_private_readonly_oauth_tools(
    runtime, actor, google_dot, policy_change,
):
    identity = google_dot.ids["gmail"]
    async with runtime.database.sessions.begin() as db:
        account = await db.get(Connection, identity)
        policy = ConnectionPolicy.model_validate_json(account.policy)
        if policy_change == "shared":
            account.shared = True
        elif policy_change == "writable":
            account.read_only = False
        elif policy_change == "not_oauth":
            policy.oauth = False
        elif policy_change == "no_tools":
            policy.allowed_tools = []
        else:
            await db.delete(await db.get(OAuthCredential, identity))
        account.policy = policy.model_dump_json()
    turn = await runtime.admission.submit(actor, google_dot.dot, NewMessage(
        prompt="Check the eligible accounts.", request_key="dot-account-policy",
    ), dot_accounts=True)
    assert json.loads(turn.connections) == [google_dot.ids["google_calendar"]]


@pytest.mark.parametrize("target", ["dot", "slack"])
@pytest.mark.parametrize("origin", ["automation", "swarm", "responsibility", "delegation"])
@pytest.mark.parametrize("opt_in", [False, True])
async def test_generated_admission_does_not_acquire_dot_defaults(
    runtime, actor, google_dot, target, origin, opt_in,
):
    async with runtime.database.sessions.begin() as db:
        flags = {
            "automation_generated": origin == "automation",
            "swarm_generated": origin == "swarm",
            "responsibility_generated": origin == "responsibility",
        }
        if opt_in:
            flags["dot_accounts"] = True
        authority = DelegatedAuthority(ExecutionContext("private", "web"), []) if origin == "delegation" else None
        turn = await runtime.admission.submit(actor, getattr(google_dot, target), NewMessage(
            prompt="Perform only the originally authorized work.", request_key="generated-no-google-defaults",
        ), db=db, delegated_authority=authority, **flags)
        assert json.loads(turn.connections) == []


@pytest.mark.parametrize("origin", ["automation", "swarm", "delegation"])
@pytest.mark.parametrize("opt_in", [False, True])
async def test_generated_admission_preserves_exact_explicit_google_accounts(
    runtime, actor, google_dot, origin, opt_in,
):
    selected = [google_dot.ids["google_calendar"]]
    async with runtime.database.sessions.begin() as db:
        grants = await runtime.database.resolve_grants(db, actor.id, selected)
        authority = DelegatedAuthority(ExecutionContext("private", "web"), grants) if origin == "delegation" else None
        flags = {"dot_accounts": True} if opt_in else {}
        turn = await runtime.admission.submit(actor, google_dot.dot, NewMessage(
            prompt="Read only the previously selected calendar.", request_key="generated-google-selection",
            connections=selected,
        ), db=db, automation_generated=origin == "automation", swarm_generated=origin == "swarm",
            delegated_authority=authority, **flags)
        assert json.loads(turn.connections) == selected


async def test_native_settings_occurrence_keeps_saved_selection_in_dot(runtime, actor, google_dot):
    async with runtime.database.sessions.begin() as db:
        automation = SettingAutomation(
            owner_id=actor.id, owner_auth_version=actor.auth_version,
            session_id=google_dot.dot, name="Private saved task",
            prompt=runtime.security.seal("Summarize the existing conversation."),
            model="test", connections="[]", interval_minutes=60, paused=False, state="ready",
        )
        db.add(automation)
        await db.flush()
        identity = automation.id
    result = await admit_automation(runtime, identity, "google-selection-occurrence")
    assert result["status"] == "admitted"
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, result["turn_id"])
        assert json.loads(turn.connections) == []


async def test_goal_start_in_dot_uses_one_consistent_account_selection(runtime, actor, google_dot):
    result = await runtime.database.goals.command(actor, google_dot.dot, NewMessage(
        prompt="/goal Prepare a bounded meeting summary.", request_key="google-dot-goal",
    ))
    async with runtime.database.sessions() as db:
        goal = await db.get(Goal, google_dot.dot)
        turn = await db.get(Turn, result["id"])
        binding = await db.get(GoalTurn, turn.id)
        assert goal.status == "active" and binding is not None and not binding.generated
        assert json.loads(goal.connections) == json.loads(turn.connections) == []


async def test_goal_continuation_does_not_inherit_later_google_connection(runtime, actor, google_oauth):
    client, backend = google_oauth
    await connect(client, backend, "google_calendar")
    selected = [personal_oauth.connection_id(actor.id, "google_calendar")]
    dot_id = await runtime.database.dots.open(actor)
    initial = await runtime.database.goals.command(actor, dot_id, NewMessage(
        prompt="/goal Prepare a bounded calendar summary.", request_key="google-goal-before-new-account",
        connections=selected,
    ))
    await runtime.database.claim_next()
    prepared = await runtime.admission.prepare(initial["id"])
    await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="Progress saved.",
        workspace=None, usage={},
    )
    await runtime.database.settle(initial["id"])
    await connect(client, backend, "gmail")
    # Exercise the actual continuation caller, not just admission's flag guard.
    await runtime.database.goals.tick(dot_id)
    async with runtime.database.sessions() as db:
        continuations = list(await db.scalars(select(Turn).join(
            GoalTurn, GoalTurn.turn_id == Turn.id,
        ).where(GoalTurn.session_id == dot_id, GoalTurn.generated.is_(True))))
        assert len(continuations) == 1
        assert json.loads(continuations[0].connections) == selected
        assert (await db.get(Goal, dot_id)).status == "active"
