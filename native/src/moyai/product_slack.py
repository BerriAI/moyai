"""Signed canonical Slack ingress around native admission and durable delivery.

The API only verifies/persists hooks. Parent CoordinatorHandoff owns HookInbox
and delivery pump lifetimes, including drain. This module elects no coordinator
and starts no old agent runtime. Existing source Slack rows remain historical.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import re
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs
from uuid import NAMESPACE_URL, uuid5

import httpx
from app.db import database as source_database
from fastapi import APIRouter, HTTPException, Request
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai_harness.ask_user import AskUserRequest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from .contracts import NewMessage, NewSession
from .models import (
    Artifact,
    Delivery,
    Message,
    ProductTurnAuthority,
    Session,
    SlackThread,
    Turn,
    User,
    new_id,
    now,
)
from .product_connections import ProductConnections
from .product_identity import ProductGrant, ProductPrincipal
from .product_slack_models import ProductSlackBinding
from .slack_messages import navigation_action, pr_cards, session_url, thread_message, without_pr_references

if TYPE_CHECKING:
    from .product_identity import ProductIdentityAdapter
    from .runtime import Runtime

ID = re.compile(r"^[A-Z][A-Z0-9]{7,30}$")
STAMP = re.compile(r"^\d{10,16}\.\d{1,9}$")
logger = logging.getLogger(__name__)


class ProductSlackAuthority:
    """Narrow identity hook for source users enrolled by signed Slack intake.

    Parent wires ``identity.slack_authority`` and delegates only row.kind=slack
    from _current. Browser authenticate still rejects the method. The canonical
    writer lock and native enabled/auth_version controls remain with identity.
    """

    def __init__(self, identity: ProductIdentityAdapter):
        self.identity = identity

    async def current(self, db: Any, row: Any, controls: tuple[bool, int]) -> ProductPrincipal:
        from app.connection_authority import authority

        identity, settings = self.identity, self.identity.settings
        parts = row["id"].split(":")
        if len(parts) != 3 or parts[0] != "slack" or not all(ID.fullmatch(x) for x in parts[1:]):
            raise HTTPException(401, "Invalid canonical Slack identity.")
        _, team, user = parts
        allowed = {value.strip() for value in settings.slack_session_users.split(",")}
        if (
            not controls[0] or not settings.slack_bot_enabled or not settings.slack_signing_secret
            or not ("*" in allowed or user in allowed)
        ):
            raise HTTPException(403, "This Slack sender is no longer authorized.")
        connection = (await db.execute(text(
            f"SELECT * FROM {identity._table('connections')} WHERE provider='slack'",
        ))).mappings().first()
        policy = (await db.execute(text(
            f"SELECT * FROM {identity._table('connection_policies')} WHERE provider='slack'",
        ))).mappings().first()
        if not connection or (policy and not policy["enabled"]):
            raise HTTPException(403, "The Slack bot connection is unavailable.")
        credentials = json.loads(identity.legacy_security.decrypt(connection["encrypted"]))
        bot = credentials.get("bot") or {}
        if not bot.get("access_token") or (bot.get("team") or {}).get("id") != team:
            raise HTTPException(403, "The Slack workspace changed.")
        if any(row.get(key) for key in ("profile_conflict", "disabled", "disabled_at", "deleted_at", "revoked_at")):
            raise HTTPException(403, "The Slack account requires review.")
        if row.get("link_status") in {"ineligible", "email_changed", "review"}:
            raise HTTPException(403, "The Slack identity is no longer eligible.")
        email = row.get("email", "") or ""
        # A verified Slack sender is a member, not an administrator merely
        # because its profile/email happens to match a web administrator.
        state = {
            "id": row["id"], "created_at": row["created_at"],
            "connection": authority(credentials, connection["encrypted"]),
            "bot_user_id": bot.get("bot_user_id"), "team": team,
            "native_auth_version": controls[1], "allowed_users": sorted(allowed),
            "enabled": settings.slack_bot_enabled, "thread_chat": settings.slack_thread_chat_enabled,
            "dm": settings.slack_dm_enabled,
            "read_only": bool(policy and policy["read_only"]),
            "signature_version": hashlib.sha256(settings.slack_signing_secret.encode()).hexdigest(),
            "profile": {key: row.get(key) for key in (
                "email", "linked_user_id", "profile_eligible", "profile_conflict", "link_status",
            )},
        }
        revision = hmac.new(
            identity.legacy_security.secret.encode(), json.dumps(state, sort_keys=True).encode(), hashlib.sha256,
        ).hexdigest()
        return ProductPrincipal(
            id=row["id"], kind="slack", method="slack", email=email, name=row["name"] or user,
            role="member", enabled=True, authority_version=revision,
            linked_user_id=row.get("linked_user_id"),
            account_created_at=row["created_at"], native_auth_version=controls[1],
        )


class ProductSlack:
    def __init__(self, runtime: Runtime):
        from app.coordinator_handoff import HookInbox

        self.runtime = runtime
        product = runtime.product
        assert product is not None
        self.product = product
        self.store, self.settings = product.store_proxy, product.settings
        connections = getattr(runtime, "product_connections", None)
        if connections is None:
            raise RuntimeError("Product Slack requires canonical connection services.")
        self.connections: ProductConnections = connections
        self.connectors = self.connections.connectors
        from .product_slack_files import ProductSlackFiles
        self.files = ProductSlackFiles(self)
        self.inbox = HookInbox(self.store, self, list)
        self.delivery_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Call only inside the parent's CoordinatorHandoff background context."""
        if self.delivery_task is not None:
            raise RuntimeError("Product Slack background lifecycle already started.")
        self.inbox.start()
        self.delivery_task = asyncio.create_task(self.watch(), name="product-slack-delivery")

    async def close(self) -> None:
        """Parent retains its coordinator lock until this drain returns."""
        await self.inbox.close()
        if self.delivery_task is not None:
            self.delivery_task.cancel()
            await asyncio.gather(self.delivery_task, return_exceptions=True)
            self.delivery_task = None

    async def watch(self) -> None:
        while True:
            try:
                await self.pump()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - preserve the durable queue across provider/store outages
                logger.warning("Product Slack delivery remains saved (%s).", type(error).__name__)
            await asyncio.sleep(3)

    def verify_signature(self, body: bytes, headers: Any) -> None:
        stamp = headers.get("x-slack-request-timestamp", "")
        try:
            recent = abs(time.time() - int(stamp)) <= 300
        except ValueError:
            recent = False
        secret = self.settings.slack_signing_secret
        expected = "v0=" + hmac.new(
            secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256,
        ).hexdigest()
        if not secret or not recent or not hmac.compare_digest(expected, headers.get("x-slack-signature", "")):
            raise HTTPException(401, "Invalid Slack request.")

    def installation(self) -> dict[str, Any]:
        from app.connection_authority import authority

        rows = self.store.rows("SELECT * FROM connections WHERE provider='slack'")
        bot = self.connectors.slack_installation()
        if not rows or not bot.get("installed") or not self.connectors.policy("slack")["enabled"]:
            raise HTTPException(503, "The canonical Slack bot is unavailable.")
        value = json.loads(self.product.security.decrypt(rows[0]["encrypted"]))
        bot["authority"] = hashlib.sha256(json.dumps({
            "epoch": authority(value, rows[0]["encrypted"]),
            "team": bot.get("team_id"), "bot": bot.get("user_id"),
        }, sort_keys=True).encode()).hexdigest()
        return bot

    async def actor(self, team: str, user: str) -> tuple[User, ProductPrincipal]:
        def enroll():
            with self.store.connect() as conn:
                conn.begin_write()
                return self.store.slack_identity_in(conn, team, user)

        identity = await source_database(enroll)
        if self.settings.slack_identity_linking_enabled:
            rows = await source_database(self.store.rows, "SELECT * FROM users WHERE id=?", (identity,))
            if rows and rows[0].get("profile_next_check", 0) <= time.time():
                await self.connections.identities.resolve(rows[0], team)
        principal = await self.product.identity.current(identity)
        async with self.runtime.database.sessions.begin() as db:
            actor = await self.product.identity.project_native_user(db, principal)
        return actor, principal

    async def receive_payload(self, payload: dict[str, Any], _missing_cloud: Any = None) -> dict[str, Any]:
        """Only HookInbox's authenticated durable drain calls this method."""
        if payload.get("type") == "native_interaction":
            await self.interact(payload["payload"])
            return {"ok": True}
        if not self.settings.slack_bot_enabled or not self.settings.slack_session_users:
            raise HTTPException(403, "Slack chat is disabled.")
        bot = await source_database(self.installation)
        event = payload.get("event")
        if payload.get("type") != "event_callback" or not isinstance(event, dict):
            return {"ok": True}
        for key in ("type", "subtype", "user", "channel", "ts", "thread_ts", "text", "channel_type"):
            if event.get(key) is not None and not isinstance(event[key], str):
                raise HTTPException(400, "Invalid Slack event fields.")
        for key in ("item", "metadata"):
            if event.get(key) is not None and not isinstance(event[key], dict):
                raise HTTPException(400, "Invalid Slack event metadata.")
        user = event.get("user", "")
        allowed = {x.strip() for x in self.settings.slack_session_users.split(",")}
        if (
            payload.get("team_id") != bot["team_id"] or payload.get("is_ext_shared_channel")
            or event.get("is_ext_shared_channel") or not isinstance(user, str)
            or not ("*" in allowed or user in allowed)
            or event.get("bot_id") or event.get("bot_profile") or user == bot["user_id"]
            or (isinstance(event.get("metadata"), dict)
                and str(event["metadata"].get("event_type", "")).startswith("moyai"))
        ):
            return {"ok": True}
        automation = getattr(self.runtime, "product_automation_events", None)
        event_channel = event.get("channel") or (event.get("item") or {}).get("channel") or ""
        if automation is not None and (
            isinstance(event_channel, str) and not event_channel.startswith("D")
            and event.get("channel_type") not in {"im", "mpim"}
        ) and await self.automation_channel_allowed(event_channel, bot):
            await automation.receive_verified(payload)
        if event.get("type") not in {"app_mention", "message"} or event.get("subtype") not in {None, "file_share"}:
            return {"ok": True}
        channel, timestamp = event.get("channel", ""), event.get("ts", "")
        thread_ts, authored = event.get("thread_ts") or timestamp, event.get("text", "")
        if not all(isinstance(x, str) for x in (channel, timestamp, thread_ts, authored)):
            raise HTTPException(400, "Invalid Slack event fields.")
        if (
            not ID.fullmatch(user) or not ID.fullmatch(channel)
            or channel[0] not in "CDG" or user[0] not in "UW"
            or not STAMP.fullmatch(timestamp) or not STAMP.fullmatch(thread_ts)
            or Decimal(thread_ts) > Decimal(timestamp)
        ):
            raise HTTPException(400, "Invalid Slack event fields.")
        direct = channel.startswith("D") and event.get("channel_type") == "im"
        if channel.startswith("D") and (
            not direct or not self.settings.slack_dm_enabled or not self.settings.slack_thread_chat_enabled
        ):
            return {"ok": True}
        from app.slack_format import routing_text

        visible, prompt = routing_text(authored, f"<@{bot['user_id']}>")
        mentioned = event["type"] == "app_mention" or f"<@{bot['user_id']}>" in visible
        if re.match(r"^\s*(?:!aside|\(aside\))(?:\s|$)", prompt, re.IGNORECASE):
            return {"ok": True}
        async with self.runtime.database.sessions() as db:
            thread = await db.scalar(select(SlackThread).where(
                SlackThread.team_id == bot["team_id"], SlackThread.channel_id == channel,
                SlackThread.thread_ts == thread_ts,
            ))
        if thread is None and not mentioned and not direct:
            return {"ok": True}
        if not self.settings.slack_thread_chat_enabled and not mentioned:
            return {"ok": True}
        if not prompt and not event.get("files"):
            return {"ok": True}
        actor, principal = await self.actor(bot["team_id"], user)
        provenance = {
            "team_id": bot["team_id"], "channel_id": channel, "thread_ts": thread_ts,
            "user_id": user, "event_id": payload["event_id"], "message_ts": timestamp,
        }
        message_key = hashlib.sha256(json.dumps([bot["team_id"], channel, timestamp]).encode()).hexdigest()
        command = prompt.strip().lower().lstrip("/")
        answering = re.fullmatch(r"/?answer(?:\s+([\s\S]*))?", prompt.strip(), re.IGNORECASE)
        control = answering or command in {"stop", "sleep", "wake", "status", "continue"} or re.fullmatch(
            r"/?(model|harness|goal)(?:\s+[^\n]*)?", prompt.strip(), re.IGNORECASE,
        )
        body = None
        attachments = {"uploads": [], "images": [], "context": ""}
        if not control:
            if event.get("files"):
                attachments = await self.files.prepare(actor, principal, event, provenance)
            selections = [g["id"] for g in await self.connections.catalog(
                actor.id, shared_session=True, surface="slack",
            )]
            body = NewMessage(
                prompt=prompt + attachments["context"], request_key="slack-message:" + message_key,
                mode="asap", connections=selections, attachments=attachments["images"],
            )
        if thread is None:
            seen = await source_database(
                self.store.rows,
                """SELECT 1 FROM slack_receipts WHERE team_id=? AND channel=? AND message_ts=?
                   UNION ALL SELECT 1 FROM slack_events WHERE event_id=? LIMIT 1""",
                (bot["team_id"], channel, timestamp, payload["event_id"]),
            )
            if seen:
                return {"ok": True}
            thread = await self.bind(
                actor, principal, provenance, bot, prompt, body,
                wake=mentioned or command == "wake", uploads=attachments["uploads"],
            )
            if thread is None:
                return {"ok": True, "review_required": True}
        async with self.runtime.database.sessions() as db:
            binding = await db.get(ProductSlackBinding, thread.session_id)
            session = await db.get(Session, thread.session_id)
        if binding is None or session is None or session.deleted:
            raise HTTPException(403, "This Slack thread cannot be continued.")
        if binding.connection_authority != bot["authority"]:
            raise HTTPException(403, "This thread belongs to a changed Slack installation.")
        if Decimal(timestamp) < Decimal(binding.started_ts):
            return {"ok": True}
        if channel.startswith("D") and binding.requester_id != actor.id:
            raise HTTPException(403, "This DM belongs to a different Slack participant.")
        if binding.paused and not mentioned and command not in {"wake", "status"}:
            return {"ok": True}
        if command in {"stop", "sleep", "wake", "status"}:
            await self.command(actor, thread, binding, command, message_key)
            return {"ok": True}
        if answering or command == "continue":
            from .product_slack_controls import answer
            try:
                resumed = await answer(self, actor, thread, answering[1] or "" if answering else "", continuing=command == "continue")
                message = "Your response was received; the task is continuing." if resumed else (
                    "Your response was saved. Complete the remaining question, approval, or secure request, then reply /continue."
                )
            except HTTPException as error:
                if error.status_code >= 500:
                    raise
                message = str(error.detail)
            await self.notice(actor, thread, "answer:" + message_key, message)
            return {"ok": True}
        directive = re.fullmatch(r"/?(model|harness|goal)(?:\s+([^\n]*))?", prompt.strip(), re.IGNORECASE)
        if directive:
            kind, value = directive[1].lower(), (directive[2] or "").strip()
            if kind == "model" and value in self.runtime.settings.models:
                async with self.runtime.database.sessions.begin() as db:
                    await self.runtime.database.allowed_session(db, thread.session_id, actor)
                    await db.execute(update(Session).where(Session.id == thread.session_id).values(model=value))
                message = f"New messages in this session will use {value}. Running turns keep their model."
            elif kind == "model":
                message = "Choose an enabled model: " + ", ".join(self.runtime.settings.models)
            elif kind == "harness":
                message = "This session uses the Pydantic core. Switching to an older engine is unavailable."
            else:
                message = "Slack goal controls are not available on this core yet. Use the Moyai session."
            await self.notice(actor, thread, "directive:" + message_key, message)
            return {"ok": True}
        if binding.paused:
            await self.set_paused(binding.session_id, False)
        assert body is not None
        if attachments["uploads"]:
            async with self.runtime.database.sessions.begin() as db:
                turn = await self.runtime.database.submit(actor, thread.session_id, body, db=db)
                await self.files.bind(db, actor.id, thread.session_id, attachments["uploads"])
        else:
            turn = await self.runtime.database.submit(actor, thread.session_id, body)
        await self.queue(turn, thread, "accepted", {"text": "Moyai received your message."})
        return {"ok": True}

    async def automation_channel_allowed(self, channel_id: str, installed: dict[str, Any]) -> bool:
        """A G-prefixed reaction can be an MPIM; signed IDs aren't channel kinds."""
        from app.connector_errors import ConnectorError

        if not ID.fullmatch(channel_id) or channel_id[0] not in "CG":
            return False
        try:
            token = await self.connectors.slack_bot_token()
            result = await self.connectors.request(
                "GET", "https://slack.com/api/conversations.info",
                headers={"Authorization": "Bearer " + token}, params={"channel": channel_id},
            )
        except ConnectorError:
            logger.warning("Slack automation event withheld: conversation privacy could not be verified.")
            return False
        if not isinstance(result, dict):
            return False
        current = await source_database(self.installation)
        value = result.get("channel")
        return bool(
            current["authority"] == installed["authority"] and result.get("ok") is True
            and isinstance(value, dict) and value.get("id") == channel_id
            and value.get("is_im") is False and value.get("is_mpim") is False
            and value.get("is_ext_shared") is False
            and (value.get("is_channel") is True or value.get("is_group") is True)
        )

    async def bind(
        self, actor: User, principal: ProductPrincipal, provenance: dict[str, str],
        bot: dict[str, Any], prompt: str, body: NewMessage | None = None, *, wake: bool = False,
        uploads: list[str] | None = None,
    ) -> SlackThread | None:
        def source_binding():
            rows = self.store.rows(
                """SELECT t.*,r.owner_id,r.private_owner_id,r.deleted_at,r.deletion_requested_at,
                   (SELECT e.user_id FROM slack_events e WHERE e.run_id=t.run_id
                    ORDER BY e.created_at LIMIT 1) AS source_sender
                   FROM slack_threads t JOIN runs r ON r.id=t.run_id
                   WHERE t.team_id=? AND t.channel=? AND t.thread_ts=?""",
                (provenance["team_id"], provenance["channel_id"], provenance["thread_ts"]),
            )
            return rows[0] if rows else None

        source = await source_database(source_binding)
        if source and (source["private_owner_id"] or source["deleted_at"] or source["deletion_requested_at"]):
            raise HTTPException(403, "This historical Slack session is private or deleted.")
        if source and provenance["channel_id"].startswith("D") and (
            source["source_sender"] != provenance["user_id"] and source["owner_id"] != actor.id
        ):
            raise HTTPException(403, "This historical DM belongs to a different participant.")
        if source and source["paused"] and not wake and prompt.strip().lower().lstrip("/") != "status":
            return None
        try:
            async with self.runtime.database.sessions.begin() as db:
                if source:
                    continuation = getattr(self.runtime, "product_continuations", None)
                    if continuation is None:
                        raise HTTPException(503, "Historical Slack continuation is not configured.")
                    mapping = await continuation.lookup(db, source["run_id"], principal)
                    if mapping is not None:
                        session_id = mapping.native_session_id
                    elif body is not None:
                        result = await continuation.continue_for_admission(
                            db, actor, principal, source["run_id"], body,
                            surface="slack", provenance=provenance,
                        )
                        session_id = result["id"]
                    else:
                        raise HTTPException(409, "Review this historical session in Moyai before using native controls.")
                    session = await db.get(Session, session_id)
                    if session is None or session.surface != "slack" or session.visibility != "shared" or session.deleted:
                        raise HTTPException(403, "Historical Slack delivery cannot use this session.")
                else:
                    session = await self.runtime.database.create_session(
                        actor, NewSession(title=prompt[:100] or "Slack conversation", visibility="shared"), db=db,
                    )
                    session.surface = "slack"
                await db.flush()
                if uploads:
                    await self.files.bind(db, actor.id, session.id, uploads)
                thread = SlackThread(
                    id=new_id(), session_id=session.id, team_id=provenance["team_id"],
                    channel_id=provenance["channel_id"], thread_ts=provenance["thread_ts"],
                )
                db.add(thread)
                db.add(ProductSlackBinding(
                    session_id=session.id, requester_id=actor.id, team_id=provenance["team_id"],
                    source_run_id=source["run_id"] if source else None,
                    channel_id=provenance["channel_id"], thread_ts=provenance["thread_ts"],
                    started_ts=provenance["message_ts"], connection_authority=bot["authority"],
                    paused=bool(source and source["paused"] and not wake),
                ))
            return thread
        except HTTPException as error:
            if source and error.status_code == 409:
                await self.source_review_notice(source, provenance, principal, bot)
                return None
            raise
        except IntegrityError:
            async with self.runtime.database.sessions() as db:
                thread = await db.scalar(select(SlackThread).where(
                    SlackThread.team_id == provenance["team_id"], SlackThread.channel_id == provenance["channel_id"],
                    SlackThread.thread_ts == provenance["thread_ts"],
                ))
                if thread is None:
                    raise
                return thread

    async def source_review_notice(self, source, provenance, principal, bot):
        """Use the existing outbox FK for a historical-session review notice."""
        key = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()
        message = (
            "This existing conversation needs a continuation review in Moyai. "
            "Its saved history and computer have been kept. "
            "Open " + self.settings.public_url.rstrip("/") + "/#run=" + source["run_id"]
        )
        metadata = json.dumps({
            "actor_id": principal.id, "authority_version": principal.authority_version,
            "connection_authority": bot["authority"],
        })

        def queue():
            with self.store.connect() as conn:
                conn.begin_write()
                row = conn.execute("SELECT private_owner_id,deleted_at,deletion_requested_at FROM runs WHERE id=?",
                                   (source["run_id"],)).fetchone()
                if row is None or row["private_owner_id"] or row["deleted_at"] or row["deletion_requested_at"]:
                    raise HTTPException(403, "This historical session cannot export to Slack.")
                conn.execute(
                    """INSERT INTO slack_outbox(run_id,dedupe_key,kind,text,metadata,status,created_at)
                       VALUES(?,?,'native_review',?,?,'pending',?) ON CONFLICT DO NOTHING""",
                    (source["run_id"], "native-review:" + key, message, metadata, now()),
                )
        await source_database(queue)

    async def set_paused(self, session_id: str, paused: bool) -> None:
        async with self.runtime.database.sessions.begin() as db:
            await db.execute(update(ProductSlackBinding).where(
                ProductSlackBinding.session_id == session_id,
            ).values(paused=paused, delivery_after=now()))
            # Neither wake nor deployment backfills answers completed asleep.
            await db.execute(update(Delivery).where(
                Delivery.session_id == session_id, Delivery.status == "pending",
            ).values(status="suppressed"))

    async def command(self, actor: User, thread: SlackThread, binding: ProductSlackBinding, command: str, key: str):
        if command in {"stop", "sleep"}:
            await self.runtime.database.stop(actor, thread.session_id)
        if command in {"sleep", "wake"}:
            await self.set_paused(thread.session_id, command == "sleep")
        messages = {
            "sleep": "Paused this thread and requested a stop. Say wake or mention Moyai to continue.",
            "wake": "Listening again. Send your next message here.",
            "stop": "Requested a stop and cleared queued follow-ups.",
            "status": "This thread is paused." if binding.paused else "This thread is listening.",
        }
        await self.notice(actor, thread, "command:" + key, messages[command])

    async def notice(self, actor: User, thread: SlackThread, key: str, message: str):
        # Control-plane responses are terminal evidence, never agent jobs.
        # Reserving a completed turn gives both web history and the outbox a
        # stable FK without invoking a model just to explain a control/refusal.
        identity = str(uuid5(NAMESPACE_URL, f"moyai/slack-control/{thread.session_id}/{actor.id}/{key}"))
        try:
            async with self.runtime.database.sessions.begin() as db:
                await self.runtime.database.allowed_session(db, thread.session_id, actor)
                grant = await self.runtime.database._actor_product_grant(db, actor)
                turn = await db.get(Turn, identity)
                if turn is None:
                    turn = Turn(
                        id=identity, session_id=thread.session_id, actor_id=actor.id,
                        request_key=key, request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
                        prompt=self.runtime.security.seal("Slack control"),
                        model=self.runtime.settings.default_model, status="completed",
                        result=self.runtime.security.seal(message), finished_at=now(),
                        workflow_id=f"moyai-slack-control-{identity}",
                    )
                    db.add(turn)
                    await db.flush()
                    from .performance import admission
                    admission(db, turn, turn.created_at, source="control", surface="slack")
                    self.runtime.database._bind_product_grant(db, turn, grant)
                    db.add(Message(
                        session_id=thread.session_id, turn_id=turn.id, role="assistant",
                        content=self.runtime.security.seal(message), identity="slack-control:" + identity,
                    ))
        except IntegrityError:
            async with self.runtime.database.sessions() as db:
                turn = await db.get(Turn, identity)
                if turn is None:
                    raise
        await self.queue(turn, thread, "control", {"text": message}, control=True)

    async def queue(
        self, turn: Turn, thread: SlackThread, key: str, content: dict[str, Any], *, control: bool = False,
    ):
        await self.queue_batch(turn, thread, [(key, content)], control=control)

    async def queue_batch(
        self, turn: Turn, thread: SlackThread, parts: list[tuple[str, dict[str, Any]]], *, control: bool = False,
    ):
        try:
            async with self.runtime.database.sessions.begin() as db:
                binding = await db.get(ProductSlackBinding, thread.session_id)
                suppressed = bool(binding and not control and (
                    binding.paused or (turn.finished_at and turn.finished_at <= binding.delivery_after)
                ))
                for key, content in parts:
                    if key == "accepted":
                        content = thread_message(content["text"], session_url(
                            self.settings.public_url, thread.session_id,
                            binding.source_run_id or "" if binding else "",
                        ))
                    db.add(Delivery(
                        id=new_id(), identity=f"product-slack:{turn.id}:{key}",
                        session_id=thread.session_id, turn_id=turn.id,
                        destination=json.dumps({
                            "channel": thread.channel_id, "thread_ts": thread.thread_ts, "control": control,
                        }),
                        content=self.runtime.security.seal(content),
                        status="suppressed" if suppressed else "pending",
                    ))
        except IntegrityError:
            pass

    async def stage(self):
        from app.slack_format import slack_text, split_reply

        async with self.runtime.database.sessions() as db:
            pairs = (await db.execute(select(Turn, SlackThread, ProductSlackBinding).join(
                SlackThread, SlackThread.session_id == Turn.session_id,
            ).join(ProductSlackBinding, ProductSlackBinding.session_id == Turn.session_id).where(
                ~Turn.workflow_id.startswith("moyai-slack-control-"),
                Turn.status.in_(["queued", "running", "waiting", "saving", "completed", "failed", "stopped"]),
                ~select(Delivery.id).where(
                    Delivery.identity == ("product-slack:" + Turn.id + ":" + Turn.status + ":0"),
                ).exists(),
            ).order_by(Turn.created_at).limit(100))).all()
        for turn, thread, binding in pairs:
            # Admission and delivery can run concurrently. Reserve the same
            # primary reply before any later state is queued.
            await self.queue(turn, thread, "accepted", {"text": "Moyai received your message."})
            text_value = {
                "queued": "Moyai is getting ready.", "running": "Moyai is working.",
                "saving": "Moyai is saving the work.", "waiting": "Moyai needs your response.",
                "failed": "This task stopped before it completed. Open Moyai for details.",
                "stopped": "The task stopped.",
            }.get(turn.status, "")
            if turn.status == "completed":
                text_value = self.runtime.security.unseal(turn.result).decode() if turn.result else "Task completed."
            url = session_url(self.settings.public_url, turn.session_id, binding.source_run_id or "")
            cards = pr_cards(text_value, url) if turn.status == "completed" else []
            content: dict[str, Any] = {"text": slack_text(text_value)}
            controls = []
            if turn.status == "waiting" and turn.conversation:
                conversation = ConversationTypeAdapter.validate_json(self.runtime.security.unseal(turn.conversation))
                pending = conversation.deferred_tool_requests
                if pending and pending.approvals:
                    content["text"] += "\nRequested actions: " + ", ".join(
                        html.escape(call.tool_name, quote=False) for call in pending.approvals
                    ) + ". Review arguments in Moyai before approving."
                    controls = [
                            {"type": "button", "action_id": "moyai_native_approve", "value": turn.id,
                             "text": {"type": "plain_text", "text": "Approve"}},
                            {"type": "button", "action_id": "moyai_native_deny", "value": turn.id,
                             "text": {"type": "plain_text", "text": "Decline"}},
                    ]
                if pending and pending.calls:
                    for call in pending.calls:
                        if call.tool_name == "ask_user_question":
                            request = AskUserRequest.from_tool_call(call)
                            for question in request.questions:
                                content["text"] += "\n*" + html.escape(question.header) + "*: " + html.escape(question.question)
                                content["text"] += "\nChoices: " + ", ".join(html.escape(option.label) for option in question.options)
                        elif call.tool_name == "credentials_request":
                            content["text"] += "\nProvide organization access through the secure form in Moyai. Never post secret values in Slack."
                    if any(call.tool_name == "ask_user_question" for call in pending.calls):
                        content["text"] += "\nReply `/answer your response` for one question, or `/answer {\"Heading\":\"response\"}` for several."
            if turn.status == "completed":
                async with self.runtime.database.sessions() as db:
                    artifacts = list(await db.scalars(select(Artifact).where(Artifact.turn_id == turn.id)))
                for artifact in artifacts:
                    content["text"] += (
                        "\n<" + self.settings.public_url.rstrip("/") + "/api/files/" + artifact.id
                        + "|" + html.escape(artifact.name, quote=False).replace("|", "¦") + ">"
                    )
            visible = without_pr_references(content["text"], cards)
            parts = split_reply(visible, limit=2900) if visible.strip() else [""]
            await self.queue_batch(turn, thread, [
                (f"{turn.status}:{index}", thread_message(part, url, cards=cards, controls=controls)
                 if index == 0 else {"text": part})
                for index, part in enumerate(parts)
            ])

    async def interact(self, payload: dict[str, Any]):
        bot = await source_database(self.installation)
        if any(payload.get(key) is not None and not isinstance(payload[key], dict)
               for key in ("team", "user", "channel", "message")):
            raise HTTPException(400, "Invalid Slack interaction fields.")
        team = (payload.get("team") or {}).get("id")
        user = (payload.get("user") or {}).get("id")
        channel = (payload.get("channel") or {}).get("id")
        actions = payload.get("actions")
        if not isinstance(team, str) or team != bot["team_id"] or not isinstance(user, str) or not ID.fullmatch(user):
            raise HTTPException(403, "Invalid Slack interaction identity.")
        if navigation_action(payload):
            return
        if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
            raise HTTPException(422, "Choose one current approval.")
        action, turn_id = actions[0].get("action_id"), actions[0].get("value")
        if action not in {"moyai_native_approve", "moyai_native_deny"} or not isinstance(turn_id, str):
            raise HTTPException(409, "This Slack action requires the Moyai session.")
        message_ts = (payload.get("message") or {}).get("ts")
        if not isinstance(message_ts, str) or not STAMP.fullmatch(message_ts):
            raise HTTPException(403, "This action has no confirmed Slack message.")
        actor, _ = await self.actor(team, user)
        async with self.runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            thread = await db.scalar(select(SlackThread).where(SlackThread.session_id == turn.session_id)) if turn else None
            binding = await db.get(ProductSlackBinding, turn.session_id) if turn else None
            if (
                turn is None or turn.status != "waiting" or not turn.conversation
                or thread is None or thread.team_id != team or thread.channel_id != channel
                or (payload.get("message") or {}).get("thread_ts") != thread.thread_ts
                or binding is None or binding.paused or binding.connection_authority != bot["authority"]
            ):
                raise HTTPException(403, "This approval is unavailable to this Slack sender.")
            delivery = await db.scalar(select(Delivery).where(
                Delivery.identity == f"product-slack:{turn.id}:waiting:0",
                Delivery.session_id == turn.session_id, Delivery.status == "sent",
            ))
            receipt = json.loads(self.runtime.security.unseal(delivery.receipt)) if delivery and delivery.receipt else {}
            destination = json.loads(delivery.destination) if delivery else {}
            delivered = json.loads(self.runtime.security.unseal(delivery.content)) if delivery else {}
            actions_sent = [
                element for block in delivered.get("blocks", []) if block.get("type") == "actions"
                for element in block.get("elements", [])
            ]
            if (
                receipt.get("ts") != message_ts or destination.get("channel") != channel
                or destination.get("thread_ts") != thread.thread_ts
                or not any(item.get("action_id") == action and item.get("value") == turn.id for item in actions_sent)
            ):
                raise HTTPException(403, "This action does not belong to the confirmed request message.")
            pending = ConversationTypeAdapter.validate_json(
                self.runtime.security.unseal(turn.conversation),
            ).deferred_tool_requests
            if pending is None or not pending.approvals:
                raise HTTPException(409, "Answer the current request in Moyai.")
        from .product_requester import answer_actor
        from .product_slack_controls import record_replies, resume_replies
        resumed_actor = await answer_actor(self.runtime, actor, turn)
        await record_replies(self.runtime, resumed_actor, turn, approvals={
            call.tool_call_id: action == "moyai_native_approve" for call in pending.approvals
        })
        await resume_replies(self.runtime, resumed_actor, turn)

    async def delivery_allowed(self, delivery: Delivery) -> bool:
        bot = await source_database(self.installation)
        async with self.runtime.database.sessions() as db:
            binding = await db.get(ProductSlackBinding, delivery.session_id)
            session = await db.get(Session, delivery.session_id)
            turn = await db.get(Turn, delivery.turn_id)
            actor = await db.get(User, turn.actor_id) if turn else None
            admitted = await db.get(ProductTurnAuthority, delivery.turn_id)
        control = json.loads(delivery.destination).get("control") is True
        if (
            binding is None or binding.connection_authority != bot["authority"]
            or (binding.paused and not control) or session is None or session.deleted
            or session.visibility != "shared" or session.surface != "slack"
            or actor is None or not actor.enabled or admitted is None
            or (not control and turn and turn.finished_at and turn.finished_at <= binding.delivery_after)
        ):
            return False
        try:
            await self.product.identity.require_current(ProductGrant(admitted.actor_id, admitted.authority_version))
        except HTTPException as error:
            if error.status_code >= 500:
                raise
            return False
        return True

    async def deliver(self, client: httpx.AsyncClient, delivery: Delivery):
        if not await self.delivery_allowed(delivery):
            async with self.runtime.database.sessions.begin() as db:
                await db.execute(update(Delivery).where(
                    Delivery.id == delivery.id, Delivery.status == "pending",
                ).values(status="suppressed"))
            return
        from .slack_progress import claim_delivery, reply_target
        ready, reply_ts = await reply_target(self.runtime, delivery, product=True)
        if not ready:
            return
        # Refresh only the installation bot. Never use personal/shared user OAuth.
        token = await self.connectors.slack_bot_token()
        if not await self.delivery_allowed(delivery):
            return
        if not await claim_delivery(self.runtime, delivery, product=True):
            return
        try:
            still_allowed = await self.delivery_allowed(delivery)
        except BaseException:
            # No external send has begun. Restore only this known-unsent claim;
            # the sending handler below never performs this transition.
            await self._settle(delivery.id, "pending")
            raise
        if not still_allowed:
            await self._settle(delivery.id, "suppressed")
            return
        destination = json.loads(delivery.destination)
        content = json.loads(self.runtime.security.unseal(delivery.content))
        try:
            response = await client.post("chat.update" if reply_ts else "chat.postMessage", headers={"Authorization": "Bearer " + token}, json={
                "channel": destination["channel"], **content,
                **({"ts": reply_ts, "blocks": content.get("blocks", []),
                    "attachments": content.get("attachments", [])} if reply_ts else {
                    "thread_ts": destination["thread_ts"], "unfurl_links": False, "unfurl_media": False,
                }),
                "metadata": {"event_type": "moyai_delivery", "event_payload": {"id": delivery.id}},
            })
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                return
        except (httpx.HTTPError, ValueError):
            # Sending survives lost response/process death/cancellation. There
            # is no timeout-based replay; reconciliation reads Slack metadata.
            return
        status = (
            "sent" if result.get("ok") and result.get("ts")
            else "rejected" if result.get("ok") is False else "sending"
        )
        await self._settle(delivery.id, status, result)

    async def _settle(self, identity: str, status: str, receipt: dict[str, Any] | None = None):
        async def write():
            async with self.runtime.database.sessions.begin() as db:
                values: dict[str, Any] = {"status": status}
                if receipt is not None:
                    values["receipt"] = self.runtime.security.seal(receipt)
                await db.execute(update(Delivery).where(
                    Delivery.id == identity, Delivery.status == "sending",
                ).values(**values))

        task = asyncio.create_task(write())
        cancelled = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as error:
                if task.cancelled():
                    raise
                cancelled = error
        if cancelled is not None:
            raise cancelled

    async def pump(self, client: httpx.AsyncClient | None = None):
        await self.stage()
        from .slack_progress import stage_progress
        await stage_progress(self.runtime, product=True)
        async with self.runtime.database.sessions() as db:
            deliveries = list(await db.scalars(select(Delivery).where(
                Delivery.identity.startswith("product-slack:"), Delivery.status.in_(["pending", "sending"]),
            ).order_by(Delivery.created_at).limit(20)))
        if client is None:
            async with httpx.AsyncClient(base_url="https://slack.com/api/", timeout=20) as own:
                channels = await self._deliver_batch(own, deliveries)
                await self.source_notices(own, channels)
        else:
            channels = await self._deliver_batch(client, deliveries)
            await self.source_notices(client, channels)

    async def _deliver_batch(self, client: httpx.AsyncClient, deliveries: list[Delivery]):
        from .slack import reconcile_one
        from .slack_progress import skip_obsolete

        channels: set[str] = set()
        for delivery in deliveries:
            if delivery.status == "pending":
                if await skip_obsolete(self.runtime, delivery, product=True):
                    continue
                channel = json.loads(delivery.destination)["channel"]
                if channel in channels:
                    continue
                channels.add(channel)
                await self.deliver(client, delivery)
            elif await self.delivery_allowed(delivery):
                token = await self.connectors.slack_bot_token()
                # Reconciliation is a read and may retry; never post again.
                await reconcile_one(
                    self.runtime, client, delivery, headers={"Authorization": "Bearer " + token},
                )
        return channels

    async def source_notices(self, client: httpx.AsyncClient, channels: set[str]):
        """Only our review notices use the old run-bound outbox; no old answers."""
        rows = await source_database(
            self.store.rows,
            """SELECT o.*,t.channel,t.thread_ts,t.team_id FROM slack_outbox o
               JOIN slack_threads t ON t.run_id=o.run_id JOIN runs r ON r.id=o.run_id
               WHERE o.kind='native_review' AND o.status='pending'
                 AND r.private_owner_id='' AND r.deleted_at='' AND r.deletion_requested_at=''
               ORDER BY o.id LIMIT 10""",
        )
        for row in rows:
            if row["channel"] in channels:
                continue
            channels.add(row["channel"])
            metadata = json.loads(row["metadata"])

            async def authorized(row=row, metadata=metadata):
                bot = await source_database(self.installation)
                if bot["team_id"] != row["team_id"] or bot["authority"] != metadata["connection_authority"]:
                    raise HTTPException(403, "The historical Slack destination changed.")
                await self.product.identity.require_current(ProductGrant(
                    metadata["actor_id"], metadata["authority_version"],
                ))

            try:
                await authorized()
            except HTTPException as error:
                if error.status_code >= 500:
                    raise
                await source_database(self.store.execute,
                                      "UPDATE slack_outbox SET status='skipped' WHERE id=? AND status='pending'", (row["id"],))
                continue
            token = await self.connectors.slack_bot_token()
            claim = {"owned": False}

            def reserve(row=row, claim=claim):
                claim["owned"] = bool(self.store.execute(
                    """UPDATE slack_outbox SET status='sending' WHERE id=? AND status='pending'
                       AND EXISTS(SELECT 1 FROM runs r WHERE r.id=slack_outbox.run_id
                         AND r.private_owner_id='' AND r.deleted_at='' AND r.deletion_requested_at='')""",
                    (row["id"],),
                ))

            try:
                await source_database(reserve)
            except asyncio.CancelledError:
                # database() drained the actual CAS. Only its successful owner
                # can return this proven-unsent row to pending.
                if claim["owned"]:
                    await source_database(self.store.execute,
                                          "UPDATE slack_outbox SET status='pending' WHERE id=? AND status='sending'", (row["id"],))
                raise
            if not claim["owned"]:
                continue
            try:
                await authorized()
            except BaseException:
                await source_database(self.store.execute,
                                      "UPDATE slack_outbox SET status='pending' WHERE id=? AND status='sending'", (row["id"],))
                raise
            try:
                response = await client.post("chat.postMessage", headers={"Authorization": "Bearer " + token}, json={
                    "channel": row["channel"], "thread_ts": row["thread_ts"], "text": row["text"],
                    "unfurl_links": False, "unfurl_media": False,
                    "metadata": {"event_type": "moyai_delivery", "event_payload": {"id": "source:" + str(row["id"])}},
                })
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict) or not result.get("ok") or not result.get("ts"):
                    raise ValueError("Slack did not confirm the review notice.")
            except (httpx.HTTPError, ValueError, asyncio.CancelledError) as error:
                await source_database(self.store.execute,
                                      "UPDATE slack_outbox SET status='uncertain' WHERE id=? AND status='sending'", (row["id"],))
                if isinstance(error, asyncio.CancelledError):
                    raise
                continue
            # Confirmed receipt persistence is outside the ambiguous-send catch.
            await source_database(self.store.execute,
                                  "UPDATE slack_outbox SET status='sent',slack_ts=? WHERE id=? AND status='sending'",
                                  (result["ts"], row["id"]))

    def routes(self) -> APIRouter:
        from app.coordinator_handoff import bounded_body
        from app.db import database

        router = APIRouter()

        @router.post("/hooks/slack")
        @router.post("/hooks/slack/events")
        async def event(request: Request):
            return await self.inbox.receive_event(request)

        @router.post("/hooks/slack/interactions")
        async def interaction(request: Request):
            raw = await bounded_body(request, 65536)
            self.verify_signature(raw, request.headers)
            try:
                values = parse_qs(raw.decode(), strict_parsing=True)
                if len(values["payload"]) != 1:
                    raise ValueError()
                payload = json.loads(values["payload"][0])
                if not isinstance(payload, dict):
                    raise TypeError()
            except (ValueError, TypeError, KeyError, UnicodeDecodeError):
                raise HTTPException(400, "Invalid Slack interaction.") from None
            key = "native-interaction:" + hashlib.sha256(raw).hexdigest()
            await database(self.inbox.enqueue, key, {"type": "native_interaction", "payload": payload})
            self.inbox.wake.set()
            return {"ok": True}

        return router
