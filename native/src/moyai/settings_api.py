"""Authenticated Settings over native records, Harness memory and Temporal.

Mount with ``app.include_router(routes(runtime, actor))``. The caller's actor
dependency must enforce the native session/CSRF policy, as in api.create_app.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, field_validator
from pydantic_ai_harness.memory import MemoryConflictError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from temporalio.service import RPCError

from .analytics_service import report as analytics_report
from .contracts import PreparedTurn, RunScope
from .models import Connection, Turn, User, now
from .pull_requests import PullRequestService
from .security import password_hash
from .settings_models import SettingAutomation, SettingAutomationRun, SettingSecret, SettingSkill
from .settings_runtime import automation_readiness, reconcile_schedule

Scope = Literal["personal", "shared"]
CAPABILITIES = {"create": True, "update": True, "delete": True}
SKILLS_ROOT = Path(__file__).resolve().parent / "resources" / "skills"
if not SKILLS_ROOT.is_dir():
    SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SkillInput(Input):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    content: str = Field(min_length=1, max_length=65_536)
    scope: Scope = "personal"
    enabled: bool = True


class SecretInput(Input):
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$", max_length=100)
    description: str = Field(default="", max_length=1000)
    value: SecretStr = Field(min_length=1, max_length=65_536)
    scope: Scope = "personal"

    @field_validator("value")
    @classmethod
    def nonblank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip() or "\x00" in value.get_secret_value():
            raise ValueError("Enter a nonblank secret without null bytes.")
        return value


class SecretEdit(Input):
    name: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$", max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    value: SecretStr | None = Field(default=None, min_length=1, max_length=65_536)
    scope: Scope | None = None

    @field_validator("value")
    @classmethod
    def nonblank(cls, value: SecretStr | None) -> SecretStr | None:
        return SecretInput.nonblank(value) if value is not None else None


class MemoryInput(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    content: str = Field(max_length=65_536)
    version: str | None = Field(default=None, max_length=200)


class UserInput(Input):
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@+-]*$", max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    password: SecretStr = Field(min_length=12, max_length=4096)
    role: Literal["admin", "member"] = "member"
    enabled: bool = True


class UserEdit(Input):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=4096)
    role: Literal["admin", "member"] | None = None
    enabled: bool | None = None


class AutomationInput(Input):
    name: str = Field(min_length=1, max_length=150)
    prompt: str = Field(min_length=1, max_length=100_000)
    session_id: str = Field(min_length=1, max_length=36)
    model: str | None = Field(default=None, max_length=200)
    connections: list[str] = Field(default_factory=list, max_length=30)
    interval_minutes: int = Field(ge=5, le=525_600)
    paused: bool = False


class AutomationEdit(Input):
    paused: bool


def admin(user: User) -> None:
    if not user.enabled or user.role != "admin":
        raise HTTPException(403, "Administrator access is required.")


def scope_key(scope: Scope, user: User) -> str:
    if scope == "shared":
        admin(user)
        return "shared"
    return user.id


def accessible(model: Any, user: User):
    return or_(model.scope_key == user.id, model.scope_key == "shared")


def can_edit(row: Any, user: User) -> bool:
    return user.role == "admin" if row.scope_key == "shared" else row.owner_id == user.id


async def resource(db: Any, model: Any, identity: str, user: User) -> Any:
    await db.execute(update(model).where(
        model.id == identity, accessible(model, user),
    ).values(scope_key=model.scope_key))
    row = await db.get(model, identity)
    if row is None or (row.scope_key != "shared" and row.owner_id != user.id):
        raise HTTPException(404, "Resource not found.")
    if not can_edit(row, user):
        raise HTTPException(403, "Only administrators can change shared resources.")
    return row


async def lock_actor(db: Any, user: User) -> None:
    """Hold the current authorization through a mutation's commit."""
    identity = await db.scalar(update(User).where(
        User.id == user.id, User.enabled.is_(True), User.auth_version == user.auth_version,
    ).values(auth_version=User.auth_version).returning(User.id))
    if identity is None:
        raise HTTPException(401, "Your access changed. Sign in again.")


