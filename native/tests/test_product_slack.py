"""Signed local intake + real local HTTP receiver. No external Slack messages.

These protocol tests supplement the parent's integrated product deployment proof.
"""

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select
from test_product_connections import bridge as canonical_connections

from moyai.models import Delivery, ProductTurnAuthority, SlackThread, Turn, new_id
from moyai.product_identity import ProductGrant, ProductPrincipal
from moyai.product_slack import ProductSlack
from moyai.product_slack_models import ProductSlackBinding

bridge = canonical_connections


@pytest_asyncio.fixture
async def slack(runtime, actor, bridge):
    from app.coordinator_handoff import initialize_schema

    initialize_schema(bridge.store)
    settings = bridge.settings
    settings.slack_bot_enabled = True
    settings.slack_thread_chat_enabled = True
    settings.slack_dm_enabled = True
    settings.slack_session_users = "*"
    settings.slack_signing_secret = "local-slack-signature"
    bridge.connectors.save("slack", {
        "access_token": "synthetic-user-token",
        "bot": {
            "access_token": "synthetic-bot-token", "bot_user_id": "UBOT00001",
            "team": {"id": "TTEST0001"}, "scope": "chat:write",
        },
    }, "Local Slack")
    grant = ProductGrant(actor.id, "a" * 64)
    authority = {"current": True}

    async def require_current(value):
        if not authority["current"] or value != grant:
            raise HTTPException(401, "Revoked local grant.")
        return ProductPrincipal(
            actor.id, "slack", "slack", "", "Local sender", "member", True, grant.authority_version,
        )

    runtime.product = SimpleNamespace(
        store_proxy=bridge.store, security=bridge.security, settings=settings,
        identity=SimpleNamespace(require_current=require_current),
    )
    runtime.product_connections = bridge
    service = ProductSlack(runtime)
    runtime.product_slack = service
    yield SimpleNamespace(service=service, grant=grant, authority=authority, runtime=runtime)
    runtime.product = None


@pytest.fixture
def receiver():
    requests = []
    outcome = {"drop": False, "hold": False}
    entered, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            entered.set()
            if outcome["hold"]:
                assert release.wait(5)
            if outcome["drop"]:
                self.close_connection = True
                return
            value = json.dumps({"ok": True, "channel": body["channel"], "ts": "1792100000.001"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

        def do_GET(self):
            messages = [{
                "ts": "1792100000.001", "metadata": body["metadata"],
            } for body in requests]
            value = json.dumps({"ok": True, "messages": messages}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(
        url=f"http://127.0.0.1:{server.server_port}/", requests=requests, outcome=outcome,
        entered=entered, release=release,
    )
    release.set()
    server.shutdown()
    thread.join()
    server.server_close()


async def bind(slack, active):
    session, turn, _ = active
    runtime, service = slack.runtime, slack.service
    bot = service.installation()
    async with runtime.database.sessions.begin() as db:
        current = await db.get(type(session), session.id)
        current.surface, current.visibility = "slack", "shared"
        db.add(ProductTurnAuthority(
            turn_id=turn.id, actor_id=turn.actor_id, authority_version=slack.grant.authority_version,
        ))
        thread = SlackThread(
            id=new_id(), session_id=session.id, team_id="TTEST0001",
            channel_id="CTEST0001", thread_ts="1792100000.000",
        )
        db.add(thread)
        binding = ProductSlackBinding(
            session_id=session.id, requester_id=turn.actor_id, team_id=thread.team_id,
            channel_id=thread.channel_id, thread_ts=thread.thread_ts,
            started_ts=thread.thread_ts, connection_authority=bot["authority"],
            delivery_after="2026-01-01T00:00:00+00:00",
        )
        db.add(binding)
    return thread


async def queued(slack, active):
    thread = await bind(slack, active)
    await slack.service.queue(active[1], thread, "test", {"text": "Local-only delivery."})
    async with slack.runtime.database.sessions() as db:
        return await db.scalar(select(Delivery))


async def stage_failed_turn(slack, active, error):
    thread = await bind(slack, active)
    async with slack.runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, active[1].id)
        turn.status = "failed"
        turn.error = error
        turn.finished_at = datetime.now(UTC).isoformat()
        binding = await db.get(ProductSlackBinding, turn.session_id)
        binding.delivery_after = "2020-01-01T00:00:00+00:00"
    await slack.service.stage()
    async with slack.runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).where(Delivery.turn_id == active[1].id)))
    return thread, {row.identity.rsplit(":", 1)[-1]: row for row in rows}



