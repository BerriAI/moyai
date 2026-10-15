"""Authenticated product controls around the SDK's existing Modal workspace."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, TypeAdapter, field_validator
from pydantic_ai.workspaces import Workspace, WorkspaceRef, WorkspaceUnavailableError
from pydantic_ai_harness import ModalSandbox
from sqlalchemy import delete, select, update

from .contracts import RunScope
from .models import ComputerLease, ComputerOperation, Session, Turn, User, new_id

if TYPE_CHECKING:
    from .runtime import Runtime

GUEST = Path(__file__).parent / "resources" / "desktop.py"
if not GUEST.is_file():
    GUEST = Path(__file__).resolve().parents[2] / "desktop.py"
GUEST_CONTENT = GUEST.read_bytes()
REMOTE_GUEST = f"/opt/moyai/desktop-{hashlib.sha256(GUEST_CONTENT).hexdigest()}.py"
LEASE_SECONDS = 180


class DesktopCommandFailed(HTTPException):
    """The guest exited and returned an error; no foreground command remains."""

    def __init__(self):
        super().__init__(422, "The desktop command failed. Inspect the screen before another action.")


class ComputerCommand(BaseModel):
    action: Literal["start", "claim", "release", "heartbeat", "open", "terminal", "click", "type", "key", "scroll"]
    x: int = Field(default=0, ge=0, lt=1280)
    y: int = Field(default=0, ge=0, lt=800)
    button: Literal[1, 2, 3] = 1
    text: str = Field(default="", max_length=2000)
    key: str = Field(default="", max_length=12)
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    direction: Literal["up", "down"] = "down"
    steps: int = Field(default=3, ge=1, le=10)
    url: str = Field(default="", max_length=2048)

    @field_validator("url")
    @classmethod
    def safe_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Use an HTTP(S) address without embedded credentials.")
        return value


async def authorize(runtime: Runtime, session_id: str, user: User) -> Session:
    current_user = await runtime.database.user(user.id)
    if current_user.auth_version != user.auth_version:
        raise HTTPException(401, "Your session expired.")
    async with runtime.database.sessions() as db:
        session = await runtime.database.allowed_session(db, session_id, user)
        active = await db.get(Turn, session.active_turn) if session.active_turn else None
        requester = active.actor_id if active else session.last_actor_id
        if session.visibility == "private" and (
            session.owner_id != user.id or (requester is not None and requester != user.id)
        ):
            raise HTTPException(403, "The computer is private to its requester.")
        context = active or await db.scalar(select(Turn).where(
            Turn.session_id == session_id
        ).order_by(Turn.created_at.desc()).limit(1))
        if session.workspace and context:
            grants = await runtime.database.resolve_grants(
                db, user.id, json.loads(context.connections), shared_session=session.visibility == "shared",
            )
            scope = RunScope(session.id, context.id, user.id, context.model, grants, session.visibility, session.surface)
            if session.workspace_scope != scope.workspace_key:
                raise HTTPException(403, "This computer's account permissions changed.")
        return session


async def lease(runtime: Runtime, session_id: str, user: User, action: str) -> str | None:
    session = await authorize(runtime, session_id, user)
    if not session.workspace:
        raise HTTPException(409, "Send Moyai a task to start this conversation's computer.")
    async with runtime.database.sessions.begin() as db:
        # This lease arbitrates desktop viewers only. Chat and native coding
        # work continue independently of a person's mouse/keyboard control.
        locked = await db.scalar(update(Session).where(
            Session.id == session_id, Session.deleted.is_(False),
        ).values(revision=Session.revision + 1).returning(Session.id))
        if locked is None:
            raise HTTPException(404, "Conversation not found.")
        current = await db.get(ComputerLease, session_id)
        stamp = int(time.time())
        lifetime = 660 if action == "start" else LEASE_SECONDS
        pending = await db.get(ComputerOperation, session_id)
        if pending and pending.expires_at > stamp and action not in {"claim", "heartbeat"}:
            raise HTTPException(409, "Wait for the current desktop action to settle before continuing or releasing control.")
        if action == "release":
            if current and current.actor_id == user.id:
                await db.delete(current)
            return
        if current and current.expires_at > stamp and current.actor_id != user.id:
            raise HTTPException(409, "Another person is controlling this computer.")
        if action not in {"claim", "start"} and (
            current is None or current.actor_id != user.id or current.expires_at <= stamp
        ):
            raise HTTPException(409, "Take control before interacting with the computer.")
        if current is None:
            db.add(ComputerLease(session_id=session_id, actor_id=user.id, expires_at=stamp + lifetime))
        else:
            current.actor_id = user.id
            current.expires_at = max(current.expires_at, stamp + lifetime)
        if action not in {"claim", "heartbeat"}:
            identity = new_id()
            deadline = stamp + (660 if action == "start" else 90)
            if pending is None:
                db.add(ComputerOperation(session_id=session_id, identity=identity, expires_at=deadline))
            else:
                pending.identity = identity
                pending.expires_at = deadline
            return identity
        return None


async def request(runtime: Runtime, session: Session, body: dict) -> dict:
    if not session.workspace:
        raise HTTPException(409, "Send Moyai a task to start this conversation's computer.")
    reference = TypeAdapter(WorkspaceRef).validate_json(session.workspace)
    if reference.provider != "modal":
        raise HTTPException(409, "Desktop access requires a Modal computer.")
    workspace = Workspace(ModalSandbox(working_dir="/workspace").backend(reference))
    try:
        if body["action"] == "start":
            await workspace.write_bytes(REMOTE_GUEST, GUEST_CONTENT)
        elif not await workspace.exists(REMOTE_GUEST):
            if body["action"] == "frame":
                return {"available": False, "width": 1280, "height": 800}
            raise HTTPException(409, "Open the computer before sending desktop input.")
        result = await workspace.run(
            ["python", REMOTE_GUEST, json.dumps(body)],
            timeout=600 if body["action"] == "start" else 30,
        )
        if result.exit_code:
            raise DesktopCommandFailed()
        try:
            value = json.loads(result.stdout)
        except ValueError as error:
            raise DesktopCommandFailed() from error
        if not isinstance(value, dict) or value.get("error"):
            raise DesktopCommandFailed()
        return value
    except WorkspaceUnavailableError as error:
        raise HTTPException(410, "This computer has expired. Your saved files are still available.") from error


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()

    @router.get("/api/sessions/{session_id}/computer")
    async def state(session_id: str, user: User = Depends(actor)):
        session = await authorize(runtime, session_id, user)
        async with runtime.database.sessions() as db:
            current = await db.get(ComputerLease, session_id)
            controlled = bool(current and current.actor_id == user.id and current.expires_at > time.time())
            controller = await db.get(User, current.actor_id) if current and current.expires_at > time.time() else None
        value = await request(runtime, session, {"action": "frame"}) if session.workspace else {"available": False}
        await authorize(runtime, session_id, user)
        return {**value, "has_workspace": bool(session.workspace),
                "controlled": controlled, "busy": bool(session.active_turn),
                "controller": controller.display_name if controller else None}

    @router.post("/api/sessions/{session_id}/computer")
    async def control(session_id: str, body: ComputerCommand, user: User = Depends(actor)):
        operation = await lease(runtime, session_id, user, body.action)
        session = await authorize(runtime, session_id, user)
        if body.action in {"claim", "release", "heartbeat"}:
            return {"controlled": body.action != "release"}
        duration = 600 if body.action == "start" else 30
        try:
            async with asyncio.timeout(duration):
                value = await request(runtime, session, {
                    **body.model_dump(), "deadline": time.time() + duration,
                })
        except TimeoutError as error:
            raise HTTPException(503, "The desktop action timed out. Check the screen before retrying.") from error
        except HTTPException as error:
            # A missing helper or expired computer cannot execute further input;
            # its workspace ref is never replaced. Ambiguous failures keep the fence.
            if isinstance(error, DesktopCommandFailed) or error.status_code in {409, 410}:
                async with runtime.database.sessions.begin() as db:
                    cleared = await db.scalar(delete(ComputerOperation).where(
                        ComputerOperation.session_id == session_id, ComputerOperation.identity == operation,
                    ).returning(ComputerOperation.identity))
                    if cleared is not None and body.action == "start":
                        await db.execute(delete(ComputerLease).where(
                            ComputerLease.session_id == session_id, ComputerLease.actor_id == user.id,
                        ))
            raise
        await authorize(runtime, session_id, user)
        async with runtime.database.sessions.begin() as db:
            settled = await db.scalar(delete(ComputerOperation).where(
                ComputerOperation.session_id == session_id, ComputerOperation.identity == operation,
            ).returning(ComputerOperation.identity))
            if settled is not None and body.action == "start":
                await db.execute(update(ComputerLease).where(
                    ComputerLease.session_id == session_id, ComputerLease.actor_id == user.id,
                ).values(expires_at=int(time.time()) + LEASE_SECONDS))
        return {**value, "controlled": True}

    return router
