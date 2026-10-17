"""Async application persistence; no model/tool loop or competing run recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import and_, delete, event, func, or_, select, text, update
from sqlalchemy import insert as sql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .answer_settlement import AnswerSettlement
from .contracts import (
    ExecutionContext,
    Grant,
    GuidanceChanged,
    InputImage,
    NewMessage,
    NewSession,
    PreparedTurn,
    RunScope,
    SteeringInput,
)
from .delegation_authority import DelegatedAuthority, saved_authority, session_context
from .delegation_models import DelegatedSession, DelegatedTurn
from .dispatch_wakeups import notify_dispatch
from .models import (
    Base,
    ComputerOperation,
    Connection,
    Event,
    LoginAttempt,
    Message,
    ProductTurnAuthority,
    Session,
    SessionFolder,
    SessionPreference,
    Turn,
    TurnImage,
    Upload,
    User,
    new_id,
    now,
)
from .postgres import create_engine
from .product_identity import ProductGrant, ProductIdentityAdapter
from .security import Security, password_hash
from .settings import Settings
from .swarm_models import SwarmMember
from .swarm_service import SwarmService, dispatchable

if TYPE_CHECKING:
    from .product_connections import ProductConnections
    from .product_drain import ProductDrainService
    from .product_inference import ProductInferenceService
    from .product_memory_review import ProductMemoryReviewService


def grant_fingerprint(grants: list[Grant]) -> str:
    return hashlib.sha256(json.dumps(
        sorted((grant.id, grant.revision) for grant in grants)
    ).encode()).hexdigest()


class Database:
    def __init__(self, settings: Settings, security: Security):
        self.settings = settings
        self.security = security
        self.performance_tasks: set[asyncio.Task[None]] = set()
        from .performance import DispatchRecorder
        self.dispatch_timings = DispatchRecorder(self)
        self.product_identity: ProductIdentityAdapter | None = None
        self.product_connections: ProductConnections | None = None
        self.product_drain: ProductDrainService | None = None
        self.product_inference: ProductInferenceService | None = None
        self.product_memory_review: ProductMemoryReviewService | None = None
        self.product_mode = getattr(settings, "product_mode", "standalone")
        self.execution_schema = getattr(settings, "database_schema", None)
        self.native_schema_mode = getattr(settings, "native_schema_mode", "auto")
        if self.execution_schema and not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.execution_schema):
            raise ValueError("Invalid native database schema.")
        if self.native_schema_mode not in {"auto", "bootstrap", "verify"}:
            raise ValueError("Unsupported native schema mode.")
        if self.product_mode == "existing" and (
            not settings.postgres_url or not self.execution_schema or self.native_schema_mode != "verify"
        ):
            raise ValueError("Existing-product mode requires PostgreSQL, an explicit native schema and verify mode.")
        connect_args = {}
        if self.execution_schema:
            if not settings.postgres_url:
                raise ValueError("Native database schemas require PostgreSQL.")
            connect_args = {"server_settings": {"search_path": f'"{self.execution_schema}",pg_catalog'}}
        self.engine = create_engine(
            settings.sqlalchemy_url, pool_pre_ping=True, connect_args=connect_args,
            **({"pool_size": settings.database_pool_size, "max_overflow": 0, "pool_timeout": 10}
               if settings.postgres_url else {}),
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.answers = AnswerSettlement(self)
        self.swarms = SwarmService(self)
        if settings.sqlalchemy_url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

    async def start(self) -> None:
        if self.product_authority_enabled and (
            not self.settings.postgres_url or not self.execution_schema or self.native_schema_mode != "verify"
        ):
            raise RuntimeError("Product authority requires explicit PostgreSQL schema verification, not bootstrap.")
        if self.native_schema_mode == "verify" or self.product_mode == "existing":
            from .product_schema import verify_product_schema
            if self.product_mode == "existing" and self.product_identity is None:
                raise RuntimeError("Configure canonical product identity before starting the native database.")
            await verify_product_schema(self.engine, schema=self.execution_schema or "")
            return  # No create_all, local state directory, folder backfill or admin bootstrap.
        self.settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.engine.begin() as conn:
            if self.settings.postgres_url:
                if self.execution_schema and not await conn.scalar(text(
                    "SELECT 1 FROM pg_namespace WHERE nspname=:schema"
                ), {"schema": self.execution_schema}):
                    raise RuntimeError("The explicit native schema is missing; provision it first.")
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('moyai-native-schema-v1'))"))
            else:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            await conn.run_sync(Base.metadata.create_all)
            # Adopt folder names saved by the original native API. This is
            # additive and runs under the schema lock on both supported DBs.
            existing = select(SessionFolder.id).where(
                SessionFolder.user_id == SessionPreference.user_id,
                SessionFolder.name == SessionPreference.folder,
            ).exists()
            legacy = (await conn.execute(select(
                SessionPreference.user_id, SessionPreference.folder,
            ).where(SessionPreference.folder != "", ~existing).distinct())).all()
            for user_id, name in legacy:
                await conn.execute(sql_insert(SessionFolder).values(id=new_id(), user_id=user_id, name=name))
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
        if self.performance_tasks:
            await asyncio.gather(*self.performance_tasks, return_exceptions=True)
        await self.engine.dispose()

    @property
    def product_authority_enabled(self) -> bool:
        return self.product_mode == "existing" or self.product_identity is not None

    def _identity(self) -> ProductIdentityAdapter:
        if self.product_identity is None:
            raise HTTPException(503, "Canonical product authority is not configured.")
        return self.product_identity

    async def _actor_product_grant(self, db: AsyncSession, actor: User) -> ProductGrant | None:
        if not self.product_authority_enabled:
            return None
        grant = getattr(actor, "product_grant", None)
        if not isinstance(grant, ProductGrant) or grant.user_id != actor.id:
            raise HTTPException(401, "Fresh product authorization is required for this request.")
        await self._identity().authorize_admission(db, grant)
        return grant

    async def _saved_product_grant(self, db: AsyncSession, turn: Turn) -> ProductGrant | None:
        if not self.product_authority_enabled:
            return None
        binding = await db.get(ProductTurnAuthority, turn.id)
        if binding is None or binding.actor_id != turn.actor_id:
            raise PermissionError("This task has no admitted product authority; submit a new authorized request.")
        return ProductGrant(binding.actor_id, binding.authority_version)

    async def _check_product_turn(
        self, db: AsyncSession, turn: Turn, expected: ProductGrant | None,
    ) -> ProductGrant | None:
        try:
            grant = await self._saved_product_grant(db, turn)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        if self.product_authority_enabled:
            if expected != grant:
                raise HTTPException(403, "The task's original product authority cannot be replaced.")
            # One native connection/transaction avoids recursive pool checkouts.
            await self._identity().authorize_admission(db, grant)
        return grant

    @staticmethod
    def _bind_product_grant(db: AsyncSession, turn: Turn, grant: ProductGrant | None) -> None:
        if grant is not None:
            if grant.user_id != turn.actor_id:
                raise PermissionError("Product authority belongs to a different requester.")
            db.add(ProductTurnAuthority(
                turn_id=turn.id, actor_id=grant.user_id, authority_version=grant.authority_version,
            ))

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
        member = await db.get(SwarmMember, session_id)
        if member is not None and member.mission_id != session_id:
            root = await db.get(Session, member.mission_id)
            if root is None or root.deleted or (root.visibility == "private" and root.owner_id != actor.id):
                raise HTTPException(404, "Conversation not found.")
        return session

    @asynccontextmanager
    async def _admission_session(self, db: AsyncSession | None) -> AsyncIterator[AsyncSession]:
        if db is not None:
            if not db.in_transaction() or db.get_bind().engine is not self.engine.sync_engine:
                raise ValueError("Admission requires a transaction on this native database.")
            yield db  # The caller owns the entire receipt/admission transaction.
            await notify_dispatch(db, self.settings)
        else:
            async with self.sessions.begin() as transaction:
                yield transaction
                await notify_dispatch(transaction, self.settings)

    async def create_session(
        self, actor: User, body: NewSession, *, db: AsyncSession | None = None,
    ) -> Session:
        model = body.model or self.settings.default_model
        if model not in self.settings.models:
            raise HTTPException(422, "Choose an enabled model.")
        async with self._admission_session(db) as transaction:
            grant = await self._actor_product_grant(transaction, actor)
            if grant is not None:
                principal = await self._identity().authorize_admission(transaction, grant)
                if body.visibility == "private" and not principal.individual:
                    raise HTTPException(403, "Private sessions require individual Google or Cloudflare sign-in.")
            session = Session(
                id=new_id(), owner_id=actor.id, title=body.title,
                visibility=body.visibility, model=model,
            )
            transaction.add(session)
        return session

    async def list_sessions(self, actor: User, *, archived: bool = False, deleted: bool = False) -> list[Session]:
        async with self.sessions() as db:
            query = select(Session).where(
                Session.deleted.is_(deleted),
                Session.owner_id == actor.id if deleted else
                or_(Session.owner_id == actor.id, Session.visibility == "shared"),
            ).order_by(Session.updated_at.desc()).limit(100)
            if not deleted:
                query = query.where(
                    Session.archived.is_(archived),
                    ~select(SwarmMember.session_id).where(
                        SwarmMember.session_id == Session.id, SwarmMember.position > 0,
                    ).exists(),
                )
            return list((await db.scalars(query)).all())

    async def session(self, actor: User, session_id: str) -> Session:
        async with self.sessions() as db:
            return await self.allowed_session(db, session_id, actor)

    async def messages(self, actor: User, session_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            await self.allowed_session(db, session_id, actor)
            rows = list((await db.scalars(select(Message).where(
                Message.session_id == session_id, Message.id > after
            ).order_by(Message.id).limit(200))).all())
            pictures: dict[str, list[dict]] = {}
            if rows:
                for link, upload in (await db.execute(select(TurnImage, Upload).join(
                    Upload, Upload.id == TurnImage.upload_id
                ).where(TurnImage.turn_id.in_([row.turn_id for row in rows])).order_by(TurnImage.position))).all():
                    pictures.setdefault(link.turn_id, []).append({
                        "id": upload.id, "name": upload.name, "media_type": upload.media_type, "size": upload.size,
                    })
            return [{
                "id": row.id, "turn_id": row.turn_id, "role": row.role,
                "author_id": row.author_id, "content": self.security.unseal(row.content).decode(),
                "created_at": row.created_at,
                **({"source": "delegation"} if row.identity.startswith("delegation:") else {}),
                "attachments": pictures.get(row.turn_id, [])
                if row.role == "user" or row.identity.startswith("delegation:") else [],
            } for row in rows]

    async def submit(
        self, actor: User, session_id: str, body: NewMessage, *, db: AsyncSession | None = None,
        swarm_generated: bool = False,
        delegated_authority: DelegatedAuthority | None = None,
        received_at: str | None = None,
    ) -> Turn:
        admitted_at = now()
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        if db is not None:
            # Do not recover a conflicting receipt in an independent transaction.
            # The caller must roll back/retry its complete occurrence admission.
            return await self._submit(
                actor, session_id, body, fingerprint, db=db, swarm_generated=swarm_generated,
                delegated_authority=delegated_authority,
                received_at=received_at, admitted_at=admitted_at,
            )
        if swarm_generated or delegated_authority is not None:
            raise ValueError("Generated input requires its admission transaction.")
        try:
            return await self._submit(
                actor, session_id, body, fingerprint, received_at=received_at, admitted_at=admitted_at,
            )
        except IntegrityError:
            # Two requests can race the first lookup. The database's unique identity decides.
            async with self.sessions() as lookup:
                await self.allowed_session(lookup, session_id, actor)
                existing = await lookup.scalar(select(Turn).where(
                    Turn.session_id == session_id, Turn.actor_id == actor.id,
                    Turn.request_key == body.request_key,
                ))
                if existing and existing.request_fingerprint == fingerprint:
                    return existing
            raise HTTPException(409, "A conflicting request was submitted.") from None

    async def _submit(
        self, actor: User, session_id: str, body: NewMessage, fingerprint: str,
        *, db: AsyncSession | None = None, received_at: str | None = None,
        swarm_generated: bool = False, admitted_at: str | None = None,
        delegated_authority: DelegatedAuthority | None = None,
    ) -> Turn:
        async with self._admission_session(db) as transaction:
            product_grant = await self._actor_product_grant(transaction, actor)
            # Account changes and admission must serialize across API/worker
            # processes. An authorization check before this transaction can
            # become stale while a scheduled or interactive request waits.
            authorized = await transaction.scalar(update(User).where(
                User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if authorized is None:
                raise HTTPException(401, "Your access changed. Sign in again.")
            mission = await self.swarms.admit(transaction, actor, session_id, body, generated=swarm_generated)
            session = await self.allowed_session(transaction, session_id, actor)
            locked = await transaction.scalar(update(Session).where(
                Session.id == session_id, Session.deleted.is_(False),
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            await transaction.refresh(session)
            existing = await transaction.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id,
                Turn.request_key == body.request_key,
            ))
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity already belongs to another message.")
                if self.product_authority_enabled:
                    await self._check_product_turn(transaction, existing, product_grant)
                return existing
            model = body.model or session.model
            if model not in self.settings.models:
                raise HTTPException(422, "Choose an enabled model.")
            context = delegated_authority.context if delegated_authority else await self.execution_context(transaction, session)
            if context.surface == "slack" and body.connections and self.product_connections is None:
                raise HTTPException(
                    403, "Use a private web conversation for connected accounts. "
                    "Slack account export policies are not configured."
                )
            grants = await self.resolve_grants(
                transaction, actor.id, body.connections, shared_session=context.visibility == "shared",
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
            turn_id = new_id()
            turn = Turn(
                id=turn_id, session_id=session_id, actor_id=actor.id,
                request_key=body.request_key, request_fingerprint=fingerprint,
                prompt=self.security.seal(body.prompt),
                model=model, mode=body.mode, connections=json.dumps(selected_ids),
                workflow_id=f"moyai-turn-{turn_id}",
            )
            if body.mode == "asap" and session.active_turn:
                active = await transaction.get(Turn, session.active_turn)
                same_authority = True
                if active and self.product_authority_enabled:
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
                    and (self.product_drain is None or await self.product_drain.admit(transaction))
                ):
                    turn.parent_turn = active.id
                    turn.status = "steering"
            transaction.add(turn)
            await transaction.flush()
            if turn.status == "steering":
                self._record_guidance(transaction, turn, session.revision)
            from .performance import admission
            admission(
                transaction, turn, received_at or admitted_at or turn.created_at,
                source="http" if received_at else "admission", surface=session.surface,
            )
            self._bind_product_grant(transaction, turn, product_grant)
            await self.swarms.bind(transaction, mission, turn, generated=swarm_generated)
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
                session_id=session_id, turn_id=turn_id, author_id=None if swarm_generated else actor.id,
                role="system" if swarm_generated else "user", content=self.security.seal(body.prompt),
                identity=f"{'swarm' if swarm_generated else 'user'}:{turn_id}",
            ))
            session.updated_at = now()
            if session.title == "New conversation":
                from .skill_mentions import display_text
                session.title = display_text(body.prompt).strip().splitlines()[0][:100] if body.prompt.strip() else "Image conversation"
            session.model = model
        return turn

    async def admit_automation(
        self, db: AsyncSession, *, grant: ProductGrant, title: str, prompt: str,
        model: str, request_key: str, connections: list[str] | None = None,
    ) -> tuple[Session, Turn]:
        """Admit using the original owner grant in the caller's occurrence transaction.

        The service first fences the canonical source with its exclusive writer
        lock and checks source revision, generation and unique occurrence receipt.
        Enable/migrate must already have projected the native owner. This method
        neither reads a fresh grant nor commits or dispatches the new turn.
        """
        async with self._admission_session(db):
            if not self.product_authority_enabled:
                raise HTTPException(503, "Product automation authority is not configured.")
            identity = self._identity()
            if db.get_bind().dialect.name != "postgresql":
                raise HTTPException(503, "Product automation admission requires PostgreSQL.")
            fenced = await db.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory' "
                "AND pid=pg_backend_pid() AND classid=726941 "
                "AND objid=CAST(CAST(:schema AS regnamespace) AS oid) AND objsubid=2 "
                "AND mode='ExclusiveLock' AND granted)"
            ), {"schema": identity.schema})
            if not fenced:
                raise ValueError("Automation admission requires the canonical product writer lock first.")
            await identity.authorize_admission(db, grant)
            actor = await db.get(User, grant.user_id)
            if actor is None:
                raise HTTPException(401, "The automation owner must be enrolled before enabling it.")
            cast(Any, actor).product_grant = grant
            session = await self.create_session(actor, NewSession(
                title=title, model=model, visibility="shared",
            ), db=db)
            session.surface = "automation"
            turn = await self.submit(actor, session.id, NewMessage(
                prompt=prompt, model=model, request_key=request_key, connections=connections or [],
            ), db=db)
            await db.flush()
            return session, turn

    async def resolve_grants(
        self, db: AsyncSession, actor_id: str, ids: list[str], *, shared_session: bool = False,
        surface: str = "web",
    ) -> list[Grant]:
        if self.product_connections is not None:
            return await self.product_connections.resolve_grants(
                actor_id, ids, shared_session=shared_session, surface=surface, db=db,
            )
        if self.product_authority_enabled and ids:
            raise HTTPException(503, "Canonical product connections are not configured.")
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

    async def execution_context(self, db: AsyncSession, session: Session) -> ExecutionContext:
        return await session_context(self, db, session)

    async def scope_grants(self, db: AsyncSession, scope: RunScope, session: Session) -> list[Grant]:
        """Resolve under persisted execution policy, never private-child presentation."""
        context = await self.execution_context(db, session)
        if scope.execution_context is not None and scope.execution_context != context:
            raise PermissionError("This execution's original policy context changed.")
        if context != ExecutionContext(session.visibility, session.surface):
            # Older activity payloads lack this field. Recover their immutable
            # context before capability catalogs or credentials release content.
            scope.execution_context = context
        return await self.resolve_grants(
            db, scope.actor_id, [grant.id for grant in scope.grants],
            shared_session=context.visibility == "shared", surface=context.surface,
        )

    async def claim_next(self) -> Turn | None:
        claimed = await self.claim_batch(1)
        return claimed[0] if claimed else None

    async def claim_batch(self, limit: int) -> list[Turn]:
        """Reserve at most one turn per session; RPCs happen after this commit."""
        if limit < 1:
            raise ValueError("Dispatch batch size must be positive.")
        claimed_turns: list[Turn] = []
        claimed_sessions: set[str] = set()
        async with self.sessions.begin() as db:
            if self.product_drain is not None and not await self.product_drain.admit(db):
                return []
            while len(claimed_turns) < limit:
                before = len(claimed_turns)
                candidates = (await db.scalars(select(Turn).join(
                    Session, Session.id == Turn.session_id
                ).where(
                    Turn.status == "queued", Session.active_turn.is_(None),
                    Session.deleted.is_(False),
                    dispatchable(),
                ).order_by(Turn.created_at, Turn.id).limit(limit - len(claimed_turns))
                  .with_for_update(of=Session, skip_locked=True))).all()
                if not candidates:
                    break
                for turn in candidates:
                    if turn.session_id in claimed_sessions:
                        continue
                    claimed = await db.scalar(update(Session).where(
                        Session.id == turn.session_id, Session.active_turn.is_(None),
                        Session.deleted.is_(False),
                    ).values(revision=Session.revision + 1).returning(Session.id))
                    if claimed is None:
                        continue
                    admitted = await db.scalar(update(Turn).where(
                        Turn.id == turn.id, Turn.status == "queued",
                    ).values(status="starting").returning(Turn.id))
                    if admitted is not None:
                        await db.execute(update(Session).where(Session.id == turn.session_id).values(
                            active_turn=turn.id, status="queued", updated_at=now(),
                        ))
                        turn.status = "starting"
                        claimed_turns.append(turn)
                        claimed_sessions.add(turn.session_id)
                if len(claimed_turns) == before:
                    break
        return claimed_turns

    async def steer_queued(self, actor: User, turn_id: str) -> Turn:
        """Promote saved input into the SDK's existing durable enqueue path."""
        async with self._admission_session(None) as db:
            actor_grant = await self._actor_product_grant(db, actor)
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.actor_id != actor.id:
                raise HTTPException(404, "Message not found.")
            await self._check_product_turn(db, turn, actor_grant)
            await db.execute(update(Session).where(
                Session.id == turn.session_id,
            ).values(revision=Session.revision + 1))
            session = await self.allowed_session(db, turn.session_id, actor)
            await self.swarms.authorize(db, turn)
            await db.refresh(turn)
            if turn.status in {"steering", "signal_sent", "accepted"}:
                return turn
            if turn.status != "queued":
                raise HTTPException(409, "This message has already started.")
            turn.mode = "asap"
            if self.product_drain is not None and not await self.product_drain.admit(db):
                return turn
            active = await db.get(Turn, session.active_turn) if session.active_turn else None
            if active:
                await self._check_product_turn(db, active, actor_grant)
                if (
                    active.status != "running" or active.actor_id != actor.id
                    or active.model != turn.model
                    or set(json.loads(active.connections)) != set(json.loads(turn.connections))
                ):
                    raise HTTPException(409, "This message needs its own run. It will start after the active task.")
                turn.parent_turn = active.id
                turn.status = "steering"
                self._record_guidance(db, turn, session.revision)
            return turn

    def _record_guidance(self, db: AsyncSession, turn: Turn, revision: int) -> None:
        # Event IDs order admissions while holding the session fence, including
        # "Send now" for an older queued message. Message creation time cannot
        # order those corrections. A repeated promotion can have a new receipt.
        db.add(Event(
            session_id=turn.session_id, turn_id=turn.id, kind="steering_admitted",
            identity=f"guidance:{turn.id}:{revision}",
            content=self.security.seal({"parent_turn": turn.parent_turn}),
        ))

    async def dispatch_boundary(self) -> tuple[str, str] | None:
        async with self.sessions() as db:
            row = (await db.execute(select(Turn.created_at, Turn.id).where(
                Turn.status.in_(["queued", "starting"]),
            ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1))).first()
            return (row[0], row[1]) if row is not None else None

    async def starting_turns(
        self, *, limit: int = 100, after: tuple[str, str] | None = None,
        through: tuple[str, str] | None = None,
    ) -> list[Turn]:
        async with self.sessions() as db:
            query = select(Turn).where(Turn.status == "starting")
            if after is not None:
                stamp, identity = after
                query = query.where(or_(
                    Turn.created_at > stamp, and_(Turn.created_at == stamp, Turn.id > identity),
                ))
            if through is not None:
                stamp, identity = through
                query = query.where(or_(
                    Turn.created_at < stamp, and_(Turn.created_at == stamp, Turn.id <= identity),
                ))
            return list((await db.scalars(query.order_by(Turn.created_at, Turn.id).limit(limit))).all())

    async def unsettled_turns(self) -> list[Turn]:
        async with self.sessions() as db:
            return list((await db.scalars(select(Turn).where(
                Turn.status.in_(["starting", "running", "saving"])
            ).order_by(Turn.created_at).limit(100))).all())

    async def prepare(self, turn_id: str, *, picked_up_at: str | None = None) -> PreparedTurn:
        picked_up_at = picked_up_at or now()
        async with self.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            actor = await db.get(User, turn.actor_id)
            if actor is None or not actor.enabled:
                raise PermissionError("The requester no longer has access.")
            product_grant = await self._saved_product_grant(db, turn)
            if self.product_authority_enabled:
                try:
                    # Source lock precedes native row locks, as in admission.
                    await self._identity().authorize_admission(db, product_grant)
                except HTTPException as error:
                    if error.status_code in {401, 403}:
                        raise PermissionError("The task's admitted product authority was revoked.") from error
                    raise
                locked_actor = await db.scalar(update(User).where(
                    User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
                ).values(auth_version=User.auth_version).returning(User.id))
                if locked_actor is None:
                    raise PermissionError("The requester changed before preparation.")
            session = await self.allowed_session(db, turn.session_id, actor)
            member = await self.swarms.authorize(db, turn)
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
            context = await self.execution_context(db, session)
            grants = await self.resolve_grants(
                db, actor.id, json.loads(turn.connections), shared_session=context.visibility == "shared",
                surface=context.surface,
            )
            scope = RunScope(
                session.id, turn.id, actor.id, turn.model, grants, session.visibility, session.surface,
                product_grant=product_grant,
                swarm_id=member.mission_id if member else None,
                swarm_name=member.name if member else None,
                delegation_depth=delegation.depth if delegation else 0,
                delegation_generated=generated_delegation is not None,
                execution_context=context if delegation else None,
            )
            if scope.delegation_generated and delegation is not None:
                inherited = (await saved_authority(self, db, delegation)).grants
                if inherited != grants:
                    raise PermissionError("The delegated task's original account permissions changed.")
                grants = scope.grants = inherited
            if context.surface == "slack" and grants and self.product_connections is None:
                raise PermissionError("Connected accounts are not enabled for Slack delivery.")
            if session.workspace and session.workspace_scope != scope.workspace_key:
                raise PermissionError(
                    "This computer belongs to a different account context. "
                    "Start a new conversation for the changed account selection."
                )
            if turn.preparation:
                prepared = TypeAdapter(PreparedTurn).validate_json(self.security.unseal(turn.preparation))
                if self.product_authority_enabled and prepared.scope.product_grant != product_grant:
                    raise PermissionError("The prepared task's original product authority changed.")
                if [(g.id, g.revision) for g in prepared.scope.grants] != [
                    (g.id, g.revision) for g in grants
                ]:
                    raise PermissionError("Connection access changed during preparation.")
                if delegation:
                    prepared.scope.execution_context = context
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
                        )], metadata={"moyai_public_turn": message.turn_id}))
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
            from .performance import evidence
            db.add(Event(**evidence(session.id, turn.id, "worker", picked_up_at)))
            return prepared

    async def authorize_scope(
        self, scope: RunScope, *, input_id: str | None = None, db: AsyncSession | None = None,
    ) -> None:
        """Recheck live authority before a dispatched effect or resumed approval."""
        if db is None:
            async with self.sessions() as transaction:
                await self.authorize_scope(scope, input_id=input_id, db=transaction)
        else:
            actor = await db.get(User, scope.actor_id)
            turn = await db.get(Turn, scope.turn_id)
            if actor is None or not actor.enabled or turn is None:
                raise PermissionError("This execution no longer has an authorized requester.")
            await self._check_product_turn(db, turn, scope.product_grant)
            await self.swarms.authorize(db, turn, scope=scope)
            session = await self.allowed_session(db, scope.session_id, actor)
            if (
                session.active_turn != turn.id or session.status != "running" or turn.status != "running"
                or turn.actor_id != actor.id or turn.session_id != session.id
            ):
                raise PermissionError("This execution has been stopped or superseded.")
            current = await self.scope_grants(db, scope, session)
            if [(g.id, g.revision) for g in current] != [(g.id, g.revision) for g in scope.grants]:
                raise PermissionError("Connection access changed. Start a fresh authorized turn.")
            if input_id is not None:
                admission_order = select(func.max(Event.id)).where(
                    Event.turn_id == Turn.id, Event.kind == "steering_admitted",
                ).scalar_subquery()
                pending = await db.scalar(select(Turn).where(
                    Turn.parent_turn == turn.id,
                    Turn.status.in_(["steering", "signal_sent", "accepted"]),
                ).order_by(
                    admission_order.desc().nulls_last(), Turn.created_at.desc(), Turn.id.desc(),
                ).limit(1))
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
        checkpoint_workspace: bool = False,
    ) -> bool:
        return await self.answers.save_result(
            scope, conversation=conversation, output=output, workspace=workspace,
            usage=usage, deferred=deferred, checkpoint_workspace=checkpoint_workspace,
        )

    async def input_images(self, turn_id: str, *, session_id: str | None = None) -> list[InputImage]:
        async with self.sessions() as db:
            query = select(Upload).join(TurnImage, TurnImage.upload_id == Upload.id).where(
                TurnImage.turn_id == turn_id
            ).order_by(TurnImage.position)
            if session_id is not None:
                query = query.where(Upload.session_id == session_id)
            return [
                InputImage(row.digest, row.media_type, row.name, upload_id=row.id, size=row.size)
                for row in (await db.scalars(query)).all()
            ]

    async def settle(self, turn_id: str, *, snapshot: str | None = None, warning: str | None = None) -> None:
        await self.answers.settle(turn_id, snapshot=snapshot, warning=warning)

    async def fail(self, turn_id: str, message: str, *, only_unsettled: bool = False) -> None:
        await self.answers.fail(turn_id, message, only_unsettled=only_unsettled)

    async def stop(self, actor: User, session_id: str) -> str | None:
        if await self.swarms.stop_session(actor, session_id):
            return None  # Durable native Stop dispatch reconciles every member.
        async with self._admission_session(None) as db:
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

    async def delete_session(self, actor: User, session_id: str) -> None:
        """Hide a session and fence work atomically; the existing Stop dispatcher cancels execution."""
        async with self._admission_session(None) as db:
            member = await self.swarms.member(db, session_id)
            # Keep the mission-before-session lock order and commit the deletion
            # together with its team-wide attention fence.
            mission = await self.swarms.lock(db, member.mission_id, actor) if member else None
            locked = await db.scalar(update(Session).where(
                Session.id == session_id, Session.owner_id == actor.id,
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            session = await db.get(Session, session_id)
            assert session is not None
            if session.deleted:
                return
            if mission is not None:
                await self.swarms.stop_in(
                    db, mission, "attention", "A team conversation was deleted. Restore it before resuming.",
                )
            session.deleted = True
            session.updated_at = now()
            # An answer already being saved retains its confirmed outcome.
            if session.status != "saving":
                session.status = "stopped"
            await db.execute(update(Turn).where(
                Turn.session_id == session_id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering", "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))

    async def restore_session(self, actor: User, session_id: str) -> None:
        async with self.sessions.begin() as db:
            locked = await db.scalar(update(Session).where(
                Session.id == session_id, Session.owner_id == actor.id,
            ).values(revision=Session.revision + 1).returning(Session.id))
            if locked is None:
                raise HTTPException(404, "Conversation not found.")
            session = await db.get(Session, session_id)
            assert session is not None
            if not session.deleted:
                return
            pending = await db.get(ComputerOperation, session_id)
            if session.active_turn or (pending and pending.expires_at > time.time()):
                raise HTTPException(409, "Wait for the task and any desktop action to finish stopping, then restore.")
            session.deleted = False
            session.archived = False
            session.updated_at = now()

    async def stopped(self, turn_id: str, conversation: str | None = None) -> None:
        await self.answers.stopped(turn_id, conversation)

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
            await self._fence_steering(db, turn_id)
            await db.execute(update(Turn).where(
                Turn.id == turn_id, Turn.status.in_(["steering", "signal_sent"])
            ).values(status="signal_sent", accepted_at=now()))

    async def steering_uncertain(self, turn_id: str) -> None:
        async with self.sessions.begin() as db:
            await self._fence_steering(db, turn_id)
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
            if self.product_authority_enabled:
                parent_grant = await self._saved_product_grant(db, parent)
                await self._check_product_turn(db, parent, parent_grant)
                await self._check_product_turn(db, turn, parent_grant)
            return SteeringInput(turn.id, self.security.unseal(turn.prompt).decode(), "asap")

    async def steering_receipt(self, turn_id: str, enqueue_id: str | None) -> None:
        async with self._admission_session(None) as db:
            turn = await self._fence_steering(db, turn_id)
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

    async def _fence_steering(self, db: AsyncSession, turn_id: str) -> Turn | None:
        """Session before Turn, matching admission, promotion, Stop and settlement."""
        turn = await db.get(Turn, turn_id)
        if turn is not None:
            await db.execute(update(Session).where(Session.id == turn.session_id).values(
                revision=Session.revision + 1,
            ))
            await db.refresh(turn)
        return turn

    async def resume(
        self, actor: User, session_id: str, previous_id: str,
        request_key: str, results: dict[str, Any], *, received_at: str | None = None,
    ) -> Turn:
        admitted_at = now()
        fingerprint = hashlib.sha256(json.dumps(
            {"previous": previous_id, "results": results}, sort_keys=True
        ).encode()).hexdigest()
        try:
            return await self._resume(
                actor, session_id, previous_id, request_key, results, fingerprint,
                received_at=received_at, admitted_at=admitted_at,
            )
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
        *, received_at: str | None = None, admitted_at: str | None = None,
    ) -> Turn:
        async with self._admission_session(None) as db:
            product_grant = await self._actor_product_grant(db, actor)
            if self.product_authority_enabled:
                locked_actor = await db.scalar(update(User).where(
                    User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
                ).values(auth_version=User.auth_version).returning(User.id))
                if locked_actor is None:
                    raise HTTPException(401, "Your access changed. Sign in again.")
            session = await self.allowed_session(db, session_id, actor)
            existing = await db.scalar(select(Turn).where(
                Turn.session_id == session_id, Turn.actor_id == actor.id, Turn.request_key == request_key
            ))
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity belongs to a different answer.")
                await self._check_product_turn(db, existing, product_grant)
                return existing
            previous = await db.get(Turn, previous_id)
            if (
                previous is None or previous.actor_id != actor.id or previous.session_id != session.id
                or previous.status != "waiting" or session.active_turn != previous.id
                or previous.preparation is None
            ):
                raise HTTPException(409, "The pending request changed.")
            mission = await self.swarms.admit(db, actor, session_id, NewMessage(
                prompt="Answer the existing pending request.", request_key=request_key,
                model=previous.model, connections=json.loads(previous.connections),
            ))
            try:
                member = await self.swarms.authorize(db, previous)
            except PermissionError as error:
                raise HTTPException(409, str(error)) from error
            await self._check_product_turn(db, previous, product_grant)
            original = TypeAdapter(PreparedTurn).validate_json(self.security.unseal(previous.preparation))
            current_grants = await self.scope_grants(db, original.scope, session)
            if self.product_authority_enabled and original.scope.product_grant != product_grant:
                raise HTTPException(403, "An approval cannot replace the task's original product authority.")
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
            await db.flush()
            from .performance import admission
            admission(
                db, turn, received_at or admitted_at or turn.created_at,
                source="http" if received_at else "admission", surface=session.surface,
            )
            self._bind_product_grant(db, turn, product_grant)
            if member is not None:
                await self.swarms.bind(db, mission, turn, generated=True)
            delegated = await db.get(DelegatedTurn, previous.id)
            if delegated is not None:
                db.add(DelegatedTurn(turn_id=turn.id, session_id=delegated.session_id))
            previous.status = "resumed"
            return turn