def event_payload(event_id, event):
    return {
        "type": "event_callback", "team_id": "TTEST0001", "event_id": event_id, "event": event,
    }


async def post_signed_event(client, payload, *, valid=True):
    body = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        b"local-slack-signature", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256,
    ).hexdigest()
    if not valid:
        signature = "v0=invalid"
    return await client.post(
        "/hooks/slack/events", content=body,
        headers={"x-slack-request-timestamp": stamp, "x-slack-signature": signature},
    )


async def test_real_canonical_hook_inbox_signature_dedupe_without_external_side_effects(slack, monkeypatch):
    service = slack.service
    calls = []

    async def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return {"ok": True}

    monkeypatch.setattr(service.connectors, "request", request)
    app = FastAPI()
    app.include_router(service.routes())
    payload = event_payload("EVENT001", {
        "type": "app_mention", "user": "UTEST0001", "channel": "CTEST0001",
        "ts": "1792100000.000", "text": "<@UBOT00001> local-only input",
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        bad = await post_signed_event(client, payload, valid=False)
        assert bad.status_code == 401
        assert calls == []
        for _ in range(2):
            response = await post_signed_event(client, payload)
            assert response.status_code == 200
    assert calls == []
    rows = service.store.rows("SELECT id,status,payload FROM coordinator_hooks")
    assert rows[0]["payload"].startswith("sealed:v1:")
    assert "local-only input" not in rows[0]["payload"]
    assert service.inbox.decode(rows[0]["payload"])["event"]["text"].endswith("local-only input")
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    async with slack.runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0


async def test_signed_unmentioned_top_level_message_is_not_acknowledged(slack, monkeypatch):
    service = slack.service
    calls = []

    async def request(method, url, **kwargs):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(service.connectors, "request", request)
    app = FastAPI()
    app.include_router(service.routes())
    payload = event_payload("EVENT002", {
        "type": "message", "user": "UTEST0001", "channel": "CTEST0001",
        "ts": "1792100000.002", "text": "An unmentioned top-level message.",
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await post_signed_event(client, payload)

    assert response.status_code == 200
    assert "https://slack.com/api/reactions.add" not in calls


async def test_signed_message_from_user_outside_session_allowlist_is_not_acknowledged(slack, monkeypatch):
    service = slack.service
    calls = []
    service.settings.slack_session_users = "UTEST0001"

    async def request(method, url, **kwargs):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(service.connectors, "request", request)
    app = FastAPI()
    app.include_router(service.routes())
    payload = event_payload("EVENT003", {
        "type": "app_mention", "user": "UOTHER0001", "channel": "CTEST0001",
        "ts": "1792100000.003", "text": "<@UBOT00001> unauthorized input",
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await post_signed_event(client, payload)

    assert response.status_code == 200
    assert "https://slack.com/api/reactions.add" not in calls


async def test_signed_dm_with_dm_chat_disabled_is_not_acknowledged(slack, monkeypatch):
    service = slack.service
    calls = []
    service.settings.slack_dm_enabled = False

    async def request(method, url, **kwargs):
        calls.append(url)
        return {"ok": True}

    monkeypatch.setattr(service.connectors, "request", request)
    app = FastAPI()
    app.include_router(service.routes())
    payload = event_payload("EVENT004", {
        "type": "message", "user": "UTEST0001", "channel": "DTEST0001", "channel_type": "im",
        "ts": "1792100000.004", "text": "A direct message.",
    })
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        response = await post_signed_event(client, payload)

    assert response.status_code == 200
    assert "https://slack.com/api/reactions.add" not in calls


async def test_outbox_single_claim_against_real_local_receiver(slack, active, receiver):
    delivery = await queued(slack, active)
    async with httpx.AsyncClient(base_url=receiver.url, timeout=3) as client:
        await asyncio.gather(*[slack.service.deliver(client, delivery) for _ in range(4)])
        await slack.service.deliver(client, delivery)
    assert len(receiver.requests) == 1
    payload = receiver.requests[0]
    assert payload["channel"] == "CTEST0001" and payload["thread_ts"] == "1792100000.000"
    assert payload["metadata"] == {"event_type": "moyai_delivery", "event_payload": {"id": delivery.id}}
    assert payload["parse"] == "none" and payload["link_names"] is False
    assert payload["unfurl_links"] is False and payload["unfurl_media"] is False
    async with slack.runtime.database.sessions() as db:
        row = await db.get(Delivery, delivery.id)
        assert row.status == "sent" and row.receipt


async def test_unknown_send_reconciles_read_only_without_reposting(slack, active, receiver):
    delivery = await queued(slack, active)
    receiver.outcome["drop"] = True
    async with httpx.AsyncClient(base_url=receiver.url, timeout=3) as client:
        await slack.service.deliver(client, delivery)
        async with slack.runtime.database.sessions.begin() as db:
            row = await db.get(Delivery, delivery.id)
            assert row.status == "sending"
            row.claimed_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        async with slack.runtime.database.sessions() as db:
            delivery = await db.get(Delivery, delivery.id)
        await slack.service._deliver_batch(client, [delivery])
    assert len(receiver.requests) == 1
    async with slack.runtime.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "sent"


@pytest.mark.parametrize("change", ["revoked", "private", "paused", "reconnected"])
async def test_delivery_live_authority_and_private_sink_fences(slack, active, receiver, change):
    delivery = await queued(slack, active)
    if change == "revoked":
        slack.authority["current"] = False
    elif change == "reconnected":
        value = json.loads(slack.service.product.security.decrypt(
            slack.service.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]["encrypted"],
        ))
        slack.service.connectors.save("slack", value, "Replacement installation")
    else:
        async with slack.runtime.database.sessions.begin() as db:
            if change == "private":
                (await db.get(type(active[0]), active[0].id)).visibility = "private"
            else:
                (await db.get(ProductSlackBinding, active[0].id)).paused = True
    async with httpx.AsyncClient(base_url=receiver.url, timeout=3) as client:
        await slack.service.deliver(client, delivery)
    assert not receiver.requests
    async with slack.runtime.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "suppressed"


async def test_wake_does_not_backfill_completion_while_paused(slack, active):
    await bind(slack, active)
    await slack.service.set_paused(active[0].id, True)
    async with slack.runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, active[1].id)
        turn.status = "completed"
        turn.finished_at = datetime.now(UTC).isoformat()
        turn.result = slack.runtime.security.seal("Completed asleep.")
    await slack.service.set_paused(active[0].id, False)
    await slack.service.stage()
    async with slack.runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery)))
    assert rows and all(row.status == "suppressed" for row in rows)


async def test_long_answers_and_artifacts_are_staged_atomically_without_broadcast_mentions(slack, active):
    from moyai.models import Artifact

    await bind(slack, active)
    async with slack.runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, active[1].id)
        turn.status = "completed"
        turn.finished_at = datetime.now(UTC).isoformat()
        turn.result = slack.runtime.security.seal("<!channel> " + "useful output " * 600)
        db.add(Artifact(
            id=new_id(), turn_id=turn.id, session_id=turn.session_id, name="report.txt",
            digest="synthetic", content_hash="0" * 64, media_type="text/plain", size=1,
        ))
    await slack.service.stage()
    await slack.service.stage()
    async with slack.runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).order_by(Delivery.identity)))
    values = [json.loads(slack.runtime.security.unseal(row.content))["text"] for row in rows]
    assert len(rows) == 2  # Web input mirror and final answer, no acknowledgement.
    assert sum("(from Moyai)" in value for value in values) == 1
    assert all("<!channel>" not in value for value in values)
    assert any("/api/files/" in value and "report.txt" in value for value in values)
    assert len({row.identity for row in rows}) == len(rows)


