"""Explicit web-session DM linking; provider IDs never come from the client."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any, Literal, cast

from app.connector_errors import ConnectorError
from app.db import database as source_database
from app.slack_format import ID, STAMP
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from .models import Message, Session, SlackThread, User
from .product_identity import ProductGrant
from .product_slack_models import ProductSlackBinding, SlackWebLink, SlackWebTurn
from .slack_messages import session_url
from .slack_recipient import SlackRecipient, actor_grant, current, lookup

if TYPE_CHECKING:
    from .product_slack import ProductSlack


class SlackWebStatus(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    status: Literal["available", "linked", "pending"]
    title: str
    context: str
    url: str | None = None


async def check_turn(database, db, turn) -> None:
    """Revalidate signed Slack authority at every saved-turn release boundary."""
    source = await db.get(SlackWebTurn, turn.id)
    if source is not None:
        await database._identity().authorize_admission(
            db, ProductGrant(source.requester_id, source.authority_version),
        )
        await check_transport_authority(database, db, turn.session_id)


async def check_transport_authority(database, db, session_id: str) -> None:
    link = await db.get(SlackWebLink, session_id)
    if link is None or link.slack_app != "dot":
        return
    from .product_slack import SlackInstallationUnavailable

    installation = database.slack_dot_installation
    if installation is None:
        raise HTTPException(403, "Dot Slack authority is unavailable.")
    try:
        installed = await installation(db)
    except SlackInstallationUnavailable as error:
        raise HTTPException(403, "Dot Slack authority is unavailable.") from error
    if (installed["authority"] != link.connection_authority or installed["team_id"] != link.team_id):
        raise HTTPException(403, "The Dot Slack installation changed.")


class SlackWeb:
    def __init__(self, slack: ProductSlack):
        self.slack = slack
        self.database = slack.runtime.database
        self.identity = slack.product.identity

    async def owner(self, db, actor: User, session_id: str) -> Session:
        principal = await self.identity.authorize_admission(db, getattr(actor, "product_grant", None))
        session = await self.database.allowed_session(db, session_id, actor)
        if not principal.individual or session.owner_id != actor.id or session.surface != "web":
            raise HTTPException(403, "Only the owner of a web session can continue it in Slack.")
        from .delegation_models import DelegatedSession
        from .swarm_models import SwarmMember
        if await db.get(DelegatedSession, session_id) or await db.get(SwarmMember, session_id):
            raise HTTPException(409, "Open the main conversation to continue in Slack.")
        return session

    async def view(self, db, session: Session) -> SlackWebStatus:
        link = await db.get(SlackWebLink, session.id)
        message = await db.scalar(select(Message).where(
            Message.session_id == session.id, Message.role == "user",
        ).order_by(Message.id.desc()).limit(1))
        context = "Reply in this thread to continue this Moyai session."
        if message is not None:
            from .message_sources import public_message
            content, _ = await public_message(self.database, db, message)
            context = " ".join(content.split())[:180] or context
        return SlackWebStatus(
            status="linked" if link and link.status == "linked"
            else "pending" if link and link.status == "sending" else "available",
            title=session.title, context=context,
            url=(f"https://app.slack.com/client/{link.team_id}/{link.channel_id}/thread/"
                 f"{link.channel_id}-{link.thread_ts}") if link and link.thread_ts else None,
        )

    async def status(self, actor: User, session_id: str) -> SlackWebStatus:
        async with self.database.sessions.begin() as db:
            session = await self.owner(db, actor, session_id)
            return await self.view(db, session)

    async def recipient(self, actor: User, installed: dict) -> User:
        return await lookup(self.slack, actor, installed)

    async def current(self, db, link: SlackWebLink, *, sender: User | None = None) -> User:
        actor = await current(self.slack, db, SlackRecipient.model_validate(link), sender=sender)
        session = await db.get(Session, link.session_id)
        if (session is None or session.deleted or session.owner_id != actor.id
                or session.surface != "web"):
            raise HTTPException(403, "The linked web session is unavailable.")
        from .dot_models import DotSlackConversation, DotSlackPreference
        conversation = await db.get(DotSlackConversation, session.id)
        if self.slack.app == "dot" and conversation is None:
            raise HTTPException(403, "Dot only continues private Dot conversations.")
        if conversation is not None:
            if session.visibility != "private":
                raise HTTPException(403, "The connected Dot conversation is no longer private.")
            preference = await db.get(DotSlackPreference, actor.id)
            if (preference is None or preference.recipient is None
                    or preference.session_id != conversation.dot_session_id
                    or SlackRecipient.model_validate_json(self.database.security.unseal(preference.recipient))
                    != SlackRecipient.model_validate(link)):
                raise HTTPException(403, "The connected Dot identity changed.")
            await self.database.dots.own_session(db, actor.id, conversation.dot_session_id)
        return actor

    async def input_actor(self, sender: User, session_id: str) -> User:
        async with self.database.sessions.begin() as db:
            link = await db.get(SlackWebLink, session_id)
            if link is None:
                if self.slack.app == "dot":
                    raise HTTPException(403, "This Dot conversation has no linked direct message.")
                return sender
            if link.status != "linked":
                raise HTTPException(409, "This session is still connecting to Slack.")
            actor = await self.current(db, link, sender=sender)
            cast(Any, actor).slack_web_grant = actor_grant(sender)
            return actor

    async def connect(self, actor: User, session_id: str) -> SlackWebStatus:
        await self.status(actor, session_id)  # Authorize before looking up a recipient.
        installed = await source_database(self.slack.installation)
        sender = await self.recipient(actor, installed)
        token = await self.slack.bot_token()
        opened = await self.slack.connectors.request(
            "POST", "https://slack.com/api/conversations.open",
            headers={"Authorization": "Bearer " + token},
            json={"users": sender.id.split(":")[2]},
        )
        channel = (opened.get("channel") or {}).get("id", "")
        if opened.get("ok") is not True or not channel.startswith("D") or not ID.fullmatch(channel):
            raise HTTPException(502, "Slack did not confirm a direct-message conversation.")
        async with self.database.sessions.begin() as db:
            session = await self.owner(db, actor, session_id)
            await db.refresh(session, with_for_update=True)
            link = await db.get(SlackWebLink, session_id)
            first = link is None or link.status == "failed"
            if link is None:
                link = SlackWebLink(
                    session_id=session_id, owner_id=actor.id, owner_authority=actor_grant(actor).authority_version,
                    requester_id=sender.id, team_id=installed["team_id"], channel_id=channel,
                    connection_authority=installed["authority"], status="sending", slack_app="dev",
                )
                db.add(link)
                await db.flush()
            elif first:
                link.status = "sending"
            await self.current(db, link, sender=sender)
            view = await self.view(db, session)
            if link.status == "linked":
                return view
            if link.channel_id != channel:
                raise HTTPException(409, "The Slack destination changed. Review the existing link.")
        # Persist "sending" BEFORE the external effect. An uncertain first send
        # is reconciled by metadata; concurrent/retried calls never resend it.
        metadata = {"event_type": "moyai_web_session", "event_payload": {"session_id": session_id}}
        if first:
            url = session_url(self.slack.settings.public_url, session_id, "")
            try:
                result = await self.slack.connectors.request(
                    "POST", "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": "Bearer " + token},
                    json={"channel": channel,
                          "text": f"*{html.escape(view.title, quote=False)}*\n"
                                  f"{html.escape(view.context, quote=False)}\n"
                                  f"<{url}|Open in Moyai> · Reply in this thread to continue.",
                          "metadata": metadata, "unfurl_links": False, "unfurl_media": False},
                )
            except ConnectorError as error:
                if error.rejected or error.dispatched is False:
                    async with self.database.sessions.begin() as db:
                        saved = await db.get(SlackWebLink, session_id)
                        if saved and saved.status == "sending":
                            saved.status = "failed"
                raise
            stamp = result.get("ts") if result.get("ok") is True and result.get("channel") == channel else None
        else:
            result = await self.slack.connectors.request(
                "GET", "https://slack.com/api/conversations.history",
                headers={"Authorization": "Bearer " + token},
                params={"channel": channel, "limit": 100, "include_all_metadata": "true"},
            )
            matches = [item.get("ts") for item in result.get("messages", [])
                       if item.get("metadata") == metadata and item.get("user") == installed["user_id"]]
            stamp = matches[0] if len(matches) == 1 else None
        if not isinstance(stamp, str) or not STAMP.fullmatch(stamp):
            raise HTTPException(409, "Slack delivery is not yet confirmed. Check again to recover the existing DM.")
        async with self.database.sessions.begin() as db:
            session = await self.owner(db, actor, session_id)
            await db.refresh(session, with_for_update=True)
            link = await db.get(SlackWebLink, session_id)
            assert link is not None
            await self.current(db, link, sender=sender)
            if link.status != "linked":
                link.status, link.thread_ts = "linked", stamp
                db.add(SlackThread(session_id=session_id, team_id=link.team_id,
                                   channel_id=channel, thread_ts=stamp, slack_app="dev"))
                db.add(ProductSlackBinding(
                    session_id=session_id, requester_id=sender.id, team_id=link.team_id,
                    channel_id=channel, thread_ts=stamp, started_ts=stamp,
                    connection_authority=link.connection_authority, delivery_after=link.created_at,
                    slack_app="dev",
                ))
            return await self.view(db, session)

    def routes(self, actor):
        router = APIRouter()

        @router.get("/api/sessions/{session_id}/slack", operation_id="getSessionSlack")
        async def status(session_id: str, user: User = Depends(actor)) -> SlackWebStatus:
            return await self.status(user, session_id)

        @router.post("/api/sessions/{session_id}/slack", operation_id="connectSessionSlack")
        async def connect(session_id: str, user: User = Depends(actor)) -> SlackWebStatus:
            try:
                return await self.connect(user, session_id)
            except ConnectorError as error:
                raise HTTPException(502, "Slack could not confirm this operation. "
                                    "Check the bot connection, then try again to recover the existing DM.") from error

        return router