def secret_view(row: SettingSecret | SettingSkill, user: User) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "description": row.description,
        "scope": "shared" if row.scope_key == "shared" else "personal",
        "editable": can_edit(row, user), "updated_at": row.updated_at,
    }


def skill_view(runtime: Any, row: SettingSkill, user: User) -> dict[str, Any]:
    return {
        **secret_view(row, user), "content": runtime.security.unseal(row.content).decode(),
        "enabled": row.enabled,
    }


def bundled_skills() -> list[dict[str, Any]]:
    rows = []
    for path in sorted(SKILLS_ROOT.glob("*/SKILL.md")):
        text = path.read_text()
        sections = text.split("---", 2)
        meta = yaml.safe_load(sections[1]) if len(sections) == 3 and not sections[0].strip() else {}
        meta = meta if isinstance(meta, dict) else {}
        name = str(meta.get("name") or path.parent.name)
        rows.append({
            "id": f"bundled:{name}", "name": name, "description": str(meta.get("description") or ""),
            "content": sections[2].strip() if len(sections) == 3 else text,
            "scope": "bundled", "enabled": True, "editable": False, "updated_at": None,
        })
    return rows


def user_view(row: User) -> dict[str, Any]:
    return {name: getattr(row, name) for name in (
        "id", "username", "display_name", "role", "enabled", "auth_version",
    )}


def automation_view(runtime: Any, row: SettingAutomation) -> dict[str, Any]:
    return {
        "id": row.id, "name": row.name, "prompt": runtime.security.unseal(row.prompt).decode(),
        "session_id": row.session_id, "model": row.model, "connections": json.loads(row.connections),
        "interval_minutes": row.interval_minutes, "paused": row.paused, "state": row.state,
        "updated_at": row.updated_at, "editable": True,
    }


async def memory_namespaces(runtime: Any, user: User) -> list[dict[str, str]]:
    default = RunScope("", "", user.id, runtime.settings.default_model).memory_namespace
    scopes = {default: "Personal · no connected accounts"}
    # Prepared native scopes are server-authored. Never accept caller-supplied
    # prefixes, and never enumerate memory belonging to another actor.
    async with runtime.database.sessions() as db:
        preparations = await db.scalars(select(Turn.preparation).where(
            Turn.actor_id == user.id, Turn.preparation.is_not(None),
        ).distinct())
        for serialized in preparations:
            scope = TypeAdapter(PreparedTurn).validate_json(runtime.security.unseal(serialized)).scope
            if scope.actor_id == user.id:
                namespace = scope.memory_namespace
                label = f"{scope.surface} · {scope.visibility} · {len(scope.grants)} connected accounts"
                scopes.setdefault(namespace, label)
    return [{"id": key, "label": label} for key, label in scopes.items()]


async def memory_path(runtime: Any, user: User, namespace: str | None, path: str) -> tuple[str, str]:
    scopes = await memory_namespaces(runtime, user)
    namespace = namespace or scopes[0]["id"]
    if namespace not in {scope["id"] for scope in scopes}:
        raise HTTPException(404, "Memory namespace not found.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}\.md", path) or ".." in path:
        raise HTTPException(422, "Use a Markdown filename without directory segments.")
    return namespace, f"{namespace}/main/{path}"