async def test_failed_turn_stages_error_and_requester_debug_trace(slack, active):
    error = "Codex stopped (usageLimitExceeded)"
    _, deliveries = await stage_failed_turn(slack, active, error)

    message = json.loads(slack.runtime.security.unseal(deliveries["0"].content))
    debug_delivery = json.loads(slack.runtime.security.unseal(deliveries["debug"].content))
    upload = debug_delivery["upload"]
    trace = json.loads(base64.b64decode(upload["content"]))

    assert error in message["text"]
    assert "Debug log attached." not in message["text"]
    assert deliveries["0"].identity == f"product-slack:{active[1].id}:failed:0"
    assert deliveries["debug"].identity == f"product-slack:{active[1].id}:failed:debug"
    assert debug_delivery["after"] == deliveries["0"].identity
    assert upload["filename"] == f"moyai-debug-{active[1].id}.json"
    assert upload["title"] == "Moyai debug log"
    assert trace["schema_version"] == "ATIF-v1.8"
    assert any(step["source"] == "system" and step["message"] == error for step in trace["steps"])


async def test_failed_debug_upload_waits_for_message_then_uses_slack_file_helper(slack, active, monkeypatch):
    thread, deliveries = await stage_failed_turn(slack, active, "Codex stopped (usageLimitExceeded)")
    events = []

    async def upload_bytes(url, raw):
        events.append(("upload_bytes", url, raw))

    from agentchat.channels.slack import files

    monkeypatch.setattr(files, "upload_bytes", upload_bytes)

    async def handle(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        events.append((method, payload))
        assert request.headers["authorization"] == "Bearer synthetic-bot-token"
        if method in {"chat.postMessage", "chat.update"}:
            return httpx.Response(200, json={
                "ok": True, "channel": thread.channel_id, "ts": "1792100000.001",
            })
        if method == "files.getUploadURLExternal":
            return httpx.Response(200, json={
                "ok": True, "file_id": "FDEBUG001",
                "upload_url": "https://files.slack.com/upload/FDEBUG001",
            })
        if method == "files.completeUploadExternal":
            return httpx.Response(200, json={
                "ok": True,
                "files": [{"id": "FDEBUG001", "permalink": "https://slack.test/files/FDEBUG001"}],
            })
        raise AssertionError(f"Unexpected Slack API method: {method}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://slack.test/api/",
    ) as client:
        assert "accepted" not in deliveries
        await slack.service.deliver(client, deliveries["0"])
        assert [event[0] for event in events] == ["chat.postMessage"]
        assert "Codex stopped (usageLimitExceeded)" in events[0][1]["text"]
        assert "Debug log attached." not in events[0][1]["text"]
        await slack.service.deliver(client, deliveries["debug"])

    assert [event[0] for event in events] == [
        "chat.postMessage", "files.getUploadURLExternal", "upload_bytes", "files.completeUploadExternal",
    ]
    upload_content = base64.b64decode(json.loads(
        slack.runtime.security.unseal(deliveries["debug"].content),
    )["upload"]["content"])
    assert events[1][1] == {
        "filename": f"moyai-debug-{active[1].id}.json",
        "length": len(upload_content),
    }
    assert events[2][2] == upload_content
    assert events[3][1]["channel_id"] == thread.channel_id
    assert events[3][1]["thread_ts"] == thread.thread_ts
    async with slack.runtime.database.sessions() as db:
        sent = await db.get(Delivery, deliveries["debug"].id)
        receipt = json.loads(slack.runtime.security.unseal(sent.receipt))
    assert sent.status == "sent"
    assert receipt["files"] == [{
        "id": "FDEBUG001", "permalink": "https://slack.test/files/FDEBUG001",
    }]


async def test_failed_debug_upload_waits_until_failure_message_is_sent(slack, active):
    _, deliveries = await stage_failed_turn(slack, active, "Codex stopped (usageLimitExceeded)")
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://slack.test/api/",
    ) as client:
        await slack.service.deliver(client, deliveries["debug"])

    assert not requests
    async with slack.runtime.database.sessions() as db:
        saved = await db.get(Delivery, deliveries["debug"].id)
    assert saved.status == "pending"


