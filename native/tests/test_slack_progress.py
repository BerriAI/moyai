"""Real delivery adapters with a Slack transport double; no external messages."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from test_product_slack import bind
from test_product_slack import bridge as canonical_bridge
from test_product_slack import slack as canonical_slack

from moyai.models import Delivery, Turn, User, new_id
from moyai.slack import deliver_one, reconcile_one, stage_deliveries
from moyai.slack_progress import anchor_identity, claim_delivery, legacy_anchor_identity, stage_progress

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
    if kind != "final":
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
    initial = await queued(runtime, active, product=product, kind="anchor", text="Inspecting the relevant code.")
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


@pytest.mark.parametrize(("product", "status"), [
    (False, "saving"), (False, "completed"), (True, "completed"),
])
async def test_late_progress_cannot_overwrite_final(runtime, active, slack, product, status):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Inspecting the relevant code.")
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
            (await db.get(Turn, active[1].id)).status = status
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


@pytest.mark.parametrize(("product", "status"), [
    (False, "saving"), (False, "completed"), (True, "completed"),
])
@pytest.mark.parametrize("cancel", [False, True])
async def test_ambiguous_update_is_reconciled_before_final(runtime, active, slack, product, status, cancel):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Inspecting the relevant code.")
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
            (await db.get(Turn, active[1].id)).status = status
            saved = await db.get(Delivery, progress.id)
            assert saved.status == "sending"
            saved.claimed_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        await deliver(client, final)
        assert len(requests) == 2
        await reconcile_one(runtime, client, saved)
        await deliver(client, final)
    assert len(messages) == 1 and next(iter(messages.values()))["text"] == "Final verdict."
    assert len(requests) == 3


@pytest.mark.parametrize(("product", "status"), [
    (False, "saving"), (False, "completed"), (True, "completed"),
])
async def test_fast_answer_skips_unsent_placeholder(runtime, active, slack, product, status):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Inspecting the relevant code.")
    final = await queued(runtime, active, product=product, kind="final", text="Yep, I'm here.")
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, active[1].id)).status = status
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, initial)
        await deliver(client, final)
    assert len(requests) == len(messages) == 1
    assert requests[0][1]["text"] == "Yep, I'm here."


async def test_saving_transition_after_preflight_blocks_progress_at_claim(runtime, active, slack, monkeypatch):
    from moyai import slack_progress

    await bind(slack, active)
    initial = await queued(runtime, active, product=False, kind="anchor", text="Inspecting the relevant code.")
    late = await queued(runtime, active, product=False, kind="late", text="Stale update.")
    requests, messages, handle = transport()
    entered, release = asyncio.Event(), asyncio.Event()
    original = slack_progress.reply_target

    async def pause_before_claim(runtime, delivery, *, product):
        result = await original(runtime, delivery, product=product)
        if delivery.id == late.id:
            entered.set()
            await release.wait()
        return result

    monkeypatch.setattr(slack_progress, "reply_target", pause_before_claim)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver_one(runtime, client, initial)
        pending = asyncio.create_task(deliver_one(runtime, client, late))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with runtime.database.sessions.begin() as db:
                turn = await db.get(Turn, active[1].id)
                turn.status = "saving"
                turn.result = runtime.security.seal("Final verdict.")
            await stage_deliveries(runtime)
            async with runtime.database.sessions() as db:
                final = await db.scalar(select(Delivery).where(
                    Delivery.identity == f"slack-answer:{active[1].id}",
                ))
            assert final is not None
            await deliver_one(runtime, client, final)
        finally:
            release.set()
            await pending
    assert len(requests) == 2
    assert len(messages) == 1
    assert next(iter(messages.values()))["text"].startswith("Final verdict.")
    async with runtime.database.sessions.begin() as db:
        assert (await db.get(Delivery, late.id)).status == "suppressed"
        (await db.get(Turn, active[1].id)).status = "completed"
    await stage_deliveries(runtime)
    async with runtime.database.sessions() as db:
        answers = list(await db.scalars(select(Delivery).where(
            Delivery.identity == f"slack-answer:{active[1].id}",
        )))
        assert len(answers) == 1 and answers[0].status == "sent"


async def test_product_saving_is_silent_until_completion(runtime, active, slack):
    await bind(slack, active)
    initial = await queued(runtime, active, product=True, kind="anchor", text="Inspecting the relevant code.")
    requests, messages, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await slack.service.deliver(client, initial)
        for status in ("saving", "completed"):
            async with runtime.database.sessions.begin() as db:
                turn = await db.get(Turn, active[1].id)
                turn.status = status
                turn.result = runtime.security.seal("Final verdict.")
            await slack.service.stage()
            async with runtime.database.sessions() as db:
                notices = list(await db.scalars(select(Delivery).where(
                    Delivery.turn_id == active[1].id, Delivery.status == "pending",
                ).order_by(Delivery.created_at)))
            for notice in notices:
                await slack.service.deliver(client, notice)
            expected = "Inspecting the relevant code." if status == "saving" else "Final verdict."
            assert next(iter(messages.values()))["text"].startswith(expected)
    assert len(messages) == 2 and len(requests) == 3  # The original web input is mirrored once.
    assert sum("(from Moyai)" in message["text"] for message in messages.values()) == 1


@pytest.mark.parametrize("product", [False, True])
async def test_revoked_sender_cannot_replace_the_reply(runtime, active, slack, product):
    await bind(slack, active)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    initial = await queued(runtime, active, product=product, kind="anchor", text="Inspecting the relevant code.")
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
    sequence = 0

    async def read_progress(turn):
        nonlocal sequence
        sequence += 1
        return {"messages": [{"id": str(sequence), "text": f"Update {sequence}", "phase": "commentary"}], "subagents": []}

    monkeypatch.setattr(runtime.work_progress, "read", read_progress)
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


@pytest.mark.parametrize("product", [False, True])
async def test_no_reply_until_authored_progress_or_answer(runtime, active, slack, product, monkeypatch):
    await bind(slack, active)
    messages = []

    async def read_progress(turn):
        return {"messages": messages, "subagents": []}

    monkeypatch.setattr(runtime.work_progress, "read", read_progress)
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    for status in ("queued", "running", "saving"):
        async with runtime.database.sessions.begin() as db:
            (await db.get(Turn, active[1].id)).status = status
        if product:
            await slack.service.stage()
        await stage_progress(runtime, product=product)
        async with runtime.database.sessions() as db:
            rows = list(await db.scalars(select(Delivery)))
        # Product mode mirrors authored web input, but creates no bot chatter.
        assert all(json.loads(row.destination).get("input") for row in rows)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, active[1].id)).status = "running"
    messages.append({"id": "first-real-update", "text": "Inspecting the handler."})
    await stage_progress(runtime, product=product)
    await stage_progress(runtime, product=product)
    async with runtime.database.sessions() as db:
        updates = list(await db.scalars(select(Delivery).where(Delivery.destination.contains('"progress": true'))))
    assert len(updates) == 1
    requests, posted, handle = transport()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await deliver(client, updates[0])
        async with runtime.database.sessions.begin() as db:
            (await db.get(Turn, active[1].id)).status = "completed"
        final = await queued(runtime, active, product=product, kind="final", text="Final verdict.")
        await deliver(client, final)
    assert [path for path, _ in requests] == ["/chat.postMessage", "/chat.update"]
    assert requests[0][1]["text"].startswith("Inspecting the handler.")
    assert len(posted) == 1 and next(iter(posted.values()))["text"] == "Final verdict."


@pytest.mark.parametrize("product,kind", [(False, "anchor"), *[(True, kind) for kind in ("anchor", "queued", "running", "saving")]])
async def test_upgrade_suppresses_unsent_lifecycle_chatter(runtime, active, slack, product, kind):
    from moyai.slack_progress import skip_obsolete

    await bind(slack, active)
    row = await queued(runtime, active, product=product, kind="anchor", text="Legacy placeholder.")
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Delivery, row.id)
        saved.identity = (legacy_anchor_identity(active[1].id, product) if kind == "anchor"
                          else f"product-slack:{active[1].id}:{kind}:0")
        saved.destination = json.dumps({"channel": "CTEST0001", "thread_ts": "1792100000.000"})
    async with runtime.database.sessions() as db:
        saved = await db.get(Delivery, row.id)
    assert await skip_obsolete(runtime, saved, product=product)
    async with runtime.database.sessions() as db:
        assert (await db.get(Delivery, row.id)).status == "suppressed"


@pytest.mark.parametrize("product", [False, True])
@pytest.mark.parametrize("legacy_status", ["sent", "sending"])
async def test_upgrade_reuses_legacy_reply_after_reconciliation(runtime, active, slack, product, legacy_status, monkeypatch):
    await bind(slack, active)
    row = await queued(runtime, active, product=product, kind="anchor", text="Legacy placeholder.")
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Delivery, row.id)
        saved.identity = legacy_anchor_identity(active[1].id, product)
        saved.status = legacy_status
        saved.receipt = runtime.security.seal({"ts": "1792100000.001"})

    async def read_progress(turn):
        return {"messages": [{"id": "real-update", "text": "Checking the handler."}], "subagents": []}

    monkeypatch.setattr(runtime.work_progress, "read", read_progress)
    await stage_progress(runtime, product=product)
    async with runtime.database.sessions() as db:
        update = await db.scalar(select(Delivery).where(Delivery.id != row.id))
    requests, messages, handle = transport()
    messages["1792100000.001"] = {"text": "Legacy placeholder.", "ts": "1792100000.001"}
    deliver = slack.service.deliver if product else lambda client, row: deliver_one(runtime, client, row)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        if legacy_status == "sending":
            await deliver(client, update)
            assert requests == []
            async with runtime.database.sessions.begin() as db:
                (await db.get(Delivery, row.id)).status = "sent"
        await deliver(client, update)
    assert [path for path, _ in requests] == ["/chat.update"]
    assert len(messages) == 1
