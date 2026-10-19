"""Slack protocol tests with a transport double; these are not live Slack proof."""

import asyncio
import base64
import hashlib
import hmac
import itertools
import json
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import event, func, select

from moyai.api import create_app
from moyai.models import Delivery, Session, SlackInbox, SlackThread, Turn, User, new_id
from moyai.slack import admit_one, deliver_one, reconcile_one, stage_deliveries


async def test_signed_ingress_deduplicates_and_admits_linked_actor(runtime, actor):
    runtime.settings.slack_signing_secret = SecretStr("local-signature-secret")
    runtime.settings.slack_team_id = "TTEST"
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(User, actor.id)
        saved.slack_user_id = "UTEST"
    body = json.dumps({
        "type": "event_callback", "event_id": "EONE", "team_id": "TTEST",
        "event": {"type": "app_mention", "channel": "CTEST", "user": "UTEST",
                  "ts": "123.456", "text": "Explain this test."},
    }).encode()
    stamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        b"local-signature-secret", b"v0:" + stamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://test",
    ) as client:
        invalid = await client.post("/hooks/slack", content=body)
        assert invalid.status_code == 401
        for _ in range(2):
            response = await client.post("/hooks/slack", content=body, headers={
                "x-slack-signature": signature, "x-slack-request-timestamp": stamp,
            })
            assert response.status_code == 200
    async with runtime.database.sessions() as db:
        rows = list((await db.scalars(select(SlackInbox))).all())
        assert len(rows) == 1
    await admit_one(runtime, rows[0])
    await admit_one(runtime, rows[0])
    async with runtime.database.sessions() as db:
        turns = list((await db.scalars(select(Turn))).all())
        assert len(turns) == 1 and turns[0].actor_id == actor.id
        assert await db.scalar(select(SlackThread.id))
        session = await db.get(Session, turns[0].session_id)
        assert session.surface == "slack" and session.visibility == "private"
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turns[0].id)
    assert prepared.scope.memory_namespace.startswith(f"slack/{session.id}/")


@pytest.mark.parametrize("order", [("app_mention", "message"), ("message", "app_mention")])
async def test_same_slack_message_in_two_event_envelopes_admits_one_turn(runtime, actor, order):
    async with runtime.database.sessions.begin() as db:
        (await db.get(User, actor.id)).slack_user_id = "UTEST"

    async def admit(event_type, timestamp, thread_ts=None):
        inbox = SlackInbox(id=new_id(), payload=runtime.security.seal({
            "team_id": "TTEST", "event_id": new_id(),
            "event": {
                "type": event_type, "channel": "CTEST", "user": "UTEST",
                "ts": timestamp, "text": "Same user message.",
                **({"thread_ts": thread_ts} if thread_ts else {}),
            },
        }))
        async with runtime.database.sessions.begin() as db:
            db.add(inbox)
        await admit_one(runtime, inbox)

    for kind in order:
        await admit(kind, "123.456")
    # Thread replies can also arrive in either order. Different timestamps are
    # distinct messages even when the actor and text are identical.
    for timestamp in ("123.457", "123.458"):
        for kind in order:
            await admit(kind, timestamp, "123.456")
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 3
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 1


async def test_delivery_staging_drains_backlog_beyond_hundred_newer_completions(runtime, active):
    session, original, _ = active
    async with runtime.database.sessions.begin() as db:
        db.add(SlackThread(
            id=new_id(), team_id="TTEST", channel_id="CTEST", thread_ts="123.456", session_id=session.id,
        ))
        (await db.get(Turn, original.id)).status = "completed"
        for index in range(205):
            turn = Turn(
                id=new_id(), session_id=session.id, actor_id=original.actor_id,
                request_key=f"backlog-{index}", request_fingerprint=str(index),
                prompt=runtime.security.seal("Test input."), model=original.model,
                workflow_id=f"backlog-{index}", status="completed",
                result=runtime.security.seal("Answer." if index % 2 else ""),
            )
            db.add(turn)
    for _ in range(4):
        await stage_deliveries(runtime)
    async with runtime.database.sessions() as db:
        deliveries = list((await db.scalars(select(Delivery))).all())
        assert len(deliveries) == 206
        assert len({row.turn_id for row in deliveries}) == 206
        assert original.id in {row.turn_id for row in deliveries}
        assert all(runtime.security.unseal(row.content) for row in deliveries)


async def make_delivery(runtime, active):
    session, turn, _ = active
    async with runtime.database.sessions.begin() as db:
        delivery = Delivery(
            id=new_id(), identity="one-delivery", session_id=session.id, turn_id=turn.id,
            destination=json.dumps({"channel": "CTEST", "thread_ts": "123.456"}),
            content=runtime.security.seal("Verified result."),
        )
        db.add(delivery)
    return delivery


