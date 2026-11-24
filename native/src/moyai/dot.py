"""A persistent personal conversation; existing automations own every wake-up."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelResponse, TextPart, ToolReturnPart
from sqlalchemy import func, select, update

from .automations.product_automation_models import ProductAutomationBinding, ProductAutomationTurn
from .connection_policy import ConnectionPolicy
from .contracts import Grant, NewSession, RunScope
from .dot_contracts import DotDecision
from .dot_models import DotAttentionTurn, DotNotification, PersonalDot
from .models import Connection, Message, Session, Turn, User, now
from .private_instructions import individual

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database


def evidence_text(value: object) -> str:
    """Keep string leaves literal so structured MCP content can be quoted."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(evidence_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(evidence_text(item) for item in value)
    return str(value)


def validate_decision(output: str, conversation: str, grants: list[Grant]) -> DotDecision:
    decision = DotDecision.model_validate_json(output)
    allowed = {
        f"{grant.kind}_{grant.id[:8]}_{name}"
        for grant in grants
        for name in (ConnectionPolicy.model_validate(grant.policy).allowed_tools or [])
    }
    returns = [
        evidence_text(part.content)
        for message in ConversationTypeAdapter.validate_json(conversation).messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name in allowed
    ]
    if any(not any(
        item.source in text and item.source_version in text and item.quote in text for text in returns
    ) for item in decision.evidence):
        raise ValueError("Dot cited evidence outside the connected tools' results.")
    return decision


