"""Reconnect a dead desktop without executing or resuming its task.

Reconnection never runs the agent, replays a browser action, or acknowledges login.
The original task's authority and settled state fence replacement publication.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING

import modal
from fastapi import HTTPException
from opentelemetry.trace import get_current_span
from pydantic import TypeAdapter
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.workspaces import Workspace, WorkspaceRef
from pydantic_ai_harness import ModalSandbox
from sqlalchemy import delete, select, update
from temporalio.exceptions import ApplicationError

from .computer import DESKTOP_VERSION, ComputerControlResult, authorize, guest_request
from .continuations.product_continuation_errors import PREPARATION_BLOCKED_TYPE, public_preparation_message
from .contracts import PreparedTurn, RunScope
from .lifecycle_tracing import failure, scope_attributes, workspace_attributes
from .lifecycle_tracing import operation as operation_span
from .models import ComputerDesktop, ComputerLease, ComputerOperation, Event, Session, Turn, User, new_id
from .product_requester import answer_actor
from .workspace_recovery import (
    live_sandbox,
    preserve_restoration,
    require_settled,
    restored_sandbox,
    validate_checkpoint,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .runtime import Runtime

FRESH_NOTICE = (
    "Started a new computer because no current filesystem backup was available. "
    "Files and browser state from the expired computer were not recovered. "
    "Your chat and saved downloads are retained."
)
RESTORED_NOTICE = "Restored your computer from its saved files. Browser tabs and running processes may need reopening."


async def locked_computer(
    database: Database, db: AsyncSession, scope: RunScope, expected: Session, turn_status: str,
) -> tuple[Session, Turn]:
    """Same source → actor → session lock order as execution, without starting it."""
    turn = await db.get(Turn, scope.turn_id)
    if turn is None or turn.actor_id != scope.actor_id or turn.session_id != scope.session_id:
        raise PermissionError("The computer's original task changed.")
    await database._check_product_turn(db, turn, scope.product_grant)
    actor = await db.scalar(update(User).where(
        User.id == scope.actor_id, User.enabled.is_(True),
    ).values(auth_version=User.auth_version).returning(User.id))
    if actor is None:
        raise PermissionError("The original requester no longer has access.")
    locked = await db.scalar(update(Session).where(
        Session.id == scope.session_id, Session.deleted.is_(False),
        Session.active_turn == expected.active_turn, Session.status == expected.status,
    ).values(revision=Session.revision).returning(Session.id))
    if locked is None:
        raise HTTPException(409, "The task changed during recovery. Reconnect again.")
    current = await db.get(Session, scope.session_id, populate_existing=True)
    await db.refresh(turn)
    assert current is not None
    if (
        turn.status != turn_status or current.visibility != scope.visibility or current.surface != scope.surface
        or current.workspace_scope != scope.workspace_key
        or (current.visibility == "private" and current.owner_id != scope.actor_id)
    ):
        raise PermissionError("The computer's authority changed.")
    grants = await database.scope_grants(db, scope, current)
    if [(g.id, g.revision) for g in grants] != [(g.id, g.revision) for g in scope.grants]:
        raise PermissionError("Workspace account access changed.")
    await database.swarms.authorize(db, turn, scope=scope)
    await database.goals.authorize(db, turn)
    return current, turn


async def fresh_sandbox(runtime: Runtime, scope: RunScope, reference: dict[str, str]):
    """A stable name makes a lost create reply safe to retry."""
    from .agent import workspace_image

    name = "reconnect-" + hashlib.sha256(
        f"{scope.session_id}:{scope.workspace_key}:{reference['id']}".encode(),
    ).hexdigest()[:48]
    try:
        existing = await modal.Sandbox.from_name.aio(runtime.settings.modal_app, name)
        if await existing.poll.aio() is None:
            return existing
    except modal.exception.NotFoundError:
        pass
    app = await modal.App.lookup.aio(runtime.settings.modal_app, create_if_missing=True)
    try:
        return await modal.Sandbox.create.aio(
            app=app, name=name, image=workspace_image(), workdir="/workspace",
            timeout=runtime.settings.computer_lifetime, idle_timeout=runtime.settings.computer_idle_timeout,
        )
    except modal.exception.AlreadyExistsError:
        return await modal.Sandbox.from_name.aio(runtime.settings.modal_app, name)


def login_restoration_id(turn_id: str, original: str, restored: str) -> str:
    digest = hashlib.sha256(json.dumps([original, restored]).encode()).hexdigest()
    return f"browser-login-restored:{turn_id}:{digest}"


async def reconnect(runtime: Runtime, session_id: str, actor: User, client_id: str) -> ComputerControlResult:
    session = await authorize(runtime, session_id, actor)
    reference = json.loads(session.workspace or "null")
    if not reference or reference.get("provider") != "modal":
        raise HTTPException(409, "This conversation has no Modal desktop to reconnect.")
    span = get_current_span()
    span.set_attributes({
        **workspace_attributes(reference), "moyai.task.state": session.status,
        "moyai.computer.operation": "reconnect",
    })
    try:
        result = await _reconnect(runtime, session, actor, client_id, reference)
    except BaseException as error:
        failure(span, error)
        raise
    span.set_attribute("moyai.computer.available", result.available)
    span.set_attribute("moyai.recovery.mode", (
        "fresh" if result.notice == FRESH_NOTICE else "restored" if result.notice == RESTORED_NOTICE else "existing"
    ))
    return result


async def _reconnect(
    runtime: Runtime, session: Session, actor: User, client_id: str, reference: dict[str, str],
) -> ComputerControlResult:
    session_id = session.id
    try:
        with operation_span(runtime.database, "moyai.computer.health", attributes=workspace_attributes(reference)) as span:
            async with asyncio.timeout(15):
                alive = await live_sandbox(reference)
            span.set_attribute("moyai.computer.state", "alive" if alive else "unavailable")
            # A liveness check proves loss, not when or why the provider killed it.
            span.set_attribute("moyai.computer.observation", "provider_liveness")
        if alive:
            # A network reconnect is still view-only unless this browser already
            # owns control. It must not invalidate a saved checkpoint.
            current = await authorize(runtime, session_id, actor)
            if json.loads(current.workspace or "null") != reference:
                raise HTTPException(409, "The computer changed. Reconnect again.")
            async with runtime.database.sessions() as db:
                desktop = await db.get(ComputerDesktop, session_id)
                lease = await db.get(ComputerLease, session_id)
                return ComputerControlResult(
                    available=bool(desktop and desktop.workspace_id == reference["id"]
                                   and desktop.version == DESKTOP_VERSION),
                    controlled=bool(lease and lease.actor_id == actor.id and lease.expires_at > time.time()
                                    and desktop and desktop.control_client == client_id),
                )
        return await restore_computer(runtime, session, actor, reference)
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


async def restore_computer(
    runtime: Runtime, session: Session, actor: User, reference: dict[str, str],
) -> ComputerControlResult:
    database = runtime.database
    if not (
        (session.status == "waiting" and session.active_turn)
        or (session.status in {"idle", "failed", "stopped"} and not session.active_turn)
    ):
        raise HTTPException(409, "Let the running task finish or stop it, then reconnect its computer.")
    async with database.sessions() as db:
        turn = await db.get(Turn, session.active_turn) if session.active_turn else await db.scalar(
            select(Turn).where(
                Turn.session_id == session.id, Turn.preparation.is_not(None),
            ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(1),
        )
        if turn is None or not turn.preparation:
            raise HTTPException(409, "The original computer's task context is unavailable.")
        if turn.status not in {"waiting", "completed", "failed", "stopped"}:
            raise HTTPException(409, "Let the previous task settle, then reconnect its computer.")
        prepared = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(turn.preparation))
        if prepared.scope.turn_id != turn.id:
            raise PermissionError("The computer's original preparation changed.")
        conversation = ConversationTypeAdapter.validate_json(
            database.security.unseal(turn.conversation),
        ) if turn.conversation and turn.status == "waiting" else None
    await answer_actor(runtime, actor, turn)
    from .browser_login import TOOL_NAME

    pending = conversation.deferred_tool_requests if conversation else None
    originals = {
        str(metadata["workspace_id"])
        for call in pending.calls if call.tool_name == TOOL_NAME
        if (metadata := (pending.metadata or {}).get(call.tool_call_id) or {}).get("kind") == "browser_login"
        and metadata.get("workspace_id")
    } if pending else set()
    scope = prepared.scope
    get_current_span().set_attributes(scope_attributes(scope))
    turn_status = turn.status
    operation_id = new_id()
    async with database.sessions.begin() as db:
        current, _ = await locked_computer(database, db, scope, session, turn_status)
        if json.loads(current.workspace or "null") != reference:
            raise HTTPException(409, "The computer changed. Reconnect again.")
        from .maintenance_workspace import maintenance_workspace

        if maintenance_workspace(current.snapshot) is not None:
            raise HTTPException(409, "This computer is preserved for maintenance. Wait for execution to resume.")
        # A dead machine cannot accept this requester's input. Revoke its lease
        # so heartbeats from a disconnected tab cannot prevent recovery forever.
        lease = await db.get(ComputerLease, session.id)
        if lease and await database.same_person(db, actor, lease.actor_id):
            await db.delete(lease)
            await db.flush()
        await require_settled(db, session.id)
        saved = await validate_checkpoint(
            database, db, current, scope, reference, require_image=False, allow_stopped=True,
        ) if current.snapshot else None
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
            restoring = bool(saved and saved.image_id)
            if saved and saved.image_id:
                try:
                    with operation_span(database, "moyai.computer.restore", attributes={
                        **scope_attributes(scope), **workspace_attributes(reference),
                        "moyai.operation.id": operation_id,
                    }):
                        sandbox = await restored_sandbox(runtime, scope, saved)
                except ApplicationError as error:
                    # Validation already passed. Only an authoritative missing
                    # image permits fresh allocation; transport errors do not.
                    if error.type != PREPARATION_BLOCKED_TYPE or error.details != ("workspace_checkpoint_missing",):
                        raise
                    restoring = False
                    with operation_span(database, "moyai.computer.create", attributes={
                        **scope_attributes(scope), "moyai.recovery.reason": "checkpoint_missing",
                        "moyai.operation.id": operation_id,
                    }):
                        sandbox = await fresh_sandbox(runtime, scope, reference)
            else:
                with operation_span(database, "moyai.computer.create", attributes={
                    **scope_attributes(scope), "moyai.recovery.reason": "no_saved_checkpoint",
                    "moyai.operation.id": operation_id,
                }):
                    sandbox = await fresh_sandbox(runtime, scope, reference)
            if await sandbox.poll.aio() is not None:
                raise HTTPException(409, "The restored computer expired. Reconnect again.")
            await authorize(runtime, session.id, actor)
            await answer_actor(runtime, actor, turn)
            replacement = {"provider": "modal", "id": sandbox.object_id}
            workspace = Workspace(ModalSandbox(working_dir="/workspace").backend(
                TypeAdapter(WorkspaceRef).validate_python(replacement),
            ))
            with operation_span(database, "moyai.computer.desktop_start", attributes={
                **scope_attributes(scope), **workspace_attributes(replacement),
                "moyai.operation.id": operation_id,
            }):
                await guest_request(workspace, {"action": "start", "deadline": time.time() + 570})
        # Authority may have changed while Modal was creating the desktop.
        await authorize(runtime, session.id, actor)
        await answer_actor(runtime, actor, turn)
        async with database.sessions.begin() as db:
            current, _ = await locked_computer(database, db, scope, session, turn_status)
            operation = await db.get(ComputerOperation, session.id)
            if (
                operation is None or operation.identity != operation_id or operation.expires_at <= time.time()
                or current.revision != revision or current.snapshot != expected
                or json.loads(current.workspace or "null") != reference
            ):
                raise HTTPException(409, "The conversation changed during recovery. Reopen its Computer panel.")
            await db.delete(operation)
            await db.flush()
            await require_settled(db, session.id)
            if saved:
                await validate_checkpoint(
                    database, db, current, scope, reference, require_image=False, allow_stopped=True,
                )
            if restoring and runtime.product is not None:
                await preserve_restoration(db, session.id, reference["id"], replacement["id"], scope.workspace_key)
            current.workspace = json.dumps(replacement)
            # Files are still the saved image until a person takes control.
            # Keeping the binding current permits another safe idle recovery.
            current.snapshot = saved.model_copy(update={"workspace": replacement}).model_dump_json() if restoring and saved else None
            notice = RESTORED_NOTICE if restoring else FRESH_NOTICE
            current.save_warning = None if restoring else notice
            current.revision += 1
            db.add(Event(
                session_id=session.id, turn_id=scope.turn_id, kind="computer_recovered",
                identity=f"computer-recovered:{operation_id}",
                content=database.security.seal({
                    "original": reference["id"], "restored": replacement["id"],
                    "files_restored": restoring, "notice": notice,
                    "previous_snapshot": expected,
                }),
            ))
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
        # Publication committed. Do not report a recovered computer before its
        # authority and revision fences have passed.
        attributes = {
            **scope_attributes(scope), **workspace_attributes(replacement),
            "moyai.operation.id": operation_id, "moyai.computer.previous_id": reference["id"],
            "moyai.recovery.mode": "restored" if restoring else "fresh",
            "moyai.recovery.files_restored": restoring,
        }
        get_current_span().set_attributes(attributes)
        get_current_span().add_event("moyai.computer.recovered", attributes=attributes)
        return ComputerControlResult(available=True, controlled=False, notice=notice)
    finally:
        # A failed or abandoned request cannot clear another recovery's fence.
        async with database.sessions.begin() as db:
            await db.execute(delete(ComputerOperation).where(
                ComputerOperation.session_id == session.id, ComputerOperation.identity == operation_id,
            ))
