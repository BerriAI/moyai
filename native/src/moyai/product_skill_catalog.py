"""Read and invoke the canonical skill library without changing its source records."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from .product_identity import ProductGrant, ProductIdentityAdapter, ProductPrincipal

NOTE = "Select a skill with / in chat. The original library, revisions and supporting files are retained."
CAPABILITIES = {"read": True, "create": False, "update": False, "delete": False, "load": True}
METADATA = ("id", "name", "description", "scope", "icon", "revision", "archived", "created_at", "updated_at")


class ProductSkillCatalog:
    """SELECT-only reads with live product authority and a fresh release-boundary check."""

    def __init__(self, identity: ProductIdentityAdapter):
        self.identity = identity

    def _table(self, name: str) -> str:
        return self.identity._table(name)

    @staticmethod
    def _visible(row: dict[str, Any], principal: ProductPrincipal) -> bool:
        return row["scope"] == "organization" or (
            row["scope"] == "personal" and row["owner_id"] == principal.id
        )

    @staticmethod
    def _metadata(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{key: row[key] for key in METADATA},
            "archived": bool(row["archived"]),
            "reference": ("org:" if row["scope"] == "organization" else "personal:") + row["name"],
            "can_manage": False,
            "source": "canonical",
        }

    async def _row(self, grant: ProductGrant, skill_id: str) -> tuple[ProductPrincipal, dict[str, Any]]:
        if not re.fullmatch(r"[0-9a-f]{32}", skill_id):
            raise HTTPException(404, "Skill not found.")
        principal = await self.identity.require_current(grant)
        try:
            async with self.identity._reader(self.identity.source_engine) as db:
                row = (await db.execute(text(
                    f"SELECT * FROM {self._table('skills')} WHERE id=:id"
                ), {"id": skill_id})).mappings().first()
        except SQLAlchemyError:
            raise HTTPException(503, "The canonical skill library is unavailable.") from None
        if row is None or not self._visible(dict(row), principal):
            raise HTTPException(404, "Skill not found.")
        return principal, dict(row)

    async def _release(self, grant: ProductGrant, original: dict[str, Any]) -> None:
        _, fresh = await self._row(grant, original["id"])
        if any(fresh[key] != original[key] for key in (*METADATA, "owner_id", "encrypted")):
            raise HTTPException(409, "The skill changed while it was being read. Refresh its revision.")

    async def listing(
        self, grant: ProductGrant, *, archived: bool = False, offset: int = 0, limit: int = 100,
        shared: bool = False, name: str | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
            raise HTTPException(422, "Use a bounded skill page.")
        principal = await self.identity.require_current(grant)
        active = "" if archived else "AND archived=0"
        filters = (" AND scope='organization'" if shared else "") + (" AND name=:name" if name else "")
        try:
            async with self.identity._reader(self.identity.source_engine) as db:
                rows = [dict(row) for row in (await db.execute(text(
                    f"SELECT * FROM {self._table('skills')} WHERE "
                    "(scope='organization' OR (scope='personal' AND owner_id=:owner)) "
                    f"{active}{filters} ORDER BY scope,name,id LIMIT :limit OFFSET :offset"
                ), {"owner": principal.id, "limit": limit + 1, "offset": offset, "name": name})).mappings()]
            principal = await self.identity.require_current(grant)
            # Scope/archive changes during an await must not release stale metadata.
            async with self.identity._reader(self.identity.source_engine) as db:
                fresh = {
                    row["id"]: dict(row) for row in (await db.execute(text(
                        f"SELECT * FROM {self._table('skills')} WHERE id IN :ids"
                    ).bindparams(bindparam("ids", expanding=True)), {"ids": [row["id"] for row in rows[:limit]]}
                    )).mappings()
                } if rows else {}
            selected = [
                self._metadata(row) for row in rows[:limit]
                if row["id"] in fresh and fresh[row["id"]] == row
                and self._visible(row, principal) and (archived or not row["archived"])
            ]
        except SQLAlchemyError:
            raise HTTPException(503, "The canonical skill library is unavailable.") from None
        await self.identity.require_current(grant)
        return {
            "skills": selected, "can_publish": False, "capabilities": CAPABILITIES,
            "source": "canonical", "note": NOTE, "has_more": len(rows) > limit,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    async def invocation(self, grant: ProductGrant, reference: str, *, shared: bool) -> dict[str, Any]:
        """Resolve a short or scoped name, then recheck the complete revision before release."""
        name = reference.split(":", 1)[-1]
        listing = await self.listing(grant, shared=shared, name=name)
        matches = [row for row in listing["skills"] if ":" not in reference or row["reference"] == reference]
        if len(matches) != 1:
            message = "Choose a scoped skill from the / picker." if matches else "Choose an available skill from the / picker."
            raise HTTPException(422, f"Cannot invoke /{reference}. {message}")
        _, row = await self._row(grant, matches[0]["id"])
        if any(row[key] != matches[0][key] for key in METADATA):
            raise HTTPException(409, "The selected skill changed. Select it again.")
        if row["archived"] or (shared and row["scope"] != "organization"):
            raise HTTPException(403, "This skill is not available in this conversation.")
        instructions = self._decrypt(row["encrypted"])
        _, files = await self._bundle(row)
        await self._release(grant, row)
        return {**self._metadata(row), "instructions": instructions, "supporting_files": files}

    def _decrypt(self, encrypted: str) -> str:
        try:
            return self.identity.legacy_security.decrypt(encrypted)
        except (InvalidToken, ValueError, TypeError):
            # No native-codec fallback; source format is explicit.
            raise HTTPException(503, "This canonical skill could not be decoded with the configured source key.") from None

    async def _bundle(self, row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
        try:
            async with self.identity._reader(self.identity.source_engine) as db:
                bundle = (await db.execute(text(
                    f"SELECT * FROM {self._table('skill_bundles')} "
                    "WHERE skill_id=:id AND revision<=:revision ORDER BY revision DESC LIMIT 1"
                ), {"id": row["id"], "revision": row["revision"]})).mappings().first()
        except SQLAlchemyError:
            raise HTTPException(503, "The canonical supporting-file library is unavailable.") from None
        if bundle is None:
            return [], {}
        try:
            files = json.loads(self._decrypt(bundle["encrypted"]))
            manifest = json.loads(bundle["manifest"])
            if not isinstance(files, dict) or not isinstance(manifest, list):
                raise TypeError()
            expected = []
            for path, content in sorted(files.items()):
                self._path(path)
                if not isinstance(content, str) or "\x00" in content:
                    raise ValueError()
                expected.append({
                    "path": path, "size": len(content.encode()), "characters": len(content),
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                })
            if manifest != expected:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise HTTPException(409, "The canonical supporting-file manifest requires review.") from None
        return manifest, files

    @staticmethod
    def _path(path: str) -> None:
        if (
            not isinstance(path, str) or len(path) > 200 or not re.fullmatch(r"[A-Za-z0-9_.\-/]+", path)
            or any(part in {"", ".", ".."} or part.startswith(".") for part in path.split("/"))
        ):
            raise HTTPException(404, "Supporting file not found.")

    async def detail(
        self, grant: ProductGrant, skill_id: str, *, revision: int | None = None,
    ) -> dict[str, Any]:
        _, row = await self._row(grant, skill_id)
        if revision is not None and revision != row["revision"]:
            raise HTTPException(409, "The skill revision changed.")
        instructions = self._decrypt(row["encrypted"])
        manifest, _ = await self._bundle(row)
        await self._release(grant, row)
        return {
            **self._metadata(row), "instructions": instructions, "files": manifest,
            "capabilities": CAPABILITIES, "note": NOTE,
        }

    async def file(
        self, grant: ProductGrant, skill_id: str, path: str, *, revision: int | None = None,
    ) -> str:
        self._path(path)
        _, row = await self._row(grant, skill_id)
        if revision is not None and revision != row["revision"]:
            raise HTTPException(409, "The skill revision changed.")
        _, files = await self._bundle(row)
        if path not in files:
            raise HTTPException(404, "Supporting file not found.")
        await self._release(grant, row)
        return files[path]


def routes(catalog: ProductSkillCatalog) -> APIRouter:
    router = APIRouter(prefix="/api/product/skills")

    @router.get("")
    async def listing(
        request: Request, archived: bool = False,
        offset: int = Query(0, ge=0, le=10000), limit: int = Query(100, ge=1, le=100),
    ):
        principal = await catalog.identity.authenticate(request)
        return await catalog.listing(principal.grant, archived=archived, offset=offset, limit=limit)

    @router.get("/{skill_id}")
    async def detail(skill_id: str, request: Request, revision: int | None = Query(None, ge=1)):
        principal = await catalog.identity.authenticate(request)
        return await catalog.detail(principal.grant, skill_id, revision=revision)

    @router.get("/{skill_id}/files/{path:path}")
    async def file(skill_id: str, path: str, request: Request, revision: int | None = Query(None, ge=1)):
        principal = await catalog.identity.authenticate(request)
        content = await catalog.file(principal.grant, skill_id, path, revision=revision)
        return Response(content, media_type="text/plain", headers={
            "Content-Disposition": "attachment", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

    return router
