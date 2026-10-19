"""Preserve settled context and files before a failed turn releases its computer.

This is not an execution retry. A new user turn must still pass ordinary
admission, current authority, and unresolved-effect checks before restoration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_harness.step_persistence import is_provider_valid
from sqlalchemy import delete

from .contracts import PreparedTurn
from .history_handoff import latest_turn_snapshot
from .models import ComputerOperation, Session, Turn
from .workspace_recovery import WorkspaceCheckpoint, locked_scope, product_bindings, require_settled

if TYPE_CHECKING:
    from .runtime import Runtime

logger = logging.getLogger(__name__)
WARNING = "Work stopped before its computer checkpoint could be saved. Keep this computer available to recover the files."


async def preserve_failed_workspace(runtime: Runtime, turn_id: str) -> None:
    database = runtime.database
    async with database.sessions() as db:
        turn = await db.get(Turn, turn_id)
        if turn is None or not turn.preparation or turn.result_accepted:
            return
        prepared = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(turn.preparation))
    scope = prepared.scope
    identity = str(uuid5(NAMESPACE_URL, "moyai-failure-checkpoint:" + turn_id))
    try:
        snapshot = await latest_turn_snapshot(runtime, turn_id, include_interrupted=True)
        if snapshot is None or not is_provider_valid(snapshot.messages):
            snapshot = await latest_turn_snapshot(runtime, turn_id)
        if snapshot is None or not is_provider_valid(snapshot.messages):
            raise ValueError("No settled conversation is available.")
        conversation = ConversationTypeAdapter.dump_json(Conversation(
            messages=snapshot.messages, conversation_id=scope.conversation_id,
        ))
        digest = hashlib.sha256(conversation).hexdigest()
        async with database.sessions.begin() as db:
            session, turn = await locked_scope(database, db, scope, status="running")
            # A pending external write must retain its original review barrier.
            await require_settled(db, session.id)
            reference = json.loads(session.workspace or "null")
            session.conversation = turn.conversation = database.security.seal(conversation)
            session.last_actor_id, session.last_model = scope.actor_id, scope.model
            session.last_grants = scope.grants_key
            session.revision += 1
            session.save_warning = WARNING
            if not reference or reference.get("provider") not in {"modal", "lambda"}:
                return
            if session.workspace_scope != scope.workspace_key:
                raise PermissionError("The workspace authority changed.")
            context, manifest = await product_bindings(db, scope, reference) if runtime.product else (None, None)
            saved = WorkspaceCheckpoint(
                kind="interrupted", session_id=session.id, workspace=reference, scope=scope.workspace_key,
                turn_id=turn_id, conversation_digest=digest, result_fingerprint=digest,
                product_context=context, restoration_manifest=manifest,
            )
            await db.execute(delete(ComputerOperation).where(ComputerOperation.session_id == session.id))
            lease_seconds = database.settings.lambda_checkpoint_timeout + 120 if reference["provider"] == "lambda" else 240
            db.add(ComputerOperation(session_id=session.id, identity=identity, expires_at=int(time.time()) + lease_seconds))
            expected, revision = session.snapshot, session.revision
        image = await database.answers.snapshots.capture(TypeAdapter(WorkspaceRef).validate_python(reference), require_live=True)
        async with database.sessions.begin() as db:
            session, turn = await locked_scope(database, db, scope, status="running")
            operation = await db.get(ComputerOperation, session.id)
            if (
                session.snapshot != expected or session.revision != revision
                or json.loads(session.workspace or "null") != reference
                or operation is None or operation.identity != identity or operation.expires_at <= time.time()
            ):
                raise PermissionError("The computer changed during failure checkpoint capture.")
            if runtime.product and await product_bindings(db, scope, reference) != (context, manifest):
                raise PermissionError("The computer context changed during capture.")
            session.snapshot = saved.model_copy(update={"image_id": image}).model_dump_json()
            session.save_warning = None
    except Exception:
        logger.warning("Could not preserve the failed turn's workspace.", exc_info=True)
        async with database.sessions.begin() as db:
            session = await db.get(Session, scope.session_id)
            if session and session.active_turn == turn_id and session.status == "running":
                session.save_warning = WARNING
    finally:
        async with database.sessions.begin() as db:
            await db.execute(delete(ComputerOperation).where(
                ComputerOperation.session_id == scope.session_id, ComputerOperation.identity == identity,
            ))
