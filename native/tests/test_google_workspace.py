"""Google account changes preserve computers without preserving tool authority."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import Never, Protocol

import httpx
import pytest
import requests
import test_google_personal_oauth as google_fixtures
from app import gmail_tools, google_calendar_tools
from app.connector_errors import ConnectorError
from app.google_apps import GoogleProvider
from fastapi import HTTPException
from pydantic import JsonValue, TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from moyai import personal_oauth
from moyai.contracts import ExecutionContext, Grant, NewSession, RunScope
from moyai.dot_models import DotSlackConversation
from moyai.models import Connection, Session, User
from moyai.oauth_models import OAuthCredential
from moyai.personal_oauth import connection_id
from moyai.runtime import Runtime

oauth = google_fixtures.oauth
google_oauth = google_fixtures.google_oauth

COMPUTER = {"provider": "modal", "id": "synthetic-retained-google-computer"}
PRIVATE_TRACE = "Private provider response from the previous account authority."
PUBLIC_REPLY = "The requested notes are ready."


class GoogleBackend(Protocol):
    queue: list[dict[str, JsonValue]]
    requests: list[requests.PreparedRequest]


async def expired_google_credential(runtime: Runtime, identity: str) -> tuple[str, int, int]:
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(OAuthCredential, identity)
        account = await db.get(Connection, identity)
        assert saved is not None and account is not None
        tokens = TypeAdapter(dict[str, JsonValue]).validate_json(runtime.security.unseal(saved.encrypted))
        tokens["expires_at"] = time.time() - 1
        saved.encrypted = runtime.security.seal(tokens)
        return saved.encrypted, saved.revision, account.revision


async def assert_google_credential_unchanged(
    runtime: Runtime, identity: str, expected: tuple[str, int, int],
) -> None:
    async with runtime.database.sessions() as db:
        saved = await db.get(OAuthCredential, identity)
        account = await db.get(Connection, identity)
        assert saved is not None and account is not None and account.enabled
        assert (saved.encrypted, saved.revision, account.revision) == expected


def google_read(provider: GoogleProvider) -> tuple[str, dict[str, object]]:
    if provider == "gmail":
        return "gmail_message", {"message_id": "18f000001"}
    return "google_calendar_event", {"event_id": "event-123"}


async def destination(runtime, actor, target):
    dot = await runtime.database.dots.open(actor)
    if target == "dot":
        return dot
    root = await runtime.database.create_session(actor, NewSession(visibility="private"))
    async with runtime.database.sessions.begin() as db:
        db.add(DotSlackConversation(session_id=root.id, dot_session_id=dot))
    return root.id


async def prepare(runtime, client, session_id, key, *, selections=()):
    response = await client.post(f"/api/sessions/{session_id}/messages", json={
        "prompt": "Continue with my currently connected accounts.", "request_key": key,
        "connections": list(selections),
    })
    assert response.status_code == 202, response.text
    turn = await runtime.database.claim_next()
    assert turn.id == response.json()["id"]
    return await runtime.admission.prepare(turn.id)


async def preserve_computer(runtime, prepared, *, finish=True):
    await runtime.database.save_workspace_reference(prepared.scope, COMPUTER)
    if finish:
        conversation = ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id,
            messages=[
                ModelResponse(parts=[ToolCallPart("private_read", {}, "original-read")]),
                ModelRequest(parts=[ToolReturnPart("private_read", PRIVATE_TRACE, "original-read")]),
                ModelResponse(parts=[TextPart(PUBLIC_REPLY)]),
            ],
        )).decode()
        assert await runtime.database.save_result(
            prepared.scope, conversation=conversation, output=PUBLIC_REPLY, workspace=None, usage={},
        )
        await runtime.database.settle(prepared.scope.turn_id)


async def opt_out(runtime, actor, client):
    async with runtime.database.sessions() as db:
        account = await db.get(Connection, connection_id(actor.id, "gmail"))
        revision = account.revision
    response = await client.patch("/api/personal-connections/gmail/attention", json={
        "revision": revision, "use_proactively": False,
    })
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("target", ["dot", "slack"])
@pytest.mark.parametrize("change", ["connect", "opt_out", "disconnect", "reconnect", "replace_account"])
async def test_account_changes_reuse_saved_computer_but_not_private_history(
    runtime, actor, google_oauth, target, change,
):
    client, backend = google_oauth
    await google_fixtures.connect(client, backend, "google_calendar")
    if change != "connect":
        await google_fixtures.connect(client, backend, "gmail")
    session_id = await destination(runtime, actor, target)
    previous = await prepare(runtime, client, session_id, "before-google-change")
    await preserve_computer(runtime, previous)

    if change == "opt_out":
        await opt_out(runtime, actor, client)
    elif change == "disconnect":
        response = await client.delete("/api/personal-connections/gmail")
        assert response.status_code == 200, response.text
    elif change == "replace_account":
        params = await google_fixtures.begin(client, "gmail")
        google_fixtures.authorize(
            backend, "gmail", subject="new-approved-google-subject", email="another@example.test",
        )
        response = await google_fixtures.finish(client, params, "gmail")
        assert response.status_code == 303 and "personal_connection=success" in response.headers["location"]
    else:
        await google_fixtures.connect(client, backend, "gmail")

    current = await prepare(runtime, client, session_id, "after-google-change")
    assert current.workspace == COMPUTER
    assert current.scope.workspace_key == previous.scope.workspace_key
    assert current.scope.grants_key != previous.scope.grants_key
    assert current.scope.conversation_id != previous.scope.conversation_id
    assert current.scope.memory_namespace != previous.scope.memory_namespace
    assert PRIVATE_TRACE not in (current.conversation or "")
    assert PUBLIC_REPLY in (current.conversation or "")
    expected = {connection_id(actor.id, "google_calendar")}
    if change not in {"opt_out", "disconnect"}:
        expected.add(connection_id(actor.id, "gmail"))
    assert {grant.id for grant in current.scope.grants} == expected
    async with runtime.database.sessions() as db:
        session = await db.get(Session, session_id)
        assert json.loads(session.workspace) == COMPUTER
        assert session.workspace_scope == previous.scope.workspace_key


@pytest.mark.parametrize("change", ["opt_out", "disconnect"])
async def test_google_result_is_fenced_after_revocation_even_when_computer_key_is_unchanged(
    runtime, actor, google_oauth, monkeypatch, change,
):
    client, backend = google_oauth
    await google_fixtures.connect(client, backend, "gmail")
    session_id = await destination(runtime, actor, "dot")
    prepared = await prepare(runtime, client, session_id, "read-before-revocation")
    await preserve_computer(runtime, prepared, finish=False)
    grant = prepared.scope.grants[0]
    calls = []

    async def read(name, arguments, token):
        calls.append(name)
        assert token == google_fixtures.ACCESS
        if change == "opt_out":
            await opt_out(runtime, actor, client)
        else:
            assert (await client.delete("/api/personal-connections/gmail")).status_code == 200
        return {"text": PRIVATE_TRACE}

    monkeypatch.setattr(gmail_tools, "call", read)
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.product_connections.execution.tool(
            prepared.scope, grant, "gmail_message", {"message_id": "18f000001"},
        )
    assert calls == ["gmail_message"]
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.database.authorize_scope(prepared.scope)
    assert replace(prepared.scope, grants=[]).workspace_key == prepared.scope.workspace_key
    async with runtime.database.sessions() as db:
        session = await db.get(Session, session_id)
        assert json.loads(session.workspace) == COMPUTER
        assert session.workspace_scope == prepared.scope.workspace_key


async def test_other_selected_account_revision_still_blocks_computer_reuse(runtime, actor, google_oauth):
    client, backend = google_oauth
    await google_fixtures.connect(client, backend, "gmail")
    session_id = await destination(runtime, actor, "dot")
    async with runtime.database.sessions.begin() as db:
        account = Connection(
            owner_id=actor.id, kind="mcp", name="Computer account", shared=False,
            read_only=True, credential=runtime.security.seal("synthetic-computer-credential"),
        )
        db.add(account)
        await db.flush()
        identity = account.id
    previous = await prepare(runtime, client, session_id, "computer-before-change", selections=[identity])
    await preserve_computer(runtime, previous)
    async with runtime.database.sessions.begin() as db:
        account = await db.get(Connection, identity)
        account.revision += 1
    response = await client.post(f"/api/sessions/{session_id}/messages", json={
        "prompt": "Continue.", "request_key": "computer-after-change", "connections": [identity],
    })
    assert response.status_code == 409
    assert "original account context" in response.json()["detail"]
    async with runtime.database.sessions() as db:
        session = await db.get(Session, session_id)
        assert json.loads(session.workspace) == COMPUTER
        assert session.workspace_scope == previous.scope.workspace_key


def scope(grants, **kwargs):
    return RunScope("session", "turn", "owner", "model", grants=grants, **kwargs)


def google_grant(provider="gmail", **kwargs):
    return Grant("google-account", provider, 1, "encrypted-empty", policy={"oauth": True}, **kwargs)


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("surface", ["web", "automation"])
def test_private_google_computer_identity_is_separate_from_read_authority(provider, surface):
    grant = google_grant(provider)
    original = scope([grant], surface=surface)
    changed = replace(original, grants=[replace(grant, revision=2)])
    assert original.workspace_key == changed.workspace_key == replace(original, grants=[]).workspace_key
    assert original.grants_key != changed.grants_key
    assert original.conversation_id != changed.conversation_id
    assert original.memory_namespace != changed.memory_namespace
    assert replace(original, actor_id="another-owner").workspace_key != original.workspace_key
    assert replace(original, visibility="shared").workspace_key != original.workspace_key


@pytest.mark.parametrize("boundary", [
    "writable", "not_oauth", "product_account", "shared_session", "shared_authority", "slack_authority",
])
def test_google_exemption_does_not_apply_outside_private_host_read_grants(boundary):
    grant = google_grant()
    original = scope([grant])
    if boundary == "writable":
        grant.read_only = False
    elif boundary == "not_oauth":
        grant.policy = {"oauth": False}
    elif boundary == "product_account":
        grant.policy["product"] = {}
    elif boundary == "shared_session":
        original.visibility = "shared"
    elif boundary == "shared_authority":
        original.execution_context = ExecutionContext("shared", "web")
    else:
        original.execution_context = ExecutionContext("private", "slack")
    assert original.workspace_key != replace(original, grants=[replace(grant, revision=2)]).workspace_key


def test_existing_shared_slack_github_exemption_and_other_grant_fences_remain():
    github = Grant("personal-github", "github", 1, "encrypted-empty", policy={"oauth": True})
    computer = Grant("computer-account", "mcp", 1, "encrypted-computer-credential")
    original = scope([github, computer], visibility="shared", surface="slack")
    next_owner = replace(original, actor_id="another-owner", grants=[replace(github, revision=2), computer])
    assert original.workspace_key == next_owner.workspace_key
    assert original.grants_key != next_owner.grants_key
    assert original.memory_namespace != next_owner.memory_namespace
    assert original.workspace_key != replace(
        original, grants=[github, replace(computer, revision=2)],
    ).workspace_key
    private = replace(original, visibility="private", surface="web")
    assert private.workspace_key != replace(private, grants=[replace(github, revision=2), computer]).workspace_key


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize(("code", "status", "retryable", "retry_after"), [
    ("rate_limited", 429, True, 17),
    ("provider_unavailable", 503, True, None),
    ("authentication_required", 401, False, None),
    ("invalid_response", None, False, None),
])
async def test_google_execution_preserves_safe_provider_failure_receipt(
    runtime: Runtime, actor: User, google_oauth: tuple[httpx.AsyncClient, GoogleBackend],
    monkeypatch: pytest.MonkeyPatch, provider: GoogleProvider, code: str,
    status: int | None, retryable: bool, retry_after: int | None,
) -> None:
    client, backend = google_oauth
    await google_fixtures.connect(client, backend, provider)
    prepared = await prepare(runtime, client, await destination(runtime, actor, "dot"), "provider-read-failure")
    service = runtime.product_connections
    assert service is not None
    failure = ConnectorError(
        "The Google read could not complete.", code=code, dispatched=True,
        retryable=retryable, status_code=status, retry_after=retry_after,
    )
    calls: list[str] = []

    async def read(name: str, arguments: Mapping[str, object], token: str) -> Never:
        assert token == google_fixtures.ACCESS
        calls.append(name)
        raise failure from RuntimeError(PRIVATE_TRACE + token + google_fixtures.REFRESH)

    adapter = gmail_tools if provider == "gmail" else google_calendar_tools
    monkeypatch.setattr(adapter, "call", read)
    name, arguments = google_read(provider)
    with pytest.raises(ToolFailed) as caught:
        await service.execution.tool(prepared.scope, prepared.scope.grants[0], name, arguments)
    assert json.loads(str(caught.value)) == failure.receipt(write=False)
    assert caught.value.__suppress_context__ is True
    assert all(secret not in str(caught.value) for secret in (
        PRIVATE_TRACE, google_fixtures.ACCESS, google_fixtures.REFRESH,
    ))
    assert calls == [name]


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
@pytest.mark.parametrize("oauth_error", ["server_error", "temporarily_unavailable"])
async def test_google_execution_preserves_transient_refresh_and_can_retry_without_reconnecting(
    runtime: Runtime, actor: User, google_oauth: tuple[httpx.AsyncClient, GoogleBackend],
    monkeypatch: pytest.MonkeyPatch, provider: GoogleProvider, oauth_error: str,
) -> None:
    client, backend = google_oauth
    await google_fixtures.connect(client, backend, provider)
    prepared = await prepare(runtime, client, await destination(runtime, actor, "dot"), "refresh-outage")
    grant = prepared.scope.grants[0]
    expected = await expired_google_credential(runtime, grant.id)
    service = runtime.product_connections
    assert service is not None
    calls: list[str] = []

    async def read(name: str, arguments: Mapping[str, object], token: str) -> dict[str, bool]:
        calls.append(token)
        return {"read": True}

    adapter = gmail_tools if provider == "gmail" else google_calendar_tools
    monkeypatch.setattr(adapter, "call", read)
    backend.queue.append({
        "error": oauth_error,
        "error_description": PRIVATE_TRACE + google_fixtures.ACCESS + google_fixtures.REFRESH,
    })
    requests_before = len(backend.requests)
    name, arguments = google_read(provider)
    with pytest.raises(ToolFailed) as caught:
        await service.execution.tool(prepared.scope, grant, name, arguments)
    receipt = json.loads(str(caught.value))
    assert receipt["code"] == "provider_unavailable" and receipt["retryable"] is True
    assert receipt["dispatched"] is False and receipt["outcome_uncertain"] is False
    assert "reconnect" not in str(caught.value).lower()
    assert all(secret not in str(caught.value) for secret in (
        PRIVATE_TRACE, google_fixtures.ACCESS, google_fixtures.REFRESH,
    ))
    assert calls == [] and len(backend.requests) == requests_before + 1
    await assert_google_credential_unchanged(runtime, grant.id, expected)

    google_fixtures.authorize(backend, provider, refresh="rotated-google-refresh")
    backend.queue[-2]["access_token"] = "renewed-google-access"
    assert await service.execution.tool(prepared.scope, grant, name, arguments) == {"read": True}
    assert calls == ["renewed-google-access"] and len(backend.requests) == requests_before + 3
    async with runtime.database.sessions() as db:
        saved = await db.get(OAuthCredential, grant.id)
        account = await db.get(Connection, grant.id)
        assert saved is not None and account is not None and account.enabled
        tokens = TypeAdapter(dict[str, JsonValue]).validate_json(runtime.security.unseal(saved.encrypted))
        assert tokens["refresh_token"] == "rotated-google-refresh"
        assert tokens["access_token"] == "renewed-google-access"
        assert saved.revision == expected[1] + 1 and account.revision == expected[2]


@pytest.mark.parametrize("provider", ["gmail", "google_calendar"])
async def test_google_permanent_refresh_failure_still_requires_reconnect(
    runtime: Runtime, actor: User, google_oauth: tuple[httpx.AsyncClient, GoogleBackend],
    monkeypatch: pytest.MonkeyPatch, provider: GoogleProvider,
) -> None:
    client, backend = google_oauth
    await google_fixtures.connect(client, backend, provider)
    prepared = await prepare(runtime, client, await destination(runtime, actor, "dot"), "refresh-revoked")
    grant = prepared.scope.grants[0]
    expected = await expired_google_credential(runtime, grant.id)
    service = runtime.product_connections
    assert service is not None

    async def read(name: str, arguments: Mapping[str, object], token: str) -> Never:
        pytest.fail("A revoked refresh grant must not dispatch a Google read.")

    adapter = gmail_tools if provider == "gmail" else google_calendar_tools
    monkeypatch.setattr(adapter, "call", read)
    backend.queue.append({"error": "invalid_grant", "error_description": google_fixtures.REFRESH})
    name, arguments = google_read(provider)
    with pytest.raises(PermissionError, match="Reconnect") as caught:
        await service.execution.tool(prepared.scope, grant, name, arguments)
    assert google_fixtures.REFRESH not in str(caught.value)
    await assert_google_credential_unchanged(runtime, grant.id, expected)


async def test_non_google_transient_refresh_keeps_existing_reconnect_behavior(
    runtime: Runtime, actor: User, oauth: tuple[httpx.AsyncClient, list[object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = oauth
    params = await google_fixtures.begin(client, "linear")
    await google_fixtures.finish(client, params, "linear")
    identity = connection_id(actor.id, "linear")
    prepared = await prepare(
        runtime, client, await destination(runtime, actor, "dot"), "linear-refresh", selections=[identity],
    )
    await expired_google_credential(runtime, identity)
    service = runtime.product_connections
    assert service is not None

    async def unavailable(*args: object, **kwargs: object) -> Never:
        raise ConnectorError("Temporary upstream failure.", retryable=True)

    monkeypatch.setattr(personal_oauth, "exchange", unavailable)
    with pytest.raises(PermissionError, match="Reconnect"):
        await service.credentials(prepared.scope, prepared.scope.grants[0])
