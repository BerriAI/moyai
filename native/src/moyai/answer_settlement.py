"""Accept answers and settle their computer checkpoints without losing durable evidence.

Temporal retains the existing activity identities and ordering. This module owns
result acceptance, snapshot selection/publication and failure/Stop settlement;
external capture always runs outside the publication transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol
from uuid import NAMESPACE_URL, uuid5

import modal
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_harness import ModalSandbox
from sqlalchemy import delete, literal, select, update

from .contracts import PreparedTurn, RunScope
from .models import ComputerOperation, Message, Session, Turn, now
from .product_continuation_errors import preparation_blocked
from .workspace_recovery import (
    checkpoint,
    locked_scope,
    pending_checkpoint,
    product_bindings,
    require_settled,
)

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)
SNAPSHOT_WARNING = (
    "The response was saved, but the computer checkpoint could not be confirmed. "
    "Keep this computer available; automatic restoration is unavailable."
)


class SnapshotCapture(Protocol):
    async def capture(self, reference: WorkspaceRef, *, require_live: bool) -> str: ...


class ModalSnapshots:
    async def capture(self, reference: WorkspaceRef, *, require_live: bool) -> str:
        if require_live:
            async with asyncio.timeout(150):
                sandbox = await modal.Sandbox.from_id.aio(reference.id)
                if await sandbox.poll.aio() is not None:
                    raise preparation_blocked("workspace_checkpoint_missing")
                # Keep the pinned SDK's image retention policy and existing
                # timeouts. Missing images never authorize a blank restoration.
                image = await sandbox.snapshot_filesystem.aio(timeout=120)
        else:
            # Recorded workflows retain the Harness's historical capture path.
            sandbox = await ModalSandbox().backend(reference).get_sandbox()
            image = await sandbox.snapshot_filesystem.aio(timeout=120)
        return image.object_id


class ProviderSnapshots:
    def __init__(self, database: Database):
        self.database = database

    async def capture(self, reference: WorkspaceRef, *, require_live: bool) -> str:
        if reference.provider == "lambda":
            from .lambda_control import LambdaControl
            vm = await LambdaControl(self.database.settings).get(reference.id)
            await vm.running()
            identity, _ = await vm.checkpoint()
            return identity
        return await ModalSnapshots().capture(reference, require_live=require_live)


class AnswerSettlement:
    def __init__(self, database: Database, *, snapshots: SnapshotCapture | None = None):
        self.database = database
        self.snapshots = snapshots if snapshots is not None else ProviderSnapshots(database)

    async def save_computer(self, turn_id: str, reference: dict | None, *, product: bool) -> None:
        if not reference:
            await self.settle(turn_id)
            return
        ref = TypeAdapter(WorkspaceRef).validate_python(reference)
        if ref.provider not in {"modal", "lambda"}:
            # Local workspaces are used only by deterministic supplementary tests.
            await self.settle(turn_id)
            return
        try:
            if await self._save_checkpoint(turn_id, reference, product=product):
                return
            image = await self.snapshots.capture(ref, require_live=False)
        except Exception:
            logger.warning("Computer snapshot settlement failed; the answer remains saved.", exc_info=True)
            await self.settle(
                turn_id,
                warning="The response was saved, but its computer checkpoint could not be confirmed. "
                        "Keep this computer available; automatic restoration is unavailable.",
            )
            return
        await self.settle(turn_id, snapshot=image)

    async def save_result(
        self, scope: RunScope, *, conversation: str, output: str,
        workspace: dict[str, Any] | None, usage: dict[str, Any],
        deferred: bool = False,
        checkpoint_workspace: bool = False,
    ) -> bool:
        result_identity = {
            "conversation": conversation, "output": output, "workspace": workspace,
            "usage": usage, "deferred": deferred,
        }
        if checkpoint_workspace:
            result_identity["checkpoint_workspace"] = True
        fingerprint = hashlib.sha256(json.dumps(result_identity, sort_keys=True).encode()).hexdigest()
        checkpointing = checkpoint_workspace and bool(workspace and workspace.get("provider") in {"modal", "lambda"})
        async with self.database.sessions.begin() as db:
            turn = await db.get(Turn, scope.turn_id)
            session = await db.get(Session, scope.session_id)
            if turn is None or session is None:
                raise ValueError("Unknown turn.")
            if turn.result_fingerprint is not None:
                if turn.result_fingerprint != fingerprint:
                    raise ValueError("The completed result for this turn is immutable.")
                return bool(turn.result_accepted)
            product_allowed = True
            if self.database.product_authority_enabled:
                try:
                    await self.database._check_product_turn(db, turn, scope.product_grant)
                except PermissionError:
                    product_allowed = False
                except HTTPException as error:
                    if error.status_code not in {401, 403}:
                        raise
                    product_allowed = False
            # This is the first write in the transaction. Stop fences the same
            # session row first, so whichever transaction wins decides publication.
            accepted = await db.scalar(update(Session).where(
                Session.id == scope.session_id,
                Session.active_turn == turn.id,
                Session.status == "running",
                literal(product_allowed),
            ).values(
                conversation=self.database.security.seal(conversation),
                workspace=json.dumps(workspace) if workspace else session.workspace,
                workspace_scope=scope.workspace_key if workspace else session.workspace_scope,
                last_actor_id=scope.actor_id, last_model=scope.model,
                last_grants=scope.grants_key,
                revision=Session.revision + 1, updated_at=now(),
                status="waiting" if deferred and not checkpointing else "saving",
            ).returning(Session.id))
            # Preserve the factual result, even when Stop won the publication race.
            turn.result = self.database.security.seal(output)
            turn.conversation = self.database.security.seal(conversation)
            turn.result_fingerprint = fingerprint
            turn.usage = json.dumps(usage)
            turn.finished_at = now()
            if accepted is None:
                turn.result_accepted = False
                return False
            turn.result_accepted = True
            turn.status = "waiting" if deferred and not checkpointing else "saving"
            if deferred:
                from .questions import record_optional_deadline
                record_optional_deadline(self.database, db, turn)
            if checkpointing:
                if turn.preparation is None or workspace is None:
                    raise ValueError("A checkpoint requires the original prepared workspace.")
                prepared = TypeAdapter(PreparedTurn).validate_json(self.database.security.unseal(turn.preparation))
                prepared.checkpoint_workspace = True
                prepared.checkpoint_waiting = deferred
                turn.preparation = self.database.security.seal(TypeAdapter(PreparedTurn).dump_json(prepared))
                session.snapshot = pending_checkpoint(scope, workspace, conversation, fingerprint)
            await db.execute(update(Turn).where(
                Turn.parent_turn == turn.id, Turn.status == "accepted"
            ).values(status="included", finished_at=now()))
            if not deferred:
                existing = await db.scalar(select(Message.id).where(Message.identity == f"answer:{turn.id}"))
                if existing is None:
                    db.add(Message(
                        session_id=session.id, turn_id=turn.id, role="assistant",
                        content=self.database.security.seal(output), identity=f"answer:{turn.id}",
                    ))
                if self.database.product_memory_review is not None:
                    await self.database.product_memory_review.enqueue_in(db, turn, session)
            return True

    async def _save_checkpoint(self, turn_id: str, reference: dict, *, product: bool) -> bool:
        """Return False for an older workflow, whose existing settlement stays valid."""
        database = self.database
        async with database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.preparation is None:
                return False
            prepared = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(turn.preparation))
            if not prepared.checkpoint_workspace:
                return False
        identity = str(uuid5(NAMESPACE_URL, "moyai-checkpoint:" + turn_id))
        async with database.sessions.begin() as db:
            session, turn = await locked_scope(database, db, prepared.scope, status="saving")
            saved = checkpoint(session.snapshot)
            if (
                saved is None or saved.workspace != reference or saved.turn_id != turn_id
                or saved.session_id != session.id or session.workspace_scope != prepared.scope.workspace_key
                or saved.scope != prepared.scope.workspace_key
                or saved.result_fingerprint != turn.result_fingerprint
                or json.loads(session.workspace or "null") != reference
            ):
                raise PermissionError("The checkpoint source changed before capture.")
            await require_settled(db, session.id)
            if product:
                context, manifest = await product_bindings(db, prepared.scope, reference)
                saved = saved.model_copy(update={"product_context": context, "restoration_manifest": manifest})
                session.snapshot = saved.model_dump_json()
            await db.execute(delete(ComputerOperation).where(ComputerOperation.session_id == session.id))
            lease_seconds = (
                database.settings.lambda_checkpoint_timeout + 120 if reference["provider"] == "lambda" else 240
            )
            db.add(ComputerOperation(session_id=session.id, identity=identity, expires_at=int(time.time()) + lease_seconds))
            expected, revision = session.snapshot, session.revision
        try:
            image = await self.snapshots.capture(TypeAdapter(WorkspaceRef).validate_python(reference), require_live=True)
            # A returned image is evidence, but publication still needs current
            # authority and the exact source. Stop/revocation cannot resurrect it.
            async with database.sessions.begin() as db:
                session, turn = await locked_scope(database, db, prepared.scope, status="saving")
                operation = await db.get(ComputerOperation, session.id)
                if (
                    session.snapshot != expected or session.revision != revision
                    or json.loads(session.workspace or "null") != reference
                    or operation is None or operation.identity != identity
                    or operation.expires_at <= time.time()
                ):
                    raise PermissionError("The computer changed while it was being saved.")
                if product and await product_bindings(db, prepared.scope, reference) != (
                    saved.product_context, saved.restoration_manifest,
                ):
                    raise PermissionError("The computer's project context changed during capture.")
                session.snapshot = saved.model_copy(update={"image_id": image}).model_dump_json()
                self._settle_answer(turn, session)
            return True
        finally:
            async with database.sessions.begin() as db:
                await db.execute(delete(ComputerOperation).where(
                    ComputerOperation.session_id == prepared.scope.session_id,
                    ComputerOperation.identity == identity,
                ))

    async def settle(self, turn_id: str, *, snapshot: str | None = None, warning: str | None = None) -> None:
        async with self.database._admission_session(None) as db:
            session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
            if session_id is None:
                raise ValueError("Unknown turn.")
            # Read the mutable settlement intent only after taking Stop's lock.
            # Otherwise a stale "saving" object can resurrect a stopped approval.
            await db.execute(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision))
            turn = await db.get(Turn, turn_id)
            if turn is None:
                raise ValueError("Unknown turn.")
            session = await db.get(Session, turn.session_id)
            if session is None or session.active_turn != turn.id or turn.status != "saving":
                return
            self._settle_answer(turn, session, snapshot=snapshot, warning=warning)

    def _settle_answer(
        self, turn: Turn, session: Session | None, *, snapshot: str | None = None, warning: str | None = None,
    ) -> None:
        """Later cleanup cannot downgrade a published answer to failure or Stop."""
        prepared = TypeAdapter(PreparedTurn).validate_json(
            self.database.security.unseal(turn.preparation)
        ) if turn.preparation else None
        if prepared and prepared.checkpoint_waiting:
            stopped = session is None or session.deleted or session.status == "stopped"
            turn.status = "stopped" if stopped else "waiting"
            if session is not None and session.active_turn == turn.id:
                session.save_warning = warning
                session.status = "stopped" if stopped else "waiting"
                if stopped:
                    session.active_turn = None
            return
        turn.status = "completed"
        if session is not None and session.active_turn == turn.id:
            stopped = session.deleted or session.status == "stopped"
            if snapshot and not stopped:
                session.snapshot = snapshot
            session.save_warning = warning
            # The accepted answer stays completed, but its bookkeeping cannot
            # clear a later Stop/deletion or publish a late computer image.
            session.status = "stopped" if stopped else "idle"
            session.active_turn = None

    async def fail(self, turn_id: str, message: str, *, only_unsettled: bool = False) -> None:
        async with self.database._admission_session(None) as db:
            # Resolve ownership without caching a mutable Turn. Stop, deletion
            # and answer publication fence the session first; take that same
            # lock before reading the outcome this failure is allowed to settle.
            session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
            if session_id is None:
                return
            locked = await db.scalar(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision).returning(Session.id))
            if locked is None:
                return
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            if turn.status in ("completed", "stopped"):
                return
            if only_unsettled and turn.status not in ("starting", "running", "saving"):
                return
            session = await db.get(Session, session_id)
            if turn.status == "saving" and turn.result_accepted:
                self._settle_answer(
                    turn, session,
                    warning="The answer was saved, but computer snapshot settlement was interrupted.",
                )
                return
            turn.status = "failed"
            turn.error = message
            turn.finished_at = now()
            if session and session.active_turn == turn.id:
                session.active_turn = None
                session.status = "failed"

    async def stopped(self, turn_id: str, conversation: str | None = None) -> None:
        async with self.database._admission_session(None) as db:
            session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
            if session_id is None:
                return
            await db.execute(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision))
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            session = await db.get(Session, turn.session_id)
            if session is None or session.active_turn != turn.id:
                return
            if turn.status == "saving" and turn.result_accepted:
                self._settle_answer(
                    turn, session,
                    warning="The answer was saved before Stop interrupted computer snapshot settlement.",
                )
                return
            if conversation:
                session.conversation = self.database.security.seal(conversation)
                session.last_actor_id = turn.actor_id
                session.last_model = turn.model
                if turn.preparation:
                    prepared = TypeAdapter(PreparedTurn).validate_json(
                        self.database.security.unseal(turn.preparation)
                    )
                    session.last_grants = prepared.scope.grants_key
            turn.status = "stopped"
            turn.finished_at = now()
            session.active_turn = None
            session.status = "stopped"
