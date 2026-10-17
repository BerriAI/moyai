"""Slack's signed bot surface and durable delivery records around the native agent.

Personal Slack agent tools stay in Harness's Slack capability. Bot messages use
Slack's ordinary API and never inherit a personal account's credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from agentchat.channels.slack.host import SlackHost
from agentchat.channels.slack.rendering import message_request
from agentchat.channels.slack.webhook import SlackWebhook
from agentchat.models import RichReply
from app.slack_format import ASIDE_PREFIXES
from app.slack_http import SlackHTTP, webhook_errors
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .contracts import NewMessage
from .models import Delivery, Session, SlackInbox, SlackThread, Turn, User, new_id, now
from .slack_messages import navigation_action, pr_cards, saved_message, session_url, thread_message

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)


def webhook(runtime: Runtime) -> SlackWebhook:
    secret = runtime.settings.slack_signing_secret.get_secret_value()
    if not secret or not runtime.settings.slack_team_id:
        raise HTTPException(503, "Slack bot ingress is not configured.")
    return SlackWebhook(secret)


def routes(runtime: Runtime) -> APIRouter:
    product = getattr(runtime, "product_slack", None)
    if product is not None:
        return product.routes()
    router = APIRouter()

    @router.post("/hooks/slack")
    async def receive(request: Request):
        raw = await request.body()
        with webhook_errors():
            incoming = webhook(runtime).event(raw, request.headers)
        if incoming["kind"] == "challenge":
            return {"challenge": incoming["challenge"]}
        if incoming["kind"] == "ignored":
            return {"ok": True}
        body = incoming["payload"]
        if body["team_id"] != runtime.settings.slack_team_id:
            raise HTTPException(403, "This Slack workspace is not connected.")
        identity = hashlib.sha256(f"{body['team_id']}:{body['event_id']}".encode()).hexdigest()
        try:
            async with runtime.database.sessions.begin() as db:
                db.add(SlackInbox(id=identity, payload=runtime.security.seal(body)))
        except IntegrityError:
            pass  # Slack delivery retries acknowledge the same persisted event.
        return {"accepted": True}

    @router.post("/hooks/slack/interactions")
    async def interact(request: Request):
        raw = await request.body()
        with webhook_errors():
            payload = webhook(runtime).interaction(raw, request.headers)
        team = payload.get("team")
        if not isinstance(team, dict):
            raise HTTPException(400, "Invalid Slack interaction.")
        if team.get("id") != runtime.settings.slack_team_id:
            raise HTTPException(403, "This Slack workspace is not connected.")
        if not navigation_action(payload):
            raise HTTPException(409, "This Slack action requires the Moyai session.")
        return {"ok": True}

    return router


async def admit_one(runtime: Runtime, inbox: SlackInbox) -> None:
    body = json.loads(runtime.security.unseal(inbox.payload))
    host = SlackHost(
        bot_user_id="", workspace_id=runtime.settings.slack_team_id or None, exclude_prefixes=ASIDE_PREFIXES,
    )
    try:
        message = host.normalize_event(body)
    except ValueError:
        message = None
    if message is None or not message["text"]:
        await mark_inbox(runtime, inbox.id, "ignored")
        return
    team, channel, thread_ts = message["team_id"], message["channel_id"], message["thread_ts"]
    async with runtime.database.sessions() as db:
        actor = await db.scalar(select(User).where(
            User.slack_user_id == message["user_id"], User.enabled.is_(True)
        ))
        thread = await db.scalar(select(SlackThread).where(
            SlackThread.team_id == team, SlackThread.channel_id == channel,
            SlackThread.thread_ts == thread_ts,
        ))
    if actor is None:
        await mark_inbox(runtime, inbox.id, "unlinked_user")
        return
    if thread is None:
        if not message["mentioned"] and not message["direct"]:
            await mark_inbox(runtime, inbox.id, "ignored")
            return
        try:
            async with runtime.database.sessions.begin() as db:
                session = Session(
                    id=new_id(), owner_id=actor.id, title=message["text"][:100],
                    model=runtime.settings.default_model,
                    # Channel membership is not the application's global sharing
                    # policy. Keep the linked requester as the web-access owner.
                    visibility="private", surface="slack",
                )
                db.add(session)
                await db.flush()
                thread = SlackThread(
                    id=new_id(), team_id=team, channel_id=channel,
                    thread_ts=thread_ts, session_id=session.id,
                )
                db.add(thread)
        except IntegrityError:
            async with runtime.database.sessions() as db:
                thread = await db.scalar(select(SlackThread).where(
                    SlackThread.team_id == team, SlackThread.channel_id == channel,
                    SlackThread.thread_ts == thread_ts,
                ))
            if thread is None:
                raise
    try:
        # app_mention and message envelopes can describe the same Slack message.
        # Channel + message timestamp identifies that message independently of event_id.
        message_key = hashlib.sha256(json.dumps([team, channel, message["message_ts"]]).encode()).hexdigest()
        turn = await runtime.database.submit(actor, thread.session_id, NewMessage(
            prompt=message["text"], request_key=f"slack-message:{message_key}", mode="asap",
        ))
    except HTTPException as error:
        if error.status_code not in (401, 403, 404):
            raise
        await mark_inbox(runtime, inbox.id, "access_denied")
        return
    from .slack_progress import anchor_identity
    try:
        async with runtime.database.sessions.begin() as db:
            db.add(Delivery(
                id=new_id(), identity=anchor_identity(turn.id, False),
                session_id=thread.session_id, turn_id=turn.id,
                destination=json.dumps({
                    "channel": thread.channel_id, "thread_ts": thread.thread_ts,
                    "rich_reply": True, "segment_id": turn.id,
                }),
                content=runtime.security.seal(thread_message(
                    "Moyai received your message.", session_url(runtime.settings.public_url, thread.session_id),
                )),
            ))
    except IntegrityError:
        pass
    await mark_inbox(runtime, inbox.id, "accepted")


async def mark_inbox(runtime: Runtime, identity: str, status: str) -> None:
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(SlackInbox).where(
            SlackInbox.id == identity, SlackInbox.status == "pending"
        ).values(status=status))


async def stage_deliveries(runtime: Runtime) -> None:
    from app.slack_format import slack_text

    async with runtime.database.sessions() as db:
        pairs = (await db.execute(select(Turn, SlackThread).join(
            SlackThread, SlackThread.session_id == Turn.session_id
        ).where(
            Turn.status.in_(["completed", "waiting", "saving", "failed"]),
            ~select(Delivery.id).where(
                Delivery.identity == ("slack-answer:" + Turn.id)
            ).exists(),
        ).order_by(
            Turn.created_at, Turn.id
        ).limit(100))).all()
    for turn, thread in pairs:
        identity = f"slack-answer:{turn.id}"
        async with runtime.database.sessions() as db:
            if await db.scalar(select(Delivery.id).where(Delivery.identity == identity)):
                continue
        text = runtime.security.unseal(turn.result).decode() if turn.result else ""
        if turn.status == "waiting":
            text = "Moyai needs your response before continuing."
        elif turn.status == "failed":
            text = turn.error or "This task stopped before it completed."
        if not text:
            text = "This task completed without a text answer."
        link = session_url(runtime.settings.public_url, turn.session_id)
        # Standalone publishes the answer before its checkpoint finishes saving.
        cards = pr_cards(text, link) if turn.status in {"saving", "completed"} else []
        content = thread_message(slack_text(text), link, cards=cards)
        try:
            async with runtime.database.sessions.begin() as db:
                from .slack_progress import latest_segment
                segment = await latest_segment(db, turn)
                db.add(Delivery(
                    id=new_id(), identity=identity, session_id=turn.session_id, turn_id=turn.id,
                    destination=json.dumps({
                        "channel": thread.channel_id, "thread_ts": thread.thread_ts, "rich_reply": True,
                        "segment_id": segment.id,
                    }),
                    content=runtime.security.seal(content),
                ))
        except IntegrityError:
            pass


async def deliver_one(runtime: Runtime, client: httpx.AsyncClient, delivery: Delivery) -> None:
    from .slack_progress import claim_delivery, reply_target, skip_obsolete
    if await skip_obsolete(runtime, delivery, product=False):
        return
    ready, reply_ts = await reply_target(runtime, delivery, product=False)
    if not ready:
        return
    async with runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, delivery.turn_id)
        actor = await db.get(User, turn.actor_id) if turn else None
        session = await db.get(Session, delivery.session_id)
        if actor is None or not actor.enabled or session is None or session.deleted:
            await db.execute(update(Delivery).where(Delivery.id == delivery.id).values(status="suppressed"))
            return
    destination = json.loads(delivery.destination)
    content = saved_message(runtime.security.unseal(delivery.content), destination)
    request = message_request(
        RichReply(text=content["text"], blocks=tuple(content.get("blocks") or ()),
                  attachments=tuple(content.get("attachments") or ())),
        channel_id=destination["channel"], thread_ts=destination["thread_ts"], message_ts=reply_ts,
        metadata={"event_type": "moyai_delivery", "event_payload": {"id": delivery.id}},
        fallback_blocks=False,
    )
    if not await claim_delivery(runtime, delivery, product=False):
        return
    outcome = await SlackHTTP(client).delivery.send_request(request)
    if outcome["status"] == "uncertain":
        return
    receipt = outcome["receipt"]
    assert receipt is not None
    # Receipt persistence is deliberately outside the ambiguous-send handler.
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "sending"
        ).values(
            status="sent" if outcome["status"] == "confirmed" else "rejected",
            receipt=runtime.security.seal(receipt),
        ))


async def reconcile_one(
    runtime: Runtime, client: httpx.AsyncClient, delivery: Delivery, *,
    headers: dict[str, str] | None = None,
) -> None:
    async with runtime.database.sessions.begin() as db:
        # Ownership stays "sending": this timestamp only throttles read checks.
        cutoff = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
        claimed = await db.scalar(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "sending", Delivery.claimed_at < cutoff,
        ).values(claimed_at=now()).returning(Delivery.id))
    if claimed is None:
        return
    destination = json.loads(delivery.destination)
    async def authorize():
        if runtime.product_slack is not None and not await runtime.product_slack.delivery_allowed(delivery):
            raise HTTPException(403, "Slack delivery access changed during reconciliation.")

    outcome = await SlackHTTP(client, headers, authorize=authorize).delivery.reconcile(
        destination["channel"], destination["thread_ts"], event_type="moyai_delivery", event_id=delivery.id,
    )
    found = outcome["receipt"] if outcome["status"] == "confirmed" else None
    if found:
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(Delivery).where(
                Delivery.id == delivery.id, Delivery.status == "sending"
            ).values(status="sent", receipt=runtime.security.seal(found)))
    # Absence (including a truncated thread page) is not proof that posting failed.


async def pump(runtime: Runtime) -> None:
    product = getattr(runtime, "product_slack", None)
    if product is not None:
        await product.pump()
        return
    if not runtime.settings.slack_bot_token.get_secret_value():
        return
    async with runtime.database.sessions() as db:
        inbox = list((await db.scalars(select(SlackInbox).where(
            SlackInbox.status == "pending"
        ).order_by(SlackInbox.created_at).limit(10))).all())
    for item in inbox:
        await admit_one(runtime, item)
    from .slack_progress import stage_progress
    await stage_progress(runtime)
    await stage_deliveries(runtime)
    async with runtime.database.sessions() as db:
        deliveries = list((await db.scalars(select(Delivery).where(
            Delivery.status.in_(["pending", "sending"])
        ).order_by(Delivery.created_at).limit(10))).all())
    async with httpx.AsyncClient(
        base_url="https://slack.com/api/", timeout=20,
        headers={"Authorization": f"Bearer {runtime.settings.slack_bot_token.get_secret_value()}"},
    ) as client:
        for delivery in deliveries:
            try:
                if delivery.status == "pending":
                    await deliver_one(runtime, client, delivery)
                else:
                    await reconcile_one(runtime, client, delivery)
            except httpx.HTTPError:
                logger.warning("Slack read reconciliation is unavailable; the delivery remains claimed.")


async def dispatcher(runtime: Runtime) -> None:
    while True:
        try:
            await pump(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Slack admission or delivery is unavailable; saved ownership is retained.")
        await asyncio.sleep(3)
