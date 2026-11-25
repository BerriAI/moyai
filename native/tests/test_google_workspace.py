"""Google account changes preserve computers without preserving tool authority."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import test_google_personal_oauth as google_fixtures
from app import gmail_tools
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart

from moyai.contracts import ExecutionContext, Grant, NewSession, RunScope
from moyai.dot_models import DotSlackConversation
from moyai.models import Connection, Session
from moyai.personal_oauth import connection_id

oauth = google_fixtures.oauth
google_oauth = google_fixtures.google_oauth

COMPUTER = {"provider": "modal", "id": "synthetic-retained-google-computer"}
PRIVATE_TRACE = "Private provider response from the previous account authority."
PUBLIC_REPLY = "The requested notes are ready."


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