async def test_approval_actions_are_scoped_to_original_requester_and_native_pending_call(slack, active, actor, monkeypatch):
    from pydantic_ai import Conversation, ConversationTypeAdapter, DeferredToolRequests
    from pydantic_ai.messages import ToolCallPart

    thread = await bind(slack, active)
    pending = DeferredToolRequests(approvals=[ToolCallPart(
        "linear_123_create_issue", {"title": "Local-only issue"}, tool_call_id="local-call",
    )])
    async with slack.runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, active[1].id)
        turn.status = "waiting"
        turn.conversation = slack.runtime.security.seal(
            ConversationTypeAdapter.dump_json(Conversation(deferred_tool_requests=pending)).decode(),
        )
        (await db.get(type(active[0]), active[0].id)).status = "waiting"

    async def same_actor(team, user):
        assert team == thread.team_id and user == "UTEST0001"
        return actor, None

    monkeypatch.setattr(slack.service, "actor", same_actor)
    payload = {
        "team": {"id": thread.team_id}, "channel": {"id": thread.channel_id}, "user": {"id": "UTEST0001"},
        "message": {"thread_ts": thread.thread_ts, "ts": "1792100000.001"},
        "actions": [{"action_id": "moyai_native_approve", "value": active[1].id}],
    }
    with pytest.raises(HTTPException):
        await slack.service.interact({**payload, "channel": {"id": "COTHER001"}})
    await slack.service.stage()
    with pytest.raises(HTTPException):
        await slack.service.interact(payload)
    async with slack.runtime.database.sessions.begin() as db:
        delivery = await db.scalar(select(Delivery).where(
            Delivery.identity == f"product-slack:{active[1].id}:waiting:0",
        ))
        content = json.loads(slack.runtime.security.unseal(delivery.content))
        assert content["blocks"] and "linear_123_create_issue" in content["text"]
        delivery.status = "sent"
        delivery.receipt = slack.runtime.security.seal({"ok": True, "channel": thread.channel_id, "ts": "1792100000.001"})
    with pytest.raises(HTTPException):
        await slack.service.interact({**payload, "message": {**payload["message"], "ts": "1792100000.002"}})
    await slack.service.interact(payload)
    with pytest.raises(HTTPException):
        await slack.service.interact(payload)
    async with slack.runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Turn)))
    assert len(rows) == 2
    continuation = next(row for row in rows if row.id != active[1].id)
    assert json.loads(slack.runtime.security.unseal(continuation.deferred_results))["approvals"] == {"local-call": True}


