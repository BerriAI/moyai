"""Restore a waiting sign-in desktop from its verified filesystem checkpoint.

Reconnection never runs the agent, replays a browser action, or acknowledges login.
The waiting turn and its original deferred request remain the authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.workspaces import Workspace, WorkspaceRef
from pydantic_ai_harness import ModalSandbox
from sqlalchemy import delete

from .computer import DESKTOP_VERSION, ComputerControlResult, authorize, guest_request
from .contracts import PreparedTurn
from .models import ComputerDesktop, ComputerLease, ComputerOperation, Event, Session, Turn, User, new_id
from .product_continuation_errors import public_preparation_message
from .product_requester import answer_actor
from .workspace_recovery import (
    live_sandbox,
    locked_scope,
    preserve_restoration,
    restored_sandbox,
    validate_checkpoint,
)

if TYPE_CHECKING:
    from .runtime import Runtime


def login_restoration_id(turn_id: str, original: str, restored: str) -> str:
    digest = hashlib.sha256(json.dumps([original, restored]).encode()).hexdigest()
    return f"browser-login-restored:{turn_id}:{digest}"


async def reconnect(runtime: Runtime, session_id: str, actor: User, client_id: str) -> ComputerControlResult:
    session = await authorize(runtime, session_id, actor)
    reference = json.loads(session.workspace or "null")
    if not reference or reference.get("provider") != "modal":
        raise HTTPException(409, "This conversation has no Modal desktop to reconnect.")
    try:
        async with asyncio.timeout(15):
            alive = await live_sandbox(reference)
        if alive:
            # A network reconnect is still view-only unless this browser already
            # owns control. It must not invalidate a saved checkpoint.
            async with runtime.database.sessions() as db:
                desktop = await db.get(ComputerDesktop, session_id)
                lease = await db.get(ComputerLease, session_id)
                return ComputerControlResult(
                    available=bool(desktop and desktop.workspace_id == reference["id"]
                                   and desktop.version == DESKTOP_VERSION),
                    controlled=bool(lease and lease.actor_id == actor.id and lease.expires_at > time.time()
                                    and desktop and desktop.control_client == client_id),
                )
        return await restore_login(runtime, session, actor, reference)
    except HTTPException:
        raise
    except PermissionError as error:
        raise HTTPException(403, "This task's permissions changed. Reopen the conversation.") from error
    except Exception as error:
        message = public_preparation_message(error)
        if message:
            raise HTTPException(409, message) from error
        # Provider errors can contain private endpoints or credentials.
        raise HTTPException(503, "The computer could not reconnect. Try again shortly.") from error


async def restore_login(
    runtime: Runtime, session: Session, actor: User, reference: dict[str, str],
) -> ComputerControlResult:
    database = runtime.database
    async with database.sessions() as db:
        turn = await db.get(Turn, session.active_turn) if session.active_turn else None
        if turn is None or turn.status != "waiting" or not turn.preparation or not turn.conversation:
            raise HTTPException(409, "This computer expired. Send Moyai a new message to recover its saved files.")
        prepared = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(turn.preparation))
        conversation = ConversationTypeAdapter.validate_json(database.security.unseal(turn.conversation))
    await answer_actor(runtime, actor, turn)
    from .browser_login import TOOL_NAME

    pending = conversation.deferred_tool_requests
    originals = {
        str(metadata["workspace_id"])
        for call in pending.calls if call.tool_name == TOOL_NAME
        if (metadata := (pending.metadata or {}).get(call.tool_call_id) or {}).get("kind") == "browser_login"
        and metadata.get("workspace_id")
    } if pending else set()
    if not originals:
        raise HTTPException(409, "This computer expired. Answer the pending request to recover its saved files.")
    scope = prepared.scope
    operation_id = new_id()
    async with database.sessions.begin() as db:
        current, waiting = await locked_scope(database, db, scope, status="waiting")
        await database.swarms.authorize(db, waiting, scope=scope)
        await database.goals.authorize(db, waiting)
        if json.loads(current.workspace or "null") != reference:
            raise HTTPException(409, "The computer changed. Reconnect again.")
        saved = await validate_checkpoint(database, db, current, scope, reference)
        expected = current.snapshot
        # A reservation fences competing reconnections, human input and login
        # completion while provider creation happens outside the transaction.
        await db.execute(delete(ComputerOperation).where(ComputerOperation.session_id == session.id))
        db.add(ComputerOperation(
            session_id=session.id, identity=operation_id, expires_at=int(time.time()) + 660,
        ))
        current.revision += 1
        revision = current.revision
    try:
        async with asyncio.timeout(600):
            sandbox = await restored_sandbox(runtime, scope, saved)
            if await sandbox.poll.aio() is not None:
                raise HTTPException(409, "The restored computer expired. Reconnect again.")
            await authorize(runtime, session.id, actor)
            await answer_actor(runtime, actor, turn)
            replacement = {"provider": "modal", "id": sandbox.object_id}
            workspace = Workspace(ModalSandbox(working_dir="/workspace").backend(
                TypeAdapter(WorkspaceRef).validate_python(replacement),
            ))
            await guest_request(workspace, {"action": "start", "deadline": time.time() + 570})
        # Authority may have changed while Modal was creating the desktop.
        await authorize(runtime, session.id, actor)
        await answer_actor(runtime, actor, turn)
        async with database.sessions.begin() as db:
            current, waiting = await locked_scope(database, db, scope, status="waiting")
            await database.swarms.authorize(db, waiting, scope=scope)
            await database.goals.authorize(db, waiting)
            operation = await db.get(ComputerOperation, session.id)
            if (
                operation is None or operation.identity != operation_id or operation.expires_at <= time.time()
                or current.revision != revision or current.snapshot != expected
                or json.loads(current.workspace or "null") != reference
            ):
                raise HTTPException(409, "The conversation changed during recovery. Reopen its Computer panel.")
            await db.delete(operation)
            await db.flush()
            await validate_checkpoint(database, db, current, scope, reference)
            if runtime.product is not None:
                await preserve_restoration(db, session.id, reference["id"], replacement["id"], scope.workspace_key)
            current.workspace = json.dumps(replacement)
            # Files are still the saved image until a person takes control.
            # Keeping the binding current permits another safe idle recovery.
            current.snapshot = saved.model_copy(update={"workspace": replacement}).model_dump_json()
            current.revision += 1
            desktop = await db.get(ComputerDesktop, session.id)
            if desktop is None:
                desktop = ComputerDesktop(session_id=session.id)
                db.add(desktop)
            desktop.workspace_id = replacement["id"]
            desktop.version = DESKTOP_VERSION
            desktop.control_client = ""
            desktop.control_epoch = new_id()
            await db.execute(delete(ComputerLease).where(ComputerLease.session_id == session.id))
            for original in originals:
                identity = login_restoration_id(scope.turn_id, original, replacement["id"])
                db.add(Event(
                    session_id=session.id, turn_id=scope.turn_id, kind="browser_login_restored",
                    identity=identity,
                    content=database.security.seal({"original": original, "restored": replacement["id"]}),
                ))
        return ComputerControlResult(available=True, controlled=False)
    finally:
        # A failed or abandoned request cannot clear another recovery's fence.
        async with database.sessions.begin() as db:
            await db.execute(delete(ComputerOperation).where(
                ComputerOperation.session_id == session.id, ComputerOperation.identity == operation_id,
            ))
