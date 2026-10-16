"""Real delivery adapters with a Slack transport double; no external messages."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_product_slack import bind
from test_product_slack import bridge as canonical_bridge
from test_product_slack import slack as canonical_slack

from moyai.models import Delivery, Turn, User, new_id
from moyai.slack import deliver_one, reconcile_one
from moyai.slack_progress import anchor_identity, claim_delivery, stage_progress

bridge = canonical_bridge
slack = canonical_slack


async def queued(runtime, active, *, product, kind, text):
    session, turn, _ = active
    if kind == "anchor":
        identity = anchor_identity(turn.id, product)
    elif product:
        identity = f"product-slack:{turn.id}:{'completed:0' if kind == 'final' else 'progress:' + kind}"
    else:
        identity = f"slack-answer:{turn.id}" if kind == "final" else f"slack-commentary:{turn.id}:{kind}"
    destination = {"channel": "CTEST0001", "thread_ts": "1792100000.000"}
    if kind not in {"anchor", "final"}:
        destination["progress"] = True
    async with runtime.database.sessions.begin() as db:
        delivery = Delivery(
            id=new_id(), identity=identity, session_id=session.id, turn_id=turn.id,
            destination=json.dumps(destination),
            content=runtime.security.seal({"text": text} if product else text),
        )
        db.add(delivery)
    return delivery


def transport():
    requests, messages = [], {}

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ok": True, "messages": list(messages.values())})
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("chat.update"):
            assert body["ts"] in messages
            ts = body["ts"]
        else:
            ts = f"1792100000.{len(messages) + 1:03d}"
        messages[ts] = {**body, "ts": ts}
        return httpx.Response(200, json={"ok": True, "ts": ts})

    return requests, messages, handle


@pytest.mark.parametrize("product", [False, True])
async def test_one_reply_is_edited_from_progress_to_final(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    update = await queued(runtime, active, product=product, kind="one", text="Checking the handler.")
    final = await queued(runtime, active, product=product, kind="final", text="Final verdict.")
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        await deliver(client, update)
        async with runtime.database.sessions.begin() as db:
            (await db.get(Turn, active[1].id)).status = "completed"
        await deliver(client, final)
    assert [path for path, _ in requests] == ["/chat.postMessage", "/chat.update", "/chat.update"]
    assert len(messages) == 1 and next(iter(messages.values()))["text"] == "Final verdict."
    assert requests[-1][1]["blocks"] == []


@pytest.mark.parametrize("product", [False, True])
async def test_late_progress_cannot_overwrite_final(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    progress = await queued(runtime, active, product=product, kind="one", text="Checking.")
    late = await queued(runtime, active, product=product, kind="late", text="Stale update.")
    final = await queued(runtime, active, product=product, kind="final", text="Final verdict.")
    requests, messages, handle = transport()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        if request.method == "POST" and json.loads(request.content)["text"] == "Checking.":
            entered.set()
            await release.wait()
        return await handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        first = asyncio.create_task(deliver(client, progress))
        await entered.wait()
        async with runtime.database.sessions.begin() as db:
            (await db.get(Turn, active[1].id)).status = "completed"
        await deliver(client, final)
        assert len(requests) == 1
        release.set()
        await first
        await deliver(client, final)
        await deliver(client, late)
    assert len(messages) == 1 and next(iter(messages.values()))["text"] == "Final verdict."
    assert len(requests) == 3
    async with runtime.database.sessions() as db:
        assert (await db.get(Delivery, late.id)).status == "suppressed"


@pytest.mark.parametrize("product", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_ambiguous_update_is_reconciled_before_final(runtime, active, slack, product, cancel):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    progress = await queued(runtime, active, product=product, kind="one", text="Checking.")
    final = await queued(runtime, active, product=product, kind="final", text="Final verdict.")
    requests, messages, handle = transport()
    entered = asyncio.Event()

    async def lose_ack(request):
        result = await handle(request)
        if request.method == "POST" and json.loads(request.content)["text"] == "Checking.":
            entered.set()
            if cancel:
                await asyncio.Event().wait()
            raise httpx.ReadTimeout("lost update acknowledgement")
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(lose_ack), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        task = asyncio.create_task(deliver(client, progress))
        await entered.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        async with runtime.database.sessions.begin() as db:
            (await db.get(Turn, active[1].id)).status = "completed"
            saved = await db.get(Delivery, progress.id)
            assert saved.status == "sending"
            saved.claimed_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        await deliver(client, final)
        assert len(requests) == 2
        await reconcile_one(runtime, client, saved)
        await deliver(client, final)
    assert len(messages) == 1 and next(iter(messages.values()))["text"] == "Final verdict."
    assert len(requests) == 3


@pytest.mark.parametrize("product", [False, True])
async def test_fast_answer_skips_unsent_placeholder(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    final = await queued(runtime, active, product=product, kind="final", text="Yep, I'm here.")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, active[1].id)).status = "completed"
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        await deliver(client, final)
    assert len(requests) == len(messages) == 1
    assert requests[0][1]["text"] == "Yep, I'm here."


@pytest.mark.parametrize("product", [False, True])
async def test_revoked_sender_cannot_replace_the_reply(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Received.")
    final = await queued(runtime, active, product=product, kind="final", text="Final verdict.")
    requests, _, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        if product:
            slack.authority["current"] = False
        else:
            async with runtime.database.sessions.begin() as db:
                (await db.get(User, active[1].actor_id)).enabled = False
        await deliver(client, final)
    assert len(requests) == 1


async def test_progress_staging_coalesces_native_updates(runtime, active, slack, monkeypatch):
    await bind(slack, active)
    await queued(runtime, active, product=True, kind="anchor", text="Received.")
    sequence = 0

    async def read_progress(runtime, turn):
        nonlocal sequence
        sequence += 1
        return [{"id": str(sequence), "text": f"Update {sequence}", "phase": "commentary"}]

    monkeypatch.setattr("moyai.slack_progress.turn_progress", read_progress)
    await stage_progress(runtime, product=True)
    await stage_progress(runtime, product=True)
    assert sequence == 1


async def test_different_delivery_rows_share_one_turn_reservation(runtime, active, slack):
    await bind(slack, active)
    first = await queued(runtime, active, product=True, kind="one", text="First update.")
    second = await queued(runtime, active, product=True, kind="two", text="Second update.")
    claimed = await asyncio.gather(
        claim_delivery(runtime, first, product=True),
        claim_delivery(runtime, second, product=True),
    )
    assert sorted(claimed) == [False, True]