async def test_cancelled_confirmed_receipt_drains_and_contender_never_reposts(slack, active, receiver):
    from sqlalchemy import event

    delivery = await queued(slack, active)
    session_type = slack.runtime.database.sessions.class_.sync_session_class
    parent = None
    cancelled = False

    def after_commit(_session):
        nonlocal cancelled
        if receiver.requests and not cancelled:
            cancelled = True
            parent.cancel()

    event.listen(session_type, "after_commit", after_commit)
    try:
        async with httpx.AsyncClient(base_url=receiver.url, timeout=3) as client:
            parent = asyncio.create_task(slack.service.deliver(client, delivery))
            with pytest.raises(asyncio.CancelledError):
                await parent
            await slack.service.deliver(client, delivery)
    finally:
        event.remove(session_type, "after_commit", after_commit)
    assert cancelled and len(receiver.requests) == 1
    async with slack.runtime.database.sessions() as db:
        row = await db.get(Delivery, delivery.id)
        assert row.status == "sent" and row.receipt


@pytest.mark.parametrize("boundary", ["post", "receipt", "lost-response"])
async def test_planned_close_finishes_accepted_send_without_reposting(slack, active, receiver, monkeypatch, boundary):
    service = slack.service
    delivery = await queued(slack, active)
    receiver.outcome["hold"] = boundary != "receipt"
    receiver.outcome["drop"] = boundary == "lost-response"
    entered, release = asyncio.Event(), asyncio.Event()
    settle = service._settle

    async def blocked_settle(*args, **kwargs):
        entered.set()
        await release.wait()
        await settle(*args, **kwargs)

    if boundary == "receipt":
        monkeypatch.setattr(service, "_settle", blocked_settle)
    async with httpx.AsyncClient(base_url=receiver.url, timeout=3) as client:
        async def pump():
            await service._deliver_batch(client, [delivery])
        monkeypatch.setattr(service, "pump", pump)
        service.delivery_task = asyncio.create_task(service.watch())
        if boundary == "receipt":
            await asyncio.wait_for(entered.wait(), 2)
        else:
            assert await asyncio.to_thread(receiver.entered.wait, 2)
        service.stop_admitting()
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(.02)
        assert not closing.done() and not service.delivery_task.cancelling()
        async with slack.runtime.database.sessions() as db:
            assert (await db.get(Delivery, delivery.id)).status == "sending"
        receiver.release.set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        await service.close()
        await service.deliver(client, delivery)
    assert len(receiver.requests) == 1
    async with slack.runtime.database.sessions() as db:
        row = await db.get(Delivery, delivery.id)
        assert row.status == ("sending" if boundary == "lost-response" else "sent")
        assert bool(row.receipt) is (boundary != "lost-response")


