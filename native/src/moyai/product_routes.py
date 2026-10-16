"""Source-qualified reads and explicit fenced continuation for Moyai.

Mount ``routes(runtime, actor)`` after Runtime supplies product_identity,
product_history and product_files. This includes their /api/product routes and
the authenticated original ZIP alias; do not mount those routers twice.
Standalone mode exposes the same /api/history contract with native rows only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, case, column, false, func, or_, select, table
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from .models import Message, Session, SessionPreference, SessionRead, Turn, User
from .product_files import routes as file_routes
from .product_history import MAX_MESSAGE_CHARS, MAX_OFFSET, MAX_PAGE, Principal, bounded
from .product_history import routes as legacy_routes
from .swarm_models import SwarmMember

if TYPE_CHECKING:
    from .runtime import Runtime

Source = Literal["native", "legacy"]
Sources = Literal["all", "native", "legacy"]
NavigationScope = Literal["all", "mine"]
SEARCH_ROWS = 1000
SEARCH_BYTES = 4 * 1024 * 1024
CIPHER_CHARS = 180_000


def sourced(row: dict[str, Any], source: Source) -> dict[str, Any]:
    """Do not rewrite stored IDs. UI identity is the separate source-qualified key."""
    return row | {
        "source": source, "key": f"{source}:{row['id']}",
        "url": "/#run=" + quote(row.get("source_session_id") or row["id"], safe="")
        if source == "legacy" or row.get("source_session_id") else "/#" + quote(row["id"], safe=""),
    }


def merge_session_lists(
    native: list[dict[str, Any]], legacy: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge already-authorized pages, retaining collisions across sources."""
    found: dict[str, dict[str, Any]] = {}
    for source, rows in (("native", native), ("legacy", legacy)):
        for row in rows:
            item = sourced(row, source)  # type: ignore[arg-type]
            prior = found.get(item["key"])
            if prior is None or str(item.get("updated_at", "")) > str(prior.get("updated_at", "")):
                found[item["key"]] = item
    return sorted(found.values(), key=lambda row: (str(row.get("updated_at", "")), row["id"], row["source"]),
                  reverse=True)


@dataclass(frozen=True)
class Reader:
    user: User
    principal: Principal | None = None


