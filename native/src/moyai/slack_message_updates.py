"""Signed Slack edits/deletions update the transcript without creating new work."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, cast

from agentchat.channels.slack.host import SlackHost
from fastapi import HTTPException
from sqlalchemy import select, update

from .models import Message, MessageSource, Session, Turn, now


async def receive_update(events, payload: dict, installed: dict) -> bool:
    event = payload["event"]
    deleted = event["subtype"] == "message_deleted"
    original = event.get("previous_message") or {}
    current = original if deleted else event.get("message") or {}
    if not isinstance(original, dict) or not isinstance(current, dict):
        return False
    stamp = event.get("deleted_ts") if deleted else current.get("ts")
    user, channel = original.get("user") or current.get("user"), event.get("channel")
    if (not isinstance(stamp, str) or not re.fullmatch(r"\d+\.\d+", stamp)
            or not isinstance(channel, str) or not re.fullmatch(r"[CDG][A-Z0-9]+", channel)
            or not isinstance(user, str) or current.get("user", user) != user):
        return False
    verified = {**event, **current, "channel": channel, "user": user, "ts": stamp}
    if events._eligible_event({**payload, "event": verified}, installed) is None:
        return False
    slack = events.slack
    actor, _ = await slack.actor(installed["team_id"], user)
    cast(Any, actor).slack_channel_evidence = (installed["team_id"], channel)
    origin = {
        "team_id": installed["team_id"], "channel_id": channel,
        "thread_ts": original.get("thread_ts") or current.get("thread_ts") or stamp,
        "message_ts": stamp,
    }
    thread = await events.conversations.find(origin)
    if thread is not None:
        if slack.app == "dot" and await events.conversations.existing(
            thread, actor, origin, installed, mentioned=True, command="",
        ) is None:
            return False
        actor = await slack.web.input_actor(actor, thread.session_id)
    elif slack.app == "dot":
        return False
    key = hashlib.sha256(json.dumps([installed["team_id"], channel, stamp]).encode()).hexdigest()
    changed_at = str((None if deleted else (current.get("edited") or {}).get("ts"))
                     or event.get("event_ts") or payload.get("event_time", ""))
    host = SlackHost(bot_user_id=installed["user_id"], workspace_id=installed["team_id"])
    normalized = host.normalize_event({**payload, "event": {
        **verified, "type": "message", "subtype": None,
    }}) if not deleted else None
    authored = normalized["text"] if normalized else ""
    if not deleted and normalized is None and current.get("text"):
        return False
    database = slack.runtime.database
    async with database.sessions.begin() as db:
        query = select(Turn).where(Turn.actor_id == actor.id, Turn.request_key == "slack-message:" + key)
        if thread is not None:
            query = query.where(Turn.session_id == thread.session_id)
        turn = await db.scalar(query)
        if turn is None:
            return False
        await database._actor_product_grant(db, actor)
        from .slack_web import check_turn
        await check_turn(database, db, turn)
        await database.allowed_session(db, turn.session_id, actor)
        # Serialize with dispatch before deciding whether execution is editable.
        await db.execute(update(Session).where(Session.id == turn.session_id).values(revision=Session.revision + 1))
        await db.refresh(turn)
        message = await db.scalar(select(Message).where(Message.identity == f"user:{turn.id}"))
        source = await db.get(MessageSource, message.identity) if message else None
        if source is None or message is None or source.kind != "slack":
            return False
        metadata = json.loads(database.security.unseal(source.data))
        if metadata.get("slack_app", "dev") != slack.app or any(metadata.get(field) != value for field, value in (
            ("team_id", installed["team_id"]), ("channel_id", channel), ("message_ts", stamp), ("user_id", user),
        )):
            return False
        from decimal import Decimal, InvalidOperation
        try:
            if Decimal(changed_at) <= Decimal(metadata.get("changed_at", "0")):
                return False
        except InvalidOperation:
            return False
        if metadata.get("deleted"):
            return False
        old = database.security.unseal(message.content).decode()
        frozen = database.security.unseal(turn.prompt).decode()
        editable = turn.status == "queued" and frozen.startswith(old)
        if editable:
            if deleted:
                turn.status, turn.finished_at = "stopped", now()
            else:
                from .skill_invocations import gateway_connection_ids
                from .skill_mentions import mentions
                session = await db.get(Session, turn.session_id)
                authored = await slack.runtime.skill_invocations.normalize_slack(authored, actor, session)
                if set(gateway_connection_ids(authored)) - set(json.loads(turn.connections)):
                    raise HTTPException(403, "Send a new message to select a different gateway skill.")
                metadata["skill_references"] = [mention.reference for mention in mentions(authored)]
                turn.prompt = database.security.seal(authored + frozen[len(old):])
        old_files = {item.get("id") for item in original.get("files", []) if isinstance(item, dict)}
        new_files = {item.get("id") for item in current.get("files", []) if isinstance(item, dict)}
        metadata["attachments_changed"] = not deleted and old_files != new_files
        metadata.update(edited=not deleted, deleted=deleted, changed_at=changed_at, edited_after_start=not editable)
        metadata.pop("edited_in", None)
        if deleted:
            metadata["files"] = []
        source.data = database.security.seal(metadata)
        source.revision += 1
        source.mirror = False
        message.content = database.security.seal(authored)
        session = await db.get(Session, turn.session_id)
        if session is not None:
            session.updated_at = now()
    return True
