"""Read compatibility for the existing product, never an execution importer.

Inject Tesla's ProductIdentityAdapter and its *existing* AsyncEngine. This module
does not create engines, tables, users, credentials, or legacy runtime services.
Mount ``routes(adapter)`` and dispatch historical IDs here; do not feed them to
the native execution/mutation endpoints. Responses retain the original IDs.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

SOURCE_FORMAT = "moyai-product-v1"
MAX_PAGE = 100
MAX_OFFSET = 10_000
MAX_MESSAGE_CHARS = 32_768
MAX_SEARCH_CHARS = 65_536
CAPABILITIES = {
    "read": True, "continue": False, "rename": False, "move": False,
    "pin": False, "archive": False, "delete": False, "restore": False,
    "stop": False, "computer": False,
}


class Principal(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def role(self) -> str: ...

    @property
    def individual(self) -> bool: ...

    @property
    def grant(self) -> Any: ...


class Identity(Protocol):
    async def authenticate(self, request: Request, *, mutation: bool = False, admin: bool = False) -> Principal: ...

    async def require_current(self, grant: Any) -> Principal: ...


def bounded(limit: int, offset: int = 0) -> None:
    if not 1 <= limit <= MAX_PAGE or not 0 <= offset <= MAX_OFFSET:
        raise HTTPException(422, "Use a page size of 1–100 and an offset of 0–10000.")


def session_id(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HTTPException(404, "Session not found.")
    return value


class ProductHistoryAdapter:
    continuation_access: Any = None
    continuation_schema: str | None = None

    """SELECT-only, permission-scoped product history over the canonical source."""

    def __init__(
        self, identity: Identity, source_engine: AsyncEngine, *, schema: str | None = None,
    ) -> None:
        if schema is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
            raise ValueError("Invalid product schema.")
        if source_engine.dialect.name == "sqlite" and not (
            source_engine.url.query.get("mode") == "ro" and source_engine.url.query.get("uri") == "true"
        ):
            raise ValueError("Product history requires an existing SQLite URI with mode=ro&uri=true.")
        # Standalone wheels do not ship app/. Only constructing the explicitly
        # enabled legacy reader imports the original product ACL.
        from app.session_privacy import SessionPrivacy

        self.privacy = SessionPrivacy
        self.identity, self.engine = identity, source_engine
        self.schema = schema if source_engine.dialect.name == "postgresql" else None

    def table(self, name: str) -> str:
        # All table names are internal constants. Never interpolate request input.
        if not re.fullmatch(r"[a-z_]+", name):
            raise ValueError("Invalid product table.")
        return f'"{self.schema}"."{name}"' if self.schema else f'"{name}"'

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        try:
            async with asyncio.timeout(5), self.engine.connect() as conn:
                if self.engine.dialect.name == "postgresql":
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
                # No commit, DDL, bootstrap or migration, including on SQLite.
                yield conn
        except (SQLAlchemyError, TimeoutError):
            raise HTTPException(503, "Existing product history is unavailable; check its database/schema.") from None

    def access_cte(self, *, deleted: bool = False) -> str:
        t = self.table
        live = "" if deleted else "AND r.deleted_at='' AND r.deletion_requested_at=''"
        ancestor_live = "" if deleted else "OR a.deleted_at!='' OR a.deletion_requested_at!=''"
        return f"""WITH identities AS (
            SELECT :actor AS id UNION SELECT id FROM {t('users')}
              WHERE kind='slack' AND linked_user_id=:actor
        ), visible AS (
            SELECT r.id, rr.root_id FROM {t('runs')} r
            JOIN {t('run_roots')} rr ON rr.run_id=r.id
            JOIN {t('runs')} root ON root.id=rr.root_id AND root.parent_run_id=''
            WHERE (r.private_owner_id='' OR r.private_owner_id=:private_actor)
              AND (root.private_owner_id='' OR root.private_owner_id=:private_actor)
              {live}
              AND EXISTS(SELECT 1 FROM {t('run_ancestry')} self
                  WHERE self.run_id=r.id AND self.ancestor_id=r.id)
              AND EXISTS(SELECT 1 FROM {t('run_ancestry')} lineage
                  WHERE lineage.run_id=r.id AND lineage.ancestor_id=root.id)
              AND NOT EXISTS(SELECT 1 FROM {t('run_ancestry')} lineage
                  LEFT JOIN {t('runs')} a ON a.id=lineage.ancestor_id
                  WHERE lineage.run_id=r.id AND (a.id IS NULL
                    OR (a.private_owner_id!='' AND a.private_owner_id!=:private_actor)
                    {ancestor_live}))
        ), mine AS (
            SELECT DISTINCT v.root_id AS id FROM visible v JOIN {t('runs')} r ON r.id=v.id
            WHERE r.owner_id IN (SELECT id FROM identities)
              OR EXISTS(SELECT 1 FROM {t('messages')} m
                WHERE m.run_id=r.id AND m.role='user' AND m.user_id IN (SELECT id FROM identities))
        ) """

    @staticmethod
    def params(principal: Principal) -> dict[str, Any]:
        return {"actor": principal.id, "private_actor": principal.id if principal.individual else ""}

    def columns(self) -> str:
        t = self.table
        return f"""r.id,r.owner_id,r.private_owner_id,r.status,r.model,r.harness,
            r.parent_run_id,r.created_at,r.updated_at,r.deleted_at,r.deletion_requested_at,v.root_id,
            substr(COALESCE(NULLIF(r.display_title,''),NULLIF(r.agent_label,''),r.prompt),1,300) AS title,
            CASE WHEN r.side_chat_of IN (SELECT id FROM visible) THEN r.side_chat_of ELSE '' END AS side_chat_of,
            substr(owner.name,1,200) AS owner_name,
            EXISTS(SELECT 1 FROM {t('session_pins')} p WHERE p.owner_id=:actor AND p.run_id=v.root_id) AS pinned,
            EXISTS(SELECT 1 FROM {t('session_archives')} a WHERE a.owner_id=:actor
                AND a.run_id=v.root_id) AS archived,
            (r.owner_id IN (SELECT id FROM identities)) AS created_by_me,
            (v.root_id IN (SELECT id FROM mine) AND r.owner_id NOT IN (SELECT id FROM identities)) AS participated,
            (EXISTS(SELECT 1 FROM {t('slack_threads')} s WHERE s.run_id=v.root_id)
                OR EXISTS(SELECT 1 FROM {t('slack_events')} s WHERE s.run_id=v.root_id)) AS slack_connected,
            (SELECT substr(f.name,1,200) FROM {t('session_folder_memberships')} m
                JOIN {t('session_folders')} f ON f.id=m.folder_id AND f.owner_id=m.owner_id
                WHERE m.owner_id=:actor AND m.run_id=v.root_id) AS folder,
            (SELECT m.folder_id FROM {t('session_folder_memberships')} m
                WHERE m.owner_id=:actor AND m.run_id=v.root_id) AS folder_id"""

    def from_runs(self) -> str:
        return (f"FROM {self.table('runs')} r JOIN visible v ON v.id=r.id "
                f"LEFT JOIN {self.table('users')} owner ON owner.id=r.owner_id ")

    def view(self, row: dict[str, Any], principal: Principal) -> dict[str, Any]:
        # Explicit allowlist: no pending_result, side_chat_context, sandbox/token,
        # checkpoints, working events, credential configuration or tool payloads.
        return {
            "id": row["id"], "title": row["title"] or "Earlier conversation",
            "owner_id": row["owner_id"], "owner": {"id": row["owner_id"], "name": row["owner_name"]},
            "visibility": "private" if self.privacy.is_private(row) else "shared",
            "surface": "slack" if row["slack_connected"] else "web",
            "model": row["model"], "status": row["status"], "source_status": row["status"],
            "active_turn": None, "workspace": None, "save_warning": None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "archived": bool(row["archived"]), "deleted": bool(row["deleted_at"]),
            "deleting": bool(row["deletion_requested_at"]), "deleted_at": row["deleted_at"] or None,
            "pinned": bool(row["pinned"]), "folder": row["folder"] or "",
            "folder_id": row["folder_id"], "participated": bool(row["participated"]),
            "created_by_me": bool(row["created_by_me"]),
            "parent_session_id": row["parent_run_id"] or None, "root_session_id": row["root_id"],
            "side_chat_of": row["side_chat_of"] or None, "url": "/#run=" + row["id"],
            "source_format": SOURCE_FORMAT, "execution_engine": "legacy:" + row["harness"],
            "read_only": True, "capabilities": dict(CAPABILITIES),
            "unread": None, "unread_source": "browser-local",
            "read_marker_key": f"moyai-session-read:{principal.id or 'local'}:{row['id']}",
            "completion_marker": row["updated_at"] if row["status"] in {"idle", "completed"} else "",
        }

    async def allowed(
        self, conn: AsyncConnection | AsyncSession, principal: Principal, run_id: str,
    ) -> dict[str, Any]:
        session_id(run_id)
        result = await conn.execute(text(
            self.access_cte() + f"SELECT {self.columns()} {self.from_runs()} WHERE r.id=:id"
        ), self.params(principal) | {"id": run_id})
        row = result.mappings().first()
        if row is None or not self.privacy.can_access(row, principal.id if principal.individual else ""):
            raise HTTPException(404, "Session not found.")
        guard = getattr(self, "continuation_access", None)
        if guard is not None and run_id not in await guard(conn, principal, [run_id]):
            raise HTTPException(404, "Session not found.")
        return dict(row)

    async def recheck(self, principal: Principal, ids: list[str], *, deleted: bool = False) -> set[str]:
        current = await self.identity.require_current(principal.grant)
        if not ids:
            return set()
        async with self.read() as conn:
            query = text(self.access_cte(deleted=deleted) + "SELECT id FROM visible WHERE id IN :ids")
            result = await conn.execute(query.bindparams(bindparam("ids", expanding=True)),
                                        self.params(current) | {"ids": ids})
            valid = set(result.scalars())
            guard = getattr(self, "continuation_access", None)
            if guard is not None:
                valid &= await guard(conn, current, list(valid))
            return valid

    async def require(self, principal: Principal, run_id: str) -> Principal:
        current = await self.identity.require_current(principal.grant)
        async with self.read() as conn:
            await self.allowed(conn, current, run_id)
        return current

    def navigation_scope(
        self, principal: Principal, *, archived: bool | None = None, personal: bool = False,
    ) -> str:
        if principal.role == "admin" and not personal:
            return "1=1"
        t = self.table
        if archived is True and not personal:
            return f"EXISTS(SELECT 1 FROM {t('session_archives')} a WHERE a.owner_id=:actor AND a.run_id=v.root_id)"
        return f"""(v.root_id IN (SELECT id FROM mine)
            OR EXISTS(SELECT 1 FROM {t('session_pins')} p WHERE p.owner_id=:actor AND p.run_id=v.root_id)
            OR EXISTS(SELECT 1 FROM {t('session_folder_memberships')} f
                WHERE f.owner_id=:actor AND f.run_id=v.root_id)
            {f"OR EXISTS(SELECT 1 FROM {t('session_archives')} a WHERE a.owner_id=:actor AND a.run_id=v.root_id)"
              if archived is None else ""})"""

    async def sessions(
        self, principal: Principal, *, limit: int = 50, offset: int = 0,
        archived: bool = False, deleted: bool = False, exclude_continued: bool = False,
        personal: bool = False,
    ) -> dict[str, Any]:
        bounded(limit, offset)
        current = await self.identity.require_current(principal.grant)
        t = self.table
        where = "r.parent_run_id='' AND "
        if deleted:
            where += "r.deleted_at!='' AND (:is_admin=1 OR r.owner_id=:actor)"
        else:
            where += self.navigation_scope(current, archived=archived, personal=personal)
            where += f" AND {' ' if archived else 'NOT '}EXISTS(SELECT 1 FROM {t('session_archives')} a " \
                     "WHERE a.owner_id=:actor AND a.run_id=r.id)"
        if exclude_continued and self.continuation_schema:
            where += f""" AND NOT EXISTS(SELECT 1 FROM "{self.continuation_schema}".moyai_product_continuations c
                WHERE c.source_schema=:continuation_source AND c.source_session_id=r.id)"""
        async with self.read() as conn:
            result = await conn.execute(text(
                self.access_cte(deleted=deleted) + f"SELECT {self.columns()} {self.from_runs()} WHERE {where} "
                "ORDER BY r.updated_at DESC,r.created_at DESC,r.id DESC LIMIT :limit OFFSET :offset"
            ), self.params(current) | {"limit": limit + 1, "offset": offset, "is_admin": int(current.role == "admin"),
                                      "continuation_source": self.schema})
            rows = [dict(r) for r in result.mappings()]
        valid = await self.recheck(current, [r["id"] for r in rows], deleted=deleted)
        return {"sessions": [self.view(r, current) for r in rows[:limit] if r["id"] in valid],
                "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None,
                "source_format": SOURCE_FORMAT}

    async def detail(self, principal: Principal, run_id: str) -> dict[str, Any]:
        current = await self.identity.require_current(principal.grant)
        async with self.read() as conn:
            row = await self.allowed(conn, current, run_id)
        if run_id not in await self.recheck(current, [run_id]):
            raise HTTPException(404, "Session not found.")
        return {"session": self.view(row, current), "turns": [], "effects": [],
                "has_unsettled_effects": False, "continuation_supported": False}

    async def children(
        self, principal: Principal, run_id: str, *, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        bounded(limit, offset)
        current = await self.identity.require_current(principal.grant)
        async with self.read() as conn:
            await self.allowed(conn, current, run_id)
            rows = [dict(r) for r in (await conn.execute(text(
                self.access_cte() + f"SELECT {self.columns()} {self.from_runs()} "
                "WHERE r.parent_run_id=:id ORDER BY r.created_at,r.id LIMIT :limit OFFSET :offset"
            ), self.params(current) | {"id": run_id, "limit": limit + 1, "offset": offset})).mappings()]
        valid = await self.recheck(current, [run_id, *[r["id"] for r in rows]])
        if run_id not in valid:
            raise HTTPException(404, "Session not found.")
        return {"sessions": [self.view(r, current) for r in rows[:limit] if r["id"] in valid],
                "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None}

    async def folders(self, principal: Principal, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        bounded(limit, offset)
        current = await self.identity.require_current(principal.grant)
        async with self.read() as conn:
            rows = [dict(r) for r in (await conn.execute(text(
                f"SELECT id,substr(name,1,200) AS name,revision FROM {self.table('session_folders')} "
                "WHERE owner_id=:actor ORDER BY name,id LIMIT :limit OFFSET :offset"
            ), {"actor": current.id, "limit": limit + 1, "offset": offset})).mappings()]
        await self.identity.require_current(current.grant)
        return {"folders": rows[:limit], "has_more": len(rows) > limit,
                "next_offset": offset + limit if len(rows) > limit else None, "read_only": True}

    async def messages(
        self, principal: Principal, run_id: str, *, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        bounded(limit, offset)
        current = await self.identity.require_current(principal.grant)
        t = self.table
        async with self.read() as conn:
            await self.allowed(conn, current, run_id)
            rows = [dict(r) for r in (await conn.execute(text(f"""
                SELECT m.id,m.role,substr(m.content,1,:chars) AS content,
                    length(m.content)>:chars AS content_truncated,
                    m.status,m.created_at,m.started_at,m.user_id AS author_id,
                    m.response_to_id,m.model,m.revision,substr(u.name,1,200) AS author_name
                FROM {t('messages')} m LEFT JOIN {t('users')} u ON u.id=m.user_id
                WHERE m.run_id=:id AND m.status!='deleted' AND m.role IN ('user','assistant')
                ORDER BY CASE WHEN m.role='user' AND m.started_at='' AND m.status='queued' THEN 1 ELSE 0 END,
                    COALESCE(NULLIF(m.started_at,''),m.created_at),m.id LIMIT :limit OFFSET :offset
            """), {"id": run_id, "chars": MAX_MESSAGE_CHARS, "limit": limit + 1, "offset": offset})).mappings()]
            # One bounded query for the whole message page; draft uploads are never visible.
            ids = [r["id"] for r in rows[:limit]]
            attachments: dict[int, list[dict[str, Any]]] = {}
            if ids:
                from .product_files import attachment_view
                query = text(f"""SELECT a.id,a.message_id,a.name,a.media_type,a.size,a.sha256,a.created_at
                    FROM {t('attachments')} a WHERE a.message_id IN :ids AND a.upload_state='ready'
                    ORDER BY a.message_id,a.id LIMIT 501""").bindparams(bindparam("ids", expanding=True))
                found = list((await conn.execute(query, {"ids": ids})).mappings())
                for item in found[:500]:
                    attachments.setdefault(item["message_id"], []).append(attachment_view(run_id, dict(item)))
            else:
                found = []
        if run_id not in await self.recheck(current, [run_id]):
            raise HTTPException(404, "Session not found.")
        return {"messages": [
            r | {"session_id": run_id, "turn_id": f"legacy:{r['response_to_id'] or r['id']}",
                 "source_format": SOURCE_FORMAT, "attachments": attachments.get(r["id"], [])}
            for r in rows[:limit]
        ], "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None,
            "content_limit": MAX_MESSAGE_CHARS, "attachments_truncated": len(found) > 500,
            "source_format": SOURCE_FORMAT}

    async def search(
        self, principal: Principal, query: str, *, limit: int = 20, offset: int = 0,
        exclude_continued: bool = False, continued_only: bool = False,
    ) -> dict[str, Any]:
        bounded(limit, offset)
        query = query.strip()
        if not 1 <= len(query) <= 200:
            raise HTTPException(422, "Search text must contain 1–200 characters.")
        current = await self.identity.require_current(principal.grant)
        needle = "%" + query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        t = self.table
        continuation_filter = ""
        if self.continuation_schema and (exclude_continued or continued_only):
            continuation_filter = f""" AND {"NOT " if exclude_continued else ""}EXISTS(
                SELECT 1 FROM "{self.continuation_schema}".moyai_product_continuations c
                WHERE c.source_schema=:continuation_source AND c.source_session_id=r.id)"""
        # Permission filter precedes matching, including for child messages.
        # Text prefixes and response pages are bounded; PostgreSQL also has a
        # statement timeout. Search never reads working/checkpoint/tool context.
        async with self.read() as conn:
            rows = [dict(r) for r in (await conn.execute(text(
                self.access_cte() + f""", matches AS (
                    SELECT m.run_id,min(m.id) AS message_id FROM {t('messages')} m
                    JOIN visible visible_message ON visible_message.id=m.run_id
                    WHERE m.role IN ('user','assistant') AND m.status!='deleted'
                      AND lower(substr(m.content,1,:chars)) LIKE :needle ESCAPE '\\'
                    GROUP BY m.run_id
                ) SELECT {self.columns()},matches.message_id,
                    substr(m.content,1,320) AS snippet
                {self.from_runs()} LEFT JOIN matches ON matches.run_id=r.id
                LEFT JOIN {t('messages')} m ON m.id=matches.message_id
                WHERE {self.navigation_scope(current)} {continuation_filter}
                  AND (matches.message_id IS NOT NULL
                    OR lower(substr(r.display_title,1,300)||' '||substr(r.agent_label,1,300)
                        ||' '||substr(r.prompt,1,300)) LIKE :needle ESCAPE '\\')
                ORDER BY r.updated_at DESC,r.id DESC LIMIT :limit OFFSET :offset"""
            ), self.params(current) | {
                "chars": MAX_SEARCH_CHARS, "needle": needle, "limit": limit + 1, "offset": offset,
                "continuation_source": self.schema,
            })).mappings()]
        valid = await self.recheck(current, [r["id"] for r in rows])
        return {"sessions": [
            self.view(r, current) | {"match": {"message_id": r["message_id"], "snippet": r["snippet"] or ""}}
            for r in rows[:limit] if r["id"] in valid
        ], "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None,
            "source_format": SOURCE_FORMAT, "searched_message_prefix_chars": MAX_SEARCH_CHARS}


def routes(adapter: ProductHistoryAdapter) -> APIRouter:
    router = APIRouter(prefix="/api/product")

    async def actor(request: Request, response: Response) -> Principal:
        response.headers["Cache-Control"] = "no-store"
        return await adapter.identity.authenticate(request)

    @router.get("/sessions")
    async def sessions(
        request: Request, response: Response, limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET), archived: bool = False, deleted: bool = False,
    ):
        return await adapter.sessions(await actor(request, response), limit=limit, offset=offset,
                                      archived=archived, deleted=deleted)

    @router.get("/search")
    async def search(
        request: Request, response: Response, q: str = Query(min_length=1, max_length=200),
        limit: int = Query(20, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ):
        return await adapter.search(await actor(request, response), q, limit=limit, offset=offset)

    @router.get("/folders")
    async def folders(
        request: Request, response: Response, limit: int = Query(100, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ):
        return await adapter.folders(await actor(request, response), limit=limit, offset=offset)

    @router.get("/sessions/{run_id}")
    async def detail(run_id: str, request: Request, response: Response):
        return await adapter.detail(await actor(request, response), run_id)

    @router.get("/sessions/{run_id}/messages")
    async def messages(
        run_id: str, request: Request, response: Response, limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ):
        return await adapter.messages(await actor(request, response), run_id, limit=limit, offset=offset)

    @router.get("/sessions/{run_id}/children")
    async def children(
        run_id: str, request: Request, response: Response, limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET),
    ):
        return await adapter.children(await actor(request, response), run_id, limit=limit, offset=offset)

    return router
