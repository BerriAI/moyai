"""Verified Slack events: route, authorize, prepare once, and admit."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.db import database as source_database
from app.slack_format import ASIDE, ID, STAMP, routing_text
from fastapi import HTTPException
from sqlalchemy import update

from .contracts import NewMessage
from .models import Session, User
from .product_identity import ProductPrincipal
from .product_slack_controls import answer
from .product_slack_conversations import SlackConversation, SlackConversations

if TYPE_CHECKING:
    from .product_slack import ProductSlack


@dataclass(frozen=True)
class SlackCommand:
    name: str
    argument: str = ""

    @classmethod
    def parse(cls, prompt: str) -> SlackCommand | None:
        plain = prompt.strip().lower().lstrip("/")
        if plain in {"stop", "sleep", "wake", "status", "continue"}:
            return cls(plain)
        match = re.fullmatch(r"/?answer(?:\s+([\s\S]*))?", prompt.strip(), re.IGNORECASE)
        if match:
            return cls("answer", match[1] or "")
        match = re.fullmatch(r"/?(model|harness|goal)(?:\s+([^\n]*))?", prompt.strip(), re.IGNORECASE)
        return cls(match[1].lower(), (match[2] or "").strip()) if match else None


@dataclass(frozen=True)
class SlackMessage:
    event: dict[str, Any]
    origin: dict[str, str]
    prompt: str
    mentioned: bool
    direct: bool
    key: str
    command: SlackCommand | None

    @classmethod
    def parse(cls, payload: dict, event: dict, installed: dict) -> SlackMessage | None:
        if event.get("type") not in {"app_mention", "message"} or event.get("subtype") not in {None, "file_share"}:
            return None
        channel, timestamp, user = event.get("channel", ""), event.get("ts", ""), event.get("user", "")
        thread_ts, authored = event.get("thread_ts") or timestamp, event.get("text", "")
        if not all(isinstance(value, str) for value in (channel, timestamp, thread_ts, authored)):
            raise HTTPException(400, "Invalid Slack event fields.")
        if (
            not ID.fullmatch(user) or not ID.fullmatch(channel)
            or channel[0] not in "CDG" or user[0] not in "UW"
            or not STAMP.fullmatch(timestamp) or not STAMP.fullmatch(thread_ts)
            or Decimal(thread_ts) > Decimal(timestamp)
        ):
            raise HTTPException(400, "Invalid Slack event fields.")
        visible, prompt = routing_text(authored, f"<@{installed['user_id']}>")
        if ASIDE.match(prompt) or (not prompt and not event.get("files")):
            return None
        origin = {
            "team_id": installed["team_id"], "channel_id": channel, "thread_ts": thread_ts,
            "user_id": user, "event_id": payload["event_id"], "message_ts": timestamp,
        }
        key = hashlib.sha256(json.dumps([installed["team_id"], channel, timestamp]).encode()).hexdigest()
        return cls(
            event, origin, prompt,
            mentioned=event["type"] == "app_mention" or f"<@{installed['user_id']}>" in visible,
            direct=channel.startswith("D") and event.get("channel_type") == "im",
            key=key, command=SlackCommand.parse(prompt),
        )

    @property
    def command_name(self) -> str:
        return self.command.name if self.command else ""


@dataclass(frozen=True)
class SlackSubmission:
    body: NewMessage
    uploads: list[str]


class SlackEvents:
    def __init__(self, slack: ProductSlack):
        self.slack = slack
        self.conversations = SlackConversations(slack)

    async def receive(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("type") == "native_interaction":
            await self.slack.interact(payload["payload"])
            return {"ok": True}
        settings = self.slack.settings
        if not settings.slack_bot_enabled or not settings.slack_session_users:
            raise HTTPException(403, "Slack chat is disabled.")
        installed = await source_database(self.slack.installation)
        event = self._eligible_event(payload, installed)
        if event is None:
            return {"ok": True}
        await self._automation(payload, event, installed)
        message = SlackMessage.parse(payload, event, installed)
        if message is None:
            return {"ok": True}
        if message.origin["channel_id"].startswith("D") and (
            not message.direct or not settings.slack_dm_enabled or not settings.slack_thread_chat_enabled
        ):
            return {"ok": True}
        thread = await self.conversations.find(message.origin)
        if thread is None and not message.mentioned and not message.direct:
            return {"ok": True}
        if not settings.slack_thread_chat_enabled and not message.mentioned:
            return {"ok": True}
        actor, principal = await self.slack.actor(installed["team_id"], message.origin["user_id"])
        conversation = await self.conversations.resolve(
            thread, actor, message.origin, installed, mentioned=message.mentioned, command=message.command_name,
        )
        if conversation is None:
            return {"ok": True}
        return await self._admit(message, conversation, actor, principal, installed)

    def _eligible_event(self, payload: dict, installed: dict) -> dict | None:
        event = payload.get("event")
        if payload.get("type") != "event_callback" or not isinstance(event, dict):
            return None
        for key in ("type", "subtype", "user", "channel", "ts", "thread_ts", "text", "channel_type"):
            if event.get(key) is not None and not isinstance(event[key], str):
                raise HTTPException(400, "Invalid Slack event fields.")
        for key in ("item", "metadata"):
            if event.get(key) is not None and not isinstance(event[key], dict):
                raise HTTPException(400, "Invalid Slack event metadata.")
        user = event.get("user", "")
        allowed = {value.strip() for value in self.slack.settings.slack_session_users.split(",")}
        if (
            payload.get("team_id") != installed["team_id"] or payload.get("is_ext_shared_channel")
            or event.get("is_ext_shared_channel") or not isinstance(user, str)
            or not ("*" in allowed or user in allowed)
            or event.get("bot_id") or event.get("bot_profile") or user == installed["user_id"]
            or (isinstance(event.get("metadata"), dict)
                and str(event["metadata"].get("event_type", "")).startswith("moyai"))
        ):
            return None
        return event

    async def _automation(self, payload: dict, event: dict, installed: dict) -> None:
        automation = getattr(self.slack.runtime, "product_automation_events", None)
        channel = event.get("channel") or (event.get("item") or {}).get("channel") or ""
        if automation is not None and (
            isinstance(channel, str) and not channel.startswith("D")
            and event.get("channel_type") not in {"im", "mpim"}
        ) and await self.slack.automation_channel_allowed(channel, installed):
            await automation.receive_verified(payload)

    async def _prepare(
        self, message: SlackMessage, actor: User, principal: ProductPrincipal, installed: dict,
    ) -> SlackSubmission:
        attachments = {"uploads": [], "images": [], "context": ""}
        if message.event.get("files"):
            attachments = await self.slack.files.prepare(actor, principal, message.event, message.origin)
        selections = [grant["id"] for grant in await self.slack.connections.catalog(
            actor.id, shared_session=True, surface="slack",
        )]
        body = NewMessage(
            prompt=message.prompt + attachments["context"], request_key="slack-message:" + message.key,
            mode="asap", connections=selections, attachments=attachments["images"],
        )
        body = await self.slack.context.prepare(actor, principal, message.origin, installed, body)
        return SlackSubmission(body, attachments["uploads"])

    async def _admit(
        self, message: SlackMessage, conversation: SlackConversation, actor: User,
        principal: ProductPrincipal, installed: dict,
    ) -> dict[str, Any]:
        submission = None if message.command else await self._prepare(message, actor, principal, installed)
        thread = conversation.thread
        if thread is None:
            thread = await self.conversations.bind(
                conversation, actor, principal, message.origin, installed, message.prompt,
                submission.body if submission else None,
                wake=message.mentioned or message.command_name == "wake",
                uploads=submission.uploads if submission else [],
            )
            if thread is None:
                return {"ok": True, "review_required": True}
        # File/history reads can take time; recheck existing bindings too, as
        # well as any binding returned by a competing first admission.
        resolved = await self.conversations.existing(
            thread, actor, message.origin, installed, mentioned=message.mentioned, command=message.command_name,
        )
        if resolved is None:
            return {"ok": True}
        binding = resolved.binding
        assert binding is not None
        if message.command:
            await self._control(message, resolved, actor)
            return {"ok": True}
        assert submission is not None
        if binding.paused:
            await self.slack.set_paused(binding.session_id, False)
        if submission.uploads:
            async with self.slack.runtime.database.sessions.begin() as db:
                turn = await self.slack.runtime.database.submit(actor, thread.session_id, submission.body, db=db)
                await self.slack.files.bind(db, actor.id, thread.session_id, submission.uploads)
        else:
            turn = await self.slack.runtime.database.submit(actor, thread.session_id, submission.body)
        await self.slack.queue(turn, thread, "accepted", {"text": "Moyai received your message."})
        return {"ok": True}

    async def _control(self, message: SlackMessage, conversation: SlackConversation, actor: User) -> None:
        command, thread, binding = message.command, conversation.thread, conversation.binding
        assert command is not None and thread is not None and binding is not None
        if command.name in {"stop", "sleep", "wake", "status"}:
            await self.slack.command(actor, thread, binding, command.name, message.key)
        elif command.name in {"answer", "continue"}:
            try:
                resumed = await answer(
                    self.slack, actor, thread, command.argument, continuing=command.name == "continue",
                )
                text = "Your response was received; the task is continuing." if resumed else (
                    "Your response was saved. Complete the remaining question, approval, or secure request, then reply /continue."
                )
            except HTTPException as error:
                if error.status_code >= 500:
                    raise
                text = str(error.detail)
            await self.slack.notice(actor, thread, "answer:" + message.key, text)
        else:
            if command.name == "model" and command.argument in self.slack.runtime.settings.models:
                async with self.slack.runtime.database.sessions.begin() as db:
                    await self.slack.runtime.database.allowed_session(db, thread.session_id, actor)
                    await db.execute(update(Session).where(Session.id == thread.session_id).values(model=command.argument))
                text = f"New messages in this session will use {command.argument}. Running turns keep their model."
            elif command.name == "model":
                text = "Choose an enabled model: " + ", ".join(self.slack.runtime.settings.models)
            elif command.name == "harness":
                text = "This session uses the Pydantic core. Switching to an older engine is unavailable."
            else:
                text = "Slack goal controls are not available on this core yet. Use the Moyai session."
            await self.slack.notice(actor, thread, "directive:" + message.key, text)
