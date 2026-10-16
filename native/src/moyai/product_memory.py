"""Canonical personal memory CRUD and live reads for the native compatibility store.

The original encrypted rows, IDs, revisions, tombstones and capture barriers stay
authoritative. This bridge does not copy notes into another owner's namespace or
start a reviewer/tool loop. Native Harness remains the memory tool engine.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from app.database import SQL_PARTS
from app.memory import Memory as CanonicalMemory
from app.memory import Note, Preferences, Revision
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Database
from .models import Message, User, now
from .product_identity import ProductGrant, ProductIdentityAdapter, ProductPrincipal
from .product_memory_models import ProductMemoryBarrier

if TYPE_CHECKING:
    from .product_memory_review import ProductMemoryReviewService

SOURCE_TABLES = frozenset({
    "personal_memories", "memory_preferences", "memory_operations", "memory_capture_barriers",
    "memory_selections", "messages",
})
CAPABILITIES = {"read": True, "create": True, "update": True, "delete": True, "preferences": True, "review": False}


class SourceMemoryConnection:
    """Run unchanged canonical save policy on the caller's SQLAlchemy transaction.

    Only the finite memory-policy table inventory is qualified. Parameters,
    quoted values and comments are never rewritten as SQL identifiers.
    """

    def __init__(self, session: Any, identity: ProductIdentityAdapter):
        self.session, self.identity = session, identity

    def execute(self, statement: str, parameters=()):
        parts = SQL_PARTS.split(statement)
        index = 0
        values = {}
        for position in range(0, len(parts), 2):
            part = parts[position]
            part = re.sub(
                r"\b(" + "|".join(sorted(SOURCE_TABLES)) + r")\b",
                lambda match: self.identity._table(match[0]), part,
            )
            chunks = part.split("?")
            joined = chunks[0]
            for chunk in chunks[1:]:
                if index >= len(parameters):
                    raise ValueError("Canonical memory query parameters changed.")
                key = f"memory_{index}"
                value = parameters[index]
                values[key] = int(value) if isinstance(value, bool) else value
                joined += f":{key}" + chunk
                index += 1
            parts[position] = joined
        if index != len(parameters):
            raise ValueError("Canonical memory query parameters changed.")
        result = self.session.execute(text("".join(parts)), values)
        return result.mappings() if result.returns_rows else result


class ProductMemoryService:
    def __init__(
        self, database: Database, identity: ProductIdentityAdapter, *,
        recall_available: bool = False, auto_save_available: bool = False,
        background_review_available: bool = False,
    ):
        self.database, self.identity = database, identity
        self.recall_available = recall_available
        self.auto_save_available = auto_save_available
        self.background_review_available = background_review_available
        self.native_review: ProductMemoryReviewService | None = None
        # The reused service is a policy helper only. No schema setup, source
        # connection, reviewer, checkpoint manager or legacy runtime is started.
        self.canonical = CanonicalMemory(
            SimpleNamespace(schema_updates=False), identity.legacy_security,
            lambda owner, actor, **_: bool(owner and owner == actor), None,
        )

    def _owner(self, principal: ProductPrincipal) -> str:
        if principal.individual:
            return principal.id
        if (
            principal.id == "shared:local:admin" and principal.method == "local"
            and self.identity.legacy_security.local_preview()
        ):
            return principal.id
        raise HTTPException(403, "Personal memory requires an independently verified individual sign-in.")

    async def owner_in(self, db: AsyncSession, principal: ProductPrincipal) -> str:
        if principal.kind == "slack" and principal.method == "slack":
            owner = await self.identity.personal_memory_owner(db, principal.id)
            if owner:
                return owner
            raise HTTPException(403, "Personal memory requires a fresh, unambiguous verified Slack profile.")
        return self._owner(principal)

    def _table(self, name: str) -> str:
        return self.identity._table(name)

    async def _preferences(self, db: AsyncSession, owner: str) -> dict[str, Any]:
        row = (await db.execute(text(
            f"SELECT enabled,auto_save,revision FROM {self._table('memory_preferences')} WHERE owner_id=:owner"
        ), {"owner": owner})).mappings().first()
        return {
            "enabled": bool(row["enabled"]), "auto_save": bool(row["auto_save"]), "revision": row["revision"],
        } if row else Preferences().model_dump()

    def _unpack(self, row: dict[str, Any]) -> dict[str, Any]:
        try:
            note = self.canonical.unpack(row)
            if (
                not isinstance(note, dict)
                or not all(isinstance(note.get(key), str) for key in ("key", "title", "content", "kind", "repo_url"))
                or not isinstance(note.get("source"), dict)
                or note["kind"] not in {"preference", "feedback", "project", "reference"}
            ):
                raise ValueError()
            source = dict(note["source"])
            if source.get("core") == "pydantic-v1" and re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(source.get("native_session_id", "")),
            ):
                source.update(
                    engine="native", session_id=source["native_session_id"], url="/#" + source["native_session_id"],
                )
            elif "core" not in source and re.fullmatch(r"[0-9a-f]{32}", str(source.get("run_id", ""))):
                # run_id is the original canonical provenance field; native
                # saves use explicit native_* fields and never fabricate it.
                source.update(engine="legacy", session_id=source["run_id"], url="/#run=" + source["run_id"])
            note["source"] = source
            return note
        except (InvalidToken, ValueError, TypeError, KeyError):
            raise HTTPException(503, "The canonical memory record requires source-format review.") from None

    async def snapshot(
        self, grant: ProductGrant, *, include_deleted: bool = False,
    ) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        principal = await self.identity.require_current(grant)
        owner = self._owner(principal)
        clause = "" if include_deleted else " AND deleted=0"
        async with self.identity._reader(self.identity.source_engine) as db:
            prefs = await self._preferences(db, owner)
            rows = [dict(row) for row in (await db.execute(text(
                f"SELECT * FROM {self._table('personal_memories')} "
                f"WHERE owner_id=:owner{clause} ORDER BY updated_at DESC,id"
            ), {"owner": owner})).mappings()]
        await self.identity.require_current(grant)
        # Re-read the source after yielding. A forgotten note or disabled
        # preference must not be returned from a stale snapshot.
        async with self.identity._reader(self.identity.source_engine) as db:
            fresh_prefs = await self._preferences(db, owner)
            fresh = [dict(row) for row in (await db.execute(text(
                f"SELECT * FROM {self._table('personal_memories')} "
                f"WHERE owner_id=:owner{clause} ORDER BY updated_at DESC,id"
            ), {"owner": owner})).mappings()]
        if fresh_prefs != prefs or fresh != rows:
            raise HTTPException(409, "The memory library changed while it was being read. Refresh its revision.")
        return owner, prefs, rows

    async def listing(self, grant: ProductGrant) -> dict[str, Any]:
        principal = await self.identity.require_current(grant)
        try:
            owner = self._owner(principal)
        except HTTPException:
            return {
                "source": "canonical", "scope": None, "preferences": None, "memories": [],
                "capabilities": {key: False for key in CAPABILITIES},
                "runtime": {"recall": False, "auto_save": False, "background_review": False},
                "note": "Personal memory is unavailable for this shared sign-in. Use an individual account.",
            }
        async with self.identity._reader(self.identity.source_engine) as db:
            pending = await db.scalar(text(
                f"SELECT count(*) FROM {self._table('memory_reviews')} "
                "WHERE owner_id=:owner AND status IN ('pending','running')"
            ), {"owner": owner})
            latest = (await db.execute(text(
                f"SELECT status,updated_at,saved_count FROM {self._table('memory_reviews')} "
                "WHERE owner_id=:owner AND status IN ('completed','failed') "
                "ORDER BY updated_at DESC,message_id DESC LIMIT 1"
            ), {"owner": owner})).mappings().first()
        native_review = await self.native_review.status(owner) if self.native_review is not None else None
        _, prefs, rows = await self.snapshot(grant)
        memories = [self._unpack(row) for row in rows]
        return {
            "source": "canonical", "scope": {"kind": "personal", "owner_id": owner},
            "supported_scopes": ["personal"], "preferences": prefs, "memories": memories,
            "capabilities": {**CAPABILITIES, "review": self.background_review_available},
            "runtime": {
                "recall": self.recall_available and prefs["enabled"],
                "auto_save": self.auto_save_available and prefs["enabled"] and prefs["auto_save"],
                "background_review": self.background_review_available,
                "note": "Saved preferences are separate from native pipeline availability.",
            },
            "review": {
                "enabled": bool(self.identity.settings.memory_review_enabled),
                "configured": self.background_review_available,
                "pending": native_review["pending"] if native_review is not None else pending,
                "latest": native_review["latest"] if native_review is not None else dict(latest) if latest else None,
                "backend": "native_temporal" if native_review is not None else "legacy_retained",
                "legacy_pending": pending, "legacy_latest": dict(latest) if latest else None,
            },
            "note": "Canonical encrypted notes and revisions are retained. Unknown team/connection scopes are not remapped.",
        }

    @asynccontextmanager
    async def writer(self, grant: ProductGrant) -> AsyncIterator[tuple[AsyncSession, str]]:
        if self.database.engine.dialect.name != "postgresql":
            raise HTTPException(503, "Canonical memory mutations require the product PostgreSQL transaction.")
        async with self.database.sessions.begin() as db:
            await db.execute(text(
                "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
            ), {"schema": self.identity.schema})
            # Native disable/revocation writes serialize here as well. The live
            # grant check follows this fence, using the same SQL transaction.
            cached = await db.scalar(update(User).where(
                User.id == grant.user_id, User.enabled.is_(True),
            ).values(auth_version=User.auth_version).returning(User.id))
            if cached is None:
                raise HTTPException(401, "Current product authorization is required.")
            principal = await self.identity.authorize_admission(db, grant)
            yield db, await self.owner_in(db, principal)

    async def save_in(
        self, db: AsyncSession, owner: str, body: Note, *,
        note_id: str = "", source: dict[str, Any] | None = None, manual: bool = False,
    ) -> dict[str, Any]:
        # Reuse the original tombstone, key, CAS, operation replay, expiry,
        # secret rejection and observation/manual-note protection exactly.
        def save(session):
            conn = SourceMemoryConnection(session, self.identity)
            result = self.canonical.save_in(conn, owner, body, source=source, note_id=note_id)
            if manual:
                self.canonical.capture_barrier_in(conn, owner)
            return result
        result = await db.run_sync(save)
        if manual:
            await self.native_barrier_in(db, owner)
        return result

    async def native_barrier_in(self, db: AsyncSession, owner: str) -> None:
        maximum = await db.scalar(select(func.coalesce(func.max(Message.id), 0)))
        await db.execute(insert(ProductMemoryBarrier).values(
            owner_id=owner, last_message_id=maximum, updated_at=now(),
        ).on_conflict_do_update(
            index_elements=["owner_id"],
            set_={"last_message_id": func.greatest(ProductMemoryBarrier.last_message_id, maximum), "updated_at": now()},
        ))

    async def save(self, grant: ProductGrant, body: Note, *, note_id: str = "") -> dict[str, Any]:
        async with self.writer(grant) as (db, owner):
            result = await self.save_in(db, owner, body, note_id=note_id, manual=True)
        await self.identity.require_current(grant)
        return result

    async def preferences(self, grant: ProductGrant, body: Preferences) -> dict[str, Any]:
        async with self.writer(grant) as (db, owner):
            old = await self._preferences(db, owner)
            if old["revision"] != body.revision:
                raise HTTPException(409, "Memory settings changed. Refresh before saving.")
            await db.execute(text(
                f"INSERT INTO {self._table('memory_preferences')} VALUES (:owner,:enabled,:auto_save,:revision) "
                "ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled,auto_save=excluded.auto_save,"
                "revision=excluded.revision"
            ), {"owner": owner, "enabled": int(body.enabled), "auto_save": int(body.auto_save), "revision": body.revision + 1})
            await db.run_sync(lambda session: self.canonical.capture_barrier_in(
                SourceMemoryConnection(session, self.identity), owner,
            ))
            await self.native_barrier_in(db, owner)
            if not body.enabled:
                await db.execute(text(
                    f"DELETE FROM {self._table('memory_selections')} WHERE owner_id=:owner"
                ), {"owner": owner})
            result = await self._preferences(db, owner)
        await self.identity.require_current(grant)
        return result

    async def forget_in(self, db: AsyncSession, owner: str, note_id: str, revision: int) -> None:
        # The original forget policy keeps the opaque tombstone and operation
        # references, and erases the encrypted body rather than archiving it.
        row = await db.run_sync(lambda session: self.canonical.get(
            SourceMemoryConnection(session, self.identity), owner, note_id,
        ))
        if row["revision"] != revision:
            raise HTTPException(409, "This memory changed. Review it before deleting.")
        await db.execute(text(
            f"UPDATE {self._table('personal_memories')} SET encrypted='',deleted=1,"
            "revision=revision+1,updated_at=:stamp WHERE id=:id AND owner_id=:owner"
        ), {"stamp": now(), "id": note_id, "owner": owner})
        await db.execute(text(
            f"DELETE FROM {self._table('memory_selections')} WHERE memory_id=:id"
        ), {"id": note_id})
        await db.run_sync(lambda session: self.canonical.capture_barrier_in(
            SourceMemoryConnection(session, self.identity), owner,
        ))
        await self.native_barrier_in(db, owner)

    async def forget(self, grant: ProductGrant, note_id: str, revision: int) -> dict[str, Any]:
        async with self.writer(grant) as (db, owner):
            await self.forget_in(db, owner, note_id, revision)
        await self.identity.require_current(grant)
        return {"forgotten": True}


def routes(service: ProductMemoryService) -> APIRouter:
    router = APIRouter(prefix="/api/product/memory")

    async def authenticated(request: Request, *, mutation: bool = False) -> ProductPrincipal:
        principal = await service.identity.authenticate(request, mutation=mutation)
        if mutation:
            # A valid product cookie may reach this route before /me. Project
            # the already-authenticated identity; this never enrolls a source user.
            async with service.database.sessions.begin() as db:
                await service.identity.project_native_user(db, principal)
        return principal

    @router.get("")
    async def listing(request: Request):
        principal = await authenticated(request)
        return await service.listing(principal.grant)

    @router.put("/preferences")
    async def preferences(body: Preferences, request: Request):
        principal = await authenticated(request, mutation=True)
        return await service.preferences(principal.grant, body)

    @router.post("", status_code=201)
    async def create(body: Note, request: Request):
        principal = await authenticated(request, mutation=True)
        return await service.save(principal.grant, body)

    @router.put("/{note_id}")
    async def update_note(note_id: str, body: Note, request: Request):
        principal = await authenticated(request, mutation=True)
        return await service.save(principal.grant, body, note_id=note_id)

    @router.delete("/{note_id}")
    async def forget(note_id: str, body: Revision, request: Request):
        principal = await authenticated(request, mutation=True)
        return await service.forget(principal.grant, note_id, body.revision)

    return router
