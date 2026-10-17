"""Steering exercises actual admission/outbox code with a local Slack transport."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from test_product_slack import bind
from test_slack_progress import bridge as canonical_bridge
from test_slack_progress import queued, transport
from test_slack_progress import slack as canonical_slack

from moyai.commentary import Commentary
from moyai.contracts import NewMessage
from moyai.models import Delivery, ProductTurnAuthority, Turn, User, new_id, now
from moyai.slack import deliver_one, reconcile_one, stage_deliveries
from moyai.slack_progress import anchor_identity, stage_progress

bridge = canonical_bridge
slack = canonical_slack


async def steer(runtime, active, slack):
    session, root, _ = active
    async with runtime.database.sessions() as db:
        actor = await db.get(User, root.actor_id)
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Also verify the word count.", mode="asap", request_key=new_id(),
    ))
    assert turn.parent_turn == root.id
    async with runtime.database.sessions.begin() as db:
        db.add(ProductTurnAuthority(
            turn_id=turn.id, actor_id=turn.actor_id, authority_version=slack.grant.authority_version,
        ))
    return turn


async def pending(runtime, deliver, client):
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).where(
            Delivery.status == "pending",
        ).order_by(Delivery.created_at)))
    for row in rows:
        await deliver(client, row)


@pytest.mark.parametrize("product", [False, True])
async def test_steering_freezes_previous_slot_and_final_replaces_new_slot(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    update = await queued(runtime, active, product=product, kind="one", text="Checking the file.")
    stale = await queued(runtime, active, product=product, kind="stale", text="Never replace the preserved update.")
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        await deliver(client, update)
        earlier_ts = next(iter(messages))
        correction = await steer(runtime, active, slack)
        await Commentary(runtime.database).save(active[2].scope, 2, [{
            "id": "unused", "text": "Checking the requested word count.", "created_at": now(),
        }])
        await stage_progress(runtime, product=product)
        await pending(runtime, deliver, client)
        assert len(messages) == 2
        assert messages[earlier_ts]["text"] == "Checking the file."
        assert "Checking the requested word count." in list(messages.values())[-1]["text"]
        async with runtime.database.sessions.begin() as db:
            root = await db.get(Turn, active[1].id)
            root.status, root.finished_at = "completed", now()
            root.result = runtime.security.seal("Final result: three words.")
        if product:
            await slack.service.stage()
        else:
            await stage_deliveries(runtime)
        await pending(runtime, deliver, client)
        assert len(messages) == 2
        assert messages[earlier_ts]["text"] == "Checking the file."
        assert list(messages.values())[-1]["text"].startswith("Final result: three words.")
        assert sum(path == "/chat.postMessage" for path, _ in requests) == 2
    async with runtime.database.sessions() as db:
        assert (await db.get(Delivery, stale.id)).status == "suppressed"
        anchor = await db.scalar(select(Delivery).where(Delivery.identity == anchor_identity(correction.id, product)))
        assert anchor.status == "sent"


@pytest.mark.parametrize("product", [False, True])
async def test_steering_between_target_read_and_claim_cannot_edit_old_slot(runtime, active, slack, product, monkeypatch):
    from moyai import slack_progress

    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Last visible update.")
    stale = await queued(runtime, active, product=product, kind="stale", text="Late stale update.")
    entered, release = asyncio.Event(), asyncio.Event()
    original = slack_progress.reply_target

    async def pause(runtime, delivery, *, product):
        result = await original(runtime, delivery, product=product)
        if delivery.id == stale.id:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(slack_progress, "reply_target", pause)
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        task = asyncio.create_task(deliver(client, stale))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            await steer(runtime, active, slack)
        finally:
            release.set()
            await task
    assert len(requests) == 1
    assert next(iter(messages.values()))["text"] == "Last visible update."


@pytest.mark.parametrize("product", [False, True])
async def test_ambiguous_previous_update_is_reconciled_before_new_slot(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    update = await queued(runtime, active, product=product, kind="one", text="Last visible update.")
    requests, messages, handle = transport()

    async def lose_ack(request):
        result = await handle(request)
        if request.method == "POST" and json.loads(request.content)["text"] == "Last visible update.":
            raise httpx.ReadTimeout("Lost acknowledgement")
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(lose_ack), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        await deliver(client, update)
        await steer(runtime, active, slack)
        await stage_progress(runtime, product=product)
        await pending(runtime, deliver, client)
        assert len(messages) == 1
        async with runtime.database.sessions.begin() as db:
            saved = await db.get(Delivery, update.id)
            assert saved.status == "sending"
            saved.claimed_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        await reconcile_one(runtime, client, saved)
        await pending(runtime, deliver, client)
        assert len(messages) == 2
        assert next(iter(messages.values()))["text"] == "Last visible update."
        assert sum(path == "/chat.postMessage" for path, _ in requests) == 2


@pytest.mark.parametrize("product", [False, True])
async def test_fast_final_after_steering_posts_once_without_touching_previous_slot(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Preserved progress.")
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        await steer(runtime, active, slack)
        await stage_progress(runtime, product=product)
        async with runtime.database.sessions.begin() as db:
            root = await db.get(Turn, active[1].id)
            root.status, root.finished_at = "completed", now()
            root.result = runtime.security.seal("Paragraph with useful results.\n" * 300)
        if product:
            await slack.service.stage()
        else:
            await stage_deliveries(runtime)
        await pending(runtime, deliver, client)
        assert len(messages) == 2
        assert next(iter(messages.values()))["text"] == "Preserved progress."
        final = list(messages.values())[-1]
        assert "useful results" in final["text"]
        assert len([block for block in final["blocks"] if block["type"] == "section"]) > 1
        assert sum(path == "/chat.postMessage" for path, _ in requests) == 2
