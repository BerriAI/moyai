"""Coalesce native commentary into a confirmed Slack working message.

Final replies retain their existing durable outbox. Updating a known timestamp
is idempotent, and cannot overwrite or duplicate that separate final reply.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .models import Delivery, Event, Session, SlackThread, Turn, User, new_id, now
from .progress import turn_progress

if TYPE_CHECKING:
    from .runtime import Runtime

TERMINAL = {"completed", "failed", "stopped", "waiting"}


async def stage_progress(runtime: Runtime) -> None:
    """Standalone bot: reserve one working post, independently of its final answer."""
    async with runtime.database.sessions() as db:
        pairs = (await db.execute(select(Turn, SlackThread).join(
            SlackThread, SlackThread.session_id == Turn.session_id,
        ).where(
            Turn.status == "running",
            ~select(Delivery.id).where(Delivery.identity == ("slack-progress:" + Turn.id)).exists(),
        ).order_by(Turn.created_at).limit(20))).all()
    for turn, thread in pairs:
        try:
            async with runtime.database.sessions.begin() as db:
                db.add(Delivery(
                    id=new_id(), identity=f"slack-progress:{turn.id}",
                    session_id=turn.session_id, turn_id=turn.id,
                    destination=json.dumps({"channel": thread.channel_id, "thread_ts": thread.thread_ts}),
                    content=runtime.security.seal("Moyai is working."),
                ))
        except IntegrityError:
            pass


async def standalone_allowed(runtime: Runtime, delivery: Delivery) -> bool:
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, delivery.turn_id)
        actor = await db.get(User, turn.actor_id) if turn else None
        session = await db.get(Session, delivery.session_id)
        thread = await db.scalar(select(SlackThread).where(SlackThread.session_id == delivery.session_id))
    destination = json.loads(delivery.destination)
    return bool(
        actor and actor.enabled and session and not session.deleted and thread
        and destination.get("channel") == thread.channel_id
        and destination.get("thread_ts") == thread.thread_ts
        and thread.team_id == runtime.settings.slack_team_id
    )


async def sync_progress(
    runtime: Runtime, client: httpx.AsyncClient, *,
    product: bool = False,
    allowed: Callable[[Delivery], Awaitable[bool]],
    headers: Callable[[], Awaitable[dict[str, str]]] | None = None,
) -> None:
    """Only edit confirmed posts; concurrent pollers reserve each update with CAS."""
    pattern = "product-slack:%:running:0" if product else "slack-progress:%"
    cutoff = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).where(
            Delivery.status == "sent", Delivery.identity.like(pattern),
            or_(Delivery.claimed_at.is_(None), Delivery.claimed_at < cutoff),
            ~select(Event.id).where(
                Event.identity == ("slack-progress-finished:" + Delivery.id),
            ).exists(),
        ).order_by(Delivery.claimed_at.asc().nullsfirst(), Delivery.created_at).limit(20)))
    for delivery in rows:
        await update_progress(runtime, client, delivery, product=product, allowed=allowed, headers=headers)


async def update_progress(
    runtime: Runtime, client: httpx.AsyncClient, delivery: Delivery, *, product: bool,
    allowed: Callable[[Delivery], Awaitable[bool]],
    headers: Callable[[], Awaitable[dict[str, str]]] | None,
) -> None:
    stamp = now()
    cutoff = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    async with runtime.database.sessions.begin() as db:
        claimed = await db.scalar(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "sent",
            or_(Delivery.claimed_at.is_(None), Delivery.claimed_at < cutoff),
        ).values(claimed_at=stamp).returning(Delivery.id))
    if claimed is None:
        return
    if not delivery.receipt or not await allowed(delivery):
        return
    receipt = json.loads(runtime.security.unseal(delivery.receipt))
    timestamp = receipt.get("ts")
    if not isinstance(timestamp, str) or not timestamp:
        return
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, delivery.turn_id)
    if turn is None:
        return
    terminal = turn.status in TERMINAL
    message = {
        "completed": "Work completed.", "failed": "Work stopped with an error. Open Moyai for details.",
        "stopped": "Work stopped.", "waiting": "Waiting for your response.",
    }.get(turn.status)
    if message is None:
        progress = await turn_progress(runtime, turn)
        if not progress:
            return
        message = progress[-1]["text"][:2400]
    from app.slack_format import slack_text
    message = slack_text(message)
    if runtime.settings.public_url:
        message += f"\n<{runtime.settings.public_url.rstrip('/')}/#{turn.session_id}|Open Moyai>"
    current = runtime.security.unseal(delivery.content).decode()
    if product:
        current = json.loads(current).get("text", "")
    if current == message:
        return
    request_headers = await headers() if headers else None
    if not await allowed(delivery):
        return
    # Longer than the bot's 20-second HTTP timeout, so another poller cannot
    # replace a still-running update. Final sends use separate delivery rows.
    if not await allowed(delivery):
        return
    try:
        response = await client.post("chat.update", headers=request_headers, json={
            "channel": json.loads(delivery.destination)["channel"], "ts": timestamp,
            "text": message, "blocks": [],
        })
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            return
    except (httpx.HTTPError, ValueError):
        # A lost update acknowledgement can be retried at this known timestamp.
        # It never changes the receipt or ownership of the original post.
        return
    # Confirmed updates and their terminal marker settle together. Cancellation
    # is allowed to propagate only after this transaction has finished.
    async def settle():
        async with runtime.database.sessions.begin() as db:
            changed = await db.scalar(update(Delivery).where(
                Delivery.id == delivery.id, Delivery.status == "sent", Delivery.claimed_at == stamp,
            ).values(content=runtime.security.seal({"text": message} if product else message)).returning(Delivery.id))
            if changed and terminal:
                db.add(Event(
                    session_id=delivery.session_id, turn_id=delivery.turn_id,
                    kind="slack_progress_finished", content=runtime.security.seal({}),
                    identity=f"slack-progress-finished:{delivery.id}",
                ))

    task = asyncio.create_task(settle())
    cancelled = None
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
    if cancelled is not None:
        raise cancelled