class Dots:
    def __init__(self, database: Database):
        self.database = database

    async def grants(self, db: AsyncSession, owner: str, ids: list[str], *, lock: bool = False) -> list[Grant]:
        """Require explicit personal read-tool policy in addition to provider hints."""
        if not ids:
            raise HTTPException(409, "Choose a personal read-only connection with an explicit read-tool allowlist.")
        query = select(Connection).where(Connection.id.in_(ids)).execution_options(populate_existing=True)
        rows = list(await db.scalars(query.with_for_update() if lock else query))
        if len(rows) != len(set(ids)) or any(
            row.owner_id != owner or row.shared or not row.enabled or not row.read_only
            or not ConnectionPolicy.model_validate_json(row.policy).allowed_tools
            for row in rows
        ):
            raise HTTPException(409, "Attention requires your private read-only accounts with explicitly allowed read tools.")
        return await self.database.resolve_grants(db, owner, ids, shared_session=False, surface="automation")

    async def worker_session(self, db: AsyncSession, session_id: str) -> bool:
        return bool(await db.scalar(select(DotAttentionTurn.turn_id).join(
            Turn, Turn.id == DotAttentionTurn.turn_id,
        ).where(Turn.session_id == session_id).limit(1)))

    async def own_session(self, db: AsyncSession, owner: str, session_id: str) -> Session:
        dot = await db.get(PersonalDot, owner)
        session = await db.get(Session, session_id)
        if (
            dot is None or dot.session_id != session_id or session is None
            or session.owner_id != owner or session.visibility != "private" or session.surface != "web"
            or session.deleted or session.archived
        ):
            raise HTTPException(409, "Open your active private Dot conversation before setting up attention checks.")
        return session

    async def read(self, actor: User) -> str | None:
        async with self.database.sessions.begin() as db:
            grant = await self.database._actor_product_grant(db, actor)
            if not await individual(self.database, db, actor, grant=grant):
                raise HTTPException(403, "Your Dot requires an individual sign-in.")
            dot = await db.get(PersonalDot, actor.id)
            if dot is None:
                return None
            session = await db.get(Session, dot.session_id)
            return session.id if session and not session.deleted else None

    async def open(self, actor: User) -> str:
        async with self.database._admission_session(None) as db:
            grant = await self.database._actor_product_grant(db, actor)
            if not await individual(self.database, db, actor, grant=grant):
                raise HTTPException(403, "Your Dot requires an individual sign-in.")
            locked = await db.scalar(update(User).where(
                User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if locked is None:
                raise HTTPException(401, "Your access changed. Sign in again.")
            dot = await db.get(PersonalDot, actor.id)
            session = await db.get(Session, dot.session_id) if dot else None
            if session is not None and not session.deleted:
                if session.visibility != "private" or session.owner_id != actor.id or session.surface != "web":
                    raise HTTPException(409, "The saved Dot conversation is unavailable.")
                session.archived = False
                return session.id
            session = await self.database.create_session(actor, NewSession(title="Your Dot", visibility="private"), db=db)
            await db.flush()
            if dot is None:
                db.add(PersonalDot(owner_id=actor.id, session_id=session.id))
            else:
                dot.session_id = session.id
                dot.checkpoint_cursor = 0
            return session.id

    async def bind_attention(
        self, db: AsyncSession, turn: Turn, *, automation_id: str, generation: int,
        destination: str, grants_key: str,
    ) -> None:
        await self.own_session(db, turn.actor_id, destination)
        worker = await db.get(Session, turn.session_id)
        if (
            await db.get(ProductAutomationTurn, turn.id) is None or worker is None
            or worker.surface != "automation" or worker.visibility != "private"
            or worker.id == destination or worker.owner_id != turn.actor_id
        ):
            raise PermissionError("Attention requires an admitted private automation occurrence.")
        db.add(DotAttentionTurn(
            turn_id=turn.id, automation_id=automation_id, generation=generation,
            destination_session_id=destination, grants_key=grants_key,
        ))

    async def authorize(
        self, db: AsyncSession, turn: Turn, *, scope: RunScope | None = None, publication: bool = False,
    ) -> DotAttentionTurn | None:
        from .database import grant_fingerprint

        record = await db.get(DotAttentionTurn, turn.id)
        if record is None:
            if scope is not None and scope.attention:
                raise PermissionError("This turn has no background attention authority.")
            return None
        if scope is not None and not scope.attention:
            raise PermissionError("Attention must use its restricted execution mode.")
        if publication:
            # The product authority check holds the canonical shared writer fence.
            # Serialize native destination/connection changes through their rows.
            await db.scalar(select(User).where(User.id == turn.actor_id).with_for_update())
            await db.scalar(select(Session).where(
                Session.id == record.destination_session_id,
            ).with_for_update().execution_options(populate_existing=True))
        await self.own_session(db, turn.actor_id, record.destination_session_id)
        binding = await db.get(ProductAutomationBinding, record.automation_id, populate_existing=True)
        if (
            binding is None or binding.owner_id != turn.actor_id
            or binding.generation != record.generation or binding.state == "deleted"
        ):
            raise PermissionError("This attention check was paused or changed.")
        grants = await self.grants(db, turn.actor_id, json.loads(turn.connections), lock=publication)
        if grant_fingerprint(grants) != record.grants_key or (
            scope is not None and scope.grants_key != record.grants_key
        ):
            raise PermissionError("The attention check's selected accounts changed. Review and enable it again.")
        return record

    async def attention_prompt(self, db: AsyncSession, turn: Turn) -> str:
        record = await self.authorize(db, turn)
        assert record is not None
        messages = list(await db.scalars(select(Message).where(
            Message.session_id == record.destination_session_id, Message.role.in_(["user", "assistant"]),
        ).order_by(Message.id.desc()).limit(30)))
        return json.dumps({
            "instructions": self.database.security.unseal(turn.prompt).decode(),
            "current_time": now(),
            "conversation": [
                {"role": message.role, "text": self.database.security.unseal(message.content).decode()[:3000]}
                for message in reversed(messages)
            ],
            "context_limit": "The latest 30 public messages, each limited to 3000 characters.",
        }, ensure_ascii=False)

    async def publication(
        self, db: AsyncSession, turn: Turn, scope: RunScope, *, output: str, conversation: str,
    ) -> None:
        """The caller holds both the worker and destination publication fences."""
        record = await db.get(DotAttentionTurn, turn.id)
        if record is None:
            raise PermissionError("The attention occurrence is unavailable.")
        decision = validate_decision(output, conversation, scope.grants)
        if decision.action == "quiet":
            record.outcome = "quiet"
            return
        session = await self.own_session(db, turn.actor_id, record.destination_session_id)
        primary = decision.evidence[0]
        fingerprint = hashlib.sha256(json.dumps(
            [primary.source.strip(), primary.source_version.strip()], ensure_ascii=False,
        ).encode()).hexdigest()
        if await db.scalar(select(DotNotification.turn_id).where(
            DotNotification.session_id == session.id, DotNotification.fingerprint == fingerprint,
        )):
            record.outcome = "duplicate"
            return
        content = decision.message + "\n\nSource: " + primary.source
        message = Message(
            session_id=session.id, turn_id=turn.id, role="assistant",
            content=self.database.security.seal(content), identity=f"dot:{turn.id}",
        )
        db.add(message)
        await db.flush()
        db.add(DotNotification(
            turn_id=turn.id, session_id=session.id, message_id=message.id, fingerprint=fingerprint,
            content=self.database.security.seal(content),
        ))
        record.outcome = "notified"
        # Public delivery must not change the SDK checkpoint or its computer
        # recovery digest. The next normal preparation incorporates this mail.
        session.revision += 1
        session.updated_at = now()

    async def cursor(self, db: AsyncSession, session_id: str) -> int | None:
        if not await db.scalar(select(PersonalDot.owner_id).where(PersonalDot.session_id == session_id)):
            return None
        return await db.scalar(select(func.max(DotNotification.message_id)).where(
            DotNotification.session_id == session_id,
        )) or 0

    async def carry_notifications(
        self, db: AsyncSession, session_id: str, conversation: str | None, after: int,
    ) -> str:
        """Enrich only the prepared input; leave the saved recovery checkpoint intact."""
        notifications = list(await db.scalars(select(DotNotification).where(
            DotNotification.session_id == session_id, DotNotification.message_id > after,
        ).order_by(DotNotification.message_id)))
        if not notifications:
            return conversation or ConversationTypeAdapter.dump_json(Conversation()).decode()
        saved = ConversationTypeAdapter.validate_json(conversation) if conversation else Conversation()
        saved.messages.extend(ModelResponse(parts=[TextPart(
            self.database.security.unseal(item.content).decode(),
        )]) for item in notifications)
        return ConversationTypeAdapter.dump_json(saved).decode()

    async def saved_checkpoint(self, db: AsyncSession, scope: RunScope) -> None:
        """Atomically track which public deliveries a saved SDK conversation includes."""
        if scope.dot_notification_cursor is not None:
            dot = await db.get(PersonalDot, scope.actor_id)
            if dot is not None and dot.session_id == scope.session_id:
                dot.checkpoint_cursor = scope.dot_notification_cursor

    async def checkpoint_cursor(self, db: AsyncSession, session_id: str) -> int:
        return await db.scalar(select(PersonalDot.checkpoint_cursor).where(
            PersonalDot.session_id == session_id,
        )) or 0
