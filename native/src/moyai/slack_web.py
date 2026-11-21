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

if TYPE_CHECKING:
    from .product_slack import ProductSlack


class SlackWebStatus(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    status: Literal["available", "linked", "pending"]
    title: str
    context: str
    url: str | None = None


def actor_grant(actor: User) -> ProductGrant:
    grant = getattr(actor, "product_grant", None)
    if not isinstance(grant, ProductGrant) or grant.user_id != actor.id:
        raise HTTPException(401, "Fresh product authorization is required.")
    return grant


async def check_turn(database, db, turn) -> None:
    """Revalidate signed Slack authority at every saved-turn release boundary."""
    source = await db.get(SlackWebTurn, turn.id)
    if source is not None:
        await database._identity().authorize_admission(
            db, ProductGrant(source.requester_id, source.authority_version),
        )


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
        settings = self.slack.settings
        if not (settings.slack_bot_enabled and settings.slack_identity_linking_enabled
                and settings.slack_dm_enabled and settings.slack_thread_chat_enabled):
            raise HTTPException(409, "Enable Slack identity linking and direct-message conversations first.")
        principal = await self.identity.require_current(actor_grant(actor))
        if not principal.individual or principal.email.rpartition("@")[2] not in settings.identity_domains():
            raise HTTPException(403, "An eligible organization sign-in is required.")
        token = await self.slack.connectors.slack_bot_token()
        result = await self.slack.connectors.request(
            "GET", "https://slack.com/api/users.lookupByEmail",
            headers={"Authorization": "Bearer " + token}, params={"email": principal.email},
            allowed_errors=("users_not_found", "missing_scope"),
        )
        profile = result.get("user") or {}
        recipient = profile.get("id")
        if (result.get("ok") is not True or not isinstance(recipient, str) or not ID.fullmatch(recipient)
                or profile.get("team_id") != installed["team_id"]):
            raise HTTPException(409, "Your Slack account could not be verified. Check the bot's profile permissions.")
        allowed = {value.strip() for value in settings.slack_session_users.split(",")}
        if "*" not in allowed and recipient not in allowed:
            raise HTTPException(403, "Your Slack account is not enabled for Moyai conversations.")
        sender, _ = await self.slack.actor(installed["team_id"], recipient, refresh_profile=True)
        async with self.database.sessions.begin() as db:
            individual = await self.identity.individual_for_admission(db, actor_grant(sender))
            if individual.id != actor.id:
                raise HTTPException(403, "Your Slack account link needs review.")
        return sender

    async def current(self, db, link: SlackWebLink, *, sender: User | None = None) -> User:
        installed = await self.slack.installation_in(db)
        if installed["authority"] != link.connection_authority or not self.slack.settings.slack_dm_enabled:
            raise HTTPException(403, "The linked Slack installation changed.")
        owner = await self.identity.authorize_admission(db, ProductGrant(link.owner_id, link.owner_authority))
        if sender is not None:
            if sender.id != link.requester_id:
                raise HTTPException(403, "This DM belongs to a different Slack account.")
            individual = await self.identity.individual_for_admission(db, actor_grant(sender))
            if individual.id != owner.id:
                raise HTTPException(403, "Your Slack account link needs review.")
        else:
            # Delivery must stop when the verified recipient is revoked or reassigned.
            raw = await self.identity._current(
                db, link.requester_id, await self.identity._native_controls(link.requester_id, db),
            )
            individual = await self.identity.individual_for_admission(db, raw.grant)
            if individual.id != owner.id:
                raise HTTPException(403, "The linked Slack recipient changed.")
        session = await db.get(Session, link.session_id)
        if session is None or session.deleted or session.owner_id != owner.id or session.surface != "web":
            raise HTTPException(403, "The linked web session is unavailable.")
        actor = await self.identity.read_native_user(db, owner)
        if actor is None:
            raise HTTPException(403, "The session owner is unavailable.")
        return actor

    async def input_actor(self, sender: User, session_id: str) -> User:
        async with self.database.sessions.begin() as db:
            link = await db.get(SlackWebLink, session_id)
            if link is None:
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
        token = await self.slack.connectors.slack_bot_token()
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
                    connection_authority=installed["authority"], status="sending",
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
                                   channel_id=channel, thread_ts=stamp))
                db.add(ProductSlackBinding(
                    session_id=session_id, requester_id=sender.id, team_id=link.team_id,
                    channel_id=channel, thread_ts=stamp, started_ts=stamp,
                    connection_authority=link.connection_authority, delivery_after=link.created_at,
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
