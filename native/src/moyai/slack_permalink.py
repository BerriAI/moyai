"""Resolve a verified message's permalink without exposing bot credentials."""

from __future__ import annotations

import asyncio
from urllib.parse import urlencode, urlsplit

import httpx
from app.db import database as source_database
from fastapi import HTTPException

from .message_sources import public_message
from .models import Message


async def permalink(runtime, actor, session_id: str, message_id: int) -> str:
    async with runtime.database.sessions() as db:
        await runtime.database.allowed_session(db, session_id, actor)
        message = await db.get(Message, message_id)
        if message is None or message.session_id != session_id:
            raise HTTPException(404, "Message not found.")
        _, source = await public_message(runtime.database, db, message)
        if not source or source.get("kind") != "slack":
            raise HTTPException(404, "Slack source unavailable.")
    team, channel, stamp = (source[key] for key in ("team_id", "channel_id", "message_ts"))
    params = {"channel": channel, "message_ts": stamp}
    fallback = "https://slack.com/app_redirect?" + urlencode({"team": team, "channel": channel})
    try:
        async with asyncio.timeout(5):
            if runtime.product_slack is not None:
                slack = runtime.product_slack
                installed = await source_database(slack.installation)
                if installed["team_id"] != team:
                    raise HTTPException(404, "Slack source unavailable.")
                token = await slack.connectors.slack_bot_token()
                result = await slack.connectors.request("GET", "https://slack.com/api/chat.getPermalink",
                    headers={"Authorization": "Bearer " + token}, params=params)
            else:
                if runtime.settings.slack_team_id != team:
                    raise HTTPException(404, "Slack source unavailable.")
                token = runtime.settings.slack_bot_token.get_secret_value()
                async with httpx.AsyncClient(timeout=5) as client:
                    response = await client.get("https://slack.com/api/chat.getPermalink",
                        headers={"Authorization": "Bearer " + token}, params=params)
                    response.raise_for_status()
                    result = response.json()
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 - a failed read falls back to the fixed Slack channel URL
        result = {}
    await runtime.database.session(actor, session_id)
    value = result.get("permalink", "") if isinstance(result, dict) else ""
    if not isinstance(value, str):
        return fallback
    try:
        parsed = urlsplit(value)
        if parsed.port not in (None, 443):
            return fallback
    except ValueError:
        return fallback
    if (parsed.scheme == "https" and (parsed.hostname or "").endswith(".slack.com")
            and parsed.username is None and parsed.password is None
            and parsed.path == f"/archives/{channel}/p{stamp.replace('.', '')}"):
        return parsed._replace(query="", fragment="").geturl()
    return fallback
