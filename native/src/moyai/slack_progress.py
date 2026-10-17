"""One Slack reply per steering segment, using the durable delivery lane."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .models import Delivery, Event, SlackThread, Turn, new_id, now
from .slack_messages import session_url, thread_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .runtime import Runtime

TERMINAL = {"completed", "failed", "stopped", "waiting"}


def anchor_identity(turn_id: str, product: bool) -> str:
    return f"product-slack:{turn_id}:accepted" if product else f"slack-progress:{turn_id}"


async def latest_segment(db: AsyncSession, root: Turn) -> Turn:
    """Admission freezes the preceding slot, before native steering is acknowledged."""
    from .database import Database
    return await db.scalar(select(Turn).where(
        Turn.parent_turn == root.id, Turn.actor_id == root.actor_id,
        Turn.session_id == root.session_id,
        Turn.status.in_(["steering", "signal_sent", "accepted", "included", "unconfirmed"]),
    ).order_by(Database.guidance_order().desc().nulls_last(), Turn.created_at.desc(), Turn.id.desc()).limit(1)) or root


async def execution_turn(db: AsyncSession, delivery: Delivery) -> Turn | None:
    turn = await db.get(Turn, delivery.turn_id)
    if turn and turn.parent_turn:
        parent = await db.get(Turn, turn.parent_turn)
        if parent and parent.session_id == turn.session_id and parent.actor_id == turn.actor_id:
            return parent
    return turn


def segment_id(delivery: Delivery) -> str:
    return json.loads(delivery.destination).get("segment_id", delivery.turn_id)


def final_notice(delivery: Delivery, product: bool) -> bool:
    if not product:
        return delivery.identity == f"slack-answer:{delivery.turn_id}"
    suffix = delivery.identity.removeprefix(f"product-slack:{delivery.turn_id}:")
    return suffix.split(":")[0] in {"completed", "failed", "stopped", "waiting"}


async def obsolete_segment(db: AsyncSession, delivery: Delivery, root: Turn, *, product: bool) -> bool:
    destination = json.loads(delivery.destination)
    if destination.get("control"):
        return False
    current = await latest_segment(db, root)
    # Older queued final deliveries predate segment metadata. Resolve those to
    # the current slot without replaying or discarding an answer during upgrade.
    if "segment_id" not in destination and final_notice(delivery, product):
        return False
    return segment_id(delivery) != current.id


async def stage_progress(runtime: Runtime, *, product: bool = False) -> None:
    """Queue at most one commentary revision per 30 seconds; never send from here."""
    from app.slack_format import slack_text

    async with runtime.database.sessions() as db:
        pairs = (await db.execute(select(Turn, SlackThread).join(
            SlackThread, SlackThread.session_id == Turn.session_id,
        ).where(Turn.status == "running").order_by(Turn.created_at).limit(100))).all()
    cutoff = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
    for turn, thread in pairs:
        source_run_id = ""
        public_url = runtime.settings.public_url
        if product:
            from .product_slack_models import ProductSlackBinding
            async with runtime.database.sessions() as db:
                binding = await db.get(ProductSlackBinding, turn.session_id)
            source_run_id = binding.source_run_id or "" if binding else ""
            if runtime.product_slack:
                public_url = runtime.product_slack.settings.public_url
        url = session_url(public_url, turn.session_id, source_run_id)
        async with runtime.database.sessions() as db:
            segment = await latest_segment(db, turn)
            boundary = await db.scalar(select(Event.created_at).where(
                Event.turn_id == segment.id, Event.kind == "steering_admitted",
            ).order_by(Event.id.desc()).limit(1)) or segment.accepted_at or segment.created_at
            primary = anchor_identity(segment.id, product)
            prefix = (
                f"product-slack:{turn.id}:progress:{segment.id}:"
                if product else f"slack-commentary:{turn.id}:{segment.id}:"
            )
            anchor = await db.scalar(select(Delivery).where(Delivery.identity == primary))
            recent = await db.scalar(select(Delivery.id).where(
                Delivery.identity.startswith(prefix),
                or_(Delivery.created_at > cutoff, Delivery.status.in_(["pending", "sending"])),
            ).limit(1))
        if anchor is None:
            if product:
                # Admission normally reserves this slot; polling also repairs
                # a crash between admission and acknowledgement staging.
                if runtime.product_slack is None:
                    continue
                await runtime.product_slack.queue(
                    segment, thread, "accepted", {"text": "Moyai received your message."},
                )
            else:
                try:
                    async with runtime.database.sessions.begin() as db:
                        db.add(Delivery(
                            id=new_id(), identity=primary, session_id=turn.session_id, turn_id=segment.id,
                            destination=json.dumps({
                                "channel": thread.channel_id, "thread_ts": thread.thread_ts, "rich_reply": True,
                                "segment_id": segment.id,
                            }),
                            content=runtime.security.seal(thread_message("Moyai is working.", url)),
                        ))
                except IntegrityError:
                    pass
        if recent:
            continue
        work = await runtime.work_progress.read(turn)
        messages = [
            *work["messages"],
            *(
                {**message, "text": f"{agent['name']}: {message['text']}"}
                for agent in work["subagents"] for message in agent["messages"]
            ),
        ]
        if segment.id != turn.id:
            messages = [
                message for message in messages
                if message.get("created_at", "") >= boundary
            ]
        if not messages:
            continue
        latest = max(enumerate(messages), key=lambda item: (item[1].get("created_at", ""), item[0]))[1]
        identity = prefix + hashlib.sha256(latest["id"].encode()).hexdigest()[:32]
        message = slack_text(latest["text"][:2400])
        try:
            async with runtime.database.sessions.begin() as db:
                db.add(Delivery(
                    id=new_id(), identity=identity, session_id=turn.session_id, turn_id=turn.id,
                    destination=json.dumps({
                        "channel": thread.channel_id, "thread_ts": thread.thread_ts, "progress": True,
                        "rich_reply": True,
                        "segment_id": segment.id,
                    }),
                    content=runtime.security.seal(thread_message(message, url)),
                ))
        except IntegrityError:
            pass


async def skip_obsolete(runtime: Runtime, delivery: Delivery, *, product: bool) -> bool:
    """Discard only known-unsent placeholders, never an ambiguous external write."""
    if delivery.status != "pending":
        return False
    async with runtime.database.sessions() as db:
        turn = await execution_turn(db, delivery)
        if turn is None:
            return False
        if not obsolete_notice(delivery, turn.status, product) and not await obsolete_segment(
            db, delivery, turn, product=product,
        ):
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
    # Standalone publishes its answer before checkpoint settlement. Product
    # mode still needs its saving notice until the turn completes.
    return (
        (transient or destination.get("progress") is True)
        and (status in TERMINAL or (not product and status == "saving"))
    ) or (kind in {"queued", "running", "saving"} and kind != status)


async def claim_delivery(runtime: Runtime, delivery: Delivery, *, product: bool) -> bool:
    """Serialize all revisions of one reply, including across delivery pollers."""
    async with runtime.database.sessions.begin() as db:
        root = await execution_turn(db, delivery)
        if root is None:
            return False
        # Acquire the turn's write lock on both PostgreSQL and SQLite without
        # changing its status. A row lock alone is ignored by SQLite.
        status = await db.scalar(update(Turn).where(Turn.id == root.id).values(
            status=Turn.status,
        ).returning(Turn.status))
        if status is None:
            return False
        if obsolete_notice(delivery, status, product) or await obsolete_segment(db, delivery, root, product=product):
            await db.execute(update(Delivery).where(
                Delivery.id == delivery.id, Delivery.status == "pending",
            ).values(status="suppressed"))
            return False
        if await db.scalar(select(Delivery.id).where(
            Delivery.turn_id.in_(select(Turn.id).where(or_(
                Turn.id == root.id, Turn.parent_turn == root.id,
            ))),
            Delivery.id != delivery.id, Delivery.status == "sending",
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
    primary = anchor_identity(segment_id(delivery), product)
    if delivery.identity == anchor_identity(delivery.turn_id, product):
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
        root = await execution_turn(db, delivery)
        if root is None:
            return False, None
        if "segment_id" not in destination and final_notice(delivery, product):
            primary = anchor_identity((await latest_segment(db, root)).id, product)
        unresolved = await db.scalar(select(Delivery.id).where(
            Delivery.turn_id.in_(select(Turn.id).where(or_(
                Turn.id == root.id, Turn.parent_turn == root.id,
            ))),
            Delivery.id != delivery.id,
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
