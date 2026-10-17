"""Checkpoint files at settled SDK boundaries; restore before native execution.

Temporal owns retries. These records never authorize a tool replay or replace a
conversation, approval, effect receipt, or canonical execution context.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Any, Literal

import modal
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import select, update

from .contracts import PreparedTurn, RunScope
from .models import ComputerLease, ComputerOperation, Effect, Session, Turn, User
from .performance import ComputerTiming, computer_timing
from .product_continuation_errors import preparation_blocked

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database
    from .runtime import Runtime

VERSION = "moyai-workspace-checkpoint-v1"


class WorkspaceCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    session_id: str
    workspace: dict[str, str]
    scope: str
    turn_id: str
    result_fingerprint: str
    conversation_digest: str
    image_id: str | None = None
    product_context: str | None = None
    restoration_manifest: str | None = None


def checkpoint(value: str | None) -> WorkspaceCheckpoint | None:
    # Pre-checkpoint raw image IDs have no source/authority binding. They are
    # retained for evidence, but cannot silently authorize restoration.
    if not value:
        return None
    try:
        return WorkspaceCheckpoint.model_validate_json(value)
    except ValidationError:
        return None


def pending_checkpoint(
    scope: RunScope, reference: dict, conversation: str, result_fingerprint: str,
) -> str:
    return WorkspaceCheckpoint(
        session_id=scope.session_id, workspace=reference, scope=scope.workspace_key, turn_id=scope.turn_id,
        result_fingerprint=result_fingerprint,
        conversation_digest=hashlib.sha256(conversation.encode()).hexdigest(),
    ).model_dump_json()


async def locked_scope(
    database: Database, db: AsyncSession, scope: RunScope, *, status: str,
) -> tuple[Session, Turn]:
    """Use the same source → actor → session lock order as turn admission."""
    turn = await db.get(Turn, scope.turn_id)
    if turn is None or turn.actor_id != scope.actor_id or turn.session_id != scope.session_id:
        raise PermissionError("The workspace task changed.")
    await database._check_product_turn(db, turn, scope.product_grant)
    actor_id = await db.scalar(update(User).where(
        User.id == scope.actor_id, User.enabled.is_(True),
    ).values(auth_version=User.auth_version).returning(User.id))
    if actor_id is None:
        raise PermissionError("The workspace requester no longer has access.")
    locked = await db.scalar(update(Session).where(
        Session.id == scope.session_id, Session.active_turn == scope.turn_id,
        Session.status == status, Session.deleted.is_(False),
    ).values(revision=Session.revision).returning(Session.id))
    if locked is None:
        raise PermissionError("The workspace task stopped or was superseded.")
    session = await db.get(Session, scope.session_id, populate_existing=True)
    await db.refresh(turn)
    assert session is not None
    if (
        turn.status != status or session.visibility != scope.visibility
        or session.surface != scope.surface
        or (session.visibility == "private" and session.owner_id != scope.actor_id)
    ):
        raise PermissionError("The workspace authority changed.")
    current = await database.scope_grants(db, scope, session)
    if [(g.id, g.revision) for g in current] != [(g.id, g.revision) for g in scope.grants]:
        raise PermissionError("Workspace account access changed.")
    return session, turn


async def require_settled(db: AsyncSession, session_id: str) -> None:
    if await db.scalar(select(Effect.id).where(
        Effect.session_id == session_id, Effect.state == "pending",
    ).limit(1)):
        raise preparation_blocked("workspace_uncertain_effect")
    operation = await db.get(ComputerOperation, session_id)
    lease = await db.get(ComputerLease, session_id)
    if (operation and operation.expires_at > time.time()) or (lease and lease.expires_at > time.time()):
        raise preparation_blocked("workspace_in_use")


async def product_bindings(db: AsyncSession, scope: RunScope, reference: dict) -> tuple[str | None, str | None]:
    from .product_continuation_models import ProductExecutionContext, ProductWorkspaceRestoration

    context = await db.get(ProductExecutionContext, scope.turn_id)
    restoration = await db.get(ProductWorkspaceRestoration, (scope.session_id, reference["id"]))
    if restoration is not None and restoration.workspace_scope != scope.workspace_key:
        raise PermissionError("The workspace's original restoration scope changed.")
    return (
        context.fingerprint if context else None,
        restoration.manifest_fingerprint if restoration else None,
    )


async def live_sandbox(reference: dict) -> bool:
    """Only an authoritative missing/terminated result permits restoration."""
    try:
        sandbox = await modal.Sandbox.from_id.aio(reference["id"])
        return await sandbox.poll.aio() is None
    except (modal.exception.NotFoundError, modal.exception.SandboxTerminatedError,
            modal.exception.SandboxTimeoutError):
        return False


async def validate_checkpoint(
    database: Database, db: AsyncSession, session: Session, scope: RunScope, reference: dict,
) -> WorkspaceCheckpoint:
    saved = checkpoint(session.snapshot)
    if (
        saved is None or not saved.image_id or saved.workspace != reference or saved.session_id != session.id
        or saved.scope != scope.workspace_key or session.workspace_scope != saved.scope
        or session.conversation is None
        or hashlib.sha256(database.security.unseal(session.conversation)).hexdigest() != saved.conversation_digest
    ):
        raise preparation_blocked("workspace_checkpoint_missing")
    source = await db.get(Turn, saved.turn_id)
    if (
        source is None or source.session_id != session.id or not source.result_accepted
        or source.result_fingerprint != saved.result_fingerprint
        or source.status not in {"completed", "waiting", "resumed"}
    ):
        raise preparation_blocked("workspace_checkpoint_missing")
    if database.product_authority_enabled and await product_bindings(db, scope, reference) != (
        saved.product_context, saved.restoration_manifest,
    ):
        raise preparation_blocked("workspace_checkpoint_missing")
    await require_settled(db, session.id)
    return saved


async def restored_sandbox(runtime: Runtime, scope: RunScope, saved: WorkspaceCheckpoint):
    """Stable names recover a lost create reply across Temporal activity retries."""
    assert saved.image_id is not None
    name = "restore-" + hashlib.sha256(
        f"{scope.session_id}:{scope.turn_id}:{saved.image_id}:{saved.workspace['id']}".encode()
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
            app=app, name=name, image=modal.Image.from_id(saved.image_id), workdir="/workspace",
            timeout=runtime.settings.computer_lifetime, idle_timeout=runtime.settings.computer_idle_timeout,
        )
    except modal.exception.AlreadyExistsError:
        return await modal.Sandbox.from_name.aio(runtime.settings.modal_app, name)
    except modal.exception.NotFoundError as error:
        raise preparation_blocked("workspace_checkpoint_missing") from error


async def preserve_restoration(db: AsyncSession, session_id: str, old_id: str, new_id: str, scope: str) -> None:
    from .product_continuation_models import ProductWorkspaceRestoration

    original = await db.get(ProductWorkspaceRestoration, (session_id, old_id))
    if original is None:
        return
    if original.workspace_scope != scope:
        raise PermissionError("The saved workspace restoration scope changed.")
    existing = await db.get(ProductWorkspaceRestoration, (session_id, new_id))
    if existing is not None:
        if (
            existing.workspace_scope != original.workspace_scope
            or existing.manifest_fingerprint != original.manifest_fingerprint
        ):
            raise PermissionError("The restored computer has conflicting provenance.")
        return
    db.add(ProductWorkspaceRestoration(
        session_id=session_id, workspace_id=new_id, workspace_scope=original.workspace_scope,
        manifest_fingerprint=original.manifest_fingerprint, files=original.files, bytes=original.bytes,
    ))


async def prepare_workspace(runtime: Runtime, prepared: PreparedTurn) -> PreparedTurn:
    reference = prepared.workspace
    if not reference or reference.get("provider") not in {"modal", "lambda"}:
        return prepared
    async with computer_timing(runtime.database, prepared.scope, "reuse", preparation=True) as timing:
        return await _prepare_workspace(runtime, prepared, reference, timing)


async def _prepare_workspace(
    runtime: Runtime, prepared: PreparedTurn, reference: dict[str, Any], timing: ComputerTiming,
) -> PreparedTurn:
    database, scope = runtime.database, prepared.scope
    await database.authorize_scope(scope)
    async with database.sessions.begin() as db:
        session, turn = await locked_scope(database, db, scope, status="running")
        if json.loads(session.workspace or "null") != reference:
            raise PermissionError("The computer changed before preparation.")
        preparation, revision = turn.preparation, session.revision
    async with asyncio.timeout(30):
        if reference["provider"] == "lambda":
            from pydantic_ai.workspaces import WorkspaceUnavailableError

            from .lambda_control import LambdaControl
            try:
                alive = await (await LambdaControl(runtime.settings).get(reference["id"])).alive()
            except WorkspaceUnavailableError:
                alive = False
        else:
            alive = await live_sandbox(reference)
    saved, expected = None, None
    if not alive:
        timing.mode = "restore"
        async with database.sessions.begin() as db:
            session, _ = await locked_scope(database, db, scope, status="running")
            if json.loads(session.workspace or "null") != reference:
                raise PermissionError("The computer changed before restoration.")
            if session.revision != revision:
                raise RuntimeError("Preparation changed before restore; retry the authorized preparation.")
            saved = await validate_checkpoint(database, db, session, scope, reference)
            expected = session.snapshot
        if reference["provider"] == "lambda":
            from .lambda_control import LambdaControl
            name = "native-restore:" + hashlib.sha256(
                f"{scope.workspace_key}:{scope.turn_id}:{saved.image_id}:{reference['id']}".encode()
            ).hexdigest()
            restored = await LambdaControl(runtime.settings).create(name, checkpoint=saved.image_id)
            replacement = {"provider": "lambda", "id": restored.id}
        else:
            async with asyncio.timeout(180):
                sandbox = await restored_sandbox(runtime, scope, saved)
                if await sandbox.poll.aio() is not None:
                    raise preparation_blocked("workspace_checkpoint_missing")
            replacement = {"provider": "modal", "id": sandbox.object_id}
    else:
        replacement = reference
    async with database.sessions.begin() as db:
        session, turn = await locked_scope(database, db, scope, status="running")
        if (
            json.loads(session.workspace or "null") != reference or session.workspace_scope != scope.workspace_key
            or turn.preparation != preparation
        ):
            raise PermissionError("The computer changed before restoration could be published.")
        if session.revision != revision:
            raise RuntimeError("Preparation changed during restore; retry before executing any model or tool.")
        if saved is not None:
            if session.snapshot != expected:
                raise preparation_blocked("workspace_checkpoint_missing")
            await validate_checkpoint(database, db, session, scope, reference)
            if runtime.product is not None:
                await preserve_restoration(db, session.id, reference["id"], replacement["id"], scope.workspace_key)
        prepared.workspace = replacement
        session.workspace = json.dumps(replacement)
        # Once the SDK can mutate this computer, the preceding savepoint is no
        # longer current. A crash cannot silently roll files back to that point.
        session.snapshot = None
        session.revision += 1
        turn.preparation = runtime.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
    return prepared