async def test_claim_writer_wait_and_competing_poll_keep_loop_responsive(slack, active, receiver):
    delivery = await queued(slack, active)
    ticks = []

    async def pulse():
        while True:
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.02)

    async with slack.runtime.database.engine.connect() as holder, httpx.AsyncClient(base_url=receiver.url) as client:
        await holder.exec_driver_sql("BEGIN IMMEDIATE")
        heartbeat = asyncio.create_task(pulse())
        first = asyncio.create_task(slack.service.deliver(client, delivery))
        contender = asyncio.create_task(slack.service.deliver(client, delivery))
        try:
            await asyncio.sleep(0.6)
            assert not first.done() and not contender.done() and not receiver.requests
        finally:
            await holder.rollback()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(first, contender), 5)
    assert len(ticks) >= 10 and max(b - a for a, b in itertools.pairwise(ticks)) < 0.3
    assert len(receiver.requests) == 1


async def test_source_review_claim_cancellation_cannot_release_a_competing_sender(slack, receiver, monkeypatch):
    service = slack.service
    source = service.store.create_run("Historical review", "", "demo", [], chat_enabled=True, user_id="google:owner")
    service.store.execute(
        """INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts)
           VALUES('TTEST0001','CTEST0001','1792100000.000',?,'1792100000.000')""", (source["id"],),
    )
    principal = await service.product.identity.require_current(slack.grant)
    await service.source_review_notice(
        {"run_id": source["id"]},
        {"team_id": "TTEST0001", "channel_id": "CTEST0001", "thread_ts": "1792100000.000",
         "user_id": "UTEST0001", "event_id": "REVIEW001", "message_ts": "1792100000.001"},
        principal, service.installation(),
    )
    entered, release = threading.Event(), threading.Event()
    execute = service.store.execute
    blocked = False

    def blocked_claim(sql, *args, **kwargs):
        nonlocal blocked
        if sql.startswith("UPDATE slack_outbox SET status='sending'") and not blocked:
            blocked = True
            entered.set()
            assert release.wait(5)
        return execute(sql, *args, **kwargs)

    monkeypatch.setattr(service.store, "execute", blocked_claim)
    receiver.outcome["hold"] = True
    async with httpx.AsyncClient(base_url=receiver.url, timeout=5) as client:
        first = asyncio.create_task(service.source_notices(client, set()))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        second = asyncio.create_task(service.source_notices(client, set()))
        try:
            assert await asyncio.to_thread(receiver.entered.wait, 2)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert service.store.rows("SELECT status FROM slack_outbox")[0]["status"] == "sending"
        finally:
            release.set()
            receiver.release.set()
            await asyncio.gather(first, second, return_exceptions=True)
    assert len(receiver.requests) == 1
    saved = service.store.rows("SELECT id,status FROM slack_outbox")[0]
    assert saved["status"] == "sent"
    payload = receiver.requests[0]
    assert payload["channel"] == "CTEST0001" and payload["thread_ts"] == "1792100000.000"
    assert payload["metadata"] == {
        "event_type": "moyai_delivery", "event_payload": {"id": "source:" + str(saved["id"])},
    }
    assert payload["parse"] == "none" and payload["link_names"] is False