async def change_user(runtime: Any, caller: User, identity: str, changes: UserEdit) -> dict[str, Any]:
    admin(caller)
    values = changes.model_dump(exclude_none=True)
    if "password" in values:
        values["password"] = await asyncio.to_thread(password_hash, values["password"].get_secret_value())
    async with runtime.database.sessions.begin() as db:
        # A database write lock, not a process-local lock: concurrent admin
        # demotions cannot both observe the other as the last administrator.
        await db.execute(update(User).where(User.role == "admin").values(auth_version=User.auth_version))
        fresh = await db.get(User, caller.id)
        if fresh is None or not fresh.enabled or fresh.role != "admin" or fresh.auth_version != caller.auth_version:
            raise HTTPException(403, "Administrator access changed.")
        row = await db.get(User, identity)
        if row is None:
            raise HTTPException(404, "User not found.")
        if row.role == "admin" and row.enabled and (
            values.get("role", row.role) != "admin" or not values.get("enabled", row.enabled)
        ):
            count = await db.scalar(select(func.count()).select_from(User).where(
                User.role == "admin", User.enabled.is_(True),
            ))
            if count == 1:
                raise HTTPException(409, "Keep at least one enabled administrator.")
        if values:
            for key, value in values.items():
                setattr(row, key, value)
            row.auth_version += 1
            # Use native stopped status so existing authority checks fence tools
            # immediately and the normal dispatcher cancels active workflows.
            await db.execute(update(Turn).where(
                Turn.actor_id == row.id,
                Turn.status.in_(["queued", "starting", "running", "waiting", "steering", "signal_sent", "accepted"]),
            ).values(status="stopped", finished_at=now()))
        return user_view(row)


async def analytics(
    runtime: Any, user: User, start: str | None, end: str | None, model: str | None,
    status: str | None, user_id: str | None, limit: int, offset: int, format: str,
    *, preset: str = "7d", export: str = "turns", service: PullRequestService | None = None,
    unified_billing: bool = False,
) -> Any:
    return await analytics_report(
        runtime, user, start, end, model, status, user_id, limit, offset, format,
        preset=preset, export=export, service=service,
        billing=getattr(runtime, "product_billing", None) if unified_billing else None,
    )


