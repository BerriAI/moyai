"""Canonical PostgreSQL routing and local HTTP proofs; live proof is separate."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from email.utils import formatdate
from types import MethodType, SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import test_slack_web as support
from app.coordinator_handoff import initialize_schema
from app.db import Store
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select, text, update
from test_personal_dot import finish, observer
from test_product_slack import post_signed_event
from test_product_slack import receiver as local_receiver

from moyai.dot_models import DotSlackConversation, DotSlackPreference
from moyai.dot_notifications import DotNotificationEdit, DotNotifications, allowed
from moyai.dot_slack_threads import new_direct_message, recover_root
from moyai.models import Delivery, DeliverySchedule, Session, SlackThread, Turn, User
from moyai.private_history import PrivateHistory
from moyai.private_instructions import PrivateInstructions
from moyai.product_connections import ProductConnections
from moyai.product_slack import ProductSlack, SlackInstallationUnavailable
from moyai.product_slack_models import ProductSlackBinding, SlackWebLink, SlackWebTurn
from moyai.slack import reconcile_one
from moyai.slack_audience import can_read
from moyai.slack_delivery_status import retry

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = support.product_database
admitted_product = support.admitted_product
slack_admission = support.slack_admission
linked = support.linked
receiver = local_receiver


@pytest_asyncio.fixture
async def dot(linked, monkeypatch):
    product = linked
    service = product.service
    service.app = "dot"
    service.dot = service
    service.for_app = MethodType(ProductSlack.for_app, service)
    # The signed Slack fixtures use historical timestamps. The provider clock
    # deliberately differs from the host clock; ingress must use the former.
    product.provider_date = formatdate(1791999998, usegmt=True)
    request = service.connectors.request
    async def with_provider_date(method, url, **kwargs):
        headers = kwargs.pop("response_headers", None)
        if headers is not None and product.provider_date is not None:
            headers["date"] = product.provider_date
        return await request(method, url, **kwargs)
    service.connectors.request = with_provider_date
    product.clock_waits = []
    async def elapsed_wait(seconds):
        product.clock_waits.append(seconds)
    monkeypatch.setattr("moyai.dot_notifications.sleep", elapsed_wait)
    service.settings.public_url = "https://moyai.example.test"
    service.stopping = asyncio.Event()
    service.runtime.product_slack = service
    product.database.slack_dot_installation = service.installation_in
    product.database.product_connections = ProductConnections(SimpleNamespace(
        store_proxy=SimpleNamespace(schema_updates=False), identity=product.adapter,
        security=product.adapter.legacy_security, settings=service.settings,
    ), product.database)
    for name in ("deliver", "delivery_allowed", "_claim", "_settle", "_retry_rejected", "_schedule"):
        setattr(service, name, MethodType(getattr(ProductSlack, name), service))
    product.dot_id = await product.database.dots.open(product.actor)
    product.notifications = DotNotifications(service.runtime)
    yield product


async def enable(dot):
    settings = await dot.notifications.read(dot.actor)
    return await dot.notifications.edit(dot.actor, DotNotificationEdit(revision=settings.revision, enabled=True))


@pytest.mark.parametrize("provider_offset", [-3600, 3600])
async def test_binding_uses_slack_clock_and_accepts_first_dm_after_success(dot, provider_offset):
    provider_second = int(datetime.now(UTC).timestamp()) + provider_offset
    dot.provider_date = formatdate(provider_second, usegmt=True)
    await enable(dot)
    assert dot.clock_waits == [2]
    async with dot.database.sessions() as db:
        assert (await db.get(DotSlackPreference, dot.actor.id)).started_ts == f"{provider_second + 2}.000000"
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    def origin(stamp):
        return {"team_id": "TLOCAL001", "channel_id": "DLOCAL001", "thread_ts": stamp, "message_ts": stamp}
    with pytest.raises(HTTPException) as old:
        await new_direct_message(dot.service, sender, origin(f"{provider_second + 1}.900000"), "Queued before cutover")
    assert old.value.status_code == 403
    thread = await new_direct_message(
        dot.service, sender, origin(f"{provider_second + 2}.001000"), "First DM after successful opt-in",
    )
    async with dot.database.sessions() as db:
        assert (await db.get(Session, thread.session_id)).owner_id == dot.actor.id


@pytest.mark.parametrize("provider_date", [
    None, "invalid", "Wed, 25 Nov 2026 03:00:00 -0500", "Wed, 25 Nov 2026 03:00:00 GMT junk",
])
async def test_missing_or_invalid_provider_clock_does_not_enable_notifications(dot, provider_date):
    dot.provider_date = provider_date
    with pytest.raises(HTTPException) as unavailable:
        await enable(dot)
    assert unavailable.value.status_code == 502
    assert dot.clock_waits == []
    async with dot.database.sessions() as db:
        assert await db.get(DotSlackPreference, dot.actor.id) is None


@pytest.mark.parametrize("capture_headers", [False, True])
async def test_connector_preserves_body_and_exposes_provider_headers(monkeypatch, capture_headers):
    from app.connectors import Connectors

    provider_date = "Wed, 25 Nov 2026 03:00:00 GMT"
    original = httpx.AsyncClient
    def client(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"ok": True, "channel": {"id": "DLOCAL001"}}, headers={"Date": provider_date},
        )))
    monkeypatch.setattr("app.connectors.httpx.AsyncClient", client)
    headers = {} if capture_headers else None
    body = await Connectors.__new__(Connectors).request(
        "POST", "https://slack.example/conversations.open", response_headers=headers,
    )
    assert body == {"ok": True, "channel": {"id": "DLOCAL001"}}
    if headers is not None:
        assert headers["date"] == provider_date


@pytest_asyncio.fixture
async def dot_ingress(dot):
    await enable(dot)
    return ingress_for(dot)


async def test_dot_admits_only_pinned_private_im_and_rejects_cross_app_thread(dot, dot_ingress):
    from moyai.product_slack_conversations import SlackConversations

    stamp = "1792100000.001"
    await dot_ingress.receive(dm_event("DOT_CHANNEL", stamp, channel_type="channel"))
    await dot_ingress.receive(dm_event("DOT_MPIM", stamp, channel_type="mpim"))
    with pytest.raises(HTTPException) as unpinned:
        await dot_ingress.receive(dm_event("DOT_UNPINNED", stamp, channel="DOTHER001"))
    assert unpinned.value.status_code == 403
    async with dot.database.sessions() as db:
        assert not (await db.scalars(select(SlackThread))).all()
    await dot_ingress.receive(dm_event("DOT_OWNER_IM", stamp))
    async with dot.database.sessions() as db:
        thread = (await db.scalars(select(SlackThread))).one()
        assert thread.slack_app == "dot"
        assert (await db.get(Session, thread.session_id)).visibility == "private"
    dev = SlackConversations(SimpleNamespace(app="dev", runtime=dot.service.runtime))
    with pytest.raises(HTTPException) as wrong_app:
        await dev.find({"team_id": thread.team_id, "channel_id": thread.channel_id, "thread_ts": stamp})
    assert wrong_app.value.status_code == 403


def ingress_for(dot):
    from moyai.product_slack_events import SlackEvents

    async def catalog(*args, **kwargs):
        return []
    async def slack_access(db, session, actor):
        return await can_read(dot.service, db, session, actor)
    dot.service.store = SimpleNamespace(rows=lambda query, params: [])
    dot.service.connections = SimpleNamespace(catalog=catalog, personal_github=False)
    dot.database.slack_access = slack_access
    return SlackEvents(dot.service)


async def test_dev_top_level_dm_stays_shared_after_dot_opt_in(dot, dot_ingress):
    dot.service.app = "dev"
    await dot_ingress.receive(dm_event("DEV_NORMAL_DM", "1792100000.001", channel="DDEV00001"))
    async with dot.database.sessions() as db:
        thread = (await db.scalars(select(SlackThread))).one()
        session = await db.get(Session, thread.session_id)
        assert (thread.slack_app, session.visibility, session.surface) == ("dev", "shared", "slack")
        assert await db.get(DotSlackConversation, session.id) is None


async def test_dot_signed_inbox_drains_and_replay_does_not_repeat_effects(dot, tmp_path, monkeypatch):
    fixture = dot.service
    runtime = fixture.runtime
    settings = fixture.settings
    settings.slack_dot_signing_secret = "local-dot-signature"
    store = Store(tmp_path / "inbox")
    try:
        initialize_schema(store)
        runtime.product = SimpleNamespace(
            store_proxy=store, settings=settings, security=dot.adapter.legacy_security, identity=dot.adapter,
        )
        runtime.product_history = None
        runtime.product_connections = ProductConnections(runtime.product, dot.database)
        connections = runtime.product_connections

        async def catalog(*args, **kwargs):
            return []

        monkeypatch.setattr(connections, "catalog", catalog)
        monkeypatch.setattr(connections.connectors, "request", fixture.connectors.request)
        for provider, app_id, bot_id in (
            ("slack", "ADEV00001", "UBOT00001"), ("slack_dot", "ADOT00001", "UDOT00001"),
        ):
            connections.connectors.save(provider, {"bot": {
                "access_token": "local-bot-token", "team": {"id": "TLOCAL001"},
                "app_id": app_id, "bot_user_id": bot_id,
            }}, provider)
        coordinator = runtime.product_slack = ProductSlack(runtime)
        for ingress in (coordinator, coordinator.dot):
            monkeypatch.setattr(ingress, "actor", fixture.actor)

            async def installation_in(db, transport=ingress):
                return transport.installation()

            monkeypatch.setattr(ingress, "installation_in", installation_in)
        dot.database.slack_dot_installation = coordinator.dot.installation_in
        assert (await enable(dot)).status == "on"

        app = FastAPI()
        app.include_router(coordinator.routes())
        event = {**dm_event("EDRAIN", "1792100000.001"), "api_app_id": "ADOT00001"}
        key = "dot:event:TLOCAL001:EDRAIN"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            assert (await post_signed_event(
                client, event, dot=True, secret=b"local-dot-signature",
            )).status_code == 200
            assert await coordinator.inbox.drain_one()
            assert store.rows("SELECT status FROM coordinator_hooks WHERE id=?", (key,))[0]["status"] == "done"
            assert coordinator.delivery_wakeup.is_set()
            async with dot.database.sessions.begin() as db:
                thread = (await db.scalars(select(SlackThread))).one()
                assert (thread.slack_app, (await db.get(Session, thread.session_id)).visibility) == ("dot", "private")
                turn = (await db.scalars(select(Turn).where(Turn.session_id == thread.session_id))).one()
                turn.status, turn.finished_at = "completed", datetime.now(UTC).isoformat()
                turn_id = turn.id
            await coordinator.stage()

            assert (await post_signed_event(
                client, event, dot=True, secret=b"local-dot-signature",
            )).status_code == 200
            assert not await coordinator.inbox.drain_one()
            assert store.rows("SELECT status FROM coordinator_hooks WHERE id=?", (key,))[0]["status"] == "done"
            await coordinator.stage()

            dev = {**dm_event("EDEV", "1792100000.002", channel="DDEV00001"), "api_app_id": "ADEV00001"}
            assert (await post_signed_event(
                client, dev, secret=settings.slack_signing_secret.encode(),
            )).status_code == 200
        assert await coordinator.inbox.drain_one()
        assert store.rows("SELECT status FROM coordinator_hooks WHERE id=?", ("event:TLOCAL001:EDEV",))[0]["status"] == "done"
        async with dot.database.sessions() as db:
            assert list(await db.scalars(select(Turn.id).where(Turn.session_id == thread.session_id))) == [turn_id]
            delivery = (await db.scalars(select(Delivery).where(Delivery.turn_id == turn_id))).one()
            assert delivery.slack_app == "dot"
            dev_thread = (await db.scalars(select(SlackThread).where(SlackThread.slack_app == "dev"))).one()
            assert (await db.get(Session, dev_thread.session_id)).visibility == "shared"
    finally:
        store.close()


async def test_dot_pin_retires_legacy_dev_thread_without_changing_private_history(dot, dot_ingress):
    root = "1792100000.001"
    await dot_ingress.receive(dm_event("LEGACY_DOT_ROOT", root))
    async with dot.database.sessions.begin() as db:
        thread = (await db.scalars(select(SlackThread))).one()
        thread.slack_app = None
        link = await db.get(SlackWebLink, thread.session_id)
        binding = await db.get(ProductSlackBinding, thread.session_id)
        link.slack_app = binding.slack_app = None
    dot.service.app = "dev"
    with pytest.raises(HTTPException) as closed:
        await dot_ingress.receive(dm_event("LEGACY_DOT_REPLY", "1792100000.002", thread_ts=root))
    assert closed.value.status_code == 403
    async with dot.database.sessions() as db:
        session = await db.get(Session, thread.session_id)
        assert (session.owner_id, session.visibility, session.deleted) == (dot.actor.id, "private", False)
        assert await db.scalar(select(func.count()).select_from(Turn).where(Turn.session_id == session.id)) == 1


async def test_dot_approval_requires_dot_receipt_and_resumes_as_verified_owner(dot, dot_ingress, monkeypatch):
    from pydantic_ai import Conversation, ConversationTypeAdapter, DeferredToolRequests
    from pydantic_ai.messages import ToolCallPart

    monkeypatch.setattr(dot.service.runtime, "product", dot.service.product, raising=False)
    await dot_ingress.receive(dm_event("DOT_APPROVAL_ROOT", "1792100000.001"))
    async with dot.database.sessions.begin() as db:
        thread = (await db.scalars(select(SlackThread))).one()
        turn = (await db.scalars(select(Turn).where(Turn.session_id == thread.session_id))).one()
        turn.status = "waiting"
        conversation = Conversation(deferred_tool_requests=DeferredToolRequests(approvals=[
            ToolCallPart("local_tool", {}, tool_call_id="dot-call"),
        ]))
        turn.conversation = dot.database.security.seal(ConversationTypeAdapter.dump_json(conversation).decode())
        action = {"action_id": "moyai_native_approve", "value": turn.id}
        stamp = "1792100000.002"
        delivery = Delivery(
            identity=f"product-slack:{turn.id}:waiting:0", session_id=thread.session_id, turn_id=turn.id,
            destination=json.dumps({"channel": thread.channel_id, "thread_ts": thread.thread_ts}),
            content=dot.database.security.seal({"blocks": [{"type": "actions", "elements": [action]}]}),
            receipt=dot.database.security.seal({"ts": stamp}), status="sent", slack_app="dev",
        )
        db.add(delivery)
    payload = {
        "team": {"id": thread.team_id}, "channel": {"id": thread.channel_id}, "user": {"id": "UACTUAL01"},
        "message": {"ts": stamp, "thread_ts": thread.thread_ts}, "actions": [action],
    }
    with pytest.raises(HTTPException) as wrong_app:
        await ProductSlack.interact(dot.service, payload)
    assert wrong_app.value.status_code == 403
    async with dot.database.sessions.begin() as db:
        (await db.get(Delivery, delivery.id)).slack_app = "dot"
    called = []

    async def record(runtime, actor, waiting, *, approvals):
        called.append((actor.id, waiting.id, approvals))

    async def resume(runtime, actor, waiting):
        called.append((actor.id, waiting.id))

    monkeypatch.setattr("moyai.product_slack_controls.record_replies", record)
    monkeypatch.setattr("moyai.product_slack_controls.resume_replies", resume)
    await ProductSlack.interact(dot.service, payload)
    assert called == [(dot.actor.id, turn.id, {"dot-call": True}), (dot.actor.id, turn.id)]


def dm_event(event_id, stamp, **fields):
    return {
        "type": "event_callback", "event_id": event_id, "team_id": "TLOCAL001",
        "event": {
            "type": "message", "channel": "DLOCAL001", "channel_type": "im", "user": "UACTUAL01",
            "text": "Plan the launch privately.", "ts": stamp, **fields,
        },
    }


async def test_dm_during_connection_clock_guard_never_falls_back_to_shared_chat(dot, dot_ingress):
    with pytest.raises(HTTPException) as early:
        await dot_ingress.receive(dm_event("DOT_CONNECTING", "1791999999.900000"))
    assert early.value.status_code == 403
    async with dot.database.sessions() as db:
        assert await db.scalar(select(Turn.id).where(Turn.request_key.startswith("slack-message:"))) is None
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 0


async def test_dm_arriving_while_opt_in_is_waiting_finds_the_committed_private_pin(dot, monkeypatch):
    ingress = ingress_for(dot)
    async def during_wait(seconds):
        assert seconds == 2
        with pytest.raises(HTTPException) as early:
            await ingress.receive(dm_event("DOT_IN_FLIGHT_OPT_IN", "1791999999.900000"))
        assert early.value.status_code == 403
        async with dot.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(SlackThread)) == 0
            assert await db.scalar(select(Turn.id).where(Turn.request_key.startswith("slack-message:"))) is None
    monkeypatch.setattr("moyai.dot_notifications.sleep", during_wait)
    assert (await enable(dot)).status == "on"
    await ingress.receive(dm_event("DOT_AFTER_OPT_IN", "1792000001.001000"))
    async with dot.database.sessions() as db:
        thread = (await db.scalars(select(SlackThread))).one()
        session = await db.get(Session, thread.session_id)
        assert (session.owner_id, session.visibility) == (dot.actor.id, "private")


async def test_cancellation_after_binding_commit_keeps_the_private_route(dot, monkeypatch):
    ingress = ingress_for(dot)
    async def cancelled_wait(_seconds):
        raise asyncio.CancelledError
    monkeypatch.setattr("moyai.dot_notifications.sleep", cancelled_wait)
    with pytest.raises(asyncio.CancelledError):
        await enable(dot)
    settings = await dot.notifications.read(dot.actor)
    assert settings.status == "on" and settings.revision == 1
    with pytest.raises(HTTPException) as early:
        await ingress.receive(dm_event("DOT_CANCELLED_EARLY", "1791999999.900000"))
    assert early.value.status_code == 403
    await ingress.receive(dm_event("DOT_CANCELLED_LATER", "1792000001.001000"))
    async with dot.database.sessions() as db:
        thread = (await db.scalars(select(SlackThread))).one()
        assert (await db.get(Session, thread.session_id)).visibility == "private"


async def test_pause_during_opt_in_wait_is_not_overwritten(dot, monkeypatch):
    async def pause_during_wait(_seconds):
        current = await dot.notifications.read(dot.actor)
        await dot.notifications.edit(dot.actor, DotNotificationEdit(revision=current.revision, enabled=False))
    monkeypatch.setattr("moyai.dot_notifications.sleep", pause_during_wait)
    settings = await enable(dot)
    assert not settings.enabled and settings.revision == 2
    async with dot.database.sessions() as db:
        preference = await db.get(DotSlackPreference, dot.actor.id)
        assert not preference.enabled and preference.destination_key == "dot:TLOCAL001:DLOCAL001"


@pytest.mark.parametrize("change", ["revoked", "linking_disabled", "remapped"])
async def test_revoked_dot_dm_does_not_fall_back_to_shared_slack(dot, dot_ingress, change):
    if change == "linking_disabled":
        dot.service.settings.slack_identity_linking_enabled = False
    else:
        assignment = ("profile_eligible=0, link_status='unavailable'" if change == "revoked"
                      else "email='different@example.test', linked_user_id=NULL")
        async with dot.source.begin() as db:
            await db.execute(text(f"UPDATE moyai.users SET {assignment} WHERE id=:id"), {
                "id": "slack:TLOCAL001:UACTUAL01",
            })
    with pytest.raises(HTTPException) as error:
        await dot_ingress.receive(dm_event("DOT_REVOKED", "1792100000.021"))
    assert error.value.status_code == 403
    async with dot.database.sessions() as db:
        assert await db.scalar(select(Turn.id).where(Turn.request_key.startswith("slack-message:"))) is None
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 0


async def test_legacy_receipt_prevents_new_dot_root_admission(dot, dot_ingress):
    payload = dm_event("DOT_LEGACY", "1792100000.022")
    receipt = (payload["team_id"], payload["event"]["channel"], payload["event"]["ts"], payload["event_id"])
    def rows(query, params):
        return [(1,)] if "FROM slack_receipts" in query and params == receipt else []
    dot.service.store.rows = rows
    await dot_ingress.receive(payload)
    async with dot.database.sessions() as db:
        assert await db.scalar(select(Turn.id).where(Turn.request_key.startswith("slack-message:"))) is None
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 0


async def test_operator_release_preserves_old_history_and_rejects_pre_cutover_events(dot, dot_ingress):
    root = "1792100000.021"
    await dot_ingress.receive(dm_event("DOT_OLD_OWNER", root))
    pending = await publish(dot)
    async with dot.database.sessions() as db:
        old_turn = (await db.scalars(select(Turn).where(Turn.request_key.startswith("slack-message:")))).one()
    successor_id = "google:successor"
    async with dot.source.begin() as db:
        await db.execute(text("UPDATE moyai.users SET email='former@example.test' WHERE id=:id"), {
            "id": dot.actor.id,
        })
        await db.execute(text(
            "INSERT INTO moyai.users(id,kind,email,name,linked_user_id,created_at,updated_at) "
            "VALUES (:id,'google','admin@example.test','Current owner',NULL,'c','u')",
        ), {"id": successor_id})
    principal = await dot.adapter.current(successor_id)
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    async with dot.database.sessions.begin() as db:
        successor = await dot.adapter.project_native_user(db, principal)
        assert (await dot.adapter.individual_for_admission(db, sender.product_grant)).id == successor.id
    await dot.database.dots.open(successor)
    with pytest.raises(HTTPException) as conflict:
        await dot.notifications.edit(successor, DotNotificationEdit(revision=0, enabled=True))
    assert conflict.value.status_code == 409

    # The documented operator transaction runs while admission/delivery is
    # drained; no event is admitted during the temporary unpinned interval.
    async with dot.database.sessions.begin() as db:
        await db.get(User, dot.actor.id, with_for_update=True)
        preference = await db.get(DotSlackPreference, dot.actor.id, with_for_update=True)
        assert preference.destination_key == "dot:TLOCAL001:DLOCAL001" and preference.revision == 1
        preference.enabled, preference.recipient, preference.destination_key = False, None, None
        preference.started_ts, preference.revision = None, preference.revision + 1
        await db.execute(update(Delivery).where(
            Delivery.session_id.in_(select(DotSlackConversation.session_id).where(
                DotSlackConversation.dot_session_id == preference.session_id,
            )), Delivery.status == "pending",
            Delivery.identity == ("product-slack:" + Delivery.turn_id + ":dot"),
        ).values(status="suppressed"))
    dot.provider_date = formatdate(1792199998, usegmt=True)
    assert (await dot.notifications.edit(successor, DotNotificationEdit(revision=0, enabled=True))).status == "on"
    # Queued or late pre-cutover roots cannot become conversations for the new owner.
    with pytest.raises(HTTPException) as stale:
        await dot_ingress.receive(dm_event("DOT_QUEUED_OLD_ROOT", "1792150000.001"))
    assert stale.value.status_code == 403
    with pytest.raises(HTTPException) as closed:
        await dot_ingress.receive(dm_event("DOT_OLD_REPLY", "1792300000.022", thread_ts=root))
    assert closed.value.status_code in {401, 403}
    await dot_ingress.receive(dm_event("DOT_NEW_OWNER", "1792300000.023"))
    async with dot.database.sessions() as db:
        old_session = await db.get(Session, old_turn.session_id)
        saved_turn = await db.get(Turn, old_turn.id)
        thread = (await db.scalars(select(SlackThread).where(SlackThread.session_id == old_session.id))).one()
        assert (old_session.owner_id, old_session.visibility, old_session.deleted) == (dot.actor.id, "private", False)
        assert (saved_turn.actor_id, saved_turn.prompt, thread.thread_ts) == (dot.actor.id, old_turn.prompt, root)
        assert await db.scalar(select(func.count()).select_from(Turn).where(Turn.session_id == old_session.id)) == 1
        assert (await db.get(Delivery, pending.id)).status == "suppressed"
        new_thread = await db.scalar(select(SlackThread).where(SlackThread.thread_ts == "1792300000.023"))
        new_session = await db.get(Session, new_thread.session_id)
        assert new_session.owner_id == successor.id and new_session.visibility == "private"
        assert new_session.id != old_session.id


@pytest.mark.parametrize("kind", ["private", "deleted", "deleting", "wrong_owner", "paused", "eligible"])
async def test_historical_dm_is_resolved_before_creating_dot_chat(dot, dot_ingress, monkeypatch, kind):
    source = {
        "run_id": "historical", "private_owner_id": "other" if kind == "private" else None,
        "deleted_at": "yesterday" if kind == "deleted" else None,
        "deletion_requested_at": "today" if kind == "deleting" else None,
        "source_sender": "UOTHER001" if kind == "wrong_owner" else "UACTUAL01",
        "owner_id": "other", "paused": kind == "paused",
    }
    dot.service.store.rows = lambda query, params: [source] if "FROM slack_threads" in query else []
    continued = []
    async def admit(message, conversation, *args):
        continued.append(conversation.source)
        return {"ok": True}
    monkeypatch.setattr(dot_ingress, "_admit", admit)
    if kind in {"private", "deleted", "deleting", "wrong_owner", "eligible"}:
        with pytest.raises(HTTPException) as error:
            await dot_ingress.receive(dm_event("DOT_HISTORY", "1792100000.022"))
        assert error.value.status_code == 403
    else:
        await dot_ingress.receive(dm_event("DOT_HISTORY", "1792100000.022"))
    assert continued == []
    async with dot.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(DotSlackConversation)) == 0


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("operation", ["edit", "delete", "revoked_edit", "wrong_sender"])
async def test_dot_message_updates_use_verified_owner_turn(dot, dot_ingress, reply, operation):
    original = dm_event("DOT_ORIGINAL", "1792100000.023")
    await dot_ingress.receive(original)
    if reply:
        original = dm_event("DOT_REPLY", "1792100000.024", thread_ts="1792100000.023")
        await dot_ingress.receive(original)
    async with dot.database.sessions() as db:
        turn = (await db.scalars(select(Turn).where(
            Turn.request_key.startswith("slack-message:"),
        ).order_by(Turn.created_at.desc()))).first()
        assert (turn.actor_id, turn.status) == (dot.actor.id, "queued")
    previous = original["event"]
    if operation == "revoked_edit":
        async with dot.source.begin() as db:
            await db.execute(text(
                "UPDATE moyai.users SET profile_eligible=0, link_status='unavailable' WHERE id=:id",
            ), {"id": "slack:TLOCAL001:UACTUAL01"})
    if operation == "wrong_sender":
        async with dot.source.begin() as db:
            await db.execute(text(
                "INSERT INTO moyai.users(id,kind,email,name,linked_user_id,created_at,updated_at,"
                "profile_eligible,profile_checked_at,profile_conflict,link_status) "
                "VALUES ('slack:TLOCAL001:UWRONG001','slack','other@example.test','Other sender',NULL,"
                "'c','u',1,:stamp,0,'unavailable')",
            ), {"stamp": datetime.now(UTC).isoformat()})
        previous = {**previous, "user": "UWRONG001"}
    updated = {**previous, "text": "Prepare the revised launch plan.", "edited": {"ts": "1792100000.025"}}
    payload = dm_event(
        "DOT_UPDATE", "1792100000.025",
        subtype="message_deleted" if operation == "delete" else "message_changed",
        deleted_ts=previous["ts"], previous_message=previous, message=updated, event_ts="1792100000.025",
    )
    if operation in {"revoked_edit", "wrong_sender"}:
        with pytest.raises(HTTPException) as error:
            await dot_ingress.receive(payload)
        assert error.value.status_code == 403
    else:
        assert (await dot_ingress.receive(payload))["updated"]
    async with dot.database.sessions() as db:
        saved = await db.get(Turn, turn.id)
        assert saved.status == ("stopped" if operation == "delete" else "queued")
        if operation == "edit":
            assert dot.database.security.unseal(saved.prompt).decode().startswith(updated["text"])
        else:
            assert dot.database.security.unseal(saved.prompt).decode().startswith("Plan the launch privately.")


async def publish(dot, **kwargs):
    _, _, _, prepared = await observer(dot.service.runtime, dot.actor, dot.dot_id)
    assert await finish(dot.service.runtime, prepared, **kwargs)
    async with dot.database.sessions() as db:
        return await db.scalar(select(Delivery).where(Delivery.turn_id == prepared.scope.turn_id))


async def test_opt_in_queues_only_new_published_findings(dot):
    assert (await dot.notifications.read(dot.actor)).status == "off"
    assert await publish(dot) is None
    settings = await enable(dot)
    assert settings.enabled and settings.status == "on"
    # Enabling does not replay already-published findings.
    async with dot.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Delivery)) == 0
    assert await publish(dot, action="quiet") is None
    delivery = await publish(dot, version="2026-11-25T20:00:00Z")
    assert delivery and delivery.status == "pending"
    assert delivery.session_id != dot.dot_id
    async with dot.database.sessions() as db:
        child = await db.get(Session, delivery.session_id)
        assert child.owner_id == dot.actor.id and child.visibility == "private" and child.surface == "web"
        assert child.archived
        context = dot.database.security.unseal(child.conversation).decode()
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 0
    assert "Your review is due tomorrow." in context
    assert "read_item" not in context


async def test_real_post_payload_binds_receipt_atomically_and_reply_admits_once(dot, receiver):
    await enable(dot)
    delivery = await publish(dot)
    async with httpx.AsyncClient(base_url=receiver.url) as client:
        await dot.service.deliver(client, delivery)
        await dot.service.deliver(client, delivery)
    assert len(receiver.requests) == 1
    sent = receiver.requests[0]
    assert sent["channel"] == "DLOCAL001" and "thread_ts" not in sent
    assert sent["metadata"]["event_payload"]["id"] == delivery.id
    assert sent["unfurl_links"] is False
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "sent"
        thread = await db.scalar(select(SlackThread).where(SlackThread.session_id == delivery.session_id))
        assert thread.thread_ts == "1792100000.001"
        assert (await db.get(SlackWebLink, delivery.session_id)).status == "linked"
        assert not (await db.get(Session, delivery.session_id)).archived
    from agentchat.channels.slack.host import SlackHost

    from moyai.product_slack_conversations import SlackConversations
    from moyai.product_slack_events import SlackEvents, SlackMessage
    sender, principal = await dot.service.actor("TLOCAL001", "UACTUAL01")
    event = {
        "type": "message", "channel": "DLOCAL001", "channel_type": "im", "user": "UACTUAL01",
        "text": "Help me prepare.", "ts": "1792100000.002", "thread_ts": thread.thread_ts,
    }
    payload = {"team_id": "TLOCAL001", "event": event, "event_id": "DOT_REPLY"}
    message = SlackMessage.parse(payload, event, SlackHost(bot_user_id="UBOT00001", workspace_id="TLOCAL001"))
    assert message is not None
    conversation = await SlackConversations(dot.service).resolve(
        thread, sender, message.origin, dot.service.installation(), mentioned=False, command="",
    )
    assert conversation is not None
    events = SlackEvents(dot.service)
    for _ in range(2):
        await events._admit(message, conversation, sender, principal, dot.service.installation())
    async with dot.database.sessions() as db:
        turns = list(await db.scalars(select(Turn).where(Turn.session_id == delivery.session_id)))
        assert len(turns) == 1 and turns[0].actor_id == dot.actor.id
        assert json.loads(turns[0].connections) == []
        assert await db.get(SlackWebTurn, turns[0].id)
    await dot.database.claim_batch(20)
    prepared = await dot.database.admission.prepare(turns[0].id)
    assert "Your review is due tomorrow." in prepared.conversation
    assert prepared.scope.grants == [] and not prepared.scope.attention
    from test_private_instructions_product import store
    await store(dot, dot.actor)
    assert await PrivateInstructions(dot.database).load(prepared.scope) == "PRIVATE_CANONICAL_TEST"
    async with dot.database.sessions() as db:
        assert await db.scalar(select(Session.id).where(
            Session.id == delivery.session_id, *PrivateHistory.private_chat(dot.actor.id),
        )) == delivery.session_id


async def test_new_dm_roots_are_separate_private_conversations_and_duplicate_safe(dot):
    await enable(dot)
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    def origin(stamp):
        return {"team_id": "TLOCAL001", "channel_id": "DLOCAL001", "thread_ts": stamp, "message_ts": stamp}
    threads = await asyncio.gather(*[
        new_direct_message(dot.service, sender, origin("1792100000.010"), "Plan my day")
        for _ in range(3)
    ])
    assert len({thread.session_id for thread in threads}) == 1
    another = await new_direct_message(dot.service, sender, origin("1792100000.020"), "Review the launch")
    assert another.session_id != threads[0].session_id
    async with dot.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(DotSlackConversation)) == 2
        for thread in [threads[0], another]:
            session = await db.get(Session, thread.session_id)
            assert (session.owner_id, session.visibility, session.surface) == (dot.actor.id, "private", "web")


async def test_pause_cancels_pending_notifications_but_keeps_owner_conversations(dot):
    settings = await enable(dot)
    pending = await publish(dot)
    await dot.notifications.edit(dot.actor, DotNotificationEdit(revision=settings.revision, enabled=False))
    assert not await allowed(dot.service, pending)
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, pending.id)).status == "suppressed"
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    thread = await new_direct_message(dot.service, sender, {
        "team_id": "TLOCAL001", "channel_id": "DLOCAL001",
        "thread_ts": "1792100000.030", "message_ts": "1792100000.030",
    }, "Hello again")
    assert thread is not None
    assert (await dot.service.web.input_actor(sender, thread.session_id)).id == dot.actor.id
    with pytest.raises(HTTPException) as stale:
        await dot.notifications.edit(dot.actor, DotNotificationEdit(revision=settings.revision, enabled=True))
    assert stale.value.status_code == 409
    dot.provider_date = formatdate(1792199998, usegmt=True)
    paused = await dot.notifications.read(dot.actor)
    await dot.notifications.edit(dot.actor, DotNotificationEdit(revision=paused.revision, enabled=True))
    async with dot.database.sessions() as db:
        assert (await db.get(DotSlackPreference, dot.actor.id)).started_ts == "1792000000.000000"


@pytest.mark.parametrize("kind", ["correct", "wrong_bot", "wrong_metadata", "ambiguous", "absent"])
async def test_uncertain_root_reconciles_exact_bot_and_metadata_without_reposting(dot, receiver, kind):
    await enable(dot)
    delivery = await publish(dot)
    receiver.outcome["drop"] = True
    async with httpx.AsyncClient(base_url=receiver.url) as client:
        await dot.service.deliver(client, delivery)
    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        assert row.status == "sending"
        row.claimed_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    message = {"ts": "1792100000.001", "user": "UBOT00001", "metadata": receiver.requests[0]["metadata"]}
    if kind == "wrong_bot":
        message["user"] = "UOTHER001"
    if kind == "wrong_metadata":
        message["metadata"] = {"event_type": "unrelated", "event_payload": {"id": delivery.id}}
    messages = [] if kind == "absent" else [message] * (2 if kind == "ambiguous" else 1)
    def response(request):
        assert request.method == "GET" and request.url.path == "/conversations.history"
        return httpx.Response(200, json={"ok": True, "messages": messages})
    async with httpx.AsyncClient(base_url="https://slack.example/", transport=httpx.MockTransport(response)) as client:
        await reconcile_one(dot.service.runtime, client, delivery)
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == ("sent" if kind == "correct" else "sending")
        thread = await db.scalar(select(SlackThread).where(SlackThread.session_id == delivery.session_id))
        assert (thread is not None) == (kind == "correct")
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("kind", ["in_flight", "pending", "unrelated", "expired", "api_error", "old_api_error"])
async def test_unconfirmed_root_has_a_bounded_confirmation_window(dot, kind):
    from app.connector_errors import ConnectorError

    await enable(dot)
    delivery = await publish(dot)
    moment = datetime.now(UTC)
    root = moment - timedelta(minutes=3) if kind in {"expired", "old_api_error"} else moment
    stamp = f"{root.timestamp():.6f}"
    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        row.status = "pending" if kind == "pending" else "sending"
        row.claimed_at = moment.isoformat()
    calls = []
    async def history(method, url, **kwargs):
        calls.append(kwargs["params"])
        if kind in {"api_error", "old_api_error"}:
            raise ConnectorError("Temporary failure")
        return {"ok": True, "messages": [{"ts": stamp, "user": "UOTHER001"}] if kind == "unrelated" else []}
    dot.service.connectors.request = history
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    origin = {
        "team_id": "TLOCAL001", "channel_id": "DLOCAL001",
        "thread_ts": stamp, "message_ts": f"{moment.timestamp() + 1:.6f}",
    }
    defer = await recover_root(dot.service, sender, origin)
    assert defer == (kind in {"in_flight", "api_error"})
    with pytest.raises(HTTPException) as waiting:
        await new_direct_message(dot.service, sender, origin, "Please follow up", waiting_for_root=defer)
    assert waiting.value.status_code == (503 if defer else 403)
    if defer:
        assert waiting.value.headers == {"Retry-After": "5"}
    assert len(calls) == (0 if kind == "pending" else 1)
    async with dot.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 0


async def test_reconciliation_cannot_make_an_old_send_plausible(dot):
    await enable(dot)
    delivery = await publish(dot)
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        row.status, row.created_at, row.claimed_at = "sending", old, old
    async def absent(*args, **kwargs):
        return {"ok": True, "messages": []}
    dot.service.connectors.request = absent
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    stamp = datetime.now(UTC).timestamp()
    origin = {
        "team_id": "TLOCAL001", "channel_id": "DLOCAL001",
        "thread_ts": f"{stamp:.6f}", "message_ts": f"{stamp + 1:.6f}",
    }
    assert not await recover_root(dot.service, sender, origin)
    async with httpx.AsyncClient(
        base_url="https://slack.example/",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True, "messages": []})),
    ) as client:
        await reconcile_one(dot.service.runtime, client, delivery)
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).claimed_at == old
    assert not await recover_root(dot.service, sender, origin)
    with pytest.raises(HTTPException) as rejected:
        await new_direct_message(dot.service, sender, origin, "Unrelated reply")
    assert rejected.value.status_code == 403


@pytest.mark.parametrize("app", ["dot", "dev"])
async def test_immediate_pumps_throttle_reconciliation_without_changing_dispatch_claim(dot, app):
    await enable(dot)
    delivery = await publish(dot)
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        row.status, row.created_at, row.claimed_at = "sending", old, old
        if app == "dev":
            row.slack_app = None
            (await db.get(SlackWebLink, delivery.session_id)).slack_app = None
    dot.service.app = app
    async def token():
        return f"{app}-token"
    dot.service.bot_token = token
    for name in ("pump", "_deliver_batch"):
        setattr(dot.service, name, MethodType(getattr(ProductSlack, name), dot.service))
    async def no_stage(*args, **kwargs):
        return None
    dot.service.stage = dot.service.source_notices = no_stage
    requests = []
    def history(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "messages": []})
    async with httpx.AsyncClient(
        base_url="https://slack.example/", transport=httpx.MockTransport(history),
    ) as client:
        await dot.service.pump(client)
        await dot.service.pump(client)
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].headers["Authorization"] == f"Bearer {app}-token"
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).claimed_at == old
        schedule = await db.get(DeliverySchedule, delivery.id)
        assert schedule.next_attempt > datetime.now(UTC).timestamp()


async def test_early_reply_recovers_once_from_exact_parent_even_after_pause(dot, receiver):
    settings = await enable(dot)
    delivery = await publish(dot)
    receiver.outcome["drop"] = True
    async with httpx.AsyncClient(base_url=receiver.url) as client:
        await dot.service.deliver(client, delivery)
    await dot.notifications.edit(dot.actor, DotNotificationEdit(revision=settings.revision, enabled=False))
    calls = []
    async def history(method, url, **kwargs):
        calls.append((method, url, kwargs["params"]))
        return {"ok": True, "messages": [{
            "ts": "1792100000.001", "user": "UBOT00001", "metadata": receiver.requests[0]["metadata"],
        }]}
    dot.service.connectors.request = history
    sender, _ = await dot.service.actor("TLOCAL001", "UACTUAL01")
    origin = {
        "team_id": "TLOCAL001", "channel_id": "DLOCAL001",
        "thread_ts": "1792100000.001", "message_ts": "1792100000.002",
    }
    await recover_root(dot.service, sender, origin)
    await recover_root(dot.service, sender, origin)
    assert len(calls) == 1 and calls[0][2]["oldest"] == origin["thread_ts"]
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "sent"
        assert await db.scalar(select(SlackThread.id).where(SlackThread.session_id == delivery.session_id))
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("change", ["deleted_child", "owner_changed", "installation_changed"])
async def test_changed_delivery_authority_cannot_send(dot, change):
    await enable(dot)
    delivery = await publish(dot)
    if change == "installation_changed":
        dot.service.installation()["authority"] = "b" * 64
    else:
        async with dot.database.sessions.begin() as db:
            await db.execute(update(Session).where(Session.id == delivery.session_id).values(
                **({"deleted": True} if change == "deleted_child" else {"visibility": "shared"}),
            ))
    assert not await allowed(dot.service, delivery)


@pytest.mark.parametrize("change", ["rotate_signing_secret", "remove_signing_secret", "disable", "disconnect"])
async def test_dot_authority_change_revokes_queued_turn_and_delivery(dot, dot_ingress, change):
    service = dot.service
    service.settings.slack_dot_signing_secret = "initial-dot-signature"
    service.product = SimpleNamespace(identity=dot.adapter, security=dot.adapter.legacy_security)
    service._installation = MethodType(ProductSlack._installation, service)
    async with dot.source.begin() as db:
        await db.execute(text("ALTER TABLE moyai.connection_policies ADD COLUMN checked_at TEXT"))
        await db.execute(text("ALTER TABLE moyai.connection_policies ADD COLUMN check_status TEXT"))
        for provider, app, bot in [
            ("slack", "ADEV00001", "UBOT00001"), ("slack_dot", "ADOT00001", "UBOT00002"),
        ]:
            credentials = {"bot": {
                "access_token": "synthetic-bot-token", "app_id": app, "bot_user_id": bot,
                "team": {"id": "TLOCAL001"},
            }}
            await db.execute(text(
                "INSERT INTO moyai.connections VALUES(:provider,:encrypted) "
                "ON CONFLICT(provider) DO UPDATE SET encrypted=excluded.encrypted"
            ), {"provider": provider, "encrypted": dot.adapter.legacy_security.encrypt(json.dumps(credentials))})
    await dot.source.dispose()
    await dot.database.engine.dispose()
    service.installation_in = MethodType(ProductSlack.installation_in, service)
    dot.database.slack_dot_installation = service.installation_in
    async with dot.database.sessions.begin() as db:
        installed = await service.installation_in(db)
    service.installation = lambda: installed

    await enable(dot)
    delivery = await publish(dot)
    await dot_ingress.receive(dm_event("DOT_SIGNING_CHANGE", "1792100000.123"))
    async with dot.database.sessions.begin() as db:
        turn = (await db.scalars(select(Turn).where(Turn.request_key.startswith("slack-message:")))).one()
        assert turn.status == "queued"
        assert await dot.database._saved_product_grant(db, turn)
    assert await allowed(service, delivery)

    if change == "rotate_signing_secret":
        service.settings.slack_dot_signing_secret = "rotated-dot-signature"
    elif change == "remove_signing_secret":
        service.settings.slack_dot_signing_secret = ""
    else:
        async with dot.source.begin() as db:
            if change == "disconnect":
                await db.execute(text("DELETE FROM moyai.connections WHERE provider='slack_dot'"))
            else:
                await db.execute(text("INSERT INTO moyai.connection_policies(provider,enabled,read_only) "
                                      "VALUES ('slack_dot',0,0)"))
    async with dot.database.sessions.begin() as db:
        with pytest.raises(HTTPException) as error:
            await dot.database._saved_product_grant(db, turn)
        assert error.value.status_code == 403
    if change == "rotate_signing_secret":
        assert not await allowed(service, delivery)
    else:
        with pytest.raises(SlackInstallationUnavailable):
            await allowed(service, delivery)


async def test_owner_retries_explicit_rejection_only_with_dot_delivery_authority(dot):
    await enable(dot)
    delivery = await publish(dot)
    dev = SimpleNamespace(app="dev", dot=dot.service)
    dev.for_app = MethodType(ProductSlack.for_app, dev)
    dev.delivery_allowed = MethodType(ProductSlack.delivery_allowed, dev)
    dot.service.runtime.product_slack = dev

    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        row.status = "sending"
    assert await retry(dot.service.runtime, dot.actor, delivery.session_id) == {"retried": 0}
    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        row.status, row.slack_app = "rejected", "dev"
    with pytest.raises(HTTPException) as conflict:
        await retry(dot.service.runtime, dot.actor, delivery.session_id)
    assert conflict.value.status_code == 409
    async with dot.database.sessions.begin() as db:
        row = await db.get(Delivery, delivery.id)
        assert row.status == "rejected"
        row.slack_app = "dot"
    assert await retry(dot.service.runtime, dot.actor, delivery.session_id) == {"retried": 1}
    async with dot.database.sessions() as db:
        assert (await db.get(Delivery, delivery.id)).status == "pending"