@pytest.mark.parametrize("kind,expected", [
    ("private_channel", True), ("public_channel", True), ("mpim", False),
    ("external", False), ("unknown", False), ("replaced_installation", False),
])
async def test_automation_bridge_uses_provider_channel_privacy_not_id_prefix(slack, monkeypatch, kind, expected):
    service = slack.service
    bot = service.installation()

    async def metadata(method, url, **kwargs):
        assert method == "GET" and url.endswith("/conversations.info")
        assert kwargs["params"]["channel"] == "GTEST001"
        value = {
            "id": "GTEST001", "is_im": False, "is_mpim": kind == "mpim",
            "is_ext_shared": kind == "external",
            "is_group": kind != "public_channel", "is_channel": kind == "public_channel",
        }
        if kind == "unknown":
            value.pop("is_mpim")
        if kind == "replaced_installation":
            row = service.store.rows("SELECT encrypted FROM connections WHERE provider='slack'")[0]
            credentials = json.loads(service.product.security.decrypt(row["encrypted"]))
            service.connectors.save("slack", credentials, "Reauthorized")
        return {"ok": True, "channel": value}

    monkeypatch.setattr(service.connectors, "request", metadata)
    assert await service.automation_channel_allowed("GTEST001", bot) is expected


async def test_rate_limit_is_retryable_but_lost_response_stays_uncertain(slack, active):
    from moyai.models import DeliverySchedule
    delivery = await queued(slack, active)
    async def limited(request):
        return httpx.Response(429, headers={'Retry-After': '120'}, json={'ok': False, 'error': 'ratelimited'})
    async with httpx.AsyncClient(base_url='https://slack.test/', transport=httpx.MockTransport(limited)) as client:
        await slack.service.deliver(client, delivery)
    async with slack.runtime.database.sessions() as db:
        row = await db.get(Delivery, delivery.id)
        schedule = await db.get(DeliverySchedule, delivery.id)
        assert row.status == 'pending'
        assert schedule.next_attempt > time.time() + 100