class SettingsRoute(APIRoute):
    """Validation responses must not echo submitted passwords or secret values."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_validation(request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                return JSONResponse(status_code=422, content={"detail": [
                    {key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()
                ]})
        return safe_validation


def routes(runtime: Any, actor: Any) -> APIRouter:
    original_actor = actor
    analytics_pr_service = PullRequestService(runtime)

    async def checked_actor(request: Request, user: User = Depends(original_actor)):
        request.state.settings_actor = user
        return user

    actor = checked_actor

    class RuntimeSettingsRoute(SettingsRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def checked(request):
                response = await handler(request)
                user = getattr(request.state, "settings_actor", None)
                if user is not None and not getattr(request.state, "settings_self_auth_change", False):
                    fresh = await runtime.database.user(user.id)
                    if fresh.auth_version != user.auth_version:
                        raise HTTPException(401, "Your access changed. Sign in again.")
                return response
            return checked

    router = APIRouter(prefix="/api/settings", tags=["settings"], route_class=RuntimeSettingsRoute)

    def capabilities(user: User) -> dict[str, Any]:
        return {
            "skills": {**CAPABILITIES, "shared": user.role == "admin"},
            "memory": dict(CAPABILITIES),
            "secrets": {**CAPABILITIES, "shared": user.role == "admin", "agent_access": False},
            "automations": {**CAPABILITIES, "run": False, "execution_backend": "temporal"},
            "users": user.role == "admin", "performance": user.role == "admin", "usage": user.role == "admin",
        }

    @router.get("/overview")
    async def overview(user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            counts = {
                "skills": len(bundled_skills()) + (await db.scalar(select(func.count()).select_from(
                    SettingSkill
                ).where(accessible(SettingSkill, user))) or 0),
                "secrets": await db.scalar(select(func.count()).select_from(SettingSecret).where(
                    accessible(SettingSecret, user),
                )),
                "automations": await db.scalar(select(func.count()).select_from(SettingAutomation).where(
                    SettingAutomation.owner_id == user.id, SettingAutomation.state != "deleted",
                )),
                "connections": await db.scalar(select(func.count()).select_from(Connection).where(
                    or_(Connection.owner_id == user.id, Connection.shared.is_(True)),
                )),
            }
            if user.role == "admin":
                counts["users"] = await db.scalar(select(func.count()).select_from(User))
        scopes = await memory_namespaces(runtime, user)
        counts["memory"] = sum([
            len(await runtime.stores.memory.list_paths(f"{scope['id']}/main/", limit=1001)) for scope in scopes
        ])
        available = capabilities(user)
        available["automations"] = await automation_readiness(runtime)
        return {
            "user": {"id": user.id, "name": user.display_name, "role": user.role},
            "counts": counts, "capabilities": available,
            "engine": {"agent": "pydantic-ai", "harness": "pydantic-ai-harness",
                       "durability": "temporal", "sandbox": "modal"},
        }

    @router.get("/skills")
    async def skills(user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            rows = await db.scalars(select(SettingSkill).where(accessible(SettingSkill, user)).order_by(
                SettingSkill.name,
            ))
            return {"skills": bundled_skills() + [skill_view(runtime, row, user) for row in rows],
                    "capabilities": capabilities(user)["skills"]}

    @router.post("/skills", status_code=201)
    async def create_skill(body: SkillInput, user: User = Depends(actor)):
        key = scope_key(body.scope, user)
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                row = SettingSkill(
                    owner_id=user.id, scope_key=key, name=body.name, description=body.description,
                    content=runtime.security.seal(body.content), enabled=body.enabled,
                )
                db.add(row)
                await db.flush()
                result = skill_view(runtime, row, user)
            return result
        except IntegrityError:
            raise HTTPException(409, "A skill with this name already exists in that scope.") from None

    @router.patch("/skills/{identity}")
    async def edit_skill(identity: str, body: SkillInput, user: User = Depends(actor)):
        key = scope_key(body.scope, user)
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                row = await resource(db, SettingSkill, identity, user)
                row.name, row.description, row.content = body.name, body.description, runtime.security.seal(body.content)
                row.scope_key, row.enabled, row.updated_at = key, body.enabled, now()
                # Moving a shared record to personal explicitly transfers its ownership.
                row.owner_id = user.id
                await db.flush()
                return skill_view(runtime, row, user)
        except IntegrityError:
            raise HTTPException(409, "A skill with this name already exists in that scope.") from None

    @router.delete("/skills/{identity}")
    async def delete_skill(identity: str, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            await lock_actor(db, user)
            await db.delete(await resource(db, SettingSkill, identity, user))
        return {"deleted": True}

    @router.get("/secrets")
    async def secrets(user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            rows = await db.scalars(select(SettingSecret).where(accessible(SettingSecret, user)).order_by(
                SettingSecret.name,
            ))
            return {"secrets": [secret_view(row, user) for row in rows],
                    "capabilities": capabilities(user)["secrets"],
                    "reason": "Values are encrypted and never returned. Agent access is not enabled."}

    @router.post("/secrets", status_code=201)
    async def create_secret(body: SecretInput, user: User = Depends(actor)):
        key = scope_key(body.scope, user)
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                row = SettingSecret(owner_id=user.id, scope_key=key, name=body.name,
                                    description=body.description, value=runtime.security.seal(body.value.get_secret_value()))
                db.add(row)
                await db.flush()
                return secret_view(row, user)
        except IntegrityError:
            raise HTTPException(409, "A secret with this name already exists in that scope.") from None

    @router.patch("/secrets/{identity}")
    async def edit_secret(identity: str, body: SecretEdit, user: User = Depends(actor)):
        key = scope_key(body.scope, user) if body.scope is not None else None
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                row = await resource(db, SettingSecret, identity, user)
                if body.name is not None:
                    row.name = body.name
                if body.description is not None:
                    row.description = body.description
                if body.value is not None:
                    row.value = runtime.security.seal(body.value.get_secret_value())
                if key is not None:
                    row.scope_key, row.owner_id = key, user.id
                row.updated_at = now()
                await db.flush()
                return secret_view(row, user)
        except IntegrityError:
            raise HTTPException(409, "A secret with this name already exists in that scope.") from None

    @router.delete("/secrets/{identity}")
    async def delete_secret(identity: str, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            await lock_actor(db, user)
            await db.delete(await resource(db, SettingSecret, identity, user))
        return {"deleted": True}

    @router.get("/memory")
    async def memory(namespace: str | None = None, user: User = Depends(actor)):
        scopes = await memory_namespaces(runtime, user)
        namespace, _ = await memory_path(runtime, user, namespace, "MEMORY.md")
        prefix = f"{namespace}/main/"
        paths = await runtime.stores.memory.list_paths(prefix, limit=1001)
        rows = []
        for path in paths[:1000]:
            value = await runtime.stores.memory.read(path, max_chars=65_536)
            if value:
                rows.append({"namespace": namespace, "path": path[len(prefix):], "content": value.content,
                             "version": value.version, "truncated": value.truncated})
        return {"memories": rows, "namespaces": scopes, "default_namespace": scopes[0]["id"],
                "namespace": namespace, "truncated": len(paths) > 1000,
                "capabilities": capabilities(user)["memory"]}

    @router.put("/memory")
    async def write_memory(body: MemoryInput, path: str = "MEMORY.md", namespace: str | None = None,
                           user: User = Depends(actor)):
        namespace, store_path = await memory_path(runtime, user, namespace, path)
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                result = await runtime.stores.memory.write(store_path, body.content, expected_version=body.version)
        except MemoryConflictError:
            raise HTTPException(409, "Memory changed. Reload before saving.") from None
        return {"namespace": namespace, "path": path, "content": body.content, "version": result.version}

    @router.delete("/memory")
    async def delete_memory(path: str, namespace: str | None = None, version: str | None = None,
                            user: User = Depends(actor)):
        _, store_path = await memory_path(runtime, user, namespace, path)
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                await runtime.stores.memory.delete(store_path, expected_version=version)
        except MemoryConflictError:
            raise HTTPException(409, "Memory changed. Reload before deleting.") from None
        return {"deleted": True}

    @router.get("/users")
    async def users(user: User = Depends(actor)):
        admin(user)
        async with runtime.database.sessions() as db:
            rows = await db.scalars(select(User).order_by(User.username))
            return {"users": [user_view(row) for row in rows], "capabilities": CAPABILITIES}

    @router.post("/users", status_code=201)
    async def create_user(body: UserInput, user: User = Depends(actor)):
        admin(user)
        encoded = await asyncio.to_thread(password_hash, body.password.get_secret_value())
        try:
            async with runtime.database.sessions.begin() as db:
                await lock_actor(db, user)
                row = User(username=body.username, display_name=body.display_name, password=encoded,
                           role=body.role, enabled=body.enabled)
                db.add(row)
                await db.flush()
                return user_view(row)
        except IntegrityError:
            raise HTTPException(409, "That username already exists.") from None

    @router.patch("/users/{identity}")
    async def edit_user(identity: str, body: UserEdit, request: Request, user: User = Depends(actor)):
        result = await change_user(runtime, user, identity, body)
        # A self-edit intentionally revokes this cookie. Its authorized result
        # contains only user metadata and no post-commit external read.
        request.state.settings_self_auth_change = identity == user.id and result["auth_version"] != user.auth_version
        return result

    @router.delete("/users/{identity}")
    async def delete_user(identity: str, request: Request, user: User = Depends(actor)):
        # Preserve FK-backed audit history while revoking every existing session.
        result = await change_user(runtime, user, identity, UserEdit(enabled=False))
        request.state.settings_self_auth_change = identity == user.id
        return result

    async def own_automation(db: Any, identity: str, user: User) -> SettingAutomation:
        row = await db.get(SettingAutomation, identity)
        if row is None or row.owner_id != user.id:
            raise HTTPException(404, "Automation not found.")
        return row

    async def sync_automation(identity: str) -> None:
        try:
            await asyncio.wait_for(reconcile_schedule(runtime, identity), timeout=10)
        except HTTPException:
            raise
        except (RPCError, TimeoutError, RuntimeError, OSError):
            raise HTTPException(503, "Temporal did not confirm this change. Saved intent is pending; retry.") from None

    @router.get("/automations")
    async def automations(user: User = Depends(actor)):
        available = await automation_readiness(runtime)
        async with runtime.database.sessions() as db:
            rows = await db.scalars(select(SettingAutomation).where(
                SettingAutomation.owner_id == user.id, SettingAutomation.state != "deleted",
            ).order_by(SettingAutomation.created_at.desc()))
            return {"automations": [automation_view(runtime, row) for row in rows],
                    "capabilities": available, "reason": available["reason"]}

    @router.post("/automations", status_code=201)
    async def create_automation(body: AutomationInput, user: User = Depends(actor)):
        available = await automation_readiness(runtime)
        if not available["create"]:
            raise HTTPException(503, available["reason"])
        async with runtime.database.sessions.begin() as db:
            await lock_actor(db, user)
            session = await runtime.database.allowed_session(db, body.session_id, user)
            if session.owner_id != user.id or session.visibility != "private" or session.surface != "web":
                raise HTTPException(403, "Choose your own private web conversation for an automation.")
            await runtime.database.resolve_grants(db, user.id, body.connections, shared_session=False)
            model = body.model or session.model
            if model not in runtime.settings.models:
                raise HTTPException(422, "Choose an enabled model.")
            row = SettingAutomation(
                owner_id=user.id, owner_auth_version=user.auth_version, session_id=session.id,
                name=body.name, prompt=runtime.security.seal(body.prompt), model=model,
                connections=json.dumps(body.connections), interval_minutes=body.interval_minutes, paused=body.paused,
            )
            db.add(row)
            await db.flush()
            identity = row.id
        await sync_automation(identity)
        async with runtime.database.sessions() as db:
            return automation_view(runtime, await own_automation(db, identity, user))

    @router.patch("/automations/{identity}")
    async def edit_automation(identity: str, body: AutomationEdit, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            await lock_actor(db, user)
            await db.execute(update(SettingAutomation).where(
                SettingAutomation.id == identity, SettingAutomation.owner_id == user.id,
            ).values(revision=SettingAutomation.revision))
            row = await own_automation(db, identity, user)
            if row.state == "deleted":
                raise HTTPException(404, "Automation was deleted.")
            row.paused, row.state, row.updated_at = body.paused, "pending", now()
            row.revision += 1
            # Explicit re-enable is a fresh grant following account revocation.
            row.owner_auth_version = user.auth_version
        await sync_automation(identity)
        async with runtime.database.sessions() as db:
            return automation_view(runtime, await own_automation(db, identity, user))

    @router.delete("/automations/{identity}")
    async def delete_automation(identity: str, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            await lock_actor(db, user)
            await db.execute(update(SettingAutomation).where(
                SettingAutomation.id == identity, SettingAutomation.owner_id == user.id,
            ).values(revision=SettingAutomation.revision))
            row = await own_automation(db, identity, user)
            row.paused, row.state, row.updated_at = True, "deleted", now()
            row.revision += 1
        await sync_automation(identity)
        return {"deleted": True}

    @router.get("/automations/{identity}/logs")
    async def automation_logs(identity: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            await own_automation(db, identity, user)
            rows = (await db.execute(select(SettingAutomationRun, Turn.status).outerjoin(
                Turn, Turn.id == SettingAutomationRun.turn_id,
            ).where(SettingAutomationRun.automation_id == identity).order_by(
                SettingAutomationRun.created_at.desc(),
            ).limit(100))).all()
            return {"logs": [{"id": row.id, "turn_id": row.turn_id, "status": status or row.status,
                              "created_at": row.created_at} for row, status in rows]}

    @router.get("/usage")
    @router.get("/performance")
    async def report(
        request: Request,
        start: str | None = None, end: str | None = None,
        model: str | None = Query(default=None, max_length=200),
        status: str | None = Query(default=None, max_length=30),
        user_id: str | None = Query(default=None, max_length=512),
        limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0),
        format: Literal["json", "csv"] = "json",
        preset: Literal["24h", "7d", "30d", "custom"] = "7d",
        export: Literal["turns", "users", "daily", "models"] = "turns", user: User = Depends(actor),
    ):
        return await analytics(
            runtime, user, start, end, model, status, user_id, limit, offset, format,
            preset=preset, export=export, service=analytics_pr_service,
            unified_billing=request.url.path == "/api/settings/usage",
        )

    return router
