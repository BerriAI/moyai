"""Keep verified native Slack senders fresh while their conversations are idle."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from app.db import database as source_database
from app.identities import PROFILE_REFRESH_SECONDS
from sqlalchemy import column, or_, select, table

from .models import Message

logger = logging.getLogger(__name__)


async def refresh_due(slack, *, after: str = "") -> str:
    if not slack.settings.slack_identity_linking_enabled:
        return ""
    installed = await source_database(slack.installation)
    # Native turns do not create legacy runs/messages. The legacy refresher's
    # candidate query therefore misses their senders entirely.
    users = table("users", *(column(key) for key in (
        "id", "kind", "profile_next_check", "profile_eligible", "profile_checked_at",
    )), schema=slack.product.identity.schema)
    before = datetime.fromtimestamp(time.time() - PROFILE_REFRESH_SECONDS, UTC).isoformat()
    async with slack.runtime.database.sessions() as db:
        ids = list(await db.scalars(select(users.c.id).where(
            users.c.kind == "slack", users.c.id.startswith("slack:" + installed["team_id"] + ":"),
            users.c.id > after,
            or_(users.c.profile_next_check <= int(time.time()),
                (users.c.profile_eligible == 1) & (users.c.profile_checked_at <= before)),
            select(Message.id).where(Message.author_id == users.c.id).exists(),
        ).order_by(users.c.id).limit(10)))
    # Release the read before the canonical profile writer takes its lock.
    # actor() refreshes through users.info and projects the current display name;
    # it does not admit work or replace any saved turn grant.
    for identity in ids:
        try:
            await slack.actor(installed["team_id"], identity.split(":")[2], refresh_profile=True)
        except Exception as error:  # noqa: BLE001 - one unavailable profile must not starve other senders
            logger.warning("Slack sender refresh unavailable (%s)", type(error).__name__)
    return ids[-1] if len(ids) == 10 else ""


async def watch(slack, *, stop: asyncio.Event | None = None) -> None:
    from .background_stop import pause

    after = ""
    while stop is None or not stop.is_set():
        try:
            after = await refresh_due(slack, after=after)
        except Exception as error:  # noqa: BLE001 - retry reads without exposing provider data
            logger.warning("Slack sender refresh will retry (%s)", type(error).__name__)
        await pause(30, stop)
