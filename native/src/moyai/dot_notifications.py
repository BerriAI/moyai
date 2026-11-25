"""Owner-controlled Dot notices on the canonical transactional Slack outbox."""

from __future__ import annotations

import html
import json
from asyncio import sleep
from datetime import timedelta
from email.utils import format_datetime, parsedate_to_datetime
from typing import TYPE_CHECKING, Literal

from app.connector_errors import ConnectorError
from app.db import database as source_database
from app.slack_apps import saved_slack_app
from app.slack_format import ID
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .dot_models import DotNotification, DotSlackConversation, DotSlackPreference, PersonalDot
from .models import Delivery, Session, Turn, User, now
from .product_slack_models import SlackWebLink
from .slack_recipient import SlackRecipient, actor_grant, current, enabled, lookup

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .product_slack import ProductSlack
    from .runtime import Runtime


DotDeliveryStatus = Literal["pending", "sending", "sent", "rejected", "suppressed"]
SLACK_CLOCK_GUARD_SECONDS = 2


class DotNotificationSettings(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    revision: int
    enabled: bool
    status: Literal["off", "on", "unavailable", "needs_attention"]
    description: str
    last_delivery: DotDeliveryStatus | None = None


class DotNotificationEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    enabled: bool


def is_dot_delivery(delivery: Delivery) -> bool:
    return delivery.identity == f"product-slack:{delivery.turn_id}:dot"


async def queue(
    database: Database, db: AsyncSession, turn: Turn, session_id: str, content: str,
) -> None:
    # Publication and preference writes both hold the owner and Dot row locks.
    preference = await db.get(DotSlackPreference, turn.actor_id)
    if not preference or not preference.enabled or preference.session_id != session_id or not preference.recipient:
        return
    recipient = SlackRecipient.model_validate_json(database.security.unseal(preference.recipient))
    if recipient.slack_app != "dot" or saved_slack_app(preference.slack_app) != "dot":
        return
    principal = await database._identity().authorize_admission(db, await database._saved_product_grant(db, turn))
    actor = await database._identity().read_native_user(db, principal)
    assert actor is not None
    await db.flush()
    notification = await db.get(DotNotification, turn.id)
    assert notification is not None
    from .dot_slack_threads import create
    conversation = await create(
        database, db, actor, preference, recipient, title=content.splitlines()[0], notification=notification,
    )
    db.add(Delivery(
        identity=f"product-slack:{turn.id}:dot", session_id=conversation.id, turn_id=turn.id, slack_app="dot",
        destination=json.dumps({
            "channel": recipient.channel_id, "thread_ts": "", "dot_revision": preference.revision,
        }),
        # Store only the already-published notification, never research or chat history.
        content=database.security.seal({"text": content}),
    ))


async def allowed(slack: ProductSlack, delivery: Delivery, *, reconcile: bool = False) -> bool:
    if saved_slack_app(delivery.slack_app) != slack.app or (slack.app == "dev" and not reconcile):
        return False
    database = slack.runtime.database
    try:
        async with database.sessions.begin() as db:
            notification = await db.get(DotNotification, delivery.turn_id)
            conversation = await db.get(DotSlackConversation, delivery.session_id)
            session = await db.get(Session, delivery.session_id)
            link = await db.get(SlackWebLink, delivery.session_id)
            turn = await db.get(Turn, delivery.turn_id)
            if (notification is None or turn is None or conversation is None
                    or session is None or session.deleted or session.visibility != "private"
                    or session.surface != "web" or session.owner_id != turn.actor_id or link is None
                    or conversation.notification_turn_id != notification.turn_id
                    or conversation.dot_session_id != notification.session_id):
                return False
            preference = await db.get(DotSlackPreference, turn.actor_id)
            destination = json.loads(delivery.destination)
            recipient = SlackRecipient.model_validate(link)
            if not (reconcile and slack.app == "dev") and (
                    preference is None or not preference.recipient
                    or saved_slack_app(preference.slack_app) != slack.app
                    or preference.session_id != conversation.dot_session_id
                    or (not reconcile and (
                        not preference.enabled or preference.revision != destination.get("dot_revision")
                    ))
                    or SlackRecipient.model_validate_json(database.security.unseal(preference.recipient)) != recipient):
                return False
            if destination.get("channel") != recipient.channel_id or destination.get("thread_ts") != "":
                return False
            await current(slack, db, recipient)
            if reconcile:
                # Recover a confirmed external effect even after notification
                # pause; no new message or observer authority is exercised.
                return True
            await database._check_product_turn(db, turn, await database._saved_product_grant(db, turn))
            record = await database.dots.authorize(db, turn)
            return bool(record and record.destination_session_id == conversation.dot_session_id)
    except HTTPException as error:
        if error.status_code >= 500:
            raise
        return False
    except PermissionError:
        return False


class DotNotifications:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.database = runtime.database

    async def owner(self, db: AsyncSession, actor: User) -> str:
        await self.database._actor_product_grant(db, actor)
        dot = await db.get(PersonalDot, actor.id)
        if dot is None:
            raise HTTPException(409, "Open your Dot before setting up notifications.")
        await self.database.dots.own_session(db, actor.id, dot.session_id)
        return dot.session_id

    async def read(self, actor: User) -> DotNotificationSettings:
        async with self.database.sessions.begin() as db:
            session_id = await self.owner(db, actor)
            row = await db.get(DotSlackPreference, actor.id)
            result = DotNotificationSettings(
                revision=row.revision if row else 0, enabled=bool(row and row.enabled),
                status="off", description="Get updates in your Slack DM. Reply in each message’s thread, or send a new DM to start another chat.",
            )
            slack = self.runtime.product_slack.dot if self.runtime.product_slack else None
            if slack is None or not enabled(slack) or not slack.settings.public_url:
                result.status = "unavailable"
                result.description = "Slack notifications need the workspace bot, verified account linking, and a Moyai URL."
                return result
            from .product_slack import SlackInstallationUnavailable
            try:
                await slack.installation_in(db)
            except SlackInstallationUnavailable:
                result.status = "unavailable"
                result.description = "Connect the Moyai Dot Slack app before enabling notifications."
                return result
            if row and row.enabled:
                try:
                    if row.session_id != session_id or not row.recipient or not row.started_ts:
                        raise HTTPException(409, "Your Dot changed.")
                    await current(slack, db, SlackRecipient.model_validate_json(self.database.security.unseal(row.recipient)))
                    result.status = "on"
                    result.description = "Your Dot sends updates to your Slack DM. Each thread is a separate private conversation you can also open in Moyai."
                except HTTPException as error:
                    if error.status_code >= 500:
                        raise
                    result.status = "needs_attention"
                    result.description = "Your Slack access or Dot changed. Reconnect notifications to continue."
            last = await db.scalar(select(Delivery).join(
                DotSlackConversation, DotSlackConversation.session_id == Delivery.session_id,
            ).where(
                DotSlackConversation.dot_session_id == session_id,
                Delivery.identity == ("product-slack:" + Delivery.turn_id + ":dot"),
            ).order_by(Delivery.created_at.desc(), Delivery.id.desc()).limit(1))
            if last:
                result.last_delivery = TypeAdapter(DotDeliveryStatus).validate_python(last.status)
            return result

    async def edit(self, actor: User, body: DotNotificationEdit) -> DotNotificationSettings:
        async with self.database.sessions.begin() as db:
            session_id = await self.owner(db, actor)
            row = await db.get(DotSlackPreference, actor.id)
            if body.revision != (row.revision if row else 0):
                raise HTTPException(409, "Notification settings changed. Refresh and try again.")
        recipient = None
        started_ts = None
        slack = self.runtime.product_slack.dot if self.runtime.product_slack else None
        if body.enabled:
            if slack is None or not enabled(slack) or not slack.settings.public_url:
                raise HTTPException(409, "Configure Slack direct messages and the Moyai URL first.")
            try:
                installed = await source_database(slack.installation)
                sender = await lookup(slack, actor, installed)
                token = await slack.bot_token()
                response_headers: dict[str, str] = {}
                opened = await slack.connectors.request(
                    "POST", "https://slack.com/api/conversations.open",
                    headers={"Authorization": "Bearer " + token}, json={"users": sender.id.split(":")[2]},
                    response_headers=response_headers,
                )
                channel = (opened.get("channel") or {}).get("id", "")
                if opened.get("ok") is not True or not channel.startswith("D") or not ID.fullmatch(channel):
                    raise HTTPException(502, "Slack did not confirm a direct-message conversation.")
                try:
                    provider_date = response_headers["date"]
                    provider_time = parsedate_to_datetime(provider_date)
                    if (provider_time.utcoffset() != timedelta(0)
                            or format_datetime(provider_time, usegmt=True) != provider_date):
                        raise ValueError("Slack must provide a complete HTTP Date.")
                    started_ts = f"{int(provider_time.timestamp()) + SLACK_CLOCK_GUARD_SECONDS}.000000"
                except (KeyError, TypeError, ValueError, OverflowError) as error:
                    raise HTTPException(502, "Slack did not confirm its clock. Try connecting again.") from error
                recipient = SlackRecipient(
                    owner_id=actor.id, owner_authority=actor_grant(actor).authority_version,
                    requester_id=sender.id, team_id=installed["team_id"], channel_id=channel,
                    connection_authority=installed["authority"],
                    slack_app="dot",
                )
            except ConnectorError as error:
                raise HTTPException(502, "Could not verify your Slack DM. Check the bot connection and try again.") from error
        async with self.database.sessions.begin() as db:
            # Serialize opt-in/out with publication and reject an in-flight stale enable.
            await self.database._actor_product_grant(db, actor)
            await db.execute(update(User).where(User.id == actor.id).values(auth_version=User.auth_version))
            if session_id != await self.owner(db, actor):
                raise HTTPException(409, "Your Dot changed. Refresh and try again.")
            row = await db.get(DotSlackPreference, actor.id, populate_existing=True)
            if body.revision != (row.revision if row else 0):
                raise HTTPException(409, "Notification settings changed. Refresh and try again.")
            if row is None:
                row = DotSlackPreference(owner_id=actor.id, session_id=session_id, revision=0)
                db.add(row)
            if recipient is not None:
                assert slack is not None
                await current(slack, db, recipient)
            row.session_id, row.enabled, row.revision = session_id, body.enabled, row.revision + 1
            if recipient:
                previous = SlackRecipient.model_validate_json(
                    self.database.security.unseal(row.recipient),
                ) if row.recipient else None
                if previous != recipient or row.started_ts is None:
                    row.started_ts = started_ts
                row.recipient = self.database.security.seal(recipient.model_dump_json())
                row.slack_app = "dot"
                row.destination_key = f"dot:{recipient.team_id}:{recipient.channel_id}"
            row.updated_at = now()
            try:
                await db.flush()
            except IntegrityError as error:
                raise HTTPException(409, "This Slack DM is already connected to another Dot.") from error
            # Only known-unsent rows can be cancelled. An uncertain effect stays claimed.
            await db.execute(update(Delivery).where(
                Delivery.session_id.in_(select(DotSlackConversation.session_id).where(
                    DotSlackConversation.dot_session_id == session_id,
                )), Delivery.status == "pending",
                Delivery.identity == ("product-slack:" + Delivery.turn_id + ":dot"),
            ).values(status="suppressed"))
        if recipient is not None:
            # Commit the verified pin before waiting: incoming DMs must already
            # fail closed during setup. Cancellation retains this committed
            # setting, and a concurrent pause cannot be overwritten afterward.
            # HTTP Date has one-second precision; this elapsed guard also allows
            # a small difference between Slack's API/message clocks.
            await sleep(SLACK_CLOCK_GUARD_SECONDS)
        return await self.read(actor)


def slack_content(content: dict, public_url: str, session_id: str) -> dict:
    from .slack_messages import session_url

    url = session_url(public_url, session_id)
    return {"text": "*Your Dot*\n" + html.escape(content["text"], quote=False)
            + f"\n\nReply in this thread, or <{url}|open this conversation in Moyai>."}