async def test_delivery_ownership_survives_network_await(runtime, active):
    delivery = await make_delivery(runtime, active)
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        entered.set()
        await finish.wait()
        return httpx.Response(200, json={"ok": True, "ts": "123.999"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        first = asyncio.create_task(deliver_one(runtime, client, delivery))
        await entered.wait()
        await deliver_one(runtime, client, delivery)
        finish.set()
        await first
    assert calls == ["/chat.postMessage"]
    async with runtime.database.sessions() as db:
        saved = await db.get(Delivery, delivery.id)
        assert saved.status == "sent" and "123.999" in runtime.security.unseal(saved.receipt).decode()


async def test_failed_turn_posts_error_then_uploads_debug_file(runtime, active, monkeypatch):
    session, turn, _ = active
    error = "Codex stopped (usageLimitExceeded)"
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Turn, turn.id)
        saved.status = "failed"
        saved.error = error
        saved.finished_at = datetime.now(UTC).isoformat()
        db.add(SlackThread(
            id=new_id(), team_id="TTEST", channel_id="CTEST", thread_ts="123.456", session_id=session.id,
        ))
    await stage_deliveries(runtime)
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).where(Delivery.turn_id == turn.id)))
    deliveries = {row.identity: row for row in rows}
    message = deliveries[f"slack-answer:{turn.id}"]
    debug = deliveries[f"slack-answer:{turn.id}:debug"]
    message_content = json.loads(runtime.security.unseal(message.content))
    file_content = json.loads(runtime.security.unseal(debug.content))
    debug_bytes = base64.b64decode(file_content["upload"]["content"])

    assert error in message_content["text"]
    assert "Debug log attached." not in message_content["text"]
    assert file_content["after"] == message.identity

    from agentchat.channels.slack import files

    events = []

    async def upload_bytes(url, raw):
        events.append(("upload_bytes", url, raw))

    monkeypatch.setattr(files, "upload_bytes", upload_bytes)

    async def handle(request):
        method = request.url.path.rsplit("/", 1)[-1]
        payload = (
            dict(parse_qsl(request.content.decode(), strict_parsing=True))
            if method == "files.getUploadURLExternal"
            else json.loads(request.content)
        )
        events.append((method, payload))
        if method == "chat.postMessage":
            return httpx.Response(200, json={"ok": True, "channel": "CTEST", "ts": "123.457"})
        if method == "files.getUploadURLExternal":
            return httpx.Response(200, json={
                "ok": True, "file_id": "FDEBUG001",
                "upload_url": "https://files.slack.com/upload/FDEBUG001",
            })
        if method == "files.completeUploadExternal":
            return httpx.Response(200, json={
                "ok": True, "files": [{
                    "id": "FDEBUG001", "permalink": "https://slack.test/files/FDEBUG001",
                }],
            })
        raise AssertionError(f"Unexpected Slack API method: {method}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="https://slack.test/",
    ) as client:
        await deliver_one(runtime, client, message)
        await deliver_one(runtime, client, debug)

    assert [event[0] for event in events] == [
        "chat.postMessage", "files.getUploadURLExternal", "upload_bytes", "files.completeUploadExternal",
    ]
    assert events[0][1]["thread_ts"] == "123.456"
    assert events[1][1] == {"filename": f"moyai-debug-{turn.id}.json", "length": str(len(debug_bytes))}
    assert events[2][2] == debug_bytes
    assert events[3][1]["channel_id"] == "CTEST"
    assert events[3][1]["thread_ts"] == "123.456"
    async with runtime.database.sessions() as db:
        sent = await db.get(Delivery, debug.id)
        receipt = json.loads(runtime.security.unseal(sent.receipt))
    assert sent.status == "sent"
    assert receipt["files"] == [{
        "id": "FDEBUG001", "permalink": "https://slack.test/files/FDEBUG001",
    }]


