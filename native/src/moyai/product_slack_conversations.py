"""Slack thread ownership and native/historical session binding."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.db import database as source_database
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from .contracts import NewMessage, NewSession
from .models import Session, SlackThread, User, new_id
from .product_identity import ProductPrincipal
from .product_slack_models import ProductSlackBinding

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .product_slack import ProductSlack


@dataclass(frozen=True)
class SlackConversation:
    """An eligible existing thread or a historical source awaiting admission."""

    thread: SlackThread | None = None
    binding: ProductSlackBinding | None = None
    source: dict[str, Any] | None = None


class SlackConversations:
    def __init__(self, slack: ProductSlack):
        self.slack = slack
        self.database = slack.runtime.database

    async def find(self, origin: dict[str, str]) -> SlackThread | None:
        async with self.database.sessions() as db:
            return await db.scalar(select(SlackThread).where(
                SlackThread.team_id == origin["team_id"],
                SlackThread.channel_id == origin["channel_id"],
                SlackThread.thread_ts == origin["thread_ts"],
            ))

    async def resolve(
        self, thread: SlackThread | None, actor: User, origin: dict[str, str], installed: dict,
        *, mentioned: bool, command: str,
    ) -> SlackConversation | None:
        """Check ownership and pause policy before reading files or thread content."""
        if thread is not None:
            return await self.existing(thread, actor, origin, installed, mentioned=mentioned, command=command)
        seen = await source_database(
            self.slack.store.rows,
            """SELECT 1 FROM slack_receipts WHERE team_id=? AND channel=? AND message_ts=?
               UNION ALL SELECT 1 FROM slack_events WHERE event_id=? LIMIT 1""",
            (origin["team_id"], origin["channel_id"], origin["message_ts"], origin["event_id"]),
        )
        if seen:
            return None
        source = await source_database(self._source, origin)
        if source:
            if source["private_owner_id"] or source["deleted_at"] or source["deletion_requested_at"]:
                raise HTTPException(403, "This historical Slack session is private or deleted.")
            if origin["channel_id"].startswith("D") and (
                source["source_sender"] != origin["user_id"] and source["owner_id"] != actor.id
            ):
                raise HTTPException(403, "This historical DM belongs to a different participant.")
            if source["paused"] and not mentioned and command not in {"wake", "status"}:
                return None
        return SlackConversation(source=source)

    def _source(self, origin: dict[str, str]) -> dict[str, Any] | None:
        rows = self.slack.store.rows(
            """SELECT t.*,r.owner_id,r.private_owner_id,r.deleted_at,r.deletion_requested_at,
               (SELECT e.user_id FROM slack_events e WHERE e.run_id=t.run_id
                ORDER BY e.created_at LIMIT 1) AS source_sender
               FROM slack_threads t JOIN runs r ON r.id=t.run_id
               WHERE t.team_id=? AND t.channel=? AND t.thread_ts=?""",
            (origin["team_id"], origin["channel_id"], origin["thread_ts"]),
        )
        return rows[0] if rows else None

    async def existing(
        self, thread: SlackThread, actor: User, origin: dict[str, str], installed: dict,
        *, mentioned: bool, command: str,
    ) -> SlackConversation | None:
        async with self.database.sessions() as db:
            binding = await db.get(ProductSlackBinding, thread.session_id)
            session = await db.get(Session, thread.session_id)
        if binding is None or session is None or session.deleted:
            raise HTTPException(403, "This Slack thread cannot be continued.")
        if binding.connection_authority != installed["authority"]:
            raise HTTPException(403, "This thread belongs to a changed Slack installation.")
        if Decimal(origin["message_ts"]) < Decimal(binding.started_ts):
            return None
        if origin["channel_id"].startswith("D") and binding.requester_id != actor.id:
            raise HTTPException(403, "This DM belongs to a different Slack participant.")
        if binding.paused and not mentioned and command not in {"wake", "status"}:
            return None
        return SlackConversation(thread=thread, binding=binding)

    async def _session_for_admission(
        self, db: AsyncSession, actor: User, principal: ProductPrincipal, source: dict | None,
        origin: dict[str, str], prompt: str, body: NewMessage | None,
    ) -> Session:
        if source is None:
            session = await self.database.create_session(
                actor, NewSession(title=prompt[:100] or "Slack conversation", visibility="shared"), db=db,
            )
            session.surface = "slack"
            return session
        continuation = getattr(self.slack.runtime, "product_continuations", None)
        if continuation is None:
            raise HTTPException(503, "Historical Slack continuation is not configured.")
        mapping = await continuation.lookup(db, source["run_id"], principal)
        if mapping is not None:
            session_id = mapping.native_session_id
        elif body is not None:
            result = await continuation.continue_for_admission(
                db, actor, principal, source["run_id"], body, surface="slack", provenance=origin,
            )
            session_id = result["id"]
        else:
            raise HTTPException(409, "Review this historical session in Moyai before using native controls.")
        session = await db.get(Session, session_id)
        if session is None or session.surface != "slack" or session.visibility != "shared" or session.deleted:
            raise HTTPException(403, "Historical Slack delivery cannot use this session.")
        return session

    async def bind_history(self, db, session, source_id: str, actor, provenance: dict) -> None:
        """Bind the authorized source before first native admission, in its transaction."""
        history = self.slack.runtime.product_history
        assert history is not None
        row = (await db.execute(text(f"""
            SELECT s.team_id,s.channel,s.thread_ts,s.paused,r.owner_id,
              (SELECT e.user_id FROM {history.table('slack_events')} e WHERE e.run_id=s.run_id
               ORDER BY e.created_at LIMIT 1) AS sender
            FROM {history.table('slack_threads')} s JOIN {history.table('runs')} r ON r.id=s.run_id
            WHERE s.run_id=:id
        """), {"id": source_id})).mappings().first()
        installed = await source_database(self.slack.installation)
        if row is None or installed["team_id"] != row["team_id"]:
            raise HTTPException(403, "The original Slack destination is unavailable.")
        thread = SlackThread(session_id=session.id, team_id=row["team_id"],
                             channel_id=row["channel"], thread_ts=row["thread_ts"])
        binding = ProductSlackBinding(
            session_id=session.id, source_run_id=source_id,
            requester_id=f"slack:{row['team_id']}:{row['sender']}" if row["sender"] else row["owner_id"],
            team_id=row["team_id"], channel_id=row["channel"], thread_ts=row["thread_ts"],
            started_ts=provenance.get("message_ts", row["thread_ts"]),
            connection_authority=installed["authority"], paused=bool(row["paused"]),
        )
        from .slack_audience import can_read_binding
        if not await can_read_binding(self.slack, db, binding, actor):
            raise HTTPException(403, "Your Slack access could not be verified.")
        db.add_all([thread, binding])
        await db.flush()

    async def bind(
        self, conversation: SlackConversation, actor: User, principal: ProductPrincipal,
        origin: dict[str, str], installed: dict, prompt: str, body: NewMessage | None,
        *, wake: bool, uploads: list[str],
    ) -> SlackThread | None:
        """Persist the resolved destination with an already prepared, immutable input."""
        source = conversation.source
        try:
            async with self.database.sessions.begin() as db:
                session = await self._session_for_admission(db, actor, principal, source, origin, prompt, body)
                await db.flush()
                if uploads:
                    await self.slack.files.bind(db, actor.id, session.id, uploads)
                existing = await db.scalar(select(SlackThread).where(SlackThread.session_id == session.id))
                if existing is not None:
                    return existing
                thread = SlackThread(
                    id=new_id(), session_id=session.id, team_id=origin["team_id"],
                    channel_id=origin["channel_id"], thread_ts=origin["thread_ts"],
                )
                db.add(thread)
                db.add(ProductSlackBinding(
                    session_id=session.id, requester_id=actor.id, team_id=origin["team_id"],
                    source_run_id=source["run_id"] if source else None,
                    channel_id=origin["channel_id"], thread_ts=origin["thread_ts"],
                    started_ts=origin["message_ts"], connection_authority=installed["authority"],
                    paused=bool(source and source["paused"] and not wake),
                ))
            return thread
        except HTTPException as error:
            if source and error.status_code == 409:
                await self.slack.source_review_notice(source, origin, principal, installed)
                return None
            raise
        except IntegrityError:
            # A competing intake may have created the binding. The caller must
            # recheck its authority and pause policy before submitting anything.
            thread = await self.find(origin)
            if thread is None:
                raise
            return thread
