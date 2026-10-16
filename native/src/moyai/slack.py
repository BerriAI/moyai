"""Slack's signed bot surface and durable delivery records around the native agent.

Personal Slack agent tools stay in Harness's Slack capability. Bot messages use
Slack's ordinary API and never inherit a personal account's credential.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .contracts import NewMessage
from .models import Delivery, Session, SlackInbox, SlackThread, Turn, User, new_id, now

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)


def routes(runtime: Runtime) -> APIRouter:
    product = getattr(runtime, "product_slack", None)
    if product is not None:
        return product.routes()
    router = APIRouter()

    @router.post("/hooks/slack")
    async def receive(request: Request):
        secret = runtime.settings.slack_signing_secret.get_secret_value()
        if not secret or not runtime.settings.slack_team_id:
            raise HTTPException(503, "Slack bot ingress is not configured.")
        raw = await request.body()
        if len(raw) > 256 * 1024:
            raise HTTPException(413, "Event is too large.")
        stamp = request.headers.get("x-slack-request-timestamp", "")
        try:
            recent = abs(time.time() - int(stamp)) <= 300
        except ValueError:
            recent = False
        expected = "v0=" + hmac.new(
            secret.encode(), b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256
        ).hexdigest()
        if not recent or not hmac.compare_digest(
            expected, request.headers.get("x-slack-signature", "")
        ):
            raise HTTPException(401, "Invalid Slack signature.")
        body = json.loads(raw)
        if body.get("type") == "url_verification":
            return {"challenge": body.get("challenge", "")}
        if body.get("team_id") != runtime.settings.slack_team_id:
            raise HTTPException(403, "This Slack workspace is not connected.")
        if not isinstance(body.get("event_id"), str):
            raise HTTPException(422, "Missing event identity.")
        identity = hashlib.sha256(f"{body['team_id']}:{body['event_id']}".encode()).hexdigest()
        try:
            async with runtime.database.sessions.begin() as db:
                db.add(SlackInbox(id=identity, payload=runtime.security.seal(body)))
        except IntegrityError:
            pass  # Slack delivery retries acknowledge the same persisted event.
        return {"accepted": True}

    return router


async def admit_one(runtime: Runtime, inbox: SlackInbox) -> None:
    body = json.loads(runtime.security.unseal(inbox.payload))
    event = body.get("event", {})
    if (
        event.get("type") not in ("app_mention", "message")
        or event.get("bot_id") or event.get("subtype")
        or not event.get("text") or not event.get("channel") or not event.get("ts")
    ):
        await mark_inbox(runtime, inbox.id, "ignored")
        return
    team, channel = body["team_id"], event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    async with runtime.database.sessions() as db:
        actor = await db.scalar(select(User).where(
            User.slack_user_id == event.get("user", ""), User.enabled.is_(True)
        ))
        thread = await db.scalar(select(SlackThread).where(
            SlackThread.team_id == team, SlackThread.channel_id == channel,
            SlackThread.thread_ts == thread_ts,
        ))
    if actor is None:
        await mark_inbox(runtime, inbox.id, "unlinked_user")
        return
    if thread is None:
        if event["type"] != "app_mention" and event.get("channel_type") != "im":
            await mark_inbox(runtime, inbox.id, "ignored")
            return
        try:
            async with runtime.database.sessions.begin() as db:
                session = Session(
                    id=new_id(), owner_id=actor.id, title=event["text"][:100],
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
        message_key = hashlib.sha256(json.dumps([team, channel, event["ts"]]).encode()).hexdigest()
        await runtime.database.submit(actor, thread.session_id, NewMessage(
            prompt=event["text"], request_key=f"slack-message:{message_key}", mode="asap",
        ))
    except HTTPException as error:
        if error.status_code not in (401, 403, 404):
            raise
        await mark_inbox(runtime, inbox.id, "access_denied")
        return
    await mark_inbox(runtime, inbox.id, "accepted")


async def mark_inbox(runtime: Runtime, identity: str, status: str) -> None:
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(SlackInbox).where(
            SlackInbox.id == identity, SlackInbox.status == "pending"
        ).values(status=status))


async def stage_deliveries(runtime: Runtime) -> None:
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
        link = (
            f"{runtime.settings.public_url.rstrip('/')}/#{turn.session_id}"
            if runtime.settings.public_url else ""
        )
        text = text[:3500] + ("\n\nOpen conversation: " + link if link else "")
        try:
            async with runtime.database.sessions.begin() as db:
                db.add(Delivery(
                    id=new_id(), identity=identity, session_id=turn.session_id, turn_id=turn.id,
                    destination=json.dumps({"channel": thread.channel_id, "thread_ts": thread.thread_ts}),
                    content=runtime.security.seal(text),
                ))
        except IntegrityError:
            pass


async def deliver_one(runtime: Runtime, client: httpx.AsyncClient, delivery: Delivery) -> None:
    async with runtime.database.sessions.begin() as db:
        turn = await db.get(Turn, delivery.turn_id)
        actor = await db.get(User, turn.actor_id) if turn else None
        session = await db.get(Session, delivery.session_id)
        if actor is None or not actor.enabled or session is None or session.deleted:
            await db.execute(update(Delivery).where(Delivery.id == delivery.id).values(status="suppressed"))
            return
        claimed = await db.scalar(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "pending"
        ).values(status="sending", claimed_at=now()).returning(Delivery.id))
    if claimed is None:
        return
    destination = json.loads(delivery.destination)
    try:
        response = await client.post("chat.postMessage", json={
            **destination, "text": runtime.security.unseal(delivery.content).decode(),
            "unfurl_links": False, "unfurl_media": False,
            "metadata": {"event_type": "moyai_delivery", "event_payload": {"id": delivery.id}},
        })
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError):
        # The persistent sending claim survives cancellation, lost ack and process
        # death. It is reconciled by reading Slack, never by posting it again.
        return
    # Receipt persistence is deliberately outside the ambiguous-send handler.
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "sending"
        ).values(
            status="sent" if result.get("ok") else "rejected",
            receipt=runtime.security.seal(result),
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
    response = await client.get("conversations.replies", headers=headers, params={
        "channel": destination["channel"], "ts": destination["thread_ts"],
        "limit": 100, "include_all_metadata": "true",
    })
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        return
    found = next((message for message in body.get("messages", []) if (
        (message.get("metadata") or {}).get("event_payload") or {}
    ).get("id") == delivery.id), None)
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
