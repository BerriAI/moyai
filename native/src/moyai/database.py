"""Async application persistence; no model/tool loop or competing run recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import and_, delete, event, func, or_, select, text, update
from sqlalchemy import insert as sql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from .answer_settlement import AnswerSettlement
from .automations.product_automation_models import ProductAutomationTurn
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
from .delegation_authority import session_context
from .delegation_models import DelegatedSession, DelegatedTurn
from .dispatch_wakeups import notify_dispatch
from .goal_models import Goal
from .goals import Goals
from .goals import dispatchable as goal_dispatchable
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
from .oauth_models import OAuthCredential
from .postgres import create_engine
from .preferences.settings_models import SettingAutomationRun
from .product_identity import ProductGrant, ProductIdentityAdapter
from .security import Security, password_hash
from .settings import Settings
from .swarm_models import SwarmMember
from .swarm_service import SwarmService, dispatchable
from .turn_admission import TurnAdmission

if TYPE_CHECKING:
    from .product_connections import ProductConnections
    from .product_drain import ProductDrainService
    from .product_inference import ProductInferenceService
    from .product_memory_review import ProductMemoryReviewService
    from .product_skill_learning import SkillLearningService
    from .tracing import LensTracing


def grant_fingerprint(grants: list[Grant]) -> str:
    return hashlib.sha256(json.dumps(
        sorted((grant.id, grant.revision) for grant in grants)
    ).encode()).hexdigest()


# Read projections and authorization do not need the agent's private checkpoint.
# Fail explicitly if a caller accidentally tries to use an omitted field.
SESSION_METADATA = (defer(Session.conversation, raiseload=True), defer(Session.snapshot, raiseload=True))


class Database:
    def __init__(self, settings: Settings, security: Security):
        self.settings = settings
        self.security = security
        self.tracing: LensTracing | None = None
        self.performance_tasks: set[asyncio.Task[None]] = set()
        from .performance import DispatchRecorder
        self.dispatch_timings = DispatchRecorder(self)
        self.product_identity: ProductIdentityAdapter | None = None
        self.slack_access: Any = None
        self.slack_dot_installation: Callable[[AsyncSession], Awaitable[dict[str, Any]]] | None = None
        self.slack_prefetch: Any = None
        self.slack_mentions: Any = None
        self.product_connections: ProductConnections | None = None
        self.product_drain: ProductDrainService | None = None
        self.product_inference: ProductInferenceService | None = None
        self.product_memory_review: ProductMemoryReviewService | None = None
        self.product_skill_learning: SkillLearningService | None = None
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
        self.goals = Goals(self)
        from .responsibilities import Responsibilities
        self.responsibilities = Responsibilities(self)
        from .dot import Dots
        self.dots = Dots(self)
        self.admission = TurnAdmission(self)
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
        from .slack_web import check_turn
        await check_turn(self, db, turn)
        return ProductGrant(binding.actor_id, binding.authority_version)

    async def _check_product_turn(
        self, db: AsyncSession, turn: Turn, expected: ProductGrant | None,
    ) -> ProductGrant | None:
        grant = await self._check_product_turn_binding(db, turn, expected)
        if self.product_authority_enabled:
            # One native connection/transaction avoids recursive pool checkouts.
            await self._identity().authorize_admission(db, grant)
        return grant

    async def _check_product_turn_binding(
        self, db: AsyncSession, turn: Turn, expected: ProductGrant | None,
    ) -> ProductGrant | None:
        """Check immutable provenance; callers must separately check live authority."""
        try:
            grant = await self._saved_product_grant(db, turn)
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        if self.product_authority_enabled and expected != grant:
            raise HTTPException(403, "The task's original product authority cannot be replaced.")
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

    async def allowed_session(
        self, db: AsyncSession, session_id: str, actor: User, *, metadata_only: bool = False,
    ) -> Session:
        """Authorize a fresh reader or input, including its current source audience."""
        await self.responsibilities.ordinary_chat(db, session_id)
        session = await self.execution_session(db, session_id, actor, metadata_only=metadata_only)
        await self.check_source_audience(db, session, actor, metadata_only=metadata_only)
        member = await db.get(SwarmMember, session_id)
        if member is not None and member.mission_id != session_id:
            root = await db.get(Session, member.mission_id, options=SESSION_METADATA if metadata_only else ())
            assert root is not None  # Checked by execution_session.
            await self.check_source_audience(db, root, actor, metadata_only=metadata_only)
        return session

    async def prefetch_session_audiences(
        self, actor: User, session_ids: list[str], *, deleted: bool = False,
    ) -> None:
        """Call before entering a read/admission transaction; final checks remain inside it."""
        prefetch = getattr(self, "slack_prefetch", None)
        if self.product_authority_enabled and prefetch is not None and session_ids:
            await prefetch(actor, session_ids, deleted=deleted)

    async def execution_session(
        self, db: AsyncSession, session_id: str, actor: User, *, metadata_only: bool = False,
    ) -> Session:
        """Local session checks for work whose saved turn authority is revalidated by the caller.

        Provider audience lookups belong at read/admission boundaries, not inside
        every worker/tool authorization. The original product grant and selected
        connection checks still fence execution, including Slack disconnection.
        """
        session = await db.get(Session, session_id, options=SESSION_METADATA if metadata_only else ())
        if session is None or session.deleted:
            raise HTTPException(404, "Conversation not found.")
        await self.responsibilities.read_authority(db, actor, session)
        if session.visibility == "private" and session.owner_id != actor.id:
            link = await db.get(DelegatedSession, session.id)
            if link is None or not await self.same_person(db, actor, session.owner_id):
                raise HTTPException(404, "Conversation not found.")
        member = await db.get(SwarmMember, session_id)
        if member is not None and member.mission_id != session_id:
            root = await db.get(Session, member.mission_id, options=SESSION_METADATA if metadata_only else ())
            if root is None or root.deleted or (root.visibility == "private" and root.owner_id != actor.id):
                raise HTTPException(404, "Conversation not found.")
        return session

    async def check_source_audience(
        self, db: AsyncSession, session: Session, actor: User, *, metadata_only: bool = False,
    ) -> None:
        delegated = await db.get(DelegatedSession, session.id)
        member = await db.get(SwarmMember, session.id) if session.surface == "slack" else None
        parent_id = delegated.parent_session_id if delegated else member.mission_id if member else None
        if parent_id and parent_id != session.id:
            parent = await db.get(Session, parent_id, options=SESSION_METADATA if metadata_only else ())
            if parent is None:
                raise HTTPException(404, "Conversation not found.")
            # Inherit provider audience, not the parent's navigation state. An
            # archived/deleted web parent must not hide its owner's private child.
            await self.check_source_audience(db, parent, actor, metadata_only=metadata_only)
            return
        if (session.surface == "slack" and self.product_authority_enabled
                and (self.slack_access is None or not await self.slack_access(db, session, actor))):
            raise HTTPException(404, "Conversation unavailable. Verify your linked Slack account and channel access.")

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
                    raise HTTPException(403, "Private sessions require an individual organization sign-in.")
            session = Session(
                id=new_id(), owner_id=actor.id, title=body.title,
                visibility=body.visibility, model=model,
            )
            transaction.add(session)
        return session

    async def list_sessions(self, actor: User, *, archived: bool = False, deleted: bool = False) -> list[Session]:
        from .responsibilities import ordinary_session
        async with self.sessions() as db:
            query = select(Session).where(
                Session.deleted.is_(deleted),
                ordinary_session(),
                or_(Session.owner_id == actor.id, Session.visibility == "shared"),
            ).order_by(Session.updated_at.desc()).limit(100)
            if not deleted:
                query = query.where(
                    Session.archived.is_(archived),
                    ~select(SwarmMember.session_id).where(
                        SwarmMember.session_id == Session.id, SwarmMember.position > 0,
                    ).exists(),
                )
            ids = list(await db.scalars(query.with_only_columns(Session.id)))
        await self.prefetch_session_audiences(actor, ids, deleted=deleted)
        async with self.sessions() as db:
            visible = []
            # Revalidate planned rows without admitting newly ranked audiences
            # that were never prepared while this request awaited the provider.
            for session in await db.scalars(query.where(Session.id.in_(ids))):
                try:
                    if deleted:
                        if not await self.same_person(db, actor, session.owner_id):
                            continue
                        await self.check_source_audience(db, session, actor)
                    else:
                        await self.allowed_session(db, session.id, actor)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    visible.append(session)
            return visible

    async def session(self, actor: User, session_id: str, *, metadata_only: bool = False) -> Session:
        await self.prefetch_session_audiences(actor, [session_id])
        async with self.sessions() as db:
            return await self.allowed_session(db, session_id, actor, metadata_only=metadata_only)

    async def messages(self, actor: User, session_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        from .message_sources import public_messages, source_view

        await self.prefetch_session_audiences(actor, [session_id])
        async with self.sessions() as db:
            await self.allowed_session(db, session_id, actor, metadata_only=True)
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
            authors = {user.id: user for user in await db.scalars(select(User).where(
                User.id.in_({row.author_id for row in rows if row.author_id}),
            ))}
            identities = {key: await self.same_person(db, actor, key) for key in authors}
            projected = await public_messages(self, db, rows)
            originals = {item.id: item for item in await db.scalars(select(Upload).where(
                Upload.session_id == session_id,
            ))}
            goal = await db.get(Goal, session_id)
            result = []
            for row in rows:
                content, source = projected[row.id]
                files = pictures.get(row.turn_id, [])
                if source and source.get("deleted"):
                    files = []
                if source and source.get("files"):
                    files = [{"id": item.id, "name": item.name, "media_type": item.media_type, "size": item.size}
                             for file in source["files"] if (item := originals.get(file.get("upload_id")))]
                author = authors.get(row.author_id or "")
                result.append({
                    "id": row.id, "turn_id": row.turn_id, "role": row.role,
                    "author_id": row.author_id, "author_name": author.display_name if author else None,
                    "is_self": identities.get(row.author_id or "", False), "content": content,
                    "sent_as_goal": row.role == "user" and bool(
                        (source or {}).get("sent_as_goal") or (goal and row.turn_id == goal.source_turn_id)
                    ),
                    "created_at": row.created_at, **source_view(source, session_id=session_id, message_id=row.id),
                    **({"source": "delegation"} if row.identity.startswith("delegation:") else {}),
                    "attachments": files if row.role == "user" or row.identity.startswith("delegation:") else [],
                })
        # Release the transcript connection before consulting the product
        # cache/provider, then recheck access after the asynchronous lookup.
        if self.slack_mentions is not None and await self.slack_mentions(
            result, {key: value[1] for key, value in projected.items()},
        ):
            await self.session(actor, session_id, metadata_only=True)
        return result

    async def same_person(self, db: AsyncSession, actor: User, author_id: str | None) -> bool:
        """Presentation/capability relationship, never a replacement execution grant."""
        if actor.id == author_id:
            return True
        if not author_id or self.product_identity is None:
            return False
        if not author_id.startswith("slack:") and not actor.id.startswith("slack:"):
            return False
        if not db.in_transaction():
            await db.begin()
        await self._actor_product_grant(db, actor)
        other = await db.get(User, author_id)
        if other is None or not other.enabled:
            return False
        if author_id.startswith("slack:"):
            return await self.product_identity.personal_memory_owner(db, author_id) == actor.id
        if actor.id.startswith("slack:"):
            return await self.product_identity.personal_memory_owner(db, actor.id) == author_id
        return False

    async def admit_automation(
        self, db: AsyncSession, *, grant: ProductGrant, title: str, prompt: str,
        model: str, request_key: str, connections: list[str] | None = None, session_id: str | None = None,
        visibility: Literal["private", "shared"] = "shared",
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
            if session_id:
                session = await self.automation_session(db, actor.id, session_id, visibility=visibility)
            else:
                session = await self.create_session(actor, NewSession(
                    title=title, model=model, visibility=visibility,
                ), db=db)
                session.surface = "automation"
            turn = await self.admission.submit(actor, session.id, NewMessage(
                prompt=prompt, model=model, request_key=request_key, connections=connections or [],
            ), db=db, automation_generated=True)
            await db.flush()
            return session, turn

    async def automation_session(
        self, db: AsyncSession, owner_id: str, session_id: str, *, visibility: str = "shared",
    ) -> Session:
        await self.responsibilities.ordinary_chat(db, session_id)
        session = await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        if (
            session is None or session.deleted or session.archived or session.owner_id != owner_id
            or visibility not in {"private", "shared"} or session.visibility != visibility or session.surface != "web"
            or await db.get(DelegatedSession, session_id) is not None
            or await db.get(SwarmMember, session_id) is not None
        ):
            raise HTTPException(409, "Choose an active web chat you own with the automation's visibility.")
        return session

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
        return await self.native_grants(db, actor_id, ids, shared_session=shared_session, surface=surface)

    async def native_grants(
        self, db: AsyncSession, actor_id: str, ids: list[str], *, shared_session: bool = False,
        kinds: frozenset[str] | None = None, surface: str = "web",
    ) -> list[Grant]:
        """Resolve stored native grants on the caller's admission transaction."""
        grants: list[Grant] = []
        for connection_id in dict.fromkeys(ids):
            row = await db.get(Connection, connection_id)
            policy = json.loads(row.policy) if row is not None else {}
            owner = actor_id
            if row is not None and policy.get("oauth"):
                from .personal_oauth import connection_owner
                owner = await connection_owner(self, db, actor_id, row.kind, surface)
            if (
                row is None or not row.enabled or (not row.shared and row.owner_id != owner)
                or (kinds is not None and row.kind not in kinds)
                or (row.kind == "litellm" and (row.shared or row.owner_id != actor_id))
            ):
                raise HTTPException(403, "A selected connection is no longer available.")
            slack_github = surface == "slack" and row.kind == "github" and policy.get("oauth") and owner
            if shared_session and not row.shared and not slack_github:
                raise HTTPException(403, "Use a private conversation for a personal connection.")
            if policy.get("oauth") and (
                row.shared or owner is None or row.owner_id != owner
                or await db.get(OAuthCredential, row.id) is None
            ):
                raise HTTPException(403, "This personal OAuth connection is unavailable.")
            grants.append(Grant(
                id=row.id, kind=row.kind, revision=row.revision,
                credential=row.credential, read_only=row.read_only, policy=policy,
            ))
        return grants

    async def execution_context(self, db: AsyncSession, session: Session, *, turn_id: str | None = None) -> ExecutionContext:
        automated = await db.get(ProductAutomationTurn, turn_id) if turn_id else None
        if automated is not None:
            turn = await db.get(Turn, turn_id)
            visibility = automated.visibility or "shared"
            if (
                session.deleted or session.archived or session.visibility != visibility
                or visibility not in {"private", "shared"}
                or turn is None or session.owner_id != turn.actor_id
                or session.surface not in {"web", "automation"}
            ):
                raise PermissionError("Scheduled work requires its original chat visibility and owner.")
            return ExecutionContext(cast(Literal["private", "shared"], visibility), "automation")
        return await session_context(self, db, session)

    async def human_origin(self, db: AsyncSession, scope: RunScope) -> str | None:
        """Follow native persisted resume evidence, never a client request-key prefix."""
        current = await db.get(Turn, scope.turn_id)
        seen = set()
        for _ in range(32):
            if current is None or current.id in seen or (
                current.session_id != scope.session_id or current.actor_id != scope.actor_id
                or current.parent_turn is not None
            ):
                return None
            seen.add(current.id)
            if await db.get(ProductAutomationTurn, current.id) is not None:
                return None
            # Native interval admission records its receipt before submit and
            # links turn_id afterward. Cover that publication window too.
            scheduled = await db.scalar(select(SettingAutomationRun.id).where(
                (SettingAutomationRun.turn_id == current.id)
                | (("automation:" + SettingAutomationRun.id) == current.request_key),
            ).limit(1))
            if scheduled is not None:
                return None
            if await self._saved_product_grant(db, current) != scope.product_grant:
                return None
            authored = await db.scalar(select(Message.id).where(
                Message.turn_id == current.id, Message.role == "user", Message.author_id == scope.actor_id,
                Message.identity == f"user:{current.id}",
            ).limit(1))
            if authored is not None:
                return current.id
            if not current.deferred_results:
                return None
            try:
                results = json.loads(self.security.unseal(current.deferred_results))
            except (ValueError, TypeError, UnicodeError, RecursionError, InvalidToken):
                return None
            candidates = list(await db.scalars(select(Turn.id).where(
                Turn.session_id == scope.session_id, Turn.actor_id == scope.actor_id,
                Turn.status == "resumed", Turn.created_at <= current.created_at,
            ).order_by(Turn.created_at.desc()).limit(100)))
            previous = next((identity for identity in candidates if hashlib.sha256(json.dumps(
                {"previous": identity, "results": results}, sort_keys=True,
            ).encode()).hexdigest() == current.request_fingerprint), None)
            current = await db.get(Turn, previous) if previous else None
        return None

    async def scope_grants(self, db: AsyncSession, scope: RunScope, session: Session) -> list[Grant]:
        """Resolve under persisted execution policy, never private-child presentation."""
        context = await self.execution_context(db, session, turn_id=scope.turn_id)
        if (
            context.surface == "slack" and any(grant.policy.get("oauth") for grant in scope.grants)
            and (
                scope.delegation_depth or scope.delegation_generated or scope.swarm_id
                or scope.goal_generation is not None or await self.human_origin(db, scope) is None
            )
        ):
            raise PermissionError("Personal GitHub in Slack requires a direct request from its owner.")
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
                    Turn.status == "queued",
                    or_(Session.active_turn.is_(None), Session.active_turn == Turn.id),
                    Session.deleted.is_(False),
                    dispatchable(),
                    goal_dispatchable(),
                ).order_by(Turn.created_at, Turn.id).limit(limit - len(claimed_turns))
                  .with_for_update(of=Session, skip_locked=True))).all()
                if not candidates:
                    break
                for turn in candidates:
                    if turn.session_id in claimed_sessions:
                        continue
                    claimed = await db.scalar(update(Session).where(
                        Session.id == turn.session_id,
                        or_(Session.active_turn.is_(None), Session.active_turn == turn.id),
                        Session.deleted.is_(False),
                    ).values(revision=Session.revision + 1).returning(Session.id))
                    if claimed is None:
                        continue
                    admitted = await db.scalar(update(Turn).where(
                        Turn.id == turn.id, Turn.status == "queued", goal_dispatchable(),
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
        async with self.sessions() as lookup:
            session_id = await lookup.scalar(select(Turn.session_id).where(Turn.id == turn_id))
        if session_id is not None:
            await self.prefetch_session_audiences(actor, [session_id])
        async with self._admission_session(None) as db:
            actor_grant = await self._actor_product_grant(db, actor)
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.actor_id != actor.id:
                raise HTTPException(404, "Message not found.")
            await self.responsibilities.editable_input(db, turn)
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
                await self._record_guidance(db, turn, session.revision)
            return turn

    async def _record_guidance(self, db: AsyncSession, turn: Turn, revision: int) -> None:
        # Event IDs order admissions while holding the session fence, including
        # "Send now" for an older queued message. Message creation time cannot
        # order those corrections. A repeated promotion can have a new receipt.
        db.add(Event(
            session_id=turn.session_id, turn_id=turn.id, kind="steering_admitted",
            identity=f"guidance:{turn.id}:{revision}",
            content=self.security.seal({"parent_turn": turn.parent_turn}),
        ))
        await self.goals.invalidate_for_guidance(db, turn)

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

    async def authorize_scope(
        self, scope: RunScope, *, input_id: str | None = None, db: AsyncSession | None = None,
    ) -> list[Grant]:
        """Recheck live authority and return the grants read by this check."""
        if db is None:
            async with self.sessions() as transaction:
                return await self.authorize_scope(scope, input_id=input_id, db=transaction)
        else:
            actor = await db.get(User, scope.actor_id)
            turn = await db.get(Turn, scope.turn_id)
            if actor is None or not actor.enabled or turn is None:
                raise PermissionError("This execution no longer has an authorized requester.")
            await self._check_product_turn(db, turn, scope.product_grant)
            cast(Any, actor).product_grant = scope.product_grant
            await self.swarms.authorize(db, turn, scope=scope)
            await self.goals.authorize(db, turn)
            await self.responsibilities.authorize(db, turn, scope=scope)
            await self.dots.authorize(db, turn, scope=scope)
            session = await self.execution_session(db, scope.session_id, actor)
            if (
                session.active_turn != turn.id or session.status != "running" or turn.status != "running"
                or turn.actor_id != actor.id or turn.session_id != session.id
            ):
                raise PermissionError("This execution has been stopped or superseded.")
            current = await self.scope_grants(db, scope, session)
            if [(g.id, g.revision) for g in current] != [(g.id, g.revision) for g in scope.grants]:
                raise PermissionError("Connection access changed. Start a fresh authorized turn.")
            if input_id is not None:
                pending = await self.latest_guidance(db, turn.id)
                if pending and pending != input_id:
                    raise GuidanceChanged("New guidance arrived before this action was dispatched.")
            return current

    @staticmethod
    def guidance_order():
        return select(func.max(Event.id)).where(
            Event.turn_id == Turn.id, Event.kind == "steering_admitted",
        ).scalar_subquery()

    async def latest_guidance(self, db: AsyncSession, parent_turn_id: str) -> str | None:
        """One admission-order rule for every effect and memory mutation fence."""
        return await db.scalar(select(Turn.id).where(
            Turn.parent_turn == parent_turn_id,
            Turn.status.in_(["steering", "signal_sent", "accepted"]),
        ).order_by(
            self.guidance_order().desc().nulls_last(), Turn.created_at.desc(), Turn.id.desc(),
        ).limit(1))

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

    async def fail(
        self, turn_id: str, message: str, *, only_unsettled: bool = False,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        await self.answers.fail(turn_id, message, only_unsettled=only_unsettled, diagnostics=diagnostics)

    async def stop(self, actor: User, session_id: str) -> str | None:
        if await self.swarms.stop_session(actor, session_id):
            return None  # Durable native Stop dispatch reconciles every member.
        async with self._admission_session(None) as db:
            session = await self.allowed_session(db, session_id, actor)
            if not await self.same_person(db, actor, session.owner_id) and actor.role != "admin":
                raise HTTPException(403, "Only the conversation owner or an administrator can stop it.")
            workflow_id = None
            if session.active_turn:
                turn = await db.get(Turn, session.active_turn)
                if turn:
                    workflow_id = turn.workflow_id
            await db.execute(update(Session).where(
                Session.id == session_id
            ).values(status="stopped"))
            goal = await db.get(Goal, session_id, populate_existing=True)
            if goal is not None and goal.status == "active":
                self.goals.change(goal, "paused", "Paused by Stop.")
            await db.execute(update(Turn).where(
                Turn.session_id == session_id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering",
                                 "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))
            # Keep ownership until execution/cleanup acknowledges Stop; don't admit competing work.
            return workflow_id

    async def delete_session(self, actor: User, session_id: str) -> None:
        """Hide a session and fence work atomically; the existing Stop dispatcher cancels execution."""
        await self.prefetch_session_audiences(actor, [session_id])
        async with self._admission_session(None) as db:
            await self.responsibilities.ordinary_chat(db, session_id)
            member = await self.swarms.member(db, session_id)
            # Keep the mission-before-session lock order and commit the deletion
            # together with its team-wide attention fence.
            mission = await self.swarms.lock(db, member.mission_id, actor) if member else None
            owned = await db.get(Session, session_id)
            if owned is None or not await self.same_person(db, actor, owned.owner_id):
                raise HTTPException(404, "Conversation not found.")
            await self.check_source_audience(db, owned, actor)
            locked = await db.scalar(update(Session).where(
                Session.id == session_id, Session.owner_id == owned.owner_id,
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
            goal = await db.get(Goal, session_id, populate_existing=True)
            if goal is not None and goal.status == "active":
                self.goals.change(goal, "paused", "The conversation was deleted.")
            session.updated_at = now()
            # An answer already being saved retains its confirmed outcome.
            if session.status != "saving":
                session.status = "stopped"
            await db.execute(update(Turn).where(
                Turn.session_id == session_id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering", "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))

    async def restore_session(self, actor: User, session_id: str) -> None:
        await self.prefetch_session_audiences(actor, [session_id], deleted=True)
        async with self.sessions.begin() as db:
            await self.responsibilities.ordinary_chat(db, session_id)
            owned = await db.get(Session, session_id)
            if owned is None or not await self.same_person(db, actor, owned.owner_id):
                raise HTTPException(404, "Conversation not found.")
            await self.check_source_audience(db, owned, actor)
            locked = await db.scalar(update(Session).where(
                Session.id == session_id, Session.owner_id == owned.owner_id,
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
            )).order_by(
                self.guidance_order().asc().nulls_first(), Turn.created_at, Turn.id,
            ).limit(100))
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
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(409, "This answer conflicts with an already admitted request.") from error

    async def _resume(
        self, actor: User, session_id: str, previous_id: str,
        request_key: str, results: dict[str, Any], fingerprint: str,
        *, received_at: str | None = None, admitted_at: str | None = None,
        transaction: AsyncSession | None = None, reply: NewMessage | None = None,
    ) -> Turn:
        if transaction is None:
            await self.prefetch_session_audiences(actor, [session_id])
        async with self._admission_session(transaction) as db:
            if await self.responsibilities.worker_session(db, session_id):
                raise HTTPException(409, "Responsibility evidence reviews cannot be resumed as conversations.")
            product_grant = await self._actor_product_grant(db, actor)
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
            ).values(active_turn=turn_id, status="queued", updated_at=now()).returning(Session.id))
            if claimed is None:
                raise HTTPException(409, "Another response already resumed this request.")
            from .browser_login import handoff_login_control
            await handoff_login_control(self, db, actor, previous, results)
            db.add(turn)
            await db.flush()
            from .performance import admission
            admission(
                db, turn, received_at or admitted_at or turn.created_at,
                source="http" if received_at else "admission", surface=session.surface,
            )
            self._bind_product_grant(db, turn, product_grant)
            if self.product_authority_enabled:
                from .product_slack_models import SlackWebTurn
                source = await db.get(SlackWebTurn, previous.id)
                raw = getattr(actor, "slack_web_grant", None)
                if raw is None and source is not None:
                    raw = ProductGrant(source.requester_id, source.authority_version)
                if raw is not None:
                    await self._identity().authorize_admission(db, raw)
                    db.add(SlackWebTurn(
                        turn_id=turn.id, requester_id=raw.user_id, authority_version=raw.authority_version,
                    ))
            if reply is not None:
                db.add(Message(
                    session_id=session.id, turn_id=turn.id, author_id=actor.id,
                    role="user", content=self.security.seal(reply.authored_text), identity=f"user:{turn.id}",
                ))
                from .message_sources import record_source
                await record_source(self, db, f"user:{turn.id}", reply)
            if member is not None:
                await self.swarms.bind(db, mission, turn, generated=True)
            automated = await db.get(ProductAutomationTurn, previous.id)
            if automated is None:
                await self.goals.bind(db, turn, resumed_from=previous.id)
            delegated = await db.get(DelegatedTurn, previous.id)
            if delegated is not None:
                db.add(DelegatedTurn(turn_id=turn.id, session_id=delegated.session_id))
            from .agent_messages import resume_mail
            completed_agents = await resume_mail(db, self, previous, turn)
            if original.scope.background_delegation:
                from .delegation import bind_background_resume
                original.scope.background_agents = [
                    child for child in original.scope.background_agents if child not in completed_agents
                ]
                await bind_background_resume(self, db, previous, turn, original.scope)
            if automated is not None:
                db.add(ProductAutomationTurn(
                    turn_id=turn.id, origin_turn_id=automated.origin_turn_id, visibility=automated.visibility,
                ))
            previous.status = "resumed"
            return turn
