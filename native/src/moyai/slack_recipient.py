"""The verified owner DM identity shared by chat continuation and Dot notices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.slack_apps import SlackApp
from app.slack_format import ID
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

from .models import User
from .product_identity import ProductGrant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .product_slack import ProductSlack


class SlackRecipient(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True, extra="forbid")

    owner_id: str
    owner_authority: str
    requester_id: str
    team_id: str
    channel_id: str
    connection_authority: str
    slack_app: SlackApp = "dev"

    @field_validator("slack_app", mode="before")
    @classmethod
    def legacy_app(cls, value: object) -> object:
        return "dev" if value is None else value


def actor_grant(actor: User) -> ProductGrant:
    grant = getattr(actor, "product_grant", None)
    if not isinstance(grant, ProductGrant) or grant.user_id != actor.id:
        raise HTTPException(401, "Fresh product authorization is required.")
    return grant


def enabled(slack: ProductSlack) -> bool:
    settings = slack.settings
    return bool(settings.slack_bot_enabled and settings.slack_identity_linking_enabled
                and settings.slack_dm_enabled and settings.slack_thread_chat_enabled)


async def lookup(slack: ProductSlack, actor: User, installed: dict) -> User:
    if not enabled(slack):
        raise HTTPException(409, "Enable Slack identity linking and direct-message conversations first.")
    principal = await slack.product.identity.require_current(actor_grant(actor))
    if not principal.individual or principal.email.rpartition("@")[2] not in slack.settings.identity_domains():
        raise HTTPException(403, "An eligible organization sign-in is required.")
    token = await slack.bot_token()
    result = await slack.connectors.request(
        "GET", "https://slack.com/api/users.lookupByEmail",
        headers={"Authorization": "Bearer " + token}, params={"email": principal.email},
        allowed_errors=("users_not_found", "missing_scope"),
    )
    profile = result.get("user") or {}
    recipient = profile.get("id")
    if (result.get("ok") is not True or not isinstance(recipient, str) or not ID.fullmatch(recipient)
            or profile.get("team_id") != installed["team_id"]):
        raise HTTPException(409, "Your Slack account could not be verified. Check the bot's profile permissions.")
    allowed = {value.strip() for value in slack.settings.slack_session_users.split(",")}
    if "*" not in allowed and recipient not in allowed:
        raise HTTPException(403, "Your Slack account is not enabled for Moyai conversations.")
    sender, _ = await slack.actor(installed["team_id"], recipient, refresh_profile=True)
    async with slack.runtime.database.sessions.begin() as db:
        individual = await slack.product.identity.individual_for_admission(db, actor_grant(sender))
        if individual.id != actor.id:
            raise HTTPException(403, "Your Slack account link needs review.")
    return sender


async def current(
    slack: ProductSlack, db: AsyncSession, recipient: SlackRecipient, *, sender: User | None = None,
) -> User:
    if not enabled(slack):
        raise HTTPException(403, "Slack direct-message delivery is disabled.")
    if slack.app != recipient.slack_app:
        raise HTTPException(403, "This direct message belongs to another Slack app.")
    installed = await slack.installation_in(db)
    if installed["authority"] != recipient.connection_authority or installed["team_id"] != recipient.team_id:
        raise HTTPException(403, "The linked Slack installation changed.")
    if not recipient.channel_id.startswith("D") or not ID.fullmatch(recipient.channel_id):
        raise HTTPException(403, "A verified direct-message destination is required.")
    identity = slack.product.identity
    owner = await identity.authorize_admission(db, ProductGrant(recipient.owner_id, recipient.owner_authority))
    if sender is not None:
        if sender.id != recipient.requester_id:
            raise HTTPException(403, "This DM belongs to a different Slack account.")
        grant = actor_grant(sender)
    else:
        raw = await identity._current(
            db, recipient.requester_id, await identity._native_controls(recipient.requester_id, db),
        )
        grant = raw.grant
    individual = await identity.individual_for_admission(db, grant)
    if not owner.individual or individual.id != owner.id:
        raise HTTPException(403, "The linked Slack recipient changed.")
    actor = await identity.read_native_user(db, owner)
    if actor is None:
        raise HTTPException(403, "The session owner is unavailable.")
    return actor
