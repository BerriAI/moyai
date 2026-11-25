"""Verified Slack events: route, authorize, prepare once, and admit."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from agentchat.channels.slack.host import SlackHost, SlackHostEvent
from app.db import database as source_database
from app.slack_format import ASIDE_PREFIXES
from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .contracts import ExecutionContext, NewMessage, PreparedTurn
from .inbound_events import InboundEvent
from .models import Session, SlackThread, Turn, User
from .product_identity import ProductPrincipal
from .product_slack_controls import answer
from .product_slack_conversations import SlackConversation, SlackConversations
from .product_slack_models import SlackTurnSource
from .skill_mentions import mentions

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
    source: SlackHostEvent
    origin: dict[str, str]
    prompt: str
    mentioned: bool
    direct: bool
    key: str
    command: SlackCommand | None

    @classmethod
    def parse(cls, payload: dict, event: dict, host: SlackHost) -> SlackMessage | None:
        try:
            normalized = host.normalize_event(payload)
        except ValueError:
            raise HTTPException(400, "Invalid Slack event fields.") from None
        if normalized is None:
            return None
        origin = {
            "team_id": normalized["team_id"], "channel_id": normalized["channel_id"],
            "thread_ts": normalized["thread_ts"], "user_id": normalized["user_id"],
            "message_ts": normalized["message_ts"], "event_id": payload["event_id"],
        }
        # Keep existing admission identities stable across the SDK migration.
        key = hashlib.sha256(json.dumps([
            origin["team_id"], origin["channel_id"], origin["message_ts"],
        ]).encode()).hexdigest()
        prompt = normalized["text"]
        return cls(
            event, normalized, origin, prompt, mentioned=normalized["mentioned"], direct=normalized["direct"],
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
        if isinstance(payload.get("event"), dict) and payload["event"].get("subtype") in {
            "message_changed", "message_deleted",
        }:
            from .slack_message_updates import receive_update
            return {"ok": True, "updated": await receive_update(self, payload, installed)}
        event = self._eligible_event(payload, installed)
        if event is None:
            return {"ok": True}
        host = SlackHost(
            bot_user_id=installed["user_id"], workspace_id=installed["team_id"],
            exclude_prefixes=ASIDE_PREFIXES,
        )
        message = SlackMessage.parse(payload, event, host)
        if self.slack.app == "dev":
            await self._automation(payload, event, installed)
        if message is None:
            return {"ok": True}
        if message.origin["channel_id"].startswith("D") and (
            not message.direct or not settings.slack_dm_enabled or not settings.slack_thread_chat_enabled
        ):
            return {"ok": True}
        thread = await self.conversations.find(message.origin)
        if not host.accepts(message.source, subscribed=thread is not None):
            return {"ok": True}
        if not settings.slack_thread_chat_enabled and not message.mentioned:
            return {"ok": True}
        actor, principal = await self.slack.actor(installed["team_id"], message.origin["user_id"])
        cast(Any, actor).slack_channel_evidence = (message.origin["team_id"], message.origin["channel_id"])
        # Resolve legacy receipts and historical ownership before creating any
        # new native route. Existing history must keep its own continuation.
        conversation = await self.conversations.resolve(
            thread, actor, message.origin, installed, mentioned=message.mentioned, command=message.command_name,
        )
        if conversation is None:
            return {"ok": True}
        if self.slack.app == "dot" and conversation.source is not None:
            raise HTTPException(403, "Dot cannot continue historical Slack chats.")
        if thread is None and message.direct and conversation.source is None:
            from .dot_slack_threads import new_direct_message, recover_root
            waiting = await recover_root(self.slack, actor, message.origin)
            thread = await self.conversations.find(message.origin)
            if thread is None and self.slack.app == "dot":
                thread = await new_direct_message(
                    self.slack, actor, message.origin, message.prompt, waiting_for_root=waiting,
                )
            if thread is None and waiting:
                raise HTTPException(503, "This Slack thread is still confirming delivery.",
                                    headers={"Retry-After": "5"})
            if thread is not None:
                conversation = await self.conversations.resolve(
                    thread, actor, message.origin, installed,
                    mentioned=message.mentioned, command=message.command_name,
                )
        if conversation is None:
            return {"ok": True}
        if thread is None and self.slack.app == "dot":
            raise HTTPException(403, "This is not a connected Dot conversation.")
        return await self._admit(message, conversation, actor, principal, installed)

    def _eligible_event(self, payload: dict, installed: dict) -> dict | None:
        event = payload.get("event")
        if payload.get("type") != "event_callback" or not isinstance(event, dict):
            return None
        if event.get("user") is not None and not isinstance(event["user"], str):
            raise HTTPException(400, "Invalid Slack event fields.")
        for key in ("item", "metadata"):
            if event.get(key) is not None and not isinstance(event[key], dict):
                raise HTTPException(400, "Invalid Slack event metadata.")
        user = event.get("user", "")
        if self.slack.app == "dot" and (
            event.get("type") != "message" or event.get("channel_type") != "im"
            or not isinstance(event.get("channel"), str) or not event["channel"].startswith("D")
        ):
            return None
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
        self, message: SlackMessage, actor: User, principal: ProductPrincipal, thread: SlackThread | None,
    ) -> SlackSubmission:
        admission: Session | ExecutionContext = ExecutionContext("shared", "slack")
        if thread is not None:
            async with self.slack.runtime.database.sessions() as db:
                admission = await self.slack.runtime.database.allowed_session(db, thread.session_id, actor)
        prompt = await self.slack.runtime.skill_invocations.normalize_slack(message.prompt, actor, admission)
        attachments = {"uploads": [], "images": [], "context": ""}
        if message.event.get("files"):
            attachments = await self.slack.files.prepare(actor, principal, message.event, message.origin)
        selections = []
        if getattr(actor, "slack_web_grant", None) is None:
            selections = [grant["id"] for grant in await self.slack.connections.catalog(
                actor.id, shared_session=True, surface="slack",
            )]
        body = NewMessage.from_event(InboundEvent(
            source="slack", sender_id=actor.id, deduplication_key=message.key,
            text=prompt, model_context=attachments["context"],
            attachments=tuple(attachments["images"]),
            provenance={
                **message.origin, "slack_app": self.slack.app, "files": attachments.get("files", []),
                "skill_references": [mention.reference for mention in mentions(prompt)],
            },
        ), connections=selections)
        return SlackSubmission(body, attachments["uploads"])

    async def _submit(
        self, actor: User, thread: SlackThread, body: NewMessage, message: SlackMessage,
        uploads: list[str],
    ) -> Turn:
        async def admit() -> Turn:
            async with self.slack.runtime.database.sessions.begin() as db:
                # Continue the computer's existing account selection, including
                # older bot-only threads. Admission still checks every grant
                # against this sender's current authority and workspace scope.
                query = select(Turn).join(Session, Session.id == Turn.session_id).where(
                    Turn.session_id == thread.session_id,
                )
                if getattr(actor, "slack_web_grant", None) is None:
                    query = query.where(Turn.preparation.is_not(None), Session.workspace.is_not(None))
                saved = await db.scalar(query.order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))
                submission = body
                if saved is not None:
                    selections = json.loads(saved.connections)
                    if (
                        getattr(actor, "slack_web_grant", None) is not None
                        and await self.slack.runtime.database.dots.is_conversation(
                            db, actor.id, await db.get(Session, thread.session_id),
                        )
                    ):
                        from .models import Connection
                        # Dot's implicit Google accounts follow current owner
                        # consent. Other chats retain their explicit selections.
                        google_ids = set(await db.scalars(select(Connection.id).where(
                            Connection.id.in_(selections), Connection.kind.in_(["gmail", "google_calendar"]),
                        )))
                        selections = [identity for identity in selections if identity not in google_ids]
                    if getattr(actor, "slack_web_grant", None) is None and self.slack.connections.personal_github:
                        # The shared computer retains organization accounts,
                        # but a previous participant never lends GitHub access.
                        assert saved.preparation is not None
                        prepared = TypeAdapter(PreparedTurn).validate_json(
                            self.slack.runtime.security.unseal(saved.preparation),
                        )
                        github_ids = {grant.id for grant in prepared.scope.grants if grant.kind == "github"}
                        selections = [identity for identity in selections if identity not in github_ids]
                        account = await self.slack.connections.slack_github_account(db, actor.id)
                        if account is not None:
                            selections.append(account.id)
                    submission = body.model_copy(update={"connections": selections})
                turn = await self.slack.runtime.admission.submit(
                    actor, thread.session_id, submission, db=db, dot_accounts=True,
                )
                source = await db.get(SlackTurnSource, turn.id)
                if source is None:
                    db.add(SlackTurnSource(
                        turn_id=turn.id,
                        team_id=message.origin["team_id"],
                        channel_id=message.origin["channel_id"],
                        thread_ts=message.origin["thread_ts"],
                        message_ts=message.origin["message_ts"],
                        user_id=message.origin["user_id"],
                    ))
                if uploads:
                    await self.slack.files.bind(db, actor.id, thread.session_id, uploads)
                return turn

        try:
            return await admit()
        except IntegrityError:
            return await admit()

    async def _admit(
        self, message: SlackMessage, conversation: SlackConversation, actor: User,
        principal: ProductPrincipal, installed: dict,
    ) -> dict[str, Any]:
        # A verified reply to the owner's explicitly linked DM keeps the web
        # computer/account context. Retain its signed sender grant separately.
        if conversation.thread is not None:
            actor = await self.slack.web.input_actor(actor, conversation.thread.session_id)
        try:
            submission = None if message.command else await self._prepare(message, actor, principal, conversation.thread)
        except HTTPException as error:
            if error.status_code != 422:
                raise
            # Durable webhook intake is asynchronous: a rejected selection must
            # reach the sender instead of disappearing into an inbox error.
            thread = conversation.thread or await self.conversations.bind(
                conversation, actor, principal, message.origin, installed, message.prompt,
                None, wake=message.mentioned, uploads=[],
            )
            if thread is not None:
                await self.slack.notice(actor, thread, "invalid-input:" + message.key, str(error.detail))
                self.slack.delivery_wakeup.set()
            return {"ok": True}
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
        await self._submit(actor, thread, submission.body, message, submission.uploads)
        self.slack.runtime.slack_activity.wake.set()
        self.slack.delivery_wakeup.set()
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
