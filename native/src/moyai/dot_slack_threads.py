"""Map each Dot DM root to the existing private Slack conversation machinery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.db import database as source_database
from app.slack_format import STAMP
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import select

from .contracts import NewSession
from .dot_models import DotNotification, DotSlackConversation, DotSlackPreference
from .models import Delivery, Message, Session, SlackThread, User
from .product_slack_models import ProductSlackBinding, SlackWebLink
from .slack_messages import plain_slack_title
from .slack_recipient import SlackRecipient, actor_grant, current

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .product_slack import ProductSlack


async def create(
    database: Database, db: AsyncSession, actor: User, preference: DotSlackPreference,
    recipient: SlackRecipient, *, title: str, notification: DotNotification | None = None,
) -> Session:
    session = await database.create_session(
        actor, NewSession(title="Dot · " + plain_slack_title(title)[:90], visibility="private"), db=db,
    )
    # Unconfirmed outgoing roots do not clutter Recents or accept replies.
    session.archived = notification is not None
    await db.flush()
    db.add(DotSlackConversation(
        session_id=session.id, dot_session_id=preference.session_id,
        notification_turn_id=notification.turn_id if notification else None,
    ))
    db.add(SlackWebLink(
        session_id=session.id, **recipient.model_dump(), status="sending",
    ))
    if notification is not None:
        # Seed ordinary public conversation history once. This also gives web
        # replies the finding and keeps Slack event fingerprints retry-stable.
        session.conversation = database.security.seal(ConversationTypeAdapter.dump_json(
            Conversation(messages=[ModelResponse(parts=[TextPart(
                database.security.unseal(notification.content).decode(),
            )])]),
        ))
        db.add(Message(
            session_id=session.id, turn_id=notification.turn_id, role="assistant",
            content=notification.content, identity=f"dot-slack:{notification.turn_id}",
        ))
    return session


async def bind(db: AsyncSession, session: Session, link: SlackWebLink, stamp: str) -> SlackThread:
    if not STAMP.fullmatch(stamp):
        raise HTTPException(409, "Slack has not confirmed the conversation root.")
    existing = await db.scalar(select(SlackThread).where(SlackThread.session_id == session.id))
    if existing is not None:
        if existing.thread_ts != stamp:
            raise HTTPException(409, "The Slack conversation root changed.")
        return existing
    link.thread_ts, link.status, session.archived = stamp, "linked", False
    thread = SlackThread(
        session_id=session.id, team_id=link.team_id, channel_id=link.channel_id, thread_ts=stamp,
    )
    db.add(thread)
    db.add(ProductSlackBinding(
        session_id=session.id, requester_id=link.requester_id, team_id=link.team_id,
        channel_id=link.channel_id, thread_ts=stamp, started_ts=stamp,
        connection_authority=link.connection_authority, delivery_after=link.created_at,
    ))
    await db.flush()
    return thread


async def finalize(database: Database, db: AsyncSession, delivery: Delivery, receipt: dict) -> None:
    """Persist the confirmed root in the same transaction as its send receipt."""
    session = await db.get(Session, delivery.session_id, with_for_update=True)
    link = await db.get(SlackWebLink, delivery.session_id, populate_existing=True)
    if session is None or link is None:
        raise RuntimeError("A Dot delivery must retain its private conversation.")
    if receipt.get("channel") != link.channel_id:
        raise ValueError("Slack confirmed a different conversation.")
    await bind(db, session, link, receipt.get("ts", ""))


async def recover_root(slack: ProductSlack, sender: User, origin: dict[str, str]) -> None:
    """Resolve an early reply using the existing outbox, without another send."""
    if origin["thread_ts"] == origin["message_ts"]:
        return
    database = slack.runtime.database
    async with database.sessions() as db:
        rows = list(await db.scalars(select(Delivery).join(
            SlackWebLink, SlackWebLink.session_id == Delivery.session_id,
        ).where(
            SlackWebLink.requester_id == sender.id, SlackWebLink.team_id == origin["team_id"],
            SlackWebLink.channel_id == origin["channel_id"], Delivery.status == "sending",
            Delivery.identity == ("product-slack:" + Delivery.turn_id + ":dot"),
        ).limit(100)))
    if not rows:
        return
    from .dot_notifications import allowed
    from .slack import confirmed_delivery
    rows = [delivery for delivery in rows if await allowed(slack, delivery, reconcile=True)]
    if not rows:
        return
    installed = await source_database(slack.installation)
    token = await slack.connectors.slack_bot_token()
    result = await slack.connectors.request(
        "GET", "https://slack.com/api/conversations.history",
        headers={"Authorization": "Bearer " + token}, params={
            "channel": origin["channel_id"], "oldest": origin["thread_ts"], "latest": origin["thread_ts"],
            "inclusive": "true", "limit": 1, "include_all_metadata": "true",
        },
    )
    for delivery in rows:
        found = confirmed_delivery(
            delivery.id, result.get("messages", []), bot_user=installed["user_id"], root_ts=origin["thread_ts"],
        )
        if found is not None and await allowed(slack, delivery, reconcile=True):
            await slack._settle(delivery.id, "sent", {**found, "channel": origin["channel_id"]})


async def new_direct_message(
    slack: ProductSlack, sender: User, origin: dict[str, str], title: str,
) -> SlackThread | None:
    """Only a previously opted-in, currently verified owner gets private Dot DMs."""
    if not origin["channel_id"].startswith("D") or not slack.settings.slack_identity_linking_enabled:
        return None
    database = slack.runtime.database
    async with database.sessions.begin() as db:
        try:
            individual = await slack.product.identity.individual_for_admission(db, actor_grant(sender))
        except HTTPException as error:
            if error.status_code >= 500:
                raise
            return None
        preference = await db.get(DotSlackPreference, individual.id)
        if preference is None or preference.recipient is None:
            return None
        await db.refresh(preference, with_for_update=True)
        recipient = SlackRecipient.model_validate_json(database.security.unseal(preference.recipient))
        if recipient.team_id != origin["team_id"] or recipient.channel_id != origin["channel_id"]:
            raise HTTPException(403, "This is not your connected Dot DM.")
        actor = await current(slack, db, recipient, sender=sender)
        await database.dots.own_session(db, actor.id, preference.session_id)
        # Serialize duplicate first events with owner opt-in/out and publication.
        existing = await db.scalar(select(SlackThread).where(
            SlackThread.team_id == origin["team_id"], SlackThread.channel_id == origin["channel_id"],
            SlackThread.thread_ts == origin["thread_ts"],
        ))
        if existing is not None:
            return existing
        if origin["thread_ts"] != origin["message_ts"]:
            # Never turn an unconfirmed proactive root into an unrelated/shared chat.
            pending = await db.scalar(select(Delivery.id).join(
                SlackWebLink, SlackWebLink.session_id == Delivery.session_id,
            ).where(
                SlackWebLink.requester_id == sender.id, SlackWebLink.channel_id == recipient.channel_id,
                Delivery.status.in_(("pending", "sending")),
                Delivery.identity == ("product-slack:" + Delivery.turn_id + ":dot"),
            ).limit(1))
            if pending:
                # The signed event stays in HookInbox, which owns bounded retry
                # and expiry; a 409 here would permanently discard the reply.
                raise HTTPException(503, "This Dot thread is still confirming delivery.",
                                    headers={"Retry-After": "5"})
            raise HTTPException(403, "This is not a connected Dot conversation.")
        session = await create(database, db, actor, preference, recipient, title=title)
        await db.flush()
        link = await db.get(SlackWebLink, session.id)
        assert link is not None
        return await bind(db, session, link, origin["thread_ts"])


async def refresh_recipient(slack: ProductSlack, delivery: Delivery) -> None:
    async with slack.runtime.database.sessions() as db:
        link = await db.get(SlackWebLink, delivery.session_id)
        if link is None:
            raise HTTPException(403, "The notification recipient is unavailable.")
        team, user = link.team_id, link.requester_id.split(":")[2]
    # Refresh at the effect boundary; a stale cached email mapping is insufficient.
    await slack.actor(team, user, refresh_profile=True)
