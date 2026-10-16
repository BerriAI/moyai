"""Slack transport regression tests; these do not claim live Slack delivery."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from moyai.models import Delivery, Event, Turn, new_id
from moyai.slack_progress import sync_progress


async def anchor(runtime, active):
    session, turn, _ = active
    async with runtime.database.sessions.begin() as db:
        row = Delivery(
            id=new_id(), identity=f"product-slack:{turn.id}:running:0",
            session_id=session.id, turn_id=turn.id,
            destination=json.dumps({"channel": "CTEST", "thread_ts": "1.0"}),
            content=runtime.security.seal({"text": "Moyai is working."}),
            status="sent", receipt=runtime.security.seal({"ok": True, "ts": "1.1"}),
        )
        db.add(row)
    return row


async def allow(delivery):
    return True


async def progress(runtime, turn):
    return [{"id": "progress", "text": "I found the relevant handler.", "phase": "commentary"}]


async def test_concurrent_progress_updates_reuse_one_confirmed_timestamp(runtime, active, monkeypatch):
    row = await anchor(runtime, active)
    monkeypatch.setattr("moyai.slack_progress.turn_progress", progress)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True, "ts": "1.1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        first = asyncio.create_task(sync_progress(runtime, client, product=True, allowed=allow))
        await entered.wait()
        await sync_progress(runtime, client, product=True, allowed=allow)
        release.set()
        await first
    assert len(requests) == 1
    assert requests[0][0] == "/chat.update" and requests[0][1]["ts"] == "1.1"
    async with runtime.database.sessions() as db:
        saved = await db.get(Delivery, row.id)
        assert saved.status == "sent"
        assert json.loads(runtime.security.unseal(saved.content))["text"] == "I found the relevant handler."


@pytest.mark.parametrize("cancel", [False, True])
async def test_lost_update_ack_preserves_post_receipt_and_final_outbox(runtime, active, monkeypatch, cancel):
    row = await anchor(runtime, active)
    monkeypatch.setattr("moyai.slack_progress.turn_progress", progress)
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise httpx.ReadTimeout("lost acknowledgement")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        task = asyncio.create_task(sync_progress(runtime, client, product=True, allowed=allow))
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
    async with runtime.database.sessions() as db:
        saved = await db.get(Delivery, row.id)
        assert saved.status == "sent"
        assert json.loads(runtime.security.unseal(saved.receipt))["ts"] == "1.1"
        assert json.loads(runtime.security.unseal(saved.content))["text"] == "Moyai is working."


async def test_terminal_progress_settles_once_without_posting_another_answer(runtime, active):
    row = await anchor(runtime, active)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, row.turn_id)).status = "completed"
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        await sync_progress(runtime, client, product=True, allowed=allow)
        async with runtime.database.sessions.begin() as db:
            (await db.get(Delivery, row.id)).claimed_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        await sync_progress(runtime, client, product=True, allowed=allow)
    assert calls == ["/chat.update"]
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(Event.id).where(Event.identity == f"slack-progress-finished:{row.id}"))


async def test_authority_is_rechecked_after_token_refresh(runtime, active, monkeypatch):
    await anchor(runtime, active)
    monkeypatch.setattr("moyai.slack_progress.turn_progress", progress)
    authorized = True

    async def allowed(delivery):
        return authorized

    async def headers():
        nonlocal authorized
        authorized = False
        return {"Authorization": "Bearer synthetic-token"}

    async def handler(request):
        pytest.fail("Revoked commentary must not reach Slack.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://slack.test/") as client:
        await sync_progress(runtime, client, product=True, allowed=allowed, headers=headers)
