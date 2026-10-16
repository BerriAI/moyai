"""One Slack reply per turn, edited through the existing durable delivery lane."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .models import Delivery, SlackThread, Turn, new_id, now
from .progress import turn_progress

if TYPE_CHECKING:
    from .runtime import Runtime

TERMINAL = {"completed", "failed", "stopped", "waiting"}


def anchor_identity(turn_id: str, product: bool) -> str:
    return f"product-slack:{turn_id}:accepted" if product else f"slack-progress:{turn_id}"


async def stage_progress(runtime: Runtime, *, product: bool = False) -> None:
    """Queue at most one commentary revision per 30 seconds; never send from here."""
    from app.slack_format import slack_text

    async with runtime.database.sessions() as db:
        pairs = (await db.execute(select(Turn, SlackThread).join(
            SlackThread, SlackThread.session_id == Turn.session_id,
        ).where(Turn.status == "running").order_by(Turn.created_at).limit(100))).all()
    cutoff = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    for turn, thread in pairs:
        primary = anchor_identity(turn.id, product)
        prefix = f"product-slack:{turn.id}:progress:" if product else f"slack-commentary:{turn.id}:"
        async with runtime.database.sessions() as db:
            anchor = await db.scalar(select(Delivery).where(Delivery.identity == primary))
            recent = await db.scalar(select(Delivery.id).where(
                Delivery.identity.startswith(prefix),
                or_(Delivery.created_at > cutoff, Delivery.status.in_(["pending", "sending"])),
            ).limit(1))
        if anchor is None:
            if product:
                continue
            try:
                async with runtime.database.sessions.begin() as db:
                    db.add(Delivery(
                        id=new_id(), identity=primary, session_id=turn.session_id, turn_id=turn.id,
                        destination=json.dumps({"channel": thread.channel_id, "thread_ts": thread.thread_ts}),
                        content=runtime.security.seal("Moyai is working."),
                    ))
            except IntegrityError:
                pass
        if recent:
            continue
        messages = await turn_progress(runtime, turn)
        if not messages:
            continue
        latest = messages[-1]
        identity = prefix + hashlib.sha256(latest["id"].encode()).hexdigest()[:32]
        message = slack_text(latest["text"][:2400])
        if runtime.settings.public_url:
            message += f"\n<{runtime.settings.public_url.rstrip('/')}/#{turn.session_id}|Open Moyai>"
        try:
            async with runtime.database.sessions.begin() as db:
                db.add(Delivery(
                    id=new_id(), identity=identity, session_id=turn.session_id, turn_id=turn.id,
                    destination=json.dumps({
                        "channel": thread.channel_id, "thread_ts": thread.thread_ts, "progress": True,
                    }),
                    content=runtime.security.seal({"text": message} if product else message),
                ))
        except IntegrityError:
            pass


async def skip_obsolete(runtime: Runtime, delivery: Delivery, *, product: bool) -> bool:
    """Discard only known-unsent placeholders, never an ambiguous external write."""
    if delivery.status != "pending":
        return False
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, delivery.turn_id)
    if turn is None:
        return False
    if not obsolete_notice(delivery, turn.status, product):
        return False
    async with runtime.database.sessions.begin() as db:
        changed = await db.scalar(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "pending",
        ).values(status="suppressed").returning(Delivery.id))
    return changed is not None


def obsolete_notice(delivery: Delivery, status: str, product: bool) -> bool:
    destination = json.loads(delivery.destination)
    kind = delivery.identity.removeprefix(f"product-slack:{delivery.turn_id}:").split(":")[0] if product else ""
    transient = kind in {"accepted", "queued", "running", "saving"} if product else (
        delivery.identity == anchor_identity(delivery.turn_id, False)
    )
    return (
        (transient or destination.get("progress") is True) and status in TERMINAL
    ) or (kind in {"queued", "running", "saving"} and kind != status)


async def claim_delivery(runtime: Runtime, delivery: Delivery, *, product: bool) -> bool:
    """Serialize all revisions of one reply, including across delivery pollers."""
    async with runtime.database.sessions.begin() as db:
        # Acquire the turn's write lock on both PostgreSQL and SQLite without
        # changing its status. A row lock alone is ignored by SQLite.
        status = await db.scalar(update(Turn).where(Turn.id == delivery.turn_id).values(
            status=Turn.status,
        ).returning(Turn.status))
        if status is None:
            return False
        if obsolete_notice(delivery, status, product):
            await db.execute(update(Delivery).where(
                Delivery.id == delivery.id, Delivery.status == "pending",
            ).values(status="suppressed"))
            return False
        if await db.scalar(select(Delivery.id).where(
            Delivery.turn_id == delivery.turn_id, Delivery.id != delivery.id, Delivery.status == "sending",
        ).limit(1)):
            return False
        claimed = await db.scalar(update(Delivery).where(
            Delivery.id == delivery.id, Delivery.status == "pending",
        ).values(status="sending", claimed_at=now()).returning(Delivery.id))
        return claimed is not None


async def reply_target(runtime: Runtime, delivery: Delivery, *, product: bool) -> tuple[bool, str | None]:
    """Wait for earlier ambiguous writes, then reuse the confirmed primary reply.

    The first part of a long final answer replaces the primary reply; overflow
    parts retain existing chunked delivery. Control notices remain independent.
    """
    destination = json.loads(delivery.destination)
    if destination.get("control"):
        return True, None
    primary = anchor_identity(delivery.turn_id, product)
    if delivery.identity == primary:
        return True, None
    if product and destination.get("progress") is not True:
        suffix = delivery.identity.removeprefix(f"product-slack:{delivery.turn_id}:")
        if not suffix.endswith(":0"):
            return True, None
    if not product and not (
        delivery.identity == f"slack-answer:{delivery.turn_id}" or destination.get("progress") is True
    ):
        return True, None
    async with runtime.database.sessions() as db:
        unresolved = await db.scalar(select(Delivery.id).where(
            Delivery.turn_id == delivery.turn_id, Delivery.id != delivery.id,
            Delivery.status == "sending",
        ).limit(1))
        if unresolved:
            return False, None
        anchor = await db.scalar(select(Delivery).where(Delivery.identity == primary))
        if anchor is None or anchor.status in {"suppressed", "rejected"}:
            return (destination.get("progress") is not True), None
        if anchor.status != "sent" or not anchor.receipt:
            return False, None
        receipt = json.loads(runtime.security.unseal(anchor.receipt))
        timestamp = receipt.get("ts")
        return (True, timestamp) if isinstance(timestamp, str) and timestamp else (False, None)
