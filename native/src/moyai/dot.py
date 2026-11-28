"""A persistent personal conversation; existing automations own every wake-up."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import func, select, update

from .attention_policy import attention_reads
from .automations.product_automation_models import ProductAutomationBinding, ProductAutomationTurn
from .connection_policy import ConnectionPolicy
from .contracts import Grant, NewSession, RunScope
from .dot_evidence import validate_decision
from .dot_models import DotAttentionTurn, DotDiscoveryTurn, DotNotification, DotSlackConversation, PersonalDot
from .models import Connection, Message, Session, Turn, User, now
from .oauth_models import OAuthCredential
from .private_instructions import individual

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database


class Dots:
    def __init__(self, database: Database):
        self.database = database
        from .dot_discovery import Discovery
        self.discovery = Discovery(database)

    async def grants(self, db: AsyncSession, owner: str, ids: list[str], *, lock: bool = False) -> list[Grant]:
        """Require explicit personal read-tool policy in addition to provider hints."""
        from .connection_attention import provider_enabled, slack_consent

        if not ids:
            raise HTTPException(409, "Choose a personal read-only connection with an explicit read-tool allowlist.")
        query = select(Connection).where(Connection.id.in_(ids)).execution_options(populate_existing=True)
        rows = list(await db.scalars(query.with_for_update() if lock else query))
        if any(row.owner_id != owner or row.shared or not row.enabled for row in rows):
            raise HTTPException(409, "Attention requires your private read-only accounts with explicitly allowed read tools.")
        grants = await self.database.resolve_grants(db, owner, ids, shared_session=False, surface="automation")
        native_ids = {row.id for row in rows}
        user = await db.get(User, owner, populate_existing=True)
        for grant in grants:
            if grant.policy.get("oauth") and not await provider_enabled(self.database, db, grant.kind):
                raise HTTPException(409, "This provider is disabled by the workspace.")
            reference = grant.policy.get("product") or {}
            if not attention_reads(grant) or (grant.id not in native_ids and not (
                grant.kind == "slack" and reference.get("personal_owner") == owner
                and reference.get("access") == "personal_reads" and slack_consent(user) == grant.id
            )):
                raise HTTPException(409, "Attention requires your private accounts with explicitly allowed read tools.")
        return grants

    async def worker_session(self, db: AsyncSession, session_id: str) -> bool:
        return bool(await db.scalar(select(DotAttentionTurn.turn_id).join(
            Turn, Turn.id == DotAttentionTurn.turn_id,
        ).where(Turn.session_id == session_id).limit(1)))

    async def is_conversation(self, db: AsyncSession, owner: str, session: Session | None) -> bool:
        """Identify the owner's current private Dot or one of its Slack roots."""
        if (
            session is None or session.owner_id != owner
            or session.visibility != "private" or session.surface != "web"
        ):
            return False
        dot = await db.get(PersonalDot, owner)
        if dot is None:
            return False
        if session.id == dot.session_id:
            return True
        linked = await db.get(DotSlackConversation, session.id)
        return linked is not None and linked.dot_session_id == dot.session_id

    async def connected_accounts(self, db: AsyncSession, owner: str, session: Session) -> list[str]:
        """Owner-enabled proactive accounts also support direct Dot follow-ups.

        Only the personal conversation and its private Slack roots qualify.
        Generated work keeps the exact accounts admitted by its own authority.
        """
        if not await self.is_conversation(db, owner, session):
            return []
        from .connection_attention import canonical_slack, native_grant, slack_consent

        rows = await db.execute(select(Connection, OAuthCredential.encrypted).outerjoin(
            OAuthCredential, OAuthCredential.connection_id == Connection.id,
        ).where(
            Connection.owner_id == owner, Connection.shared.is_(False),
            Connection.enabled.is_(True),
        ).execution_options(populate_existing=True))
        accounts = [
            row.id for row, credential in rows
            if (policy := ConnectionPolicy.model_validate_json(row.policy)).use_proactively
            and (not policy.oauth or credential)
            and attention_reads(native_grant(row))
        ]
        identity = slack_consent(await db.get(User, owner))
        if identity is not None and await canonical_slack(self.database, db, owner, identity) is not None:
            accounts.append(identity)
        return accounts

    async def delivery_session(self, db: AsyncSession, session_id: str | None, turn_id: str | None) -> str | None:
        """Public run links open the conversation, never the immutable observer."""
        record = await db.get(DotAttentionTurn, turn_id) if turn_id else None
        return record.destination_session_id if record else session_id

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
        destination: str, grants_key: str, discovery: bool = False,
        sources: list[dict[str, str]] | None = None,
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
        if discovery:
            from .dot_sources import repositories
            repositories(await self.grants(db, turn.actor_id, json.loads(turn.connections)), sources or [])
            await db.flush()
            db.add(DotDiscoveryTurn(turn_id=turn.id, sources=self.database.security.seal(json.dumps(sources or []))))

    async def authorize(
        self, db: AsyncSession, turn: Turn, *, scope: RunScope | None = None, publication: bool = False,
    ) -> DotAttentionTurn | None:
        from .automations.product_automation_context_models import ProductAutomationExecutionContext
        from .automations.product_automations import DISCOVERY_BATCH_SIZE, ProductAutomationService
        from .database import grant_fingerprint

        record = await db.get(DotAttentionTurn, turn.id)
        if record is None:
            if scope is not None and scope.attention:
                raise PermissionError("This turn has no background attention authority.")
            return None
        if scope is not None and not scope.attention:
            raise PermissionError("Attention must use its restricted execution mode.")
        discovery = await db.get(DotDiscoveryTurn, turn.id)
        if scope is not None and scope.discovery != (discovery is not None):
            raise PermissionError("The discovery execution mode changed.")
        if scope is not None and scope.discovery:
            from .dot_sources import repositories
            assert discovery is not None
            if scope.discovery_repositories != repositories(
                scope.grants, json.loads(self.database.security.unseal(discovery.sources)),
            ):
                raise PermissionError("The discovery source scope changed.")
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
        selected = json.loads(turn.connections)
        captured = (
            await db.get(ProductAutomationExecutionContext, (binding.id, record.generation))
            if self.database.product_authority_enabled else None
        )
        if captured is None:
            if self.database.product_authority_enabled:
                raise PermissionError("The attention check has no captured account consent.")
            grants = await self.grants(db, turn.actor_id, selected, lock=publication)
        else:
            pool, workspace = json.loads(captured.connection_ids), json.loads(captured.workspace_context)
            if (
                captured.owner_id != binding.owner_id or captured.authority_version != binding.authority_version
                or captured.fingerprint != ProductAutomationService._context_fingerprint(
                    binding.id, record.generation, binding.owner_id, binding.authority_version, pool, workspace,
                )
                or not selected or len(set(selected)) != len(selected) or not set(selected) <= set(pool)
                or (discovery is not None and len(selected) > DISCOVERY_BATCH_SIZE)
                or (discovery is None and sorted(selected) != sorted(pool))
            ):
                raise PermissionError("The attention check's captured account selection changed.")
            # A deferred account changing or revoking consent invalidates the
            # current occurrence too. Publication holds every native row fence.
            all_grants = await self.grants(db, turn.actor_id, pool, lock=publication)
            if workspace != {"format": "moyai-attention-v1", "grants_key": grant_fingerprint(all_grants)}:
                raise PermissionError("The attention account pool changed. Review and enable it again.")
            grants = [grant for grant in all_grants if grant.id in selected]
        if grant_fingerprint(grants) != record.grants_key or (
            scope is not None and scope.grants_key != record.grants_key
        ):
            raise PermissionError("The attention check's selected accounts changed. Review and enable it again.")
        return record

    async def attention_prompt(self, db: AsyncSession, turn: Turn) -> str:
        record = await self.authorize(db, turn)
        assert record is not None
        if await db.get(DotDiscoveryTurn, turn.id) is not None:
            return await self.discovery.scan_prompt(db, turn, record.destination_session_id)
        messages = list(await db.scalars(select(Message).where(
            Message.session_id == record.destination_session_id, Message.role.in_(["user", "assistant"]),
        ).order_by(Message.id.desc()).limit(30)))
        return json.dumps({
            "instructions": self.database.security.unseal(turn.prompt).decode(),
            "connection_ids": json.loads(turn.connections),
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
        if scope.discovery:
            outcome, notification, source, fingerprint = await self.discovery.apply(db, turn, output)
            record.outcome = outcome
            if notification is None:
                return
        else:
            decision, fingerprint = validate_decision(output, conversation, scope.grants)
            if decision.action == "quiet":
                record.outcome = "quiet"
                return
            notification, source = decision.message, decision.evidence[0].source
        session = await self.own_session(db, turn.actor_id, record.destination_session_id)
        assert fingerprint is not None
        assert source is not None
        destinations = select(Session.id).where(Session.owner_id == turn.actor_id)
        if await db.scalar(select(DotNotification.turn_id).where(
            DotNotification.session_id.in_(destinations) if scope.discovery else DotNotification.session_id == session.id,
            DotNotification.fingerprint == fingerprint,
        )):
            if record.outcome not in ("partial", "blocked"):
                record.outcome = "duplicate"
            return
        content = notification + "\n\nSource: " + source
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
        from .dot_notifications import queue
        await queue(self.database, db, turn, session.id, content)
        if record.outcome != "partial":
            record.outcome = "notified"
        # Public delivery must not change the SDK checkpoint or its computer
        # recovery digest. The next normal preparation incorporates this mail.
        # Message IDs already invalidate the transcript projection. Computer
        # capture uses Session.revision as a fence, so delivery must not bump it.
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
