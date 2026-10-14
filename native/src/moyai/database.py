"""Async application persistence; no model/tool loop or competing run recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import and_, delete, event, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .contracts import Grant, GuidanceChanged, NewMessage, NewSession, PreparedTurn, RunScope, SteeringInput
from .models import Base, Connection, LoginAttempt, Message, Session, Turn, User, new_id, now
from .security import Security, password_hash
from .settings import Settings


def grant_fingerprint(grants: list[Grant]) -> str:
    return hashlib.sha256(json.dumps(
        sorted((grant.id, grant.revision) for grant in grants)
    ).encode()).hexdigest()


class Database:
    def __init__(self, settings: Settings, security: Security):
        self.settings = settings
        self.security = security
        self.engine = create_async_engine(settings.sqlalchemy_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        if settings.sqlalchemy_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    async def start(self) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.engine.begin() as conn:
            if self.settings.postgres_url:
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('moyai-native-schema-v1'))"))
            else:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            await conn.run_sync(Base.metadata.create_all)
        password = self.settings.workspace_password.get_secret_value()
        if password:
            async with self.sessions.begin() as db:
                existing = await db.scalar(select(User).where(User.username == self.settings.administrator))
                if existing is None:
                    encoded = await asyncio.to_thread(password_hash, password)
                    insert = postgres_insert if self.settings.postgres_url else sqlite_insert
                    await db.execute(insert(User).values(
                        username=self.settings.administrator,
                        display_name="Administrator",
                        password=encoded,
                        role="admin",
                    ).on_conflict_do_nothing(index_elements=["username"]))

    async def close(self) -> None:
        await self.engine.dispose()

    async def user(self, user_id: str) -> User:
        async with self.sessions() as db:
            user = await db.get(User, user_id)
            if user is None or not user.enabled:
                raise HTTPException(401, "Sign in to continue.")
            return user

    async def limit_login(self, network: str, username: str) -> None:
        window = int(time.time()) // 60
        insert = postgres_insert if self.settings.postgres_url else sqlite_insert
        limited = False
        async with self.sessions.begin() as db:
            await db.execute(delete(LoginAttempt).where(LoginAttempt.window < window - 1))
            for label, limit in (
                (f"login:{network}:{username.casefold()}", 10), ("all-logins", 200),
            ):
                key = f"{window}:{self.security.object_id(label.encode())}"
                value = await db.scalar(insert(LoginAttempt).values(
                    id=key, window=window, count=1,
                ).on_conflict_do_update(
                    index_elements=["id"], set_={"count": LoginAttempt.count + 1},
                    where=LoginAttempt.count < limit,
                ).returning(LoginAttempt.count))
                limited |= value is None
        if limited:
            raise HTTPException(429, "Too many sign-in attempts. Wait one minute.")

    @staticmethod
    async def allowed_session(db: AsyncSession, session_id: str, actor: User) -> Session:
        session = await db.get(Session, session_id)
        if (
            session is None or session.deleted
            or (session.visibility == "private" and session.owner_id != actor.id)
        ):
            raise HTTPException(404, "Conversation not found.")
        return session

    async def create_session(self, actor: User, body: NewSession) -> Session:
        model = body.model or self.settings.default_model
        if model not in self.settings.models:
            raise HTTPException(422, "Choose an enabled model.")
        async with self.sessions.begin() as db:
            session = Session(
                id=new_id(), owner_id=actor.id, title=body.title,
                visibility=body.visibility, model=model,
            )
            db.add(session)
        return session

    async def list_sessions(self, actor: User, *, archived: bool = False) -> list[Session]:
        async with self.sessions() as db:
            query = select(Session).where(
                Session.deleted.is_(False),
                Session.archived.is_(archived),
                or_(Session.owner_id == actor.id, Session.visibility == "shared"),
            ).order_by(Session.updated_at.desc()).limit(100)
            return list((await db.scalars(query)).all())

    async def session(self, actor: User, session_id: str) -> Session:
        async with self.sessions() as db:
            return await self.allowed_session(db, session_id, actor)

    async def messages(self, actor: User, session_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            await self.allowed_session(db, session_id, actor)
            rows = await db.scalars(select(Message).where(
                Message.session_id == session_id, Message.id > after
            ).order_by(Message.id).limit(200))
            return [{
                "id": row.id, "turn_id": row.turn_id, "role": row.role,
                "author_id": row.author_id, "content": self.security.unseal(row.content).decode(),
                "created_at": row.created_at,
            } for row in rows]

    async def submit(self, actor: User, session_id: str, body: NewMessage) -> Turn:
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        try:
            return await self._submit(actor, session_id, body, fingerprint)
        except IntegrityError:
            # Two requests can race the first lookup. The database's unique identity decides.
            async with self.sessions() as db:
                await self.allowed_session(db, session_id, actor)
                existing = await db.scalar(select(Turn).where(
                    Turn.session_id == session_id, Turn.actor_id == actor.id,
                    Turn.request_key == body.request_key,
                ))
                if existing and existing.request_fingerprint == fingerprint:
                    return existing
            raise HTTPException(409, "A conflicting request was submitted.") from None

    async def _submit(self, actor: User, session_id: str, body: NewMessage, fingerprint: str) -> Turn:
        async with self.sessions.begin() as db:
            session = await self.allowed_session(db, session_id, actor)
            existing = await db.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id,
                Turn.request_key == body.request_key,
            ))
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity already belongs to another message.")
                return existing
            model = body.model or session.model
            if model not in self.settings.models:
                raise HTTPException(422, "Choose an enabled model.")
            if session.surface == "slack" and body.connections:
                raise HTTPException(
                    403, "Use a private web conversation for connected accounts. "
                    "Slack account export policies are not configured."
                )
            grants = await self.resolve_grants(
                db, actor.id, body.connections, shared_session=session.visibility == "shared"
            )
            proposed = RunScope(
                session.id, "", actor.id, model, grants, session.visibility, session.surface
            )
            if session.workspace and session.workspace_scope != proposed.workspace_key:
                raise HTTPException(
                    409, "Select this computer's original account context, "
                    "or start a new conversation for the changed accounts."
                )
            turn_id = new_id()
            turn = Turn(
                id=turn_id, session_id=session_id, actor_id=actor.id,
                request_key=body.request_key, request_fingerprint=fingerprint,
                prompt=self.security.seal(body.prompt),
                model=model, mode=body.mode, connections=json.dumps(body.connections),
                workflow_id=f"moyai-turn-{turn_id}",
            )
            if body.mode == "asap" and session.active_turn:
                active = await db.get(Turn, session.active_turn)
                if (
                    active and active.status == "running" and active.actor_id == actor.id
                    and active.model == model
                    and set(json.loads(active.connections)) == set(body.connections)
                ):
                    turn.parent_turn = active.id
                    turn.status = "steering"
            db.add(turn)
            await db.flush()
            db.add(Message(
                session_id=session_id, turn_id=turn_id, author_id=actor.id,
                role="user", content=self.security.seal(body.prompt),
                identity=f"user:{turn_id}",
            ))
            session.updated_at = now()
            if session.title == "New conversation":
                session.title = body.prompt.strip().splitlines()[0][:100]
            session.model = model
        return turn

    async def resolve_grants(
        self, db: AsyncSession, actor_id: str, ids: list[str], *, shared_session: bool = False
    ) -> list[Grant]:
        grants: list[Grant] = []
        for connection_id in dict.fromkeys(ids):
            row = await db.get(Connection, connection_id)
            if row is None or not row.enabled or (not row.shared and row.owner_id != actor_id):
                raise HTTPException(403, "A selected connection is no longer available.")
            if shared_session and not row.shared:
                raise HTTPException(403, "Use a private conversation for a personal connection.")
            grants.append(Grant(
                id=row.id, kind=row.kind, revision=row.revision,
                credential=row.credential, read_only=row.read_only, policy=json.loads(row.policy),
            ))
        return grants

    async def claim_next(self) -> Turn | None:
        """Atomically own a queued turn. Network dispatch happens after this transaction."""
        async with self.sessions.begin() as db:
            candidates = (await db.scalars(select(Turn).join(
                Session, Session.id == Turn.session_id
            ).where(
                Turn.status == "queued", Session.active_turn.is_(None),
                Session.deleted.is_(False),
            ).order_by(Turn.created_at, Turn.id).limit(20))).all()
            for turn in candidates:
                claimed = await db.scalar(update(Session).where(
                    Session.id == turn.session_id, Session.active_turn.is_(None),
                    Session.deleted.is_(False),
                ).values(active_turn=turn.id, status="queued", updated_at=now()).returning(Session.id))
                if claimed is not None:
                    turn.status = "starting"
                    return turn
        return None

    async def starting_turns(self) -> list[Turn]:
        async with self.sessions() as db:
            return list((await db.scalars(
                select(Turn).where(Turn.status == "starting").limit(100)
            )).all())

    async def unsettled_turns(self) -> list[Turn]:
        async with self.sessions() as db:
            return list((await db.scalars(select(Turn).where(
                Turn.status.in_(["starting", "running"])
            ).order_by(Turn.created_at).limit(100))).all())

    async def prepare(self, turn_id: str) -> PreparedTurn:
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            actor = await db.get(User, turn.actor_id)
            if actor is None or not actor.enabled:
                raise PermissionError("The requester no longer has access.")
            session = await self.allowed_session(db, turn.session_id, actor)
            if session.active_turn != turn.id or turn.status not in ("starting", "running"):
                raise PermissionError("This turn is no longer authorized to execute.")
            grants = await self.resolve_grants(
                db, actor.id, json.loads(turn.connections), shared_session=session.visibility == "shared"
            )
            scope = RunScope(
                session.id, turn.id, actor.id, turn.model, grants, session.visibility, session.surface
            )
            if session.surface == "slack" and grants:
                raise PermissionError("Connected accounts are not enabled for Slack delivery.")
            if session.workspace and session.workspace_scope != scope.workspace_key:
                raise PermissionError(
                    "This computer belongs to a different account context. "
                    "Start a new conversation for the changed account selection."
                )
            if turn.preparation:
                prepared = TypeAdapter(PreparedTurn).validate_json(self.security.unseal(turn.preparation))
                if [(g.id, g.revision) for g in prepared.scope.grants] != [
                    (g.id, g.revision) for g in grants
                ]:
                    raise PermissionError("Connection access changed during preparation.")
                return prepared
            conversation = None
            if (
                session.last_actor_id == actor.id and session.last_model == turn.model
                and session.last_grants == grant_fingerprint(grants)
                and session.conversation
            ):
                conversation = self.security.unseal(session.conversation).decode()
            elif session.conversation:
                public = await db.scalars(select(Message).where(
                    Message.session_id == session.id, Message.turn_id != turn.id,
                    Message.created_at < turn.created_at,
                ).order_by(Message.id))
                history = []
                for message in public:
                    content = self.security.unseal(message.content).decode()
                    if message.role == "assistant":
                        history.append(ModelResponse(parts=[TextPart(content=content)]))
                    else:
                        history.append(ModelRequest(parts=[UserPromptPart(
                            content=f"Earlier public message by {message.author_id}:\n{content}"
                        )]))
                conversation = ConversationTypeAdapter.dump_json(
                    Conversation(messages=history, conversation_id=scope.conversation_id)
                ).decode()
            turn.status = "running"
            turn.accepted_at = turn.accepted_at or now()
            session.status = "running"
            prepared = PreparedTurn(
                scope=scope,
                prompt=self.security.unseal(turn.prompt).decode(),
                conversation=conversation,
                workspace=json.loads(session.workspace) if session.workspace else None,
                deferred_results=json.loads(self.security.unseal(turn.deferred_results))
                if turn.deferred_results else None,
            )
            turn.preparation = self.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
            return prepared

    async def authorize_scope(self, scope: RunScope, *, input_id: str | None = None) -> None:
        """Recheck live authority before a dispatched effect or resumed approval."""
        async with self.sessions() as db:
            actor = await db.get(User, scope.actor_id)
            turn = await db.get(Turn, scope.turn_id)
            if actor is None or not actor.enabled or turn is None:
                raise PermissionError("This execution no longer has an authorized requester.")
            session = await self.allowed_session(db, scope.session_id, actor)
            if session.active_turn != turn.id or turn.status != "running" or turn.actor_id != actor.id:
                raise PermissionError("This execution has been stopped or superseded.")
            current = await self.resolve_grants(
                db, actor.id, [grant.id for grant in scope.grants],
                shared_session=session.visibility == "shared",
            )
            if [(g.id, g.revision) for g in current] != [(g.id, g.revision) for g in scope.grants]:
                raise PermissionError("Connection access changed. Start a fresh authorized turn.")
            if input_id is not None:
                pending = await db.scalar(select(Turn).where(
                    Turn.parent_turn == turn.id,
                    Turn.status.in_(["steering", "signal_sent", "accepted"]),
                ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))
                if pending and pending.id != input_id:
                    raise GuidanceChanged("New guidance arrived before this action was dispatched.")

    async def save_workspace_reference(self, scope: RunScope, reference: dict[str, Any]) -> None:
        await self.authorize_scope(scope)
        async with self.sessions.begin() as db:
            await db.execute(update(Session).where(
                Session.id == scope.session_id, Session.active_turn == scope.turn_id,
                Session.status == "running",
            ).values(workspace=json.dumps(reference), workspace_scope=scope.workspace_key))

    async def save_result(
        self, scope: RunScope, *, conversation: str, output: str,
        workspace: dict[str, Any] | None, usage: dict[str, Any],
        deferred: bool = False,
    ) -> bool:
        fingerprint = hashlib.sha256(json.dumps({
            "conversation": conversation, "output": output, "workspace": workspace,
            "usage": usage, "deferred": deferred,
        }, sort_keys=True).encode()).hexdigest()
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, scope.turn_id)
            session = await db.get(Session, scope.session_id)
            if turn is None or session is None:
                raise ValueError("Unknown turn.")
            if turn.result_fingerprint is not None:
                if turn.result_fingerprint != fingerprint:
                    raise ValueError("The completed result for this turn is immutable.")
                return bool(turn.result_accepted)
            # This is the first write in the transaction. Stop fences the same
            # session row first, so whichever transaction wins decides publication.
            accepted = await db.scalar(update(Session).where(
                Session.id == scope.session_id,
                Session.active_turn == turn.id,
                Session.status == "running",
            ).values(
                conversation=self.security.seal(conversation),
                workspace=json.dumps(workspace) if workspace else session.workspace,
                workspace_scope=scope.workspace_key if workspace else session.workspace_scope,
                last_actor_id=scope.actor_id, last_model=scope.model,
                last_grants=grant_fingerprint(scope.grants),
                revision=Session.revision + 1, updated_at=now(),
                status="waiting" if deferred else "saving",
            ).returning(Session.id))
            # Preserve the factual result, even when Stop won the publication race.
            turn.result = self.security.seal(output)
            turn.conversation = self.security.seal(conversation)
            turn.result_fingerprint = fingerprint
            turn.usage = json.dumps(usage)
            turn.finished_at = now()
            if accepted is None:
                turn.result_accepted = False
                return False
            turn.result_accepted = True
            turn.status = "waiting" if deferred else "saving"
            await db.execute(update(Turn).where(
                Turn.parent_turn == turn.id, Turn.status == "accepted"
            ).values(status="included", finished_at=now()))
            if not deferred:
                existing = await db.scalar(select(Message.id).where(Message.identity == f"answer:{turn.id}"))
                if existing is None:
                    db.add(Message(
                        session_id=session.id, turn_id=turn.id, role="assistant",
                        content=self.security.seal(output), identity=f"answer:{turn.id}",
                    ))
            return True

    async def settle(self, turn_id: str, *, snapshot: str | None = None, warning: str | None = None) -> None:
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            session = await db.get(Session, turn.session_id)
            if session is None or session.active_turn != turn.id or turn.status != "saving":
                return
            if snapshot:
                session.snapshot = snapshot
            session.save_warning = warning
            session.status = "idle"
            session.active_turn = None
            turn.status = "completed"

    async def fail(self, turn_id: str, message: str, *, only_unsettled: bool = False) -> None:
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            if turn.status in ("completed", "stopped"):
                return
            if only_unsettled and turn.status not in ("starting", "running"):
                return
            session = await db.get(Session, turn.session_id)
            turn.status = "failed"
            turn.error = message
            turn.finished_at = now()
            if session and session.active_turn == turn.id:
                session.active_turn = None
                session.status = "failed"

    async def stop(self, actor: User, session_id: str) -> str | None:
        async with self.sessions.begin() as db:
            session = await self.allowed_session(db, session_id, actor)
            if actor.id != session.owner_id and actor.role != "admin":
                raise HTTPException(403, "Only the conversation owner or an administrator can stop it.")
            workflow_id = None
            if session.active_turn:
                turn = await db.get(Turn, session.active_turn)
                if turn:
                    workflow_id = turn.workflow_id
            await db.execute(update(Session).where(
                Session.id == session_id
            ).values(status="stopped"))
            await db.execute(update(Turn).where(
                Turn.session_id == session_id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering",
                                 "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))
            # Keep ownership until execution/cleanup acknowledges Stop; don't admit competing work.
            return workflow_id

    async def stopped(self, turn_id: str, conversation: str | None = None) -> None:
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            session = await db.get(Session, turn.session_id)
            if session is None or session.active_turn != turn.id:
                return
            if conversation:
                session.conversation = self.security.seal(conversation)
                session.last_actor_id = turn.actor_id
                session.last_model = turn.model
                if turn.preparation:
                    prepared = TypeAdapter(PreparedTurn).validate_json(
                        self.security.unseal(turn.preparation)
                    )
                    session.last_grants = grant_fingerprint(prepared.scope.grants)
            turn.status = "stopped"
            turn.finished_at = now()
            session.active_turn = None
            session.status = "stopped"

    async def pending_steering(self) -> list[tuple[Turn, Turn]]:
        async with self.sessions() as db:
            cutoff = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
            children = await db.scalars(select(Turn).where(or_(
                Turn.status == "steering",
                and_(Turn.status == "signal_sent", or_(
                    Turn.accepted_at.is_(None), Turn.accepted_at < cutoff,
                )),
            )).limit(100))
            result = []
            for child in children:
                parent = await db.get(Turn, child.parent_turn)
                if parent:
                    result.append((child, parent))
            return result

    async def pending_stops(self) -> list[Turn]:
        async with self.sessions() as db:
            return list((await db.scalars(select(Turn).join(
                Session, Session.active_turn == Turn.id
            ).where(Turn.status == "stopped").limit(100))).all())

    async def signal_attempted(self, turn_id: str) -> None:
        async with self.sessions.begin() as db:
            await db.execute(update(Turn).where(
                Turn.id == turn_id, Turn.status.in_(["steering", "signal_sent"])
            ).values(status="signal_sent", accepted_at=now()))

    async def steering_uncertain(self, turn_id: str) -> None:
        async with self.sessions.begin() as db:
            await db.execute(update(Turn).where(
                Turn.id == turn_id, Turn.status == "signal_sent"
            ).values(
                status="unconfirmed",
                error="Delivery of this correction was not confirmed before the task ended. "
                      "Inspect its saved activity before submitting the work again.",
            ))

    async def prepare_steering(self, parent_id: str, turn_id: str) -> SteeringInput:
        async with self.sessions() as db:
            turn = await db.get(Turn, turn_id)
            parent = await db.get(Turn, parent_id)
            if (
                turn is None or parent is None or turn.parent_turn != parent.id
                or turn.actor_id != parent.actor_id or turn.model != parent.model
                or turn.status not in ("steering", "signal_sent", "accepted")
            ):
                raise PermissionError("The correction is no longer eligible for this run.")
            return SteeringInput(turn.id, self.security.unseal(turn.prompt).decode(), "asap")

    async def steering_receipt(self, turn_id: str, enqueue_id: str | None) -> None:
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.status not in ("steering", "signal_sent", "accepted"):
                return
            if enqueue_id is None:
                turn.status = "queued"
                turn.parent_turn = None
                turn.accepted_at = None
            else:
                turn.status = "accepted"
                turn.enqueue_id = enqueue_id
                turn.accepted_at = now()

    async def resume(
        self, actor: User, session_id: str, previous_id: str,
        request_key: str, results: dict[str, Any],
    ) -> Turn:
        fingerprint = hashlib.sha256(json.dumps(
            {"previous": previous_id, "results": results}, sort_keys=True
        ).encode()).hexdigest()
        try:
            return await self._resume(actor, session_id, previous_id, request_key, results, fingerprint)
        except (IntegrityError, HTTPException) as error:
            if isinstance(error, HTTPException) and error.status_code != 409:
                raise
            async with self.sessions() as db:
                await self.allowed_session(db, session_id, actor)
                existing = await db.scalar(select(Turn).where(
                    Turn.session_id == session_id, Turn.actor_id == actor.id, Turn.request_key == request_key
                ))
                if existing and existing.request_fingerprint == fingerprint:
                    return existing
            raise HTTPException(409, "This answer conflicts with an already admitted request.") from error

    async def _resume(
        self, actor: User, session_id: str, previous_id: str,
        request_key: str, results: dict[str, Any], fingerprint: str,
    ) -> Turn:
        async with self.sessions.begin() as db:
            session = await self.allowed_session(db, session_id, actor)
            existing = await db.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id, Turn.request_key == request_key
            ))
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity belongs to a different answer.")
                return existing
            previous = await db.get(Turn, previous_id)
            if (
                previous is None or previous.actor_id != actor.id or previous.session_id != session.id
                or previous.status != "waiting" or session.active_turn != previous.id
                or previous.preparation is None
            ):
                raise HTTPException(409, "The pending request changed.")
            current_grants = await self.resolve_grants(
                db, actor.id, json.loads(previous.connections),
                shared_session=session.visibility == "shared",
            )
            original = TypeAdapter(PreparedTurn).validate_json(self.security.unseal(previous.preparation))
            if [(g.id, g.revision) for g in current_grants] != [
                (g.id, g.revision) for g in original.scope.grants
            ]:
                raise HTTPException(403, "Access changed while waiting. This approval cannot restore it.")
            turn_id = new_id()
            turn = Turn(
                id=turn_id, session_id=session.id, actor_id=actor.id,
                request_key=request_key, request_fingerprint=fingerprint,
                prompt=self.security.seal(""), model=previous.model,
                workflow_id=f"moyai-turn-{turn_id}", connections=previous.connections,
                deferred_results=self.security.seal(results),
            )
            claimed = await db.scalar(update(Session).where(
                Session.id == session.id, Session.active_turn == previous.id, Session.status == "waiting",
            ).values(active_turn=None, status="queued", updated_at=now()).returning(Session.id))
            if claimed is None:
                raise HTTPException(409, "Another response already resumed this request.")
            db.add(turn)
            previous.status = "resumed"
            return turn
