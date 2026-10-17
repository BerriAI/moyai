"""Native Slack status lifecycle; transport doubles supplement actual-app proof."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import select
from test_product_slack import bind
from test_product_slack import bridge as canonical_bridge
from test_product_slack import slack as canonical_slack
from test_slack_progress import queued

from moyai.contracts import NewMessage
from moyai.models import Delivery, Session, SlackActivity, Turn, User, new_id, now
from moyai.product_slack_models import ProductSlackBinding
from moyai.slack_activity import SlackActivityService

bridge = canonical_bridge
slack = canonical_slack


@pytest_asyncio.fixture(params=[False, True], ids=["standalone", "product"])
async def activity(request, runtime, active, slack):
    thread = await bind(slack, active)
    if not request.param:
        runtime.product_slack = None
        runtime.settings.slack_bot_token = SecretStr("local-status-token")
        runtime.settings.slack_team_id = thread.team_id
    calls = []

    async def handle(req):
        body = json.loads(req.content)
        calls.append(body)
        assert req.url.path == "/assistant.threads.setStatus"
        assert body["channel_id"] == thread.channel_id and body["thread_ts"] == thread.thread_ts
        assert "text" not in body
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        yield SimpleNamespace(
            service=runtime.slack_activity, runtime=runtime, thread=thread, client=client,
            calls=calls, product=request.param, slack=slack, active=active,
        )


async def state(activity, status):
    async with activity.runtime.database.sessions.begin() as db:
        (await db.get(Turn, activity.active[1].id)).status = status


async def saved(activity):
    async with activity.runtime.database.sessions() as db:
        return await db.get(SlackActivity, activity.thread.id)


async def test_working_refresh_restart_and_completion_without_reply(activity):
    service, client = activity.service, activity.client
    await service.sync(client)
    assert [row["status"] for row in activity.calls] == ["is working…"]
    await service.sync(client)
    assert len(activity.calls) == 1
    async with activity.runtime.database.sessions.begin() as db:
        (await db.get(SlackActivity, activity.thread.id)).refreshed_at = time.time() - 61
    restarted = SlackActivityService(activity.runtime)
    await restarted.sync(client)
    assert len(activity.calls) == 2
    await state(activity, "completed")
    await restarted.sync(client)
    assert activity.calls[-1]["status"] == ""
    await restarted.sync(client)
    assert len(activity.calls) == 3
    async with activity.runtime.database.sessions() as db:
        assert not list(await db.scalars(select(Delivery)))  # Status never creates message slots.


@pytest.mark.parametrize("terminal", ["failed", "stopped", "waiting", "completed"])
async def test_terminal_and_input_wait_clear_without_final_message(activity, terminal):
    await activity.service.sync(activity.client)
    await state(activity, terminal)
    await activity.service.sync(activity.client)
    assert [row["status"] for row in activity.calls] == ["is working…", ""]


async def test_queued_starting_saving_and_idle_history(activity):
    await state(activity, "completed")
    await activity.service.sync(activity.client)
    assert activity.calls == []
    for status, expected in [
        ("queued", "is getting ready…"), ("starting", "is opening the workspace…"),
        ("running", "is working…"), ("saving", "is saving the work…"),
    ]:
        await state(activity, status)
        await activity.service.sync(activity.client)
        assert activity.calls[-1]["status"] == expected


@pytest.mark.parametrize("change", ["deleted", "disabled", "paused", "private", "revoked"])
async def test_authority_loss_clears_only_existing_status(activity, change):
    if not activity.product and change in {"paused", "private", "revoked"}:
        pytest.skip("Canonical product policy.")
    await activity.service.sync(activity.client)
    async with activity.runtime.database.sessions.begin() as db:
        if change == "deleted":
            (await db.get(Session, activity.thread.session_id)).deleted = True
        elif change == "disabled":
            (await db.get(User, activity.active[1].actor_id)).enabled = False
        elif change == "paused":
            (await db.get(ProductSlackBinding, activity.thread.session_id)).paused = True
        elif change == "private":
            (await db.get(Session, activity.thread.session_id)).visibility = "private"
        else:
            activity.slack.authority["current"] = False
    await activity.service.sync(activity.client)
    assert activity.calls[-1]["status"] == ""


async def test_reply_and_steer_reassert_without_changing_segments(activity, actor):
    await activity.service.sync(activity.client)
    initial = await queued(
        activity.runtime, activity.active, product=activity.product, kind="anchor", text="Working update.",
    )
    async with activity.runtime.database.sessions.begin() as db:
        row = await db.get(Delivery, initial.id)
        row.status, row.claimed_at = "sending", now()
    await activity.service.sync(activity.client)
    assert len(activity.calls) == 2
    async with activity.runtime.database.sessions.begin() as db:
        row = await db.get(Delivery, initial.id)
        row.status, row.receipt = "sent", activity.runtime.security.seal({"ok": True, "ts": "123.456"})
    await activity.service.sync(activity.client)
    assert len(activity.calls) == 3  # The actual reply arrived after the previous status request.
    steer = await activity.runtime.database.submit(actor, activity.thread.session_id, NewMessage(
        prompt="Include the count.", request_key=new_id(), mode="asap",
    ))
    assert steer.parent_turn == activity.active[1].id
    await activity.service.sync(activity.client)
    assert all(row["status"] == "is working…" for row in activity.calls)
    async with activity.runtime.database.sessions() as db:
        row = await db.get(Delivery, initial.id)
        assert row.status == "sent"
        assert json.loads(activity.runtime.security.unseal(row.content))["text"] == "Working update." \
            if activity.product else activity.runtime.security.unseal(row.content).decode() == "Working update."


async def test_status_failure_retries_but_completion_bypasses_backoff(activity):
    async def fail(req):
        activity.calls.append(json.loads(req.content))
        raise httpx.ReadTimeout("ambiguous status")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail), base_url="https://slack.test/") as client:
        await activity.service.sync(client)
        await activity.service.sync(client)
        assert len(activity.calls) == 1
    row = await saved(activity)
    assert row.status == "is working…" and row.refreshed_at == 0 and row.retry_at > time.time()
    await state(activity, "completed")
    await SlackActivityService(activity.runtime).sync(activity.client)
    assert activity.calls[-1]["status"] == ""
    assert (await saved(activity)).retry_at == 0


async def test_competing_pollers_and_late_completion_are_reconciled(activity):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(req):
        activity.calls.append(json.loads(req.content))
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed), base_url="https://slack.test/") as client:
        first = asyncio.create_task(activity.service.sync(client))
        await entered.wait()
        await SlackActivityService(activity.runtime).sync(activity.client)
        assert len(activity.calls) == 1
        await state(activity, "completed")
        release.set()
        await first
    await activity.service.sync(activity.client)
    assert [row["status"] for row in activity.calls] == ["is working…", ""]


async def test_cancelled_status_attempt_recovers_after_restart(activity):
    entered = asyncio.Event()

    async def delayed(req):
        activity.calls.append(json.loads(req.content))
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(delayed), base_url="https://slack.test/") as client:
        first = asyncio.create_task(activity.service.sync(client))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert (await saved(activity)).claim_id == ""
    await state(activity, "stopped")
    await SlackActivityService(activity.runtime).sync(activity.client)
    assert activity.calls[-1]["status"] == ""


async def test_process_death_claim_expires_and_clears_saved_attempt(activity):
    async with activity.runtime.database.sessions.begin() as db:
        db.add(SlackActivity(
            thread_id=activity.thread.id, status="is working…", retry_at=time.time() + 30,
            claim_id=new_id(), claim_until=time.time() + 15,
        ))
    await state(activity, "completed")
    await activity.service.sync(activity.client)
    assert activity.calls == []
    async with activity.runtime.database.sessions.begin() as db:
        (await db.get(SlackActivity, activity.thread.id)).claim_until = time.time() - 1
    await SlackActivityService(activity.runtime).sync(activity.client)
    assert [row["status"] for row in activity.calls] == [""]


async def test_product_coordinator_owns_and_drains_both_lanes(slack, monkeypatch):
    status_started, delivery_started = asyncio.Event(), asyncio.Event()
    status_closed, delivery_closed = asyncio.Event(), asyncio.Event()

    async def watch(started, closed):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(slack.service.inbox, "start", Mock())
    monkeypatch.setattr(slack.service.inbox, "close", AsyncMock())
    monkeypatch.setattr(slack.runtime.slack_activity, "watch", lambda: watch(status_started, status_closed))
    monkeypatch.setattr(slack.service, "watch", lambda: watch(delivery_started, delivery_closed))
    slack.service.start()
    await asyncio.wait_for(asyncio.gather(status_started.wait(), delivery_started.wait()), timeout=1)
    with pytest.raises(RuntimeError, match="already started"):
        slack.service.start()
    await slack.service.close()
    assert status_closed.is_set() and delivery_closed.is_set()
    assert slack.service.activity_task is None and slack.service.delivery_task is None


@pytest.mark.parametrize("change", ["completed", "paused", "workspace"])
async def test_token_refresh_rechecks_lifecycle_and_destination(slack, active, monkeypatch, change):
    thread = await bind(slack, active)
    original = slack.service.connectors.slack_bot_token

    async def token():
        result = await original()
        async with slack.runtime.database.sessions.begin() as db:
            if change == "completed":
                (await db.get(Turn, active[1].id)).status = "completed"
            else:
                binding = await db.get(ProductSlackBinding, thread.session_id)
                if change == "paused":
                    binding.paused = True
                else:
                    binding.connection_authority = "replaced"
        return result

    monkeypatch.setattr(slack.service.connectors, "slack_bot_token", token)
    calls = []

    async def handle(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await slack.runtime.slack_activity.sync(client)
    assert calls == [] if change == "workspace" else all(row["status"] == "" for row in calls)


async def test_status_outage_does_not_block_answer_delivery(slack, active):
    await bind(slack, active)
    delivery = await queued(slack.runtime, active, product=True, kind="anchor", text="Progress still arrives.")
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def handle(req):
        if req.url.path.endswith("setStatus"):
            entered.set()
            await release.wait()
            return httpx.Response(503)
        delivered.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "ts": "123.456"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        status = asyncio.create_task(slack.runtime.slack_activity.sync(client))
        await entered.wait()
        await asyncio.wait_for(slack.service.deliver(client, delivery), timeout=1)
        assert delivered[0]["text"] == "Progress still arrives."
        release.set()
        await status
    async with slack.runtime.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "sent"


@pytest.mark.parametrize("phase", [1, 2, 3], ids=["initial", "after-token-refresh", "before-send"])
@pytest.mark.parametrize("change", [
    "replaced", "destination", "bot-disabled", "chat-disabled", "removed", "connection-disabled",
])
async def test_unreachable_target_retires_dirty_idle_row_at_every_check(slack, active, monkeypatch, phase, change):
    runtime, service = slack.runtime, slack.runtime.slack_activity
    thread = await bind(slack, active)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, active[1].id)).status = "completed"
        db.add(SlackActivity(
            thread_id=thread.id, status="is working…", retry_at=time.time() + 30,
            delivery_version="old-reply", refreshed_at=time.time(),
        ))
    original, checks = service.target, []

    async def target(identity):
        checks.append(identity)
        if len(checks) == phase:
            if change == "removed":
                slack.service.store.execute("DELETE FROM connections WHERE provider='slack'")
            elif change == "connection-disabled":
                slack.service.store.execute(
                    "INSERT INTO connection_policies(provider,enabled,read_only) VALUES('slack',0,0) "
                    "ON CONFLICT(provider) DO UPDATE SET enabled=0",
                )
            elif change == "bot-disabled":
                slack.service.settings.slack_bot_enabled = False
            elif change == "chat-disabled":
                slack.service.settings.slack_thread_chat_enabled = False
            else:
                async with runtime.database.sessions.begin() as db:
                    binding = await db.get(ProductSlackBinding, thread.session_id)
                    if change == "replaced":
                        binding.connection_authority = "replaced"
                    else:
                        binding.channel_id = "COTHER001"
        return await original(identity)

    monkeypatch.setattr(service, "target", target)
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await service.sync(client)
        assert len(checks) == phase
        assert requests == []
        async with runtime.database.sessions() as db:
            row = await db.get(SlackActivity, thread.id)
            checked_at = row.checked_at
            assert row.status == row.delivery_version == row.claim_id == ""
            assert row.retry_at == row.refreshed_at == row.claim_until == 0
        await service.sync(client)
        assert len(checks) == phase  # Retired idle rows no longer read the installation or claim.
        async with runtime.database.sessions() as db:
            assert (await db.get(SlackActivity, thread.id)).checked_at == checked_at


async def test_active_work_recovers_after_destination_returns(activity):
    await activity.service.sync(activity.client)
    if activity.product:
        async with activity.runtime.database.sessions.begin() as db:
            binding = await db.get(ProductSlackBinding, activity.thread.session_id)
            original = binding.connection_authority
            binding.connection_authority = "replaced"
    else:
        original = activity.runtime.settings.slack_team_id
        activity.runtime.settings.slack_team_id = "TOTHER001"
    await activity.service.sync(activity.client)
    assert (await saved(activity)).status == ""
    assert len(activity.calls) == 1
    if activity.product:
        async with activity.runtime.database.sessions.begin() as db:
            (await db.get(ProductSlackBinding, activity.thread.session_id)).connection_authority = original
    else:
        activity.runtime.settings.slack_team_id = original
    await activity.service.sync(activity.client)
    assert [row["status"] for row in activity.calls] == ["is working…", "is working…"]


@pytest.mark.parametrize("error", [HTTPException(503, "Temporary read failure"), RuntimeError("Store unavailable")])
async def test_transient_installation_failure_preserves_dirty_state(slack, active, monkeypatch, error):
    thread = await bind(slack, active)
    runtime, service = slack.runtime, slack.runtime.slack_activity
    retry_at = time.time() + 30
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, active[1].id)).status = "completed"
        db.add(SlackActivity(thread_id=thread.id, status="is working…", retry_at=retry_at))
    original = slack.service.installation

    def unavailable():
        raise error

    monkeypatch.setattr(slack.service, "installation", unavailable)
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url="https://slack.test/") as client:
        await service.sync(client)
        async with runtime.database.sessions() as db:
            row = await db.get(SlackActivity, thread.id)
            assert row.status == "is working…" and row.retry_at == retry_at
        assert requests == []
        monkeypatch.setattr(slack.service, "installation", original)
        await service.sync(client)
        assert [row["status"] for row in requests] == [""]


async def test_retirement_cannot_clear_another_pollers_claim(activity):
    claim = await activity.service.claim(activity.thread.id)
    async with activity.runtime.database.sessions.begin() as db:
        row = await db.get(SlackActivity, activity.thread.id)
        row.claim_id, row.status, row.retry_at = "new-owner", "is working…", 123
        if activity.product:
            (await db.get(ProductSlackBinding, activity.thread.session_id)).connection_authority = "replaced"
        else:
            activity.runtime.settings.slack_team_id = "TOTHER001"
    assert await activity.service.target_or_retire(activity.thread.id, claim) is None
    row = await saved(activity)
    assert (row.claim_id, row.status, row.retry_at) == ("new-owner", "is working…", 123)