async def test_uncertain_backlog_cannot_starve_pending_delivery(slack, active, monkeypatch):
    delivery = await queued(slack, active)
    async with slack.runtime.database.sessions.begin() as db:
        for index in range(25):
            db.add(Delivery(id=new_id(), identity=f'product-slack:uncertain:{index}',
                session_id=delivery.session_id, turn_id=delivery.turn_id, destination=delivery.destination,
                content=delivery.content, status='sending', created_at='2020-01-01'))
    async def no_stage(*args, **kwargs):
        return None
    seen = []
    async def capture(client, rows):
        seen.extend(rows)
        return set()
    monkeypatch.setattr(slack.service, 'stage', no_stage)
    monkeypatch.setattr(slack.service, '_deliver_batch', capture)
    monkeypatch.setattr(slack.service, 'source_notices', no_stage)
    await slack.service.pump(object())
    assert any(row.id == delivery.id for row in seen)
    assert sum(row.status == 'sending' for row in seen) <= 10


async def test_web_mirror_dedup_and_edit_supersedes_known_unsent_content(slack, active):
    from moyai.models import MessageSource
    await bind(slack, active)
    service = slack.service
    await service.stage_web_inputs()
    await service.stage_web_inputs()
    async with slack.runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery)))
        assert len(rows) == 1
        original = rows[0]
    assert await service.delivery_allowed(original)
    async with slack.runtime.database.sessions.begin() as db:
        source = await db.get(MessageSource, f'user:{active[1].id}')
        source.revision += 1
    assert not await service.delivery_allowed(original)
    await service.stage_web_inputs()
    async with slack.runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).order_by(Delivery.created_at)))
        assert len(rows) == 2
        assert await service.delivery_allowed(rows[-1])


async def test_delivery_retry_only_resets_explicit_rejections(slack, active, actor):
    from moyai.slack_delivery_status import retry, status
    delivery = await queued(slack, active)
    async with slack.runtime.database.sessions.begin() as db:
        (await db.get(Delivery, delivery.id)).status = 'sending'
    assert await retry(slack.runtime, actor, active[0].id) == {'retried': 0}
    async with slack.runtime.database.sessions.begin() as db:
        assert (await status(slack.runtime.database, db, active[0].id))['sending'] == 1
        (await db.get(Delivery, delivery.id)).status = 'rejected'
    assert await retry(slack.runtime, actor, active[0].id) == {'retried': 1}
    async with slack.runtime.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == 'pending'


async def test_delivery_status_distinguishes_file_rejections_without_exposing_raw_provider_details(slack, active):
    from moyai.slack_delivery_status import status
    from moyai.slack_file_delivery import SlackFileDelivery, UploadFile

    delivery = await queued(slack, active)
    database = slack.runtime.database
    async with database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        row.status = "rejected"
        row.content = database.security.seal(SlackFileDelivery.content(
            UploadFile(filename="debug.json", title="Debug", content=b"{}"), after="answer",
        ))
        row.receipt = database.security.seal({"error": "Slack files.getUploadURLExternal rejected the upload: missing_scope."})
    async with database.sessions() as db:
        result = await status(database, db, active[0].id)
        assert result == {"pending": 0, "sending": 0, "rejected": 1, "rejected_files": 1,
                          "file_error_codes": ["missing_scope"]}
    async with database.sessions.begin() as db:
        (await db.get(Delivery, delivery.id)).receipt = database.security.seal({"error": "secret provider detail"})
    async with database.sessions() as db:
        assert (await status(database, db, active[0].id))["file_error_codes"] == []


async def test_pending_inbox_is_encrypted_and_expires_without_admission(slack):
    inbox = slack.service.inbox
    inbox.enqueue('old-event', {'text': 'private pending message'})
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    slack.service.store.execute('UPDATE coordinator_hooks SET created_at=? WHERE id=?', (old, 'old-event'))
    inbox.prune()
    row = slack.service.store.rows('SELECT status,payload,detail FROM coordinator_hooks WHERE id=?', ('old-event',))[0]
    assert row['status'] == 'expired' and row['payload'] == ''
    assert 'private pending message' not in row['detail']
