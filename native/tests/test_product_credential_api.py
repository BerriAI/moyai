"""Assembled API + real PostgreSQL boundaries; supplementary to real-app UI proof."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import test_product_credential_execution as credential_tests
from fastapi import APIRouter
from pydantic_ai import Conversation, ConversationTypeAdapter, DeferredToolRequests
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_product_credential_execution import provide, request_body
from test_product_identity import google_cookie

from moyai.api import create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import ProductTurnAuthority, Session, Turn
from moyai.presentation import public_event, saved_activity
from moyai.product_requester import answer_actor
from moyai.product_slack import ProductSlackAuthority
from moyai.product_slack_controls import question_results, record_replies

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
admitted_product = credential_tests.admitted_product
product_database = credential_tests.product_database
credentials = credential_tests.credentials


async def mixed_request(f):
    args = request_body()
    result = await f.service.request(f.scope, f.scope.turn_id, "credential-call", args)
    credential = ToolCallPart("credentials_request", {"request": args.model_dump(mode="json")}, tool_call_id="credential-call")
    question = ToolCallPart("ask_user_question", {"questions": [{
        "header": "Destination", "question": "Choose the receiver.", "multi_select": False,
        "options": [
            {"label": "Local", "description": "Only the disposable local receiver."},
            {"label": "Skip", "description": "Do not configure a receiver."},
        ],
    }]}, tool_call_id="question-call")
    approval = ToolCallPart("authorized_action", {}, tool_call_id="approval-call")
    pending = DeferredToolRequests(
        calls=[credential, question], approvals=[approval],
        metadata={"credential-call": {"kind": "credential_request", **result}},
    )
    conversation = Conversation(messages=[ModelResponse(parts=[credential, question, approval])], deferred_tool_requests=pending)
    await f.product.database.save_result(
        f.scope, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        output="", workspace=None, usage={}, deferred=True,
    )
    return result, pending


@asynccontextmanager
async def assembled_client(f, runtime, monkeypatch, *, sub=None, email="admin@example.test"):
    # Use the real app factory and auth/CSRF dependencies. Unrelated OAuth routes
    # are empty because this SQL fixture seeds only canonical identity/vault data.
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "database", f.product.database)
        patch.setattr(runtime, "security", f.runtime.security)
        patch.setattr(runtime, "product", f.runtime.product)
        patch.setattr(runtime, "product_identity", f.product.adapter)
        patch.setattr(runtime, "product_secrets", f.vault)
        patch.setattr(runtime, "settings", f.product.settings.model_copy(update={
            "public_url": f.runtime.product.settings.public_url,
        }))
        patch.setattr(f.runtime.product, "google", SimpleNamespace(routes=APIRouter), raising=False)
        patch.setattr(f.runtime, "product_secrets", f.vault, raising=False)
        cookie, csrf = google_cookie(f.product.adapter, sub=sub or f.product.subject, email=email)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
            base_url=runtime.settings.public_url, cookies={"workspace_session": cookie},
            headers={"Origin": runtime.settings.public_url, "X-CSRF-Token": csrf},
        ) as client:
            yield client


def answers(turn_id):
    return {
        "turn_id": turn_id, "request_key": "mixed-api-answer",
        "approvals": {"approval-call": True},
        "answers": {"question-call": {"answers": [{"header": "Destination", "selected": ["Local"]}]}},
    }


async def test_full_api_mixed_request_csrf_server_handles_and_one_slot_resume(credentials, runtime, monkeypatch):
    f = credentials
    pending, _ = await mixed_request(f)
    engine = create_async_engine(
        f.product.settings.sqlalchemy_url, pool_size=1, max_overflow=0, pool_timeout=2,
        connect_args={"server_settings": {"search_path": f'"{f.product.schema}",pg_catalog'}},
    )
    original_sessions, original_native = f.product.database.sessions, f.product.adapter.native_engine
    f.product.database.sessions = async_sessionmaker(engine, expire_on_commit=False)
    f.product.adapter.native_engine = engine
    try:
        async with assembled_client(f, runtime, monkeypatch) as client:
            view = await asyncio.wait_for(client.get(f"/api/sessions/{f.session.id}"), 5)
            assert view.status_code == 200, view.text
            assert view.json()["can_answer"] is True
            assert view.json()["pending"]["metadata"]["credential-call"]["request_id"] == pending["request_id"]
            forged = answers(f.turn.id)
            forged["answers"]["credential-call"] = {"request_id": pending["request_id"], "status": "provided"}
            rejected = await client.post(f"/api/sessions/{f.session.id}/answers", json=forged)
            assert rejected.status_code == 422
            body = provide().model_copy(update={"scope": "organization"}).model_dump(mode="json")
            body["value"] = provide().value.get_secret_value()
            csrf = client.headers.pop("X-CSRF-Token")
            path = "/api/credentials/requests/" + pending["request_id"]
            assert (await client.post(path, json=body)).status_code == 403
            client.headers["X-CSRF-Token"] = csrf
            resolved = await client.post(path, json=body)
            assert resolved.status_code == 200, resolved.text
            assert resolved.json()["resolved"] and not resolved.json()["resumed"]
            response = await asyncio.wait_for(
                client.post(f"/api/sessions/{f.session.id}/answers", json=answers(f.turn.id)), 5,
            )
            assert response.status_code == 202, response.text
            retried = await client.post(f"/api/sessions/{f.session.id}/answers", json=answers(f.turn.id))
            assert retried.status_code == 202 and retried.json()["id"] == response.json()["id"]
        async with f.product.database.sessions() as db:
            resumed = await db.get(Turn, response.json()["id"])
            grant = await db.get(ProductTurnAuthority, resumed.id)
            assert grant.authority_version == f.scope.product_grant.authority_version
            serialized = f.runtime.security.unseal(resumed.deferred_results).decode()
            assert pending["request_id"] in serialized and "synthetic-signing-material" not in serialized
    finally:
        f.product.database.sessions, f.product.adapter.native_engine = original_sessions, original_native
        await engine.dispose()


async def test_admin_assistance_cannot_answer_another_task_or_open_private_session(credentials, runtime, monkeypatch):
    f = credentials
    pending, _ = await mixed_request(f)
    async with f.product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('google:other','google','other@example.test','Other admin',NULL,'c','u')",
        ))
    async with assembled_client(f, runtime, monkeypatch, sub="other", email="other@example.test") as client:
        assert (await client.get(f"/api/sessions/{f.session.id}")).status_code == 404
        async with f.product.database.sessions.begin() as db:
            (await db.get(Session, f.session.id)).visibility = "shared"
        view = await client.get(f"/api/sessions/{f.session.id}")
        assert view.status_code == 200, view.text
        assert view.json()["can_answer"] is False and view.json().get("pending") is None
        assert view.json()["credential_requests"] == [{"request_id": pending["request_id"]}]
        denied = await client.post(f"/api/sessions/{f.session.id}/answers", json=answers(f.turn.id))
        assert denied.status_code == 403
        path = "/api/credentials/requests/" + pending["request_id"]
        detail = await client.get(path)
        assert detail.status_code == 200 and detail.json()["scopes"] == ["organization"]
        supplied = await client.post(path, json={
            "generation": 0, "scope": "organization", "lifetime": "persistent",
            "value": provide().value.get_secret_value(),
        })
        assert supplied.status_code == 200 and not supplied.json()["resumed"], supplied.text


async def test_last_credential_resolution_resumes_previously_recorded_slack_answers(credentials):
    f = credentials
    pending, requests = await mixed_request(f)
    async with f.product.database.sessions() as db:
        turn = await db.get(Turn, f.turn.id)
    await record_replies(
        f.runtime, f.product.actor, turn,
        calls=question_results(requests, "Local"), approvals={"approval-call": False},
    )
    response = await f.service.resolve(None, f.product.principal, pending["request_id"], provide())
    assert response["resumed"]
    async with f.product.database.sessions() as db:
        resumed = await db.get(Turn, response["turn_id"])
        results = json.loads(f.runtime.security.unseal(resumed.deferred_results))
        assert results["calls"]["credential-call"]["status"] == "provided"
        assert results["approvals"]["approval-call"] is False
        assert "Local" in json.dumps(results["calls"]["question-call"])


async def test_verified_web_slack_reply_keeps_original_grant_and_stale_links_lose_controls(credentials, runtime, monkeypatch):
    f = credentials
    adapter = f.product.adapter
    adapter.settings.slack_bot_enabled = True
    adapter.settings.slack_signing_secret = "synthetic-local-only"
    adapter.settings.slack_session_users = "UTEST0001"
    slack_id = "slack:TTEST0001:UTEST0001"
    async with f.product.source.begin() as db:
        for column in [
            "profile_eligible BOOLEAN NOT NULL DEFAULT FALSE", "profile_conflict BOOLEAN NOT NULL DEFAULT FALSE",
            "profile_checked_at TEXT NOT NULL DEFAULT ''", "link_status TEXT NOT NULL DEFAULT ''",
        ]:
            await db.execute(text("ALTER TABLE moyai.users ADD COLUMN " + column))
        await db.execute(text("CREATE TABLE moyai.connections(provider TEXT PRIMARY KEY,encrypted TEXT NOT NULL)"))
        await db.execute(text("CREATE TABLE moyai.connection_policies(provider TEXT PRIMARY KEY,enabled BOOLEAN,read_only BOOLEAN)"))
        await db.execute(text("INSERT INTO moyai.connections VALUES('slack',:encrypted)"), {
            "encrypted": adapter.legacy_security.encrypt(json.dumps({
                "bot": {"access_token": "synthetic-no-send", "team": {"id": "TTEST0001"}, "bot_user_id": "UBOT00001"},
            })),
        })
        await db.execute(text(
            "INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at,linked_user_id,profile_eligible,"
            "profile_checked_at,link_status) VALUES(:id,'slack','admin@example.test','Verified Slack','c','u',:owner,TRUE,:checked,'linked')",
        ), {"id": slack_id, "owner": f.product.user_id, "checked": datetime.now(UTC).isoformat()})
    # The fixture adds canonical Slack columns after identity was first read;
    # discard the old asyncpg SELECT-* type cache after this test-only DDL.
    await f.product.source.dispose()
    await f.product.database.engine.dispose()
    adapter.slack_authority = ProductSlackAuthority(adapter)
    principal = await adapter.current(slack_id)
    async with f.product.database.sessions.begin() as db:
        actor = await adapter.project_native_user(db, principal)
    f.session = await f.product.database.create_session(actor, NewSession(visibility="shared"))
    f.turn = await f.product.database.submit(actor, f.session.id, NewMessage(prompt="Signed local Slack task", request_key="slack-task"))
    assert (await f.product.database.claim_next()).id == f.turn.id
    f.scope = (await f.product.database.prepare(f.turn.id)).scope
    await mixed_request(f)
    async with f.product.database.sessions() as db:
        turn = await db.get(Turn, f.turn.id)
    resumed = await answer_actor(f.runtime, f.product.actor, turn)
    assert resumed.id == slack_id and resumed.product_grant == principal.grant
    async with assembled_client(f, runtime, monkeypatch) as client:
        view = await client.get(f"/api/sessions/{f.session.id}")
        assert view.status_code == 200 and view.json()["can_answer"] is True
        async with f.product.source.begin() as db:
            await db.execute(text("UPDATE moyai.users SET profile_checked_at=:checked WHERE id=:id"), {
                "id": slack_id, "checked": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            })
        # A retained accounting link and unmodified original grant cannot stand
        # in for a currently verified Slack/individual relationship.
        view = await client.get(f"/api/sessions/{f.session.id}")
        assert view.status_code == 200 and view.json()["can_answer"] is False
        assert view.json().get("pending") is None
        denied = await client.post(f"/api/sessions/{f.session.id}/answers", json=answers(f.turn.id))
        assert denied.status_code == 403


def test_live_and_saved_credential_details_stay_private_and_failures_stay_failed():
    call = ToolCallPart("credentials_run", {"request": {"command": "sensitive command context"}}, tool_call_id="one")
    result = ToolReturnPart("credentials_run", "sensitive command output", tool_call_id="one", outcome="failed")
    live = [public_event(FunctionToolCallEvent(call)), public_event(FunctionToolResultEvent(result))]
    saved = saved_activity([ModelResponse(parts=[call], run_id="run"), ModelRequest(parts=[result], run_id="run")], "run")
    assert saved == live and live[1]["failed"] is True
    assert live[0]["arguments"] is None and live[1]["content"] is None
    assert "sensitive" not in json.dumps(saved)