class ProductReadService:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self.history = getattr(runtime, "product_history", None)
        self.files = getattr(runtime, "product_files", None)
        self.identity = getattr(runtime, "product_identity", None) or (
            self.history.identity if self.history is not None else None
        )
        self.continuations = getattr(runtime, "product_continuations", None)

    async def authenticate(self, request: Request, user: User) -> Reader:
        principal = await self.identity.authenticate(request) if self.identity else None
        if principal is not None and principal.id != user.id:
            raise HTTPException(403, "Product and native identities must refer to the same account.")
        reader = Reader(user, principal)
        await self.current(reader)
        return reader

    async def current(self, reader: Reader) -> None:
        if self.identity:
            if reader.principal is None:
                raise HTTPException(401, "Current product authorization is required.")
            await self.identity.require_current(reader.principal.grant)
        current = await self.runtime.database.user(reader.user.id)
        if current.auth_version != reader.user.auth_version:
            raise HTTPException(401, "Your session expired.")

    @staticmethod
    def visible(reader: Reader, *, deleted: bool = False):
        private = reader.principal is None or reader.principal.individual
        ownership = Session.owner_id == reader.user.id
        access = or_(ownership if private else false(), Session.visibility == "shared")
        clauses = [Session.deleted.is_(deleted), access]
        if deleted:
            clauses.append(ownership)
        else:
            root = aliased(Session)
            membership = select(SwarmMember.session_id).where(SwarmMember.session_id == Session.id)
            parent_access = or_(
                root.owner_id == reader.user.id if private else false(), root.visibility == "shared",
            )
            visible_parent = select(SwarmMember.session_id).join(
                root, root.id == SwarmMember.mission_id,
            ).where(SwarmMember.session_id == Session.id, root.deleted.is_(False), parent_access)
            clauses.append(~membership.exists() | visible_parent.exists())
        return and_(*clauses)

    def native_query(self, reader: Reader):
        preference = and_(SessionPreference.session_id == Session.id, SessionPreference.user_id == reader.user.id)
        read = and_(SessionRead.session_id == Session.id, SessionRead.user_id == reader.user.id)
        participated = select(Turn.id).where(
            Turn.session_id == Session.id, Turn.actor_id == reader.user.id,
        ).exists()
        unread = select(Message.id).where(
            Message.session_id == Session.id, Message.id > SessionRead.message_id,
        ).exists()
        query = select(
            Session.id, Session.title, Session.owner_id, Session.visibility, Session.surface, Session.model,
            Session.status, Session.active_turn, Session.archived, Session.deleted, Session.created_at,
            Session.updated_at, SessionPreference.pinned, SessionPreference.folder,
            SessionRead.unread.label("marked_unread"), unread.label("new_messages"),
            (Session.owner_id == reader.user.id).label("created_by_me"),
            (and_(Session.owner_id != reader.user.id, participated)).label("participated"),
        ).outerjoin(SessionPreference, preference).outerjoin(SessionRead, read)
        if self.continuations is not None:
            from .product_continuation_models import ProductContinuation
            query = query.add_columns(ProductContinuation.source_session_id).outerjoin(
                ProductContinuation, and_(
                    ProductContinuation.native_session_id == Session.id,
                    ProductContinuation.source_schema == self.continuations.source_schema,
                ),
            )
            schema = self.continuations.source_schema
            pins = table("session_pins", column("owner_id"), column("run_id"), schema=schema)
            memberships = table(
                "session_folder_memberships", column("owner_id"), column("run_id"), column("folder_id"), schema=schema,
            )
            folders = table("session_folders", column("id"), column("owner_id"), column("name"), schema=schema)
            old_messages = table("messages", column("run_id"), column("role"), column("user_id"), schema=schema)
            roots = table("run_roots", column("run_id"), column("root_id"), schema=schema)
            users = table("users", column("id"), column("kind"), column("linked_user_id"), schema=schema)
            original = ProductContinuation.source_session_id
            old_pin = select(pins.c.run_id).where(
                pins.c.run_id == original, pins.c.owner_id == reader.user.id,
            ).exists()
            old_folder = select(func.substr(folders.c.name, 1, 200)).join(
                memberships, and_(memberships.c.folder_id == folders.c.id,
                                  memberships.c.owner_id == folders.c.owner_id),
            ).where(memberships.c.run_id == original, memberships.c.owner_id == reader.user.id).limit(1).scalar_subquery()
            linked = select(users.c.id).where(users.c.kind == "slack", users.c.linked_user_id == reader.user.id)
            owned = or_(Session.owner_id == reader.user.id, Session.owner_id.in_(linked))
            old_participation = select(old_messages.c.run_id).join(
                roots, roots.c.run_id == old_messages.c.run_id,
            ).where(
                roots.c.root_id == original, old_messages.c.role == "user",
                or_(old_messages.c.user_id == reader.user.id, old_messages.c.user_id.in_(linked)),
            ).exists()
            # Preserve each viewer's saved navigation without copying another
            # person's preferences. An explicit native preference overrides it.
            query = query.with_only_columns(
                *(item for item in query.selected_columns
                  if item.key not in {"pinned", "folder", "created_by_me", "participated"}),
                case((SessionPreference.user_id.is_not(None), SessionPreference.pinned),
                     else_=old_pin).label("pinned"),
                case((SessionPreference.user_id.is_not(None), SessionPreference.folder),
                     else_=old_folder).label("folder"),
                owned.label("created_by_me"),
                and_(~owned, or_(participated, old_participation)).label("participated"),
            )
        return query

    @staticmethod
    def native_view(row: Any) -> dict[str, Any]:
        view = dict(row)
        marked = view.pop("marked_unread")
        new_messages = view.pop("new_messages")
        view.update({
            "source_format": "moyai-native-v1", "execution_engine": "pydantic-v1",
            "read_only": False, "pinned": bool(view["pinned"]), "folder": view["folder"] or "",
            "unread": bool(marked or new_messages), "workspace": None, "save_warning": None,
            "parent_session_id": None, "capabilities": {
                "read": not view["deleted"], "continue": not view["deleted"],
                "rename": not view["deleted"], "move": not view["deleted"], "pin": not view["deleted"],
            },
        })
        return sourced(view, "native")

    async def native_recheck(self, reader: Reader, ids: list[str], *, deleted: bool = False) -> set[str]:
        await self.current(reader)
        mapped: dict[str, str] = {}
        async with self.runtime.database.sessions() as db:
            valid = set(await db.scalars(select(Session.id).where(self.visible(reader, deleted=deleted),
                                                                Session.id.in_(ids))))
            if self.continuations and self.history and reader.principal and not deleted:
                from .product_continuation_models import ProductContinuation
                mapped = dict((await db.execute(select(
                    ProductContinuation.native_session_id, ProductContinuation.source_session_id,
                ).where(
                    ProductContinuation.source_schema == self.continuations.source_schema,
                    ProductContinuation.native_session_id.in_(valid),
                ))).all())
        # Canonical revalidation reads native controls through its own connection.
        # Release this snapshot before it can borrow from the same bounded pool.
        if self.continuations and self.history and reader.principal and not deleted:
            source_allowed = await self.history.recheck(reader.principal, list(mapped.values()))
            valid -= {native for native, source in mapped.items() if source not in source_allowed}
        return valid

    async def native_list(
        self, reader: Reader, *, limit: int, offset: int, archived: bool = False, deleted: bool = False,
        scope: NavigationScope = "all",
    ) -> dict[str, Any]:
        async with self.runtime.database.sessions() as db:
            query = self.native_query(reader).where(self.visible(reader, deleted=deleted))
            if not deleted:
                query = query.where(
                    Session.archived.is_(archived),
                    ~select(SwarmMember.session_id).where(
                        SwarmMember.session_id == Session.id, SwarmMember.position > 0,
                    ).exists(),
                )
                if scope == "mine":
                    columns = query.selected_columns
                    query = query.where(or_(
                        columns.created_by_me, columns.participated, columns.pinned.is_(True),
                        func.coalesce(columns.folder, "") != "",
                    ))
            rows = list((await db.execute(query.order_by(Session.updated_at.desc(), Session.id.desc())
                                          .offset(offset).limit(limit + 1))).mappings())
        valid = await self.native_recheck(reader, [r["id"] for r in rows], deleted=deleted)
        return {"sessions": [self.native_view(r) for r in rows[:limit] if r["id"] in valid],
                "has_more": len(rows) > limit, "next_offset": offset + limit if len(rows) > limit else None}

    async def native_search(self, reader: Reader, query: str, *, limit: int, offset: int) -> dict[str, Any]:
        """Ciphertext is searched only after ACL filtering and within explicit budgets."""
        candidates: dict[str, dict[str, Any]] = {}
        matches: dict[str, dict[str, Any]] = {}
        scanned, consumed, limited = 0, 0, False
        async with self.runtime.database.sessions() as db:
            title_rows = (await db.execute(self.native_query(reader).where(
                self.visible(reader), func.lower(Session.title).contains(query.lower(), autoescape=True),
            ).order_by(Session.updated_at.desc(), Session.id.desc()).limit(offset + limit + 1))).mappings()
            candidates.update({r["id"]: self.native_view(r) for r in title_rows})
            # Never select encrypted native working snapshots or another user's private messages.
            stream = await db.stream(select(
                Message.id, Message.session_id, func.substr(Message.content, 1, CIPHER_CHARS + 1).label("content"),
            ).join(Session, Session.id == Message.session_id).where(
                self.visible(reader), Message.role.in_(("user", "assistant")),
            ).order_by(Message.id.desc()).limit(SEARCH_ROWS + 1).execution_options(yield_per=16))
            try:
                async for row in stream.mappings():
                    scanned += 1
                    consumed += len(row["content"])
                    if scanned > SEARCH_ROWS or consumed > SEARCH_BYTES:
                        limited = True
                        break
                    if len(row["content"]) > CIPHER_CHARS:
                        limited = True
                        continue
                    try:
                        content = self.runtime.security.unseal(row["content"]).decode("utf-8")
                    except (InvalidToken, UnicodeError, ValueError):
                        limited = True
                        continue
                    if query.casefold() in content.casefold():
                        matches.setdefault(row["session_id"], {
                            "message_id": row["id"], "snippet": content[:320],
                        })
            finally:
                await stream.close()
            if matches:
                rows = (await db.execute(self.native_query(reader).where(
                    self.visible(reader), Session.id.in_(matches),
                ))).mappings()
                candidates.update({r["id"]: self.native_view(r) | {"match": matches[r["id"]]} for r in rows})
        historical_limited = False
        if self.continuations and self.history and reader.principal:
            from .product_continuation_models import ProductContinuation
            earlier = await self.history.search(reader.principal, query, limit=MAX_PAGE, continued_only=True)
            historical_limited = earlier["has_more"]
            async with self.runtime.database.sessions() as db:
                mapping = dict((await db.execute(select(
                    ProductContinuation.native_session_id, ProductContinuation.source_session_id,
                ).where(
                    ProductContinuation.source_schema == self.continuations.source_schema,
                    ProductContinuation.source_session_id.in_([row["id"] for row in earlier["sessions"]]),
                ))).all())
                old_rows = {row["id"]: row for row in earlier["sessions"]}
                native_rows = (await db.execute(self.native_query(reader).where(
                    self.visible(reader), Session.id.in_(mapping),
                ))).mappings()
                for row in native_rows:
                    candidates.setdefault(row["id"], self.native_view(row) | {
                        "match": old_rows[mapping[row["id"]]].get("match"),
                    })
        ordered = merge_session_lists(list(candidates.values()), [])
        page = ordered[offset:offset + limit]
        valid = await self.native_recheck(reader, [r["id"] for r in page])
        return {
            "sessions": [r for r in page if r["id"] in valid], "has_more": len(ordered) > offset + limit,
            "next_offset": offset + limit if len(ordered) > offset + limit else None,
            "search_limited": limited or historical_limited,
            "notice": "Message search is limited to 1,000 native messages / 4 MiB and 100 matching continued "
                      "histories per query. Narrow the search for older matches."
            if limited or historical_limited else None,
        }

    @staticmethod
    def cursor_scope(
        reader: Reader, source: Sources, query: str, archived: bool, deleted: bool,
        scope: NavigationScope = "all",
    ) -> str:
        return hashlib.sha256(json.dumps([reader.user.id, source, query, archived, deleted, scope]).encode()).hexdigest()

    @staticmethod
    def offsets(cursor: str | None, scope: str) -> dict[str, int]:
        if cursor is None:
            return {"native": 0, "legacy": 0}
        try:
            if len(cursor) > 512:
                raise ValueError
            data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            if not isinstance(data, dict) or data.get("scope") != scope or data.get("v") != 1:
                raise ValueError
            positions = {key: data[key] for key in ("native", "legacy")}
            if any(type(value) is not int or not 0 <= value <= MAX_OFFSET for value in positions.values()):
                raise ValueError
            return positions
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "Invalid history cursor. Restart the search.") from None

    async def listing(
        self, reader: Reader, *, source: Sources = "all", query: str = "", limit: int = 50,
        cursor: str | None = None, archived: bool = False, deleted: bool = False,
        scope: NavigationScope = "all",
    ) -> dict[str, Any]:
        bounded(limit)
        query = query.strip()
        if len(query) > 200:
            raise HTTPException(422, "Search text must contain at most 200 characters.")
        await self.current(reader)
        cursor_scope = self.cursor_scope(reader, source, query, archived, deleted, scope)
        offsets = self.offsets(cursor, cursor_scope)
        pages: dict[str, dict[str, Any]] = {}
        notices, states = [], {}
        for selected in (("native", "legacy") if source == "all" else (source,)):
            if selected == "legacy" and self.history is None:
                states[selected] = "disabled"
                if source == "legacy":
                    raise HTTPException(503, "Existing product history is not configured.")
                continue
            try:
                async with asyncio.timeout(10):
                    if selected == "native":
                        page = await self.native_search(reader, query, limit=limit, offset=offsets[selected]) if query else \
                            await self.native_list(reader, limit=limit, offset=offsets[selected],
                                                   archived=archived, deleted=deleted, scope=scope)
                    else:
                        if reader.principal is None:
                            raise HTTPException(401, "Current product authorization is required.")
                        if self.history is None:
                            raise HTTPException(503, "Existing product history is not configured.")
                        page = await self.history.search(
                            reader.principal, query, limit=limit, offset=offsets[selected],
                            **({"exclude_continued": True} if source == "all" and self.continuations else {}),
                        ) \
                            if query else await self.history.sessions(
                                reader.principal, limit=limit, offset=offsets[selected], archived=archived, deleted=deleted,
                                 personal=scope == "mine",
                                **({"exclude_continued": True} if source == "all" and self.continuations else {}),
                            )
                pages[selected], states[selected] = page, "available"
                if page.get("notice"):
                    notices.append(page["notice"])
            except (HTTPException, SQLAlchemyError, TimeoutError) as exc:
                if isinstance(exc, HTTPException) and exc.status_code < 500:
                    raise
                if source != "all":
                    raise HTTPException(503, "Session history is temporarily unavailable.") from None
                states[selected] = "unavailable"
                notices.append(f"{selected.capitalize()} history is temporarily unavailable. Refresh to retry.")
        # Current authority fences partial results too. A source failure never downgrades ACL.
        await self.current(reader)
        if "native" in pages:
            page = pages["native"]
            valid = await self.native_recheck(reader, [r["id"] for r in page["sessions"]], deleted=deleted)
            page["sessions"] = [row for row in page["sessions"] if row["id"] in valid]
        if "legacy" in pages:
            if self.history is None:
                raise HTTPException(503, "Existing product history is not configured.")
            page = pages["legacy"]
            valid = await self.history.recheck(reader.principal, [r["id"] for r in page["sessions"]],
                                               deleted=deleted)
            page["sessions"] = [row for row in page["sessions"] if row["id"] in valid]
        merged = merge_session_lists(pages.get("native", {}).get("sessions", []),
                                     pages.get("legacy", {}).get("sessions", []))
        chosen = merged[:limit]
        has_more = len(merged) > limit or any(page["has_more"] for page in pages.values())
        for selected, page in pages.items():
            consumed = sum(row["source"] == selected for row in chosen)
            offsets[selected] += consumed
            if not page["sessions"] and page["has_more"]:
                offsets[selected] = page["next_offset"]
        if any(value > MAX_OFFSET for value in offsets.values()):
            has_more = False
            notices.append("History page limit reached. Narrow the search to find more sessions.")
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {"v": 1, "scope": cursor_scope, **offsets}, separators=(",", ":"),
        ).encode()).decode().rstrip("=") if has_more else None
        return {"sessions": chosen, "has_more": has_more, "next_cursor": next_cursor, "sources": states,
                "partial": "unavailable" in states.values(), "notice": " ".join(notices) or None,
                "search_limits": {
                    "native_messages": SEARCH_ROWS, "native_ciphertext_bytes": SEARCH_BYTES,
                    "legacy_message_prefix_chars": pages.get("legacy", {}).get("searched_message_prefix_chars"),
                } if query else None}

    async def detail(self, reader: Reader, run_id: str, source: Source) -> dict[str, Any]:
        await self.current(reader)
        if source == "legacy":
            if self.history is None or reader.principal is None:
                raise HTTPException(503, "Existing product history is not configured.")
            result = await self.history.detail(reader.principal, run_id)
            result["session"] = sourced(result["session"], "legacy")
            if self.continuations is not None and not result["session"].get("parent_session_id"):
                async with self.runtime.database.sessions() as db:
                    binding = await self.continuations.lookup(db, run_id, reader.principal)
                    result["continuation"] = self.continuations.result(binding) if binding else None
                result["continuation_supported"] = True
        else:
            async with self.runtime.database.sessions() as db:
                row = (await db.execute(self.native_query(reader).where(
                    self.visible(reader), Session.id == run_id,
                ))).mappings().first()
            if row is None or run_id not in await self.native_recheck(reader, [run_id]):
                raise HTTPException(404, "Conversation not found.")
            result = {"session": self.native_view(row)}
        await self.current(reader)
        return result

    async def messages(
        self, reader: Reader, run_id: str, source: Source, *, limit: int = 50, offset: int = 0,
    ) -> dict[str, Any]:
        bounded(limit, offset)
        await self.detail(reader, run_id, source)
        if source == "legacy":
            if self.history is None or reader.principal is None:
                raise HTTPException(503, "Existing product history is not configured.")
            result = await self.history.messages(reader.principal, run_id, limit=limit, offset=offset)
        else:
            async with self.runtime.database.sessions() as db:
                rows = list((await db.execute(select(
                    Message.id, Message.turn_id, Message.role, Message.author_id, Message.created_at,
                    func.substr(Message.content, 1, CIPHER_CHARS + 1).label("content"),
                ).where(Message.session_id == run_id, Message.role.in_(("user", "assistant")))
                             .order_by(Message.id).offset(offset).limit(limit + 1))).mappings())
            messages = []
            for row in rows[:limit]:
                item = dict(row)
                if len(item["content"]) > CIPHER_CHARS:
                    item.update(content="", content_unavailable=True)
                else:
                    try:
                        decoded = self.runtime.security.unseal(item["content"]).decode("utf-8")
                    except (InvalidToken, ValueError, UnicodeError):
                        item.update(content="", content_unavailable=True)
                    else:
                        item.update(content=decoded[:MAX_MESSAGE_CHARS],
                                    content_truncated=len(decoded) > MAX_MESSAGE_CHARS)
                messages.append(item)
            result = {"messages": messages, "has_more": len(rows) > limit,
                      "next_offset": offset + limit if len(rows) > limit else None}
        await self.detail(reader, run_id, source)
        return result


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()
    service = ProductReadService(runtime)

    async def reader(request: Request, response: Response, user: User = Depends(actor)) -> Reader:
        response.headers["Cache-Control"] = "no-store"
        return await service.authenticate(request, user)

    @router.get("/api/history/sessions")
    async def sessions(
        source: Sources = "all", limit: int = Query(50, ge=1, le=MAX_PAGE),
        cursor: str | None = Query(None, max_length=512), archived: bool = False, deleted: bool = False,
        scope: NavigationScope = "all",
        current: Reader = Depends(reader),
    ):
        return await service.listing(current, source=source, limit=limit, cursor=cursor,
                                     archived=archived, deleted=deleted, scope=scope)

    @router.get("/api/history/search")
    async def search(
        q: str = Query("", max_length=200), source: Sources = "all", limit: int = Query(20, ge=1, le=MAX_PAGE),
        cursor: str | None = Query(None, max_length=512), current: Reader = Depends(reader),
    ):
        return await service.listing(current, source=source, query=q, limit=limit, cursor=cursor)

    @router.get("/api/history/sessions/{run_id}")
    async def detail(run_id: str, source: Source, current: Reader = Depends(reader)):
        return await service.detail(current, run_id, source)

    @router.get("/api/history/sessions/{run_id}/messages")
    async def messages(
        run_id: str, source: Source, limit: int = Query(50, ge=1, le=MAX_PAGE),
        offset: int = Query(0, ge=0, le=MAX_OFFSET), current: Reader = Depends(reader),
    ):
        return await service.messages(current, run_id, source, limit=limit, offset=offset)

    if service.history is not None:
        router.include_router(legacy_routes(service.history))
    if service.continuations is not None:
        from .product_continuation_routes import continuation_routes
        router.include_router(continuation_routes(runtime, reader))
    if service.files is not None:
        router.include_router(file_routes(service.files))

        @router.get("/api/runs/{run_id}/artifact")
        async def original_archive(
            run_id: str, revision: str = Query("", max_length=64), current: Reader = Depends(reader),
        ):
            if current.principal is None:
                raise HTTPException(401, "Current product authorization is required.")
            if service.files is None:
                raise HTTPException(503, "Existing product files are not configured.")
            return await service.files.original_archive(current.principal, run_id, revision=revision)

    return router
