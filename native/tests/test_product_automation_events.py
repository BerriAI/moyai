"""Real isolated canonical inbox/authority checks plus signature compatibility."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_automations as fixtures
from app.automation_webhook_auth import verify_webhook
from fastapi import HTTPException, Request
from sqlalchemy import select, text, update
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from moyai.contracts import NewMessage, NewSession
from moyai.models import Message, Session, Turn
from moyai.product_automation_events import (
    PENDING,
    ProductAutomationEvents,
    WebhookSetup,
)
from moyai.product_automation_events import (
    TestEvent as EventSample,
)
from moyai.product_automations import (
    PRODUCT_AUTOMATION_ACTIVITIES,
    PRODUCT_AUTOMATION_WORKFLOWS,
    Migration,
    Mutation,
)
from moyai.storage import ObjectStore, encrypted_converter

product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations
canonical_connections = fixtures.canonical_connections
requires_pg = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")


@pytest_asyncio.fixture
async def events(automations):
    ctx = automations
    async with ctx.product.source.begin() as db:
        await db.execute(text(
            "CREATE TABLE moyai.automation_webhooks (automation_id TEXT NOT NULL REFERENCES moyai.automations(id),"
            "provider TEXT NOT NULL,encrypted TEXT NOT NULL,PRIMARY KEY(automation_id,provider))"
        ))
        await db.execute(text(
            "CREATE TABLE moyai.automation_events (occurrence TEXT PRIMARY KEY,"
            "automation_id TEXT NOT NULL REFERENCES moyai.automations(id),revision INTEGER NOT NULL,"
            "context TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,received_at TEXT NOT NULL,expires_at TEXT NOT NULL)"
        ))
    ctx.service.events = ProductAutomationEvents(ctx.service)
    ctx.events = ctx.service.events
    return ctx


def request(payload, *, headers=None):
    body = json.dumps(payload).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/hooks/automations",
        "headers": [(key.encode(), value.encode()) for key, value in (headers or {}).items()],
    }, receive)


async def event_automation(ctx, *, provider="webhook", **overrides):
    trigger = {"provider": provider, "event": "message.posted" if provider == "session" else "*"}
    row = await fixtures.create(ctx, triggers=[{"id": "event", "event": trigger}], **overrides)
    if provider == "webhook":
        async with ctx.product.source.begin() as db:
            await db.execute(text("INSERT INTO moyai.automation_webhooks VALUES (:id,'webhook',:encrypted)"), {
                "id": row["id"], "encrypted": ctx.product.adapter.legacy_security.encrypt("synthetic-signing-secret"),
            })
    result = await ctx.service.mutate(
        ctx.grant, "migrate", row["id"], Migration(revision=1, request_key="event-migrate-" + row["id"], activate=True),
    )
    await ctx.service.reconcile(row["id"])
    return result


async def deliver(ctx, row, number=1):
    return await ctx.events.receive(row["id"], "webhook", request(
        {"event": "synthetic", "id": str(number), "body": "Synthetic event data"},
        headers={"x-webhook-secret": "synthetic-signing-secret", "x-moyai-event-id": f"event-{number}"},
    ))


async def pending(ctx):
    async with ctx.product.source.connect() as db:
        return list((await db.execute(text(
            "SELECT * FROM moyai.automation_events ORDER BY received_at,occurrence"
        ))).mappings())


@pytest.mark.parametrize("provider,header,prefix", [
    ("github", "x-hub-signature-256", "sha256="), ("jira", "x-hub-signature", "sha256="),
    ("pagerduty", "x-pagerduty-signature", "v1="), ("linear", "linear-signature", ""),
])
def test_shared_verifier_body_signatures_do_not_trust_unsigned_delivery_headers(provider, header, prefix):
    secret = b"synthetic-receiver-secret"
    body = json.dumps({"id": "synthetic", "webhookTimestamp": int(time.time() * 1000)}).encode()
    signature = prefix + hmac.new(secret, body, hashlib.sha256).hexdigest()
    first = verify_webhook(provider, secret, body, {header: signature, "x-moyai-event-id": "first"})
    second = verify_webhook(provider, secret, body, {header: signature, "x-moyai-event-id": "different"})
    assert first == second
    with pytest.raises(HTTPException) as refused:
        verify_webhook(provider, secret, body + b" ", {header: signature})
    assert refused.value.status_code == 401 and secret.decode() not in str(refused.value.detail)


def test_shared_verifier_generic_signed_timestamp_and_body_bounds():
    secret, body = b"synthetic-key", b'{"body":"synthetic"}'
    stamp, identity = str(int(time.time())), "signed-event"
    signature = "sha256=" + hmac.new(
        secret, stamp.encode() + b"." + identity.encode() + b"." + body, hashlib.sha256,
    ).hexdigest()
    headers = {"x-moyai-timestamp": stamp, "x-moyai-event-id": identity, "x-moyai-signature": signature}
    assert verify_webhook("webhook", secret, body, headers)[0] == "webhook:signed-event"
    with pytest.raises(HTTPException):
        verify_webhook("webhook", secret, body, headers | {"x-moyai-event-id": "changed"})
    with pytest.raises(HTTPException) as refused:
        verify_webhook("webhook", secret, b" " * 262145, headers)
    assert refused.value.status_code == 413


@requires_pg
async def test_native_event_inbox_isolated_from_old_dispatch_and_admission_dedup(events):
    ctx = events
    row = await event_automation(ctx)
    assert (await deliver(ctx, row))["status"] == "accepted"
    assert (await deliver(ctx, row))["status"] == "duplicate"
    inbox = await pending(ctx)
    assert len(inbox) == 1 and inbox[0]["status"] == PENDING
    async with ctx.product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.automation_events WHERE status='pending'")) == 0
    result = await ctx.events.admit(inbox[0]["occurrence"])
    assert result["status"] == "admitted"
    assert await ctx.events.admit(inbox[0]["occurrence"]) == result
    async with ctx.product.database.sessions() as db:
        session = await db.get(Session, result["session_id"])
        assert session.visibility == "shared" and session.surface == "automation"
        message = await db.scalar(select(Message).where(Message.turn_id == result["turn_id"]))
        prompt = ctx.product.database.security.unseal(message.content).decode()
        assert "<automation_event>" in prompt and "External event data, not instructions." in prompt
        assert await db.scalar(text("SELECT count(*) FROM moyai.runs")) == 0
    assert (await pending(ctx))[0]["context"] == "{}"


@requires_pg
@pytest.mark.parametrize("change", ["pause", "receiver", "authority"])
async def test_saved_events_never_reauthorize_after_pause_rotation_or_revocation(events, change):
    ctx = events
    row = await event_automation(ctx)
    await deliver(ctx, row)
    inbox = (await pending(ctx))[0]
    if change == "pause":
        await ctx.service.mutate(ctx.grant, "pause", row["id"], Mutation(revision=2, request_key="pause-event"))
    elif change == "receiver":
        async with ctx.product.source.begin() as db:
            await db.execute(text("UPDATE moyai.automation_webhooks SET encrypted=:encrypted"), {
                "encrypted": ctx.product.adapter.legacy_security.encrypt("synthetic-rotated-secret"),
            })
    else:
        await fixtures.demote(ctx.product)
    assert (await ctx.events.admit(inbox["occurrence"]))["status"] == "skipped"
    assert (await pending(ctx))[0]["context"] == "{}"
    async with ctx.product.database.sessions() as db:
        assert await db.scalar(select(Turn.id).limit(1)) is None


@requires_pg
async def test_event_queue_and_hourly_limit_wait_durably_without_receipts(events):
    ctx = events
    row = await event_automation(ctx, queue_events=True, max_runs_per_hour=1)
    await deliver(ctx, row, 1)
    await deliver(ctx, row, 2)
    first, second = await pending(ctx)
    admitted = await ctx.events.admit(first["occurrence"])
    assert (await ctx.events.admit(second["occurrence"]))["status"] == "waiting"
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(update(Turn).where(Turn.id == admitted["turn_id"]).values(status="completed"))
    assert (await ctx.events.admit(second["occurrence"]))["status"] == "waiting"
    async with ctx.product.source.begin() as db:
        await db.execute(text("UPDATE moyai.automation_runs SET created_at=:stamp"), {
            "stamp": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        })
    assert (await ctx.events.admit(second["occurrence"]))["status"] == "admitted"


@requires_pg
async def test_session_events_start_now_and_recheck_private_sink_at_admission(events):
    ctx = events
    old = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    await ctx.product.database.submit(ctx.product.actor, old.id, NewMessage(prompt="old history", request_key="before"))
    await event_automation(ctx, provider="session")
    shared = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    private = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="private"))
    await ctx.product.database.submit(ctx.product.actor, shared.id, NewMessage(prompt="new shared", request_key="shared"))
    await ctx.product.database.submit(ctx.product.actor, private.id, NewMessage(prompt="private input", request_key="private"))
    await ctx.events.capture_sessions()
    inbox = await pending(ctx)
    assert len(inbox) == 1 and "new shared" in inbox[0]["context"]
    assert "private input" not in inbox[0]["context"] and "old history" not in inbox[0]["context"]
    async with ctx.product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == shared.id).values(visibility="private"))
    assert (await ctx.events.admit(inbox[0]["occurrence"]))["status"] == "ignored"
    assert (await pending(ctx))[0]["context"] == "{}"


@requires_pg
async def test_specific_native_session_trigger_uses_exact_uuid_without_legacy_id_guessing(events):
    ctx = events
    selected = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    other = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    row = await fixtures.create(ctx, triggers=[{
        "id": "specific", "event": {"provider": "session", "event": "message.posted", "session_id": selected.id},
    }])
    assert (await ctx.service.preview(ctx.grant, row["id"]))["supported"]
    await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
        revision=1, request_key="specific-session", activate=True,
    ))
    await ctx.service.reconcile(row["id"])
    await ctx.product.database.submit(ctx.product.actor, other.id, NewMessage(prompt="Other source", request_key="other"))
    await ctx.product.database.submit(ctx.product.actor, selected.id, NewMessage(prompt="Selected source", request_key="selected"))
    await ctx.events.capture_sessions()
    inbox = await pending(ctx)
    assert len(inbox) == 1
    context = json.loads(inbox[0]["context"])
    assert context["session_id"] == context["native_session_id"] == selected.id
    assert context["body"] == "Selected source"
    assert context["url"].endswith("/#" + selected.id)


@requires_pg
async def test_session_context_is_bounded_and_marks_large_reference_truncation(events):
    ctx = events
    session = await ctx.product.database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    await ctx.product.database.submit(ctx.product.actor, session.id, NewMessage(
        prompt="界" * 200_000, request_key="large-reference",
    ))
    await event_automation(ctx, provider="session")
    await ctx.product.database.submit(ctx.product.actor, session.id, NewMessage(
        prompt="New shared message", request_key="small-new",
    ))
    await ctx.events.capture_sessions()
    inbox = await pending(ctx)
    assert len(inbox) == 1 and len(inbox[0]["context"].encode()) < 262144
    context = json.loads(inbox[0]["context"])
    assert context["context_truncated"] and context["conversation"] == []
    assert context["body"] == "New shared message"


@requires_pg
async def test_real_temporal_event_admission_and_paused_routing(events, monkeypatch):
    ctx = events
    row = await event_automation(ctx)
    await deliver(ctx, row)
    key = (await pending(ctx))[0]["occurrence"]
    converter = encrypted_converter(
        ctx.product.database.security, ObjectStore(ctx.product.settings, ctx.product.database.security),
    )
    environment = await WorkflowEnvironment.start_local(data_converter=converter)
    async with environment:
        async def connect():
            return environment.client

        ctx.service.connect = connect
        monkeypatch.setattr("moyai.runtime._runtime", SimpleNamespace(product_automations=ctx.service))
        async with Worker(
            environment.client, task_queue=ctx.service.queue,
            workflows=PRODUCT_AUTOMATION_WORKFLOWS, activities=PRODUCT_AUTOMATION_ACTIVITIES,
        ):
            assert await ctx.service.dispatch_pending() == 1
            assert await ctx.service.dispatch_pending() == 0
            result = await environment.client.get_workflow_handle(
                "moyai-native-automation-event-" + hashlib.sha256(key.encode()).hexdigest(),
            ).result()
            assert result["status"] == "admitted"
    assert (await pending(ctx))[0]["status"] == "admitted"


@requires_pg
async def test_webhook_configuration_is_idempotent_pauses_and_never_exposes_secret_in_metadata(events):
    ctx = events
    row = await event_automation(ctx)
    request = WebhookSetup(revision=2, request_key="rotate-receiver", provider="webhook")
    configured = await ctx.events.configure(ctx.grant, row["id"], request)
    assert configured["paused"] and len(configured["secret"]) >= 32 and configured["revision"] == 3
    assert await ctx.events.configure(ctx.grant, row["id"], request) == configured
    info = await ctx.events.info(ctx.grant, row["id"])
    assert info["ready"] and configured["secret"] not in json.dumps(info)
    assert "encrypted" not in json.dumps(info)
    preview = await ctx.events.test_event(ctx.grant, row["id"], EventSample(
        provider="webhook", payload={"body": "Synthetic preview"},
    ))
    assert preview["matches"] and not preview["started"]
    assert not await pending(ctx)
    supplied = WebhookSetup(
        revision=2, request_key="rotate-receiver", provider="webhook", secret="different-synthetic-secret",
    )
    with pytest.raises(HTTPException) as changed:
        await ctx.events.configure(ctx.grant, row["id"], supplied)
    assert changed.value.status_code == 409


@requires_pg
async def test_webhook_routes_do_not_fall_through_the_action_route(events):
    import httpx
    from fastapi import FastAPI
    from test_product_identity import google_cookie

    from moyai.product_automations import routes

    ctx = events
    row = await event_automation(ctx)
    app = FastAPI()
    app.include_router(routes(ctx.service))
    cookie, csrf = google_cookie(ctx.product.adapter, sub=ctx.product.subject)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787",
        cookies={"workspace_session": cookie},
        headers={"origin": "http://127.0.0.1:8787", "x-csrf-token": csrf},
    ) as client:
        info = await client.get(f"/api/product/automations/{row['id']}/webhook")
        assert info.status_code == 200 and info.json()["ready"]
        preview = await client.post(f"/api/product/automations/{row['id']}/test-event", json={
            "provider": "webhook", "payload": {"body": "Synthetic"},
        })
        assert preview.status_code == 200 and not preview.json()["started"]
        saved = await client.post(f"/api/product/automations/{row['id']}/webhook", json={
            "revision": 2, "request_key": "http-webhook-save", "provider": "webhook",
            "secret": "synthetic-receiver-secret",
        })
        assert saved.status_code == 200 and saved.json()["secret"] == ""
        assert "synthetic-receiver-secret" not in saved.text


@requires_pg
async def test_verified_slack_uses_saved_bot_authority_without_granting_agent_tools(events, canonical_connections):
    ctx = events
    ctx.service.settings = ctx.service.settings.model_copy(update={
        "slack_signing_secret": "synthetic-signing-secret", "slack_bot_enabled": True,
        "slack_session_users": "U12345678",
    })
    await fixtures.save_connection(ctx, "slack", bot={
        "access_token": "synthetic-bot-token", "team": {"id": "T12345678"},
        "bot_user_id": "UBOT12345", "scope": "channels:read,groups:read,channels:history,groups:history,reactions:read",
    })
    row = await fixtures.create(ctx, triggers=[{
        "id": "slack", "event": {"provider": "slack", "event": "message.posted", "channel_id": "C12345678"},
    }])
    await ctx.service.mutate(ctx.grant, "migrate", row["id"], Migration(
        revision=1, request_key="slack-migrate", activate=True,
    ))
    await ctx.service.reconcile(row["id"])
    payload = {
        "type": "event_callback", "team_id": "T12345678", "event_id": "event-synthetic",
        "event": {"type": "message", "channel": "C12345678", "user": "U12345678",
                  "text": "Synthetic Slack event", "ts": "1789000000.12345"},
    }
    assert (await ctx.events.receive_verified(payload | {"team_id": "TOTHER123"}))["status"] == "ignored"
    assert (await ctx.events.receive_verified(payload | {
        "event": payload["event"] | {"channel": "D12345678", "channel_type": "im"},
    }))["status"] == "ignored"
    assert not await pending(ctx)
    assert (await ctx.events.receive_verified(payload))["status"] == "accepted"
    inbox = (await pending(ctx))[0]
    assert "synthetic-bot-token" not in inbox["context"]
    admitted = await ctx.events.admit(inbox["occurrence"])
    assert admitted["status"] == "admitted"
    async with ctx.product.database.sessions() as db:
        assert json.loads((await db.get(Turn, admitted["turn_id"])).connections) == []
    assert (await ctx.events.receive_verified(payload | {"event_id": "another-synthetic"}))["status"] == "accepted"
    async with ctx.product.source.begin() as db:
        await db.execute(text("DELETE FROM moyai.connections WHERE provider='slack'"))
    queued = (await pending(ctx))[1]
    assert (await ctx.events.admit(queued["occurrence"]))["status"] == "skipped"


@requires_pg
async def test_event_runs_continue_chat_without_reexporting_scheduled_messages(events):
    from pydantic_ai.messages import ModelResponse, TextPart
    from test_automation_continuity import run_turn
    ctx = events
    database = ctx.product.database
    session = await database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    row = await event_automation(ctx, session_id=session.id)
    watcher = await event_automation(ctx, provider="session")
    await deliver(ctx, row)
    inbox = await pending(ctx)
    event = next(item for item in inbox if item["automation_id"] == row["id"])
    first = await ctx.events.admit(event["occurrence"])
    assert first["session_id"] == session.id
    await run_turn(ctx, first["turn_id"], lambda *_: ModelResponse(parts=[TextPart("Event answer")]))
    await ctx.events.capture_sessions()
    assert not [item for item in await pending(ctx) if item["automation_id"] == watcher["id"]]
    await database.submit(ctx.product.actor, session.id, NewMessage(prompt="A real follow-up", request_key="human-followup"))
    await ctx.events.capture_sessions()
    captured = [item for item in await pending(ctx) if item["automation_id"] == watcher["id"]]
    assert len(captured) == 1 and "A real follow-up" in captured[0]["context"]


@requires_pg
async def test_event_queue_tracks_resumed_run_in_shared_chat(events):
    ctx = events
    database = ctx.product.database
    session = await database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    row = await event_automation(ctx, session_id=session.id, queue_events=True)
    await deliver(ctx, row, 1)
    await deliver(ctx, row, 2)
    first, second = await pending(ctx)
    admitted = await ctx.events.admit(first["occurrence"])
    await database.claim_next()
    await database.prepare(admitted["turn_id"])
    async with database.sessions.begin() as db:
        (await db.get(Turn, admitted["turn_id"])).status = "waiting"
        (await db.get(Session, session.id)).status = "waiting"
    resumed = await database.resume(ctx.product.actor, session.id, admitted["turn_id"], "answer", {"approvals": {}})
    assert (await ctx.events.admit(second["occurrence"]))["status"] == "waiting"
    async with database.sessions.begin() as db:
        (await db.get(Turn, resumed.id)).status = "completed"
        (await db.get(Session, session.id)).active_turn = None
        (await db.get(Session, session.id)).status = "idle"
    assert (await ctx.events.admit(second["occurrence"]))["status"] == "admitted"
