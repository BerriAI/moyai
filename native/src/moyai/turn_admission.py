"""Turn submission, duplicate receipts and authorized execution preparation.

The interface accepts an existing native transaction for generated work; its
caller keeps ownership of commit/rollback and the occurrence receipt. Database
supplies live persistence and policy dependencies, including the session factory
replaced during role admission. No Runtime, transport or model is required.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .automations.product_automation_models import ProductAutomationTurn
from .contracts import ExecutionContext, NewMessage, PreparedTurn, RunScope
from .delegation_authority import DelegatedAuthority, saved_authority
from .delegation_models import DelegatedSession, DelegatedTurn
from .failed_action_attempts import settle_failed_action_attempts
from .models import Event, Message, ProductTurnAuthority, Session, Turn, TurnImage, Upload, User, new_id, now

if TYPE_CHECKING:
    from .database import Database


class TurnAdmission:
    def __init__(self, database: Database):
        self.database = database

    async def submit(
        self, actor: User, session_id: str, body: NewMessage, *, db: AsyncSession | None = None,
        swarm_generated: bool = False,
        automation_generated: bool = False,
        responsibility_generated: bool = False,
        delegated_authority: DelegatedAuthority | None = None,
        received_at: str | None = None,
        dot_accounts: bool = False,
    ) -> Turn:
        admitted_at = now()
        body.validate_event(actor.id)
        # Preserve transport receipts admitted before clarification replies
        # acquired an explicit target.
        fingerprint = hashlib.sha256(body.model_dump_json(
            exclude={"reply_to"} if body.reply_to is None else None,
        ).encode()).hexdigest()
        if db is not None:
            # Do not recover a conflicting receipt in an independent transaction.
            # The caller must roll back/retry its complete occurrence admission.
            return await self._submit(
                actor, session_id, body, fingerprint, db=db, swarm_generated=swarm_generated,
                automation_generated=automation_generated,
                responsibility_generated=responsibility_generated,
                delegated_authority=delegated_authority,
                received_at=received_at, admitted_at=admitted_at,
                dot_accounts=dot_accounts,
            )
        if swarm_generated or automation_generated or responsibility_generated or delegated_authority is not None:
            raise ValueError("Generated input requires its admission transaction.")
        try:
            return await self._submit(
                actor, session_id, body, fingerprint, received_at=received_at, admitted_at=admitted_at,
                dot_accounts=dot_accounts,
            )
        except IntegrityError:
            # Two requests can race the first lookup. The database's unique identity decides.
            async with self.database.sessions() as lookup:
                await self.database.allowed_session(lookup, session_id, actor)
                existing = await lookup.scalar(select(Turn).where(
                    Turn.session_id == session_id, Turn.actor_id == actor.id,
                    Turn.request_key == body.request_key,
                ))
                if existing and existing.request_fingerprint == fingerprint:
                    provenance = await lookup.get(ProductAutomationTurn, existing.id)
                    if provenance is None or provenance.origin_turn_id != existing.id:
                        return existing
            raise HTTPException(409, "A conflicting request was submitted.") from None


    async def _submit(
        self, actor: User, session_id: str, body: NewMessage, fingerprint: str,
        *, db: AsyncSession | None = None, received_at: str | None = None,
        swarm_generated: bool = False, admitted_at: str | None = None,
        automation_generated: bool = False,
        responsibility_generated: bool = False,
        delegated_authority: DelegatedAuthority | None = None,
        dot_accounts: bool = False,
    ) -> Turn:
        if db is None and not swarm_generated and not automation_generated and delegated_authority is None:
            await self.database.prefetch_session_audiences(actor, [session_id])
        async with self.database._admission_session(db) as transaction:
            if await self.database.dots.worker_session(transaction, session_id):
                raise HTTPException(409, "This is an immutable attention check. Reply in Your Dot.")
            if not responsibility_generated and await self.database.responsibilities.worker_session(transaction, session_id):
                raise HTTPException(409, "This chat is an immutable responsibility review. Open the source chat to follow up.")
            product_grant = await self.database._actor_product_grant(transaction, actor)
            # Account changes and admission must serialize across API/worker
            # processes. An authorization check before this transaction can
            # become stale while a scheduled or interactive request waits.
            authorized = await transaction.scalar(update(User).where(
                User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if authorized is None:
                raise HTTPException(401, "Your access changed. Sign in again.")
            mission = await self.database.swarms.admit(transaction, actor, session_id, body, generated=swarm_generated)
            session = await (self.database.execution_session if swarm_generated or delegated_authority is not None
                             else self.database.allowed_session)(transaction, session_id, actor)
            locked = await transaction.scalar(update(Session).where(
                Session.id == session_id, Session.deleted.is_(False),
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            await transaction.refresh(session)
            await settle_failed_action_attempts(transaction, session_id)
            existing = await transaction.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id,
                Turn.request_key == body.request_key,
            ))
            if existing:
                provenance = await transaction.get(ProductAutomationTurn, existing.id)
                generated_origin = bool(provenance and provenance.origin_turn_id == existing.id)
                if existing.request_fingerprint != fingerprint or generated_origin != automation_generated:
                    raise HTTPException(409, "This request identity already belongs to another message.")
                if self.database.product_authority_enabled:
                    await self.database._check_product_turn(transaction, existing, product_grant)
                return existing
            model = body.model or session.model
            if model not in self.database.settings.models:
                raise HTTPException(422, "Choose an enabled model.")
            context = delegated_authority.context if delegated_authority else await self.database.execution_context(transaction, session)
            if automation_generated:
                context = ExecutionContext(cast(Literal["private", "shared"], session.visibility), "automation")
            if context.surface == "slack" and body.connections and self.database.product_connections is None:
                raise HTTPException(
                    403, "Use a private web conversation for connected accounts. "
                    "Slack account export policies are not configured."
                )
            from .skill_invocations import gateway_connection_ids
            selections: list[str] = list(dict.fromkeys([
                *body.connections, *(gateway_connection_ids(body.prompt) if not responsibility_generated else []),
            ]))
            if dot_accounts and not (
                swarm_generated or automation_generated or responsibility_generated or delegated_authority is not None
            ):
                selections = list(dict.fromkeys([
                    *selections, *await self.database.dots.connected_accounts(transaction, actor.id, session),
                ]))
            if responsibility_generated and selections:
                raise PermissionError("Responsibility reviews cannot select connected accounts.")
            grants = await self.database.resolve_grants(
                transaction, actor.id, selections, shared_session=context.visibility == "shared",
                surface=context.surface,
            )
            if delegated_authority is not None and grants != delegated_authority.grants:
                raise PermissionError("Connection access changed before delegation.")
            selected_ids = [grant.id for grant in grants]
            proposed = RunScope(
                session.id, "", actor.id, model, grants, session.visibility, session.surface
            )
            if session.workspace and session.workspace_scope != proposed.workspace_key:
                raise HTTPException(
                    409, "Select this computer's original account context, "
                    "or start a new conversation for the changed accounts."
                )
            active = await transaction.get(Turn, session.active_turn) if session.active_turn else None
            if body.reply_to is not None and (
                active is None or active.id != body.reply_to or active.status != "waiting"
            ):
                raise HTTPException(409, "That question is no longer waiting. Review the current conversation.")
            if active and active.status == "waiting" and (body.reply_to or body.mode == "asap"):
                from .questions import can_reply_with_text, pending_questions, text_results
                pending = pending_questions(self.database, active)
                eligible = (
                    not swarm_generated and not automation_generated and active.actor_id == actor.id
                    and active.model == model and not body.attachments
                    and set(json.loads(active.connections)) == set(selected_ids)
                    and can_reply_with_text(pending)
                )
                if body.reply_to and not eligible:
                    raise HTTPException(409, "Use the question form with the original model and account context.")
                if eligible and pending is not None:
                    return await self.database._resume(
                        actor, session_id, active.id, body.request_key,
                        text_results(pending, body.prompt), fingerprint,
                        received_at=received_at, admitted_at=admitted_at, transaction=transaction, reply=body,
                    )
            turn_id = new_id()
            turn = Turn(
                id=turn_id, session_id=session_id, actor_id=actor.id,
                request_key=body.request_key, request_fingerprint=fingerprint,
                prompt=self.database.security.seal(body.prompt),
                model=model, mode=body.mode, connections=json.dumps(selected_ids),
                workflow_id=f"moyai-turn-{turn_id}",
            )
            if body.mode == "asap" and session.active_turn and not automation_generated:
                active = await transaction.get(Turn, session.active_turn)
                same_authority = True
                if active and self.database.product_authority_enabled:
                    binding = await transaction.get(ProductTurnAuthority, active.id)
                    same_authority = bool(
                        binding and product_grant and binding.actor_id == product_grant.user_id
                        and binding.authority_version == product_grant.authority_version
                    )
                if (
                    active and active.status == "running" and active.actor_id == actor.id
                    and active.model == model
                    and set(json.loads(active.connections)) == set(selected_ids)
                    and same_authority
                    and (self.database.product_drain is None or await self.database.product_drain.admit(transaction))
                ):
                    turn.parent_turn = active.id
                    turn.status = "steering"
            transaction.add(turn)
            await transaction.flush()
            if automation_generated:
                transaction.add(ProductAutomationTurn(
                    turn_id=turn.id, origin_turn_id=turn.id, visibility=session.visibility,
                ))
            if turn.status == "steering":
                await self.database._record_guidance(transaction, turn, session.revision)
            from .performance import admission
            admission(
                transaction, turn, received_at or admitted_at or turn.created_at,
                source="http" if received_at else "admission", surface=session.surface,
            )
            self.database._bind_product_grant(transaction, turn, product_grant)
            raw = getattr(actor, "slack_web_grant", None)
            if raw is not None:
                from .product_slack_models import SlackWebTurn
                from .slack_web import check_transport_authority
                await self.database._identity().authorize_admission(transaction, raw)
                await check_transport_authority(self.database, transaction, turn.session_id)
                transaction.add(SlackWebTurn(
                    turn_id=turn.id, requester_id=raw.user_id, authority_version=raw.authority_version,
                ))
            await self.database.swarms.bind(transaction, mission, turn, generated=swarm_generated)
            if not automation_generated and not responsibility_generated:
                await self.database.goals.bind(transaction, turn)
            for position, image_id in enumerate(body.attachments):
                # Reserve the upload's destination atomically before publishing
                # it into a conversation, including concurrent first attachment.
                image = await transaction.scalar(update(Upload).where(
                    Upload.id == image_id, Upload.owner_id == actor.id,
                    or_(Upload.session_id.is_(None), Upload.session_id == session_id),
                ).values(session_id=session_id).returning(Upload.id))
                if image is None:
                    raise HTTPException(404, "A file is unavailable in this conversation.")
                transaction.add(TurnImage(turn_id=turn.id, upload_id=image_id, position=position))
            transaction.add(Message(
                session_id=session_id, turn_id=turn_id,
                author_id=None if swarm_generated or automation_generated or responsibility_generated else actor.id,
                role="system" if swarm_generated or automation_generated or responsibility_generated else "user",
                content=self.database.security.seal(body.authored_text),
                identity=f"{'responsibility' if responsibility_generated else 'automation' if automation_generated else 'swarm' if swarm_generated else 'user'}:{turn_id}",
            ))
            from .message_sources import record_source
            if not automation_generated and not responsibility_generated:
                await record_source(self.database, transaction, f"{'swarm' if swarm_generated else 'user'}:{turn_id}", body)
            session.updated_at = now()
            if session.title == "New conversation":
                from .skill_mentions import display_text
                authored = display_text(body.authored_text).strip()
                session.title = authored.splitlines()[0][:100] if authored else "Attachment conversation"
            session.model = model
        return turn


    async def prepare(self, turn_id: str, *, picked_up_at: str | None = None) -> PreparedTurn:
        from .database import grant_fingerprint

        picked_up_at = picked_up_at or now()
        async with self.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            actor = await db.get(User, turn.actor_id)
            if actor is None or not actor.enabled:
                raise PermissionError("The requester no longer has access.")
            product_grant = await self.database._saved_product_grant(db, turn)
            if self.database.product_authority_enabled:
                try:
                    # Source lock precedes native row locks, as in admission.
                    await self.database._identity().authorize_admission(db, product_grant)
                except HTTPException as error:
                    if error.status_code in {401, 403}:
                        raise PermissionError("The task's admitted product authority was revoked.") from error
                    raise
                locked_actor = await db.scalar(update(User).where(
                    User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
                ).values(auth_version=User.auth_version).returning(User.id))
                if locked_actor is None:
                    raise PermissionError("The requester changed before preparation.")
                cast(Any, actor).product_grant = product_grant
            session = await self.database.execution_session(db, turn.session_id, actor)
            member = await self.database.swarms.authorize(db, turn)
            delegation = await db.get(DelegatedSession, session.id)
            generated_delegation = await db.get(DelegatedTurn, turn.id) if delegation else None
            locked = await db.scalar(update(Session).where(
                Session.id == turn.session_id, Session.active_turn == turn.id,
                Session.deleted.is_(False), Session.status.in_(["queued", "running"]),
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise PermissionError("This turn is no longer authorized to execute.")
            await db.refresh(turn)
            await db.refresh(session)
            if session.active_turn != turn.id or turn.status not in ("starting", "running"):
                raise PermissionError("This turn is no longer authorized to execute.")
            context = await self.database.execution_context(db, session, turn_id=turn.id)
            grants = await self.database.resolve_grants(
                db, actor.id, json.loads(turn.connections), shared_session=context.visibility == "shared",
                surface=context.surface,
            )
            scope = RunScope(
                session.id, turn.id, actor.id, turn.model, grants, session.visibility,
                "automation" if context.surface == "automation" else session.surface,
                product_grant=product_grant,
                swarm_id=member.mission_id if member else None,
                swarm_name=member.name if member else None,
                delegation_depth=delegation.depth if delegation else 0,
                delegation_generated=generated_delegation is not None,
                execution_context=context if delegation or context.surface == "automation" else None,
            )
            goal = await self.database.goals.authorize(db, turn)
            if goal is not None:
                scope.goal_generation = goal.generation
            responsibility = await self.database.responsibilities.authorize(db, turn)
            if responsibility is not None:
                scope.responsibility_id = responsibility.responsibility_id
                scope.responsibility_generation = responsibility.generation
            attention = await self.database.dots.authorize(db, turn)
            scope.attention = attention is not None
            if attention is not None:
                from .dot_models import DotDiscoveryTurn
                from .dot_sources import repositories
                discovery = await db.get(DotDiscoveryTurn, turn.id)
                scope.discovery = discovery is not None
                if discovery is not None:
                    scope.discovery_repositories = repositories(
                        grants, json.loads(self.database.security.unseal(discovery.sources)),
                    )
            if scope.delegation_generated and delegation is not None:
                inherited = (await saved_authority(self.database, db, delegation)).grants
                if inherited != grants:
                    raise PermissionError("The delegated task's original account permissions changed.")
                grants = scope.grants = inherited
            if context.surface == "slack" and grants and self.database.product_connections is None:
                raise PermissionError("Connected accounts are not enabled for Slack delivery.")
            if session.workspace and session.workspace_scope != scope.workspace_key:
                raise PermissionError(
                    "This computer belongs to a different account context. "
                    "Start a new conversation for the changed account selection."
                )
            if turn.preparation:
                prepared = TypeAdapter(PreparedTurn).validate_json(self.database.security.unseal(turn.preparation))
                if self.database.product_authority_enabled and prepared.scope.product_grant != product_grant:
                    raise PermissionError("The prepared task's original product authority changed.")
                if [(g.id, g.revision) for g in prepared.scope.grants] != [
                    (g.id, g.revision) for g in grants
                ]:
                    raise PermissionError("Connection access changed during preparation.")
                if delegation or context.surface == "automation":
                    prepared.scope.execution_context = context
                if (
                    prepared.scope.attention != scope.attention or prepared.scope.discovery != scope.discovery
                    or prepared.scope.discovery_repositories != scope.discovery_repositories
                ):
                    raise PermissionError("The prepared attention authority changed.")
                return prepared
            if attention is not None:
                if turn.deferred_results:
                    raise PermissionError("Attention checks cannot resume action approvals.")
                turn.status, session.status = "running", "running"
                turn.accepted_at = turn.accepted_at or now()
                prepared = PreparedTurn(
                    scope=scope, prompt=await self.database.dots.attention_prompt(db, turn),
                    conversation=None, workspace=None,
                )
                turn.preparation = self.database.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
                return prepared
            if responsibility is not None:
                if session.workspace or session.conversation or turn.deferred_results or grants:
                    raise PermissionError("Responsibility reviews require an isolated empty worker session.")
                turn.status, session.status = "running", "running"
                turn.accepted_at = turn.accepted_at or now()
                prepared = PreparedTurn(
                    scope=scope, prompt=self.database.security.unseal(turn.prompt).decode(),
                    conversation=None, workspace=None,
                )
                turn.preparation = self.database.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
                return prepared
            if turn.deferred_results:
                from .delegation import resumed_agents
                scope.background_agents = list(await resumed_agents(self.database, db, scope))
                scope.background_delegation = bool(scope.background_agents)
            conversation = None
            scope.dot_notification_cursor = await self.database.dots.cursor(db, session.id)
            if (
                session.last_actor_id == actor.id and session.last_model == turn.model
                and session.last_grants == grant_fingerprint(grants)
                and session.conversation
            ):
                conversation = self.database.security.unseal(session.conversation).decode()
                if scope.dot_notification_cursor is not None:
                    conversation = await self.database.dots.carry_notifications(
                        db, session.id, conversation, await self.database.dots.checkpoint_cursor(db, session.id),
                    )
            elif session.conversation:
                from .message_sources import public_messages

                public = list(await db.scalars(select(Message).where(
                    Message.session_id == session.id, Message.turn_id != turn.id,
                    or_(Message.created_at < turn.created_at, Message.identity.like("dot:%")),
                ).order_by(Message.id)))
                projected = await public_messages(self.database, db, public)
                history = []
                for message in public:
                    content, _ = projected[message.id]
                    if message.role == "assistant":
                        history.append(ModelResponse(parts=[TextPart(content=content)]))
                    else:
                        history.append(ModelRequest(parts=[UserPromptPart(
                            content=f"Earlier public message by {message.author_id}:\n{content}"
                        )], metadata={"moyai_public_turn": message.turn_id}))
                conversation = ConversationTypeAdapter.dump_json(
                    Conversation(messages=history, conversation_id=scope.conversation_id)
                ).decode()
            elif scope.dot_notification_cursor is not None:
                conversation = await self.database.dots.carry_notifications(db, session.id, None, 0)
            turn.status = "running"
            turn.accepted_at = turn.accepted_at or now()
            session.status = "running"
            prepared = PreparedTurn(
                scope=scope,
                prompt=self.database.security.unseal(turn.prompt).decode(),
                conversation=conversation,
                workspace=json.loads(session.workspace) if session.workspace else None,
                deferred_results=json.loads(self.database.security.unseal(turn.deferred_results))
                if turn.deferred_results else None,
            )
            turn.preparation = self.database.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
            from .performance import evidence
            db.add(Event(**evidence(session.id, turn.id, "worker", picked_up_at)))
            return prepared