async def test_lost_send_ack_is_reconciled_without_reposting(runtime, active):
    delivery = await make_delivery(runtime, active)
    calls = []

    async def handler(request):
        calls.append(request.method)
        if request.method == "POST":
            raise httpx.ReadTimeout("Acknowledgment lost after dispatch.", request=request)
        return httpx.Response(200, json={
            "ok": True, "messages": [{
                "ts": "123.999", "metadata": {"event_payload": {"id": delivery.id}},
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        await deliver_one(runtime, client, delivery)
        await deliver_one(runtime, client, delivery)
        async with runtime.database.sessions.begin() as db:
            saved = await db.get(Delivery, delivery.id)
            assert saved.status == "sending"
            saved.claimed_at = (datetime.now(UTC) - timedelta(seconds=40)).isoformat()
        await reconcile_one(runtime, client, delivery)
    assert calls == ["POST", "GET"]
    async with runtime.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "sent"


async def test_cancel_after_receipt_commit_never_reclassifies_or_reposts(runtime, active):
    delivery = await make_delivery(runtime, active)
    posted = False
    cancelled = False
    calls = 0

    async def handler(request):
        nonlocal posted, calls
        calls += 1
        posted = True
        return httpx.Response(200, json={"ok": True, "ts": "123.999"})

    session_type = runtime.database.sessions.class_.sync_session_class

    def after_commit(_session):
        nonlocal cancelled
        if posted and not cancelled:
            cancelled = True
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    event.listen(session_type, "after_commit", after_commit)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://slack.test/"
        ) as client:
            task = asyncio.create_task(deliver_one(runtime, client, delivery))
            with pytest.raises(asyncio.CancelledError):
                await task
            await deliver_one(runtime, client, delivery)
    finally:
        event.remove(session_type, "after_commit", after_commit)
    assert cancelled and calls == 1
    async with runtime.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "sent"


async def test_sqlite_writer_wait_does_not_block_slack_event_loop(runtime, active):
    delivery = await make_delivery(runtime, active)
    ticks = []
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "ts": "123.999"})

    async def heartbeat():
        while True:
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.02)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://slack.test/"
    ) as client, runtime.database.engine.connect() as owner:
        await owner.exec_driver_sql("BEGIN IMMEDIATE")
        pulse = asyncio.create_task(heartbeat())
        task = asyncio.create_task(deliver_one(runtime, client, delivery))
        try:
            await asyncio.sleep(0.65)
            assert not task.done() and calls == []
        finally:
            await owner.rollback()
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
        await asyncio.wait_for(task, 5)
    assert len(ticks) >= 10
    assert max(b - a for a, b in itertools.pairwise(ticks)) < 0.3
    assert calls == ["/chat.postMessage"]


async def test_file_rejection_preserves_safe_provider_error_before_upload():
    from unittest.mock import AsyncMock

    from agentchat.models import UploadFile

    from moyai.slack_file_delivery import SlackFileDelivery

    before = AsyncMock()
    for code, expected in [("missing_scope", "missing_scope"), ("invalid_arguments", "invalid_arguments"),
                           ("private token value", "unknown_error")]:
        async def handle(request, code=code):
            assert request.url.path == "/files.getUploadURLExternal"
            return httpx.Response(200, json={"ok": False, "error": code})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
            with pytest.raises(ValueError, match=f"files.getUploadURLExternal rejected the upload: {expected}"):
                await SlackFileDelivery.send(client, SlackFileDelivery.content(
                    UploadFile(filename="debug.json", content=b"{}"), after="failure"),
                    {"channel": "CTEST", "thread_ts": "1.2"}, objects=AsyncMock(), headers=None, before_send=before)


async def test_file_upload_uses_form_data_for_upload_url(monkeypatch):
    from unittest.mock import AsyncMock

    from agentchat.channels.slack import files
    from agentchat.models import UploadFile

    from moyai.slack_file_delivery import SlackFileDelivery

    file_content = b'{"trace":true}'
    events = []

    async def upload_bytes(url, raw):
        events.append(("upload_bytes", url, raw))

    monkeypatch.setattr(files, "upload_bytes", upload_bytes)

    async def handle(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "files.getUploadURLExternal":
            assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            assert dict(parse_qsl(request.content.decode(), strict_parsing=True)) == {
                "filename": "debug.json",
                "length": str(len(file_content)),
            }
            events.append(method)
            return httpx.Response(200, json={
                "ok": True,
                "file_id": "F1",
                "upload_url": "https://files.slack.com/upload/v1/F1",
            })
        if method == "files.completeUploadExternal":
            assert request.headers["content-type"].startswith("application/json")
            payload = json.loads(request.content)
            assert payload["channel_id"] == "CTEST"
            assert payload["thread_ts"] == "1.2"
            assert payload["files"] == [{"id": "F1", "title": "debug.json"}]
            events.append(method)
            return httpx.Response(200, json={
                "ok": True,
                "files": [{"id": "F1", "permalink": "https://slack.test/files/F1"}],
            })
        raise AssertionError(f"Unexpected Slack API method: {method}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        base_url="https://slack.test/",
    ) as client:
        receipt = await SlackFileDelivery.send(
            client,
            SlackFileDelivery.content(
                UploadFile(filename="debug.json", content=file_content, title="debug.json"),
                after="failure",
            ),
            {"channel": "CTEST", "thread_ts": "1.2"},
            objects=AsyncMock(),
            headers=None,
            before_send=AsyncMock(),
        )

    assert events == [
        "files.getUploadURLExternal",
        ("upload_bytes", "https://files.slack.com/upload/v1/F1", file_content),
        "files.completeUploadExternal",
    ]
    assert receipt == {"files": [{"id": "F1", "permalink": "https://slack.test/files/F1"}]}
