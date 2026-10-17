"""Agent management of the canonical skill library, with atomic write receipts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Toolset
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import RunScope
from .effects import effect_id
from .models import Effect, Session, now
from .product_automations import ProductAutomationService
from .product_identity import ProductPrincipal
from .product_skill_catalog import ProductSkillCatalog

TOOLSET_ID = "moyai_product_skills_v1"
MUTATIONS = frozenset({"skill_create", "skill_update", "skill_delete"})
READ = {"annotations": {"readOnlyHint": True}}
WRITE = {"annotations": {"readOnlyHint": False}}


class SkillContent(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    instructions: str = Field(min_length=1, max_length=65_536)
    supporting_files: dict[str, str] | None = Field(
        default=None, description="Relative paths to UTF-8 text. Omit to retain existing files; {} removes them.",
    )

    @model_validator(mode="after")
    def valid_content(self):
        if any(not value.strip() or "\x00" in value for value in (self.description, self.instructions)):
            raise ValueError("Description and instructions must be nonblank text without null bytes.")
        files = self.supporting_files
        if files is not None:
            if len(files) > 50 or sum(len(value.encode()) for value in files.values()) > 262_144:
                raise ValueError("Use at most 50 supporting files and 256 KiB of UTF-8 content.")
            for path, value in files.items():
                try:
                    ProductSkillCatalog._path(path)
                except HTTPException:
                    raise ValueError("Use safe relative supporting-file paths.") from None
                if "\x00" in value:
                    raise ValueError("Supporting files must be text without null bytes.")
        return self


class CreateSkill(SkillContent):
    scope: Literal["personal", "organization"] = "personal"


class UpdateSkill(SkillContent):
    skill_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1, description="Current revision returned by skill_read.")
    archived: bool = False


class DeleteSkill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1)


class ProductSkillTools:
    def __init__(self, catalog: ProductSkillCatalog, authority: ProductAutomationService):
        self.catalog, self.authority = catalog, authority
        self.database, self.identity = authority.database, catalog.identity

    async def caller(self, db: AsyncSession, scope: RunScope, run_id: str | None) -> ProductPrincipal:
        # Reuse the product's existing persisted human-origin and execution fences.
        # Scope, owner and role are server-authored; none is a model-selected identity.
        if run_id != scope.turn_id or scope.surface != "web" or scope.product_grant is None:
            raise HTTPException(403, "Manage skills from an authenticated direct web conversation.")
        # Keep canonical/native lock ordering identical for reads and mutations.
        await self.authority._writer(db)
        await self.authority.authorize_scope_in(db, scope)
        principal = await self.authority.management_principal(db, scope)
        session = await db.scalar(select(Session).where(Session.id == scope.session_id).with_for_update())
        if (
            session is None or session.deleted or session.surface != "web"
            or session.visibility != scope.visibility
            or await self.authority.automated_session(db, session.id)
            or await self.authority.human_origin(db, scope) is None
        ):
            raise HTTPException(403, "Skill management requires a current human web conversation.")
        return principal

    @staticmethod
    def visible(row: dict[str, Any], principal: ProductPrincipal, scope: RunScope) -> bool:
        return row["scope"] == "organization" or (
            scope.visibility == "private" and row["scope"] == "personal" and row["owner_id"] == principal.id
        )

    @staticmethod
    def manage(skill_scope: str, principal: ProductPrincipal, scope: RunScope) -> None:
        if skill_scope == "organization":
            if principal.role != "admin" or scope.visibility != "shared":
                raise HTTPException(403, "Organization skills require an administrator in a shared conversation.")
        elif scope.visibility != "private" or not principal.individual:
            raise HTTPException(403, "Personal skills require an individual requester in a private conversation.")

    async def read(
        self, scope: RunScope, run_id: str | None, *, skill_id: str | None = None,
        archived: bool = False, offset: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        async with self.database.sessions.begin() as db:
            principal = await self.caller(db, scope, run_id)
        if skill_id is None:
            result = await self.catalog.listing(
                principal.grant, shared=scope.visibility != "private", archived=archived, offset=offset, limit=limit,
            )
        else:
            _, row = await self.catalog._row(principal.grant, skill_id)
            if not self.visible(row, principal, scope):
                raise HTTPException(404, "Skill not found.")
            result = await self.catalog.detail(principal.grant, skill_id)
            # Return the complete editable bundle so an update can preserve files.
            _, files = await self.catalog._bundle(row)
            await self.catalog._release(principal.grant, row)
            result["supporting_files"] = files
        async with self.database.sessions.begin() as db:
            await self.caller(db, scope, run_id)
        writable = principal.individual if scope.visibility == "private" else principal.role == "admin"
        result["capabilities"] = {"read": True, "create": writable, "update": writable, "delete": writable}
        result["note"] = "Use skill_read before editing. Deletion archives; skill_update can restore."
        if "skills" in result:
            for skill in result["skills"]:
                skill["can_manage"] = writable and (
                    (skill["scope"] == "personal") == (scope.visibility == "private")
                )
        else:
            result["can_manage"] = writable and (
                (result["scope"] == "personal") == (scope.visibility == "private")
            )
            result["capabilities"]["update"] = result["capabilities"]["delete"] = result["can_manage"]
        return result

    async def mutate(
        self, scope: RunScope, run_id: str | None, receipt_id: str, name: str,
        request: CreateSkill | UpdateSkill | DeleteSkill,
    ) -> dict[str, Any]:
        if name not in MUTATIONS:
            raise HTTPException(422, "Unknown skill action.")
        if not isinstance(request, {"skill_create": CreateSkill, "skill_update": UpdateSkill,
                                    "skill_delete": DeleteSkill}[name]):
            raise HTTPException(422, "The skill action and request do not match.")
        fingerprint = hashlib.sha256(json.dumps(
            {"name": name, "request": request.model_dump(mode="json")}, sort_keys=True,
        ).encode()).hexdigest()
        async with self.database.sessions.begin() as db:
            principal = await self.caller(db, scope, run_id)
            receipt = await db.scalar(select(Effect).where(Effect.id == receipt_id).with_for_update())
            if receipt is None or (
                receipt.actor_id != scope.actor_id or receipt.session_id != scope.session_id
                or receipt.turn_id != scope.turn_id or receipt.tool_name != name
            ):
                raise HTTPException(409, "This skill action has no matching execution receipt.")
            if receipt.state == "confirmed" and receipt.resolution:
                saved = json.loads(self.database.security.unseal(receipt.resolution))
                if saved.get("skill_fingerprint") == fingerprint:
                    return saved["result"]
            if receipt.state != "pending" or receipt.resolution is not None:
                raise HTTPException(409, "This skill action was already settled. Read the current skill.")

            table = self.catalog._table
            stamp = now()
            if isinstance(request, CreateSkill) and name == "skill_create":
                self.manage(request.scope, principal, scope)
                identity = uuid4().hex
                await db.execute(text(
                    f"INSERT INTO {table('skills')} "
                    "(id,name,description,encrypted,scope,owner_id,namespace,revision,archived,"
                    "created_at,updated_at,client_id,icon) "
                    "VALUES (:id,:name,:description,:encrypted,:scope,:owner,:namespace,1,0,:stamp,:stamp,:client,'auto')"
                ), {
                    "id": identity, "name": request.name, "description": request.description,
                    "encrypted": self.identity.legacy_security.encrypt(request.instructions),
                    "scope": request.scope, "owner": principal.id,
                    "namespace": "organization" if request.scope == "organization" else principal.id,
                    "stamp": stamp, "client": receipt_id,
                })
                revision, archived = 1, False
            elif isinstance(request, (UpdateSkill, DeleteSkill)) and name != "skill_create":
                identity = request.skill_id
                row = (await db.execute(text(
                    f"SELECT * FROM {table('skills')} WHERE id=:id FOR UPDATE"
                ), {"id": identity})).mappings().first()
                if row is None or not self.visible(dict(row), principal, scope):
                    raise HTTPException(404, "Skill not found.")
                self.manage(row["scope"], principal, scope)
                if row["revision"] != request.revision:
                    raise HTTPException(409, "The skill changed. Read its current revision before editing.")
                revision = row["revision"] + 1
                archived = isinstance(request, DeleteSkill) or request.archived
                values: dict[str, Any] = {
                    "id": identity, "revision": revision, "archived": int(archived), "stamp": stamp,
                }
                content = ""
                if isinstance(request, UpdateSkill):
                    content = ",name=:name,description=:description,encrypted=:encrypted"
                    values.update(
                        name=request.name, description=request.description,
                        encrypted=self.identity.legacy_security.encrypt(request.instructions),
                    )
                await db.execute(text(
                    f"UPDATE {table('skills')} SET revision=:revision,archived=:archived,updated_at=:stamp"
                    f"{content} WHERE id=:id"
                ), values)
            else:
                raise HTTPException(422, "The skill action and request do not match.")

            if isinstance(request, SkillContent) and request.supporting_files is not None:
                files = request.supporting_files
                manifest = [{
                    "path": path, "size": len(value.encode()), "characters": len(value),
                    "sha256": hashlib.sha256(value.encode()).hexdigest(),
                } for path, value in sorted(files.items())]
                await db.execute(text(
                    f"INSERT INTO {table('skill_bundles')} (skill_id,revision,encrypted,manifest) "
                    "VALUES (:id,:revision,:encrypted,:manifest)"
                ), {
                    "id": identity, "revision": revision,
                    "encrypted": self.identity.legacy_security.encrypt(json.dumps(files, ensure_ascii=False)),
                    "manifest": json.dumps(manifest),
                })
            await db.execute(text(
                f"INSERT INTO {table('skill_audit')} (skill_id,actor_id,action,created_at) "
                "VALUES (:id,:actor,:action,:stamp)"
            ), {"id": identity, "actor": principal.id, "action": name, "stamp": stamp})
            result = {"id": identity, "revision": revision, "archived": archived, "source": "canonical"}
            # Data and receipt commit together: an activity redelivery cannot write twice.
            receipt.state, receipt.settled_at = "confirmed", stamp
            receipt.resolution = self.database.security.seal({
                "skill_fingerprint": fingerprint, "result": result,
            })
        return result

    async def refused(self, scope: RunScope, receipt_id: str, name: str) -> None:
        # Only for acknowledged rollback/validation errors, never transport errors.
        async with self.database.sessions.begin() as db:
            row = await db.scalar(select(Effect).where(Effect.id == receipt_id).with_for_update())
            if row is not None and (
                row.actor_id == scope.actor_id and row.session_id == scope.session_id
                and row.turn_id == scope.turn_id and row.tool_name == name
                and row.state == "pending" and row.resolution is None
            ):
                row.state, row.settled_at = "not_dispatched", now()


def toolset(service: ProductSkillTools) -> FunctionToolset[RunScope]:
    tools: FunctionToolset[RunScope] = FunctionToolset(id=TOOLSET_ID)

    async def read(ctx: RunContext[RunScope], **kwargs) -> dict[str, Any]:
        try:
            return await service.read(ctx.deps, ctx.run_id, **kwargs)
        except HTTPException as error:
            raise ToolFailed(str(error.detail)) from error
        except PermissionError as error:
            raise ToolFailed("This conversation is no longer authorized to manage skills.") from error

    async def mutate(ctx: RunContext[RunScope], name: str, request) -> dict[str, Any]:
        if not ctx.tool_call_id:
            raise ToolFailed("Skill changes require a native tool execution identity.")
        receipt = effect_id(ctx, ToolCallPart(name, {}, tool_call_id=ctx.tool_call_id))
        try:
            return await service.mutate(ctx.deps, ctx.run_id, receipt, name, request)
        except (HTTPException, PermissionError, IntegrityError) as error:
            await service.refused(ctx.deps, receipt, name)
            message = str(error.detail) if isinstance(error, HTTPException) else (
                "A skill with this name already exists in that scope." if isinstance(error, IntegrityError)
                else "This conversation is no longer authorized to manage skills."
            )
            raise ToolFailed(message) from error

    @tools.tool(metadata=READ)
    async def skill_list(
        ctx: RunContext[RunScope], archived: bool = False, offset: int = 0, limit: int = 20,
    ) -> dict[str, Any]:
        """List saved skills before creating or editing one. Personal skills are visible only in private chats."""
        return await read(ctx, archived=archived, offset=offset, limit=limit)

    @tools.tool(metadata=READ)
    async def skill_read(ctx: RunContext[RunScope], skill_id: str) -> dict[str, Any]:
        """Read a saved skill's instructions, supporting files and revision before updating or deleting it."""
        return await read(ctx, skill_id=skill_id)

    @tools.tool(metadata=WRITE, sequential=True)
    async def skill_create(ctx: RunContext[RunScope], request: CreateSkill) -> dict[str, Any]:
        """Save a reusable skill when the user asks. List first to avoid duplicates.

        Personal skills require a private chat; organization skills require an
        administrator in a shared chat. Never publish private conversation content.
        Saved skills appear in Settings and the / picker in subsequent messages.
        """
        return await mutate(ctx, "skill_create", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def skill_update(ctx: RunContext[RunScope], request: UpdateSkill) -> dict[str, Any]:
        """Update a user-requested skill at the revision from skill_read.

        Preserve unrequested fields and files. Scope and ownership are unchanged.
        Set archived=false to restore a deleted skill. Changes apply to future invocations.
        """
        return await mutate(ctx, "skill_update", request)

    @tools.tool(metadata=WRITE, sequential=True)
    async def skill_delete(ctx: RunContext[RunScope], request: DeleteSkill) -> dict[str, Any]:
        """Delete a skill when the user asks, using its current revision.

        Deletion archives the skill, removing it from the / picker while retaining
        instructions and files. Restore with skill_read then skill_update.
        """
        return await mutate(ctx, "skill_delete", request)

    return tools


def capability(catalog: ProductSkillCatalog, authority: ProductAutomationService) -> Toolset[RunScope]:
    return Toolset(toolset(ProductSkillTools(catalog, authority)), id=TOOLSET_ID)
