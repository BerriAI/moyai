"""Save active computer files and continue a settled graph on a replacement.

Only the workflow's completed tool-batch boundary enters this module. Recovery
keeps the current graph, including failed and completed tool results; it never
rewinds a tool call to match an older filesystem image.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import delete, select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .continuations.product_continuation_errors import preparation_blocked
from .history_handoff import AgentCheckpoint, CheckpointSave, save_history_checkpoint
from .models import ComputerDesktop, ComputerLease, ComputerOperation, Event, new_id
from .workspace_recovery import (
    WorkspaceCheckpoint,
    live_sandbox,
    locked_scope,
    preserve_restoration,
    product_bindings,
    require_settled,
    restored_sandbox,
)

if TYPE_CHECKING:
    from .runtime import Runtime

VERSION = "moyai-active-workspace-recovery-v1"
BOUNDARY_KIND = "active_workspace_boundary"
RECOVERY_NOTICE = (
    "The computer expired during this task. Moyai restored a replacement from the last saved filesystem "
    "checkpoint and retained the current task history and tool results. File changes after that checkpoint "
    "may be missing. Inspect current files before using them. If using the browser, "
    "list its current tabs before reusing any tab IDs. "
    "Do not repeat completed actions or actions with an uncertain outcome."
)
RECOVERY_STATUS = (
    "The computer expired. Restored its latest saved files and continued this task. "
    "Changes since the last backup may be missing."
)
SAVE_WARNING = "The active computer's latest files could not be backed up. The task is continuing."
logger = logging.getLogger(__name__)


class ActiveWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["active-workspace-v1"] = "active-workspace-v1"
    session_id: str
    turn_id: str
    scope: str
    workflow_id: str
    workspace: dict[str, str]
    graph: str
    image_id: str
    product_context: str | None = None
    restoration_manifest: str | None = None


def active_workspace(value: str | None) -> ActiveWorkspace | None:
    if value:
        try:
            return ActiveWorkspace.model_validate_json(value)
        except ValidationError:
            pass
    return None


def restored_graph(graph: AgentCheckpoint, replacement: dict[str, str]) -> AgentCheckpoint:
    conversation = ConversationTypeAdapter.validate_json(graph.conversation)
    last = conversation.messages[-1]
    if not isinstance(last, ModelRequest):
        raise preparation_blocked("workspace_checkpoint_invalid")
    conversation.messages[-1] = replace(last, parts=[*last.parts, UserPromptPart(RECOVERY_NOTICE)])
    return replace(
        graph, prepared=replace(graph.prepared, workspace=replacement), workspace=replacement,
        conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        deployment_handoff=None, allow_history_only=False,
    )


@activity.defn(name="moyai_active_workspace_boundary_v1")
async def active_workspace_boundary(graph: AgentCheckpoint, recover_only: bool = False) -> CheckpointSave:
    from .runtime import current

    info = activity.info()
    if not info.workflow_id or not info.workflow_run_id:
        raise PermissionError("Active computer recovery requires its workflow execution.")
    return await preserve_or_recover(
        current(), graph, workflow_id=info.workflow_id, workflow_run_id=info.workflow_run_id,
        recover_only=recover_only,
    )


async def preserve_or_recover(
    runtime: Runtime, graph: AgentCheckpoint, *, workflow_id: str, workflow_run_id: str,
    recover_only: bool = False,
) -> CheckpointSave:
    reference = graph.workspace
    if not reference or reference.get("provider") != "modal":
        return CheckpointSave()
    database, scope = runtime.database, graph.prepared.scope
    await database.authorize_scope(scope)
    graph = replace(
        graph, prepared=replace(graph.prepared, workspace=reference),
        deployment_handoff=None, allow_history_only=False,
    )
    graph_ref = runtime.security.object_id(TypeAdapter(AgentCheckpoint).dump_json(graph))
    identity = BOUNDARY_KIND + ":" + hashlib.sha256(
        json.dumps([workflow_run_id, graph_ref]).encode(),
    ).hexdigest()
    operation_id = str(uuid5(NAMESPACE_URL, identity))

    async with database.sessions.begin() as db:
        session, turn = await locked_scope(database, db, scope, status="running")
        await database.swarms.authorize(db, turn, scope=scope)
        await database.goals.authorize(db, turn)
        if turn.workflow_id != workflow_id:
            raise PermissionError("This checkpoint belongs to another workflow.")
        if session.workspace is None:
            # Lazy SDK allocation can become visible at the first graph node,
            # before Authority.before_model_request records its reference.
            session.workspace = json.dumps(reference)
            session.workspace_scope = scope.workspace_key
            session.revision += 1
        receipt = await db.scalar(select(Event).where(Event.identity == identity))
        if receipt is not None:
            saved = json.loads(database.security.unseal(receipt.content))
            if (
                receipt.turn_id != scope.turn_id or receipt.session_id != session.id
                or saved["graph"] != graph_ref
                or json.loads(session.workspace or "null") != saved["workspace"]
            ):
                raise preparation_blocked("workspace_checkpoint_invalid")
            return CheckpointSave(saved["continuation"])
        if json.loads(session.workspace or "null") != reference or session.workspace_scope != scope.workspace_key:
            raise preparation_blocked("workspace_checkpoint_invalid")

    # A transport error is not proof of death. Let Temporal retry this read;
    # never replace a computer whose availability is merely unknown.
    async with asyncio.timeout(20):
        alive = await live_sandbox(reference)
    if recover_only and alive:
        return CheckpointSave()
    saved_graph = await save_history_checkpoint(graph)
    if saved_graph.reference is None:
        if alive:
            return CheckpointSave()
        raise preparation_blocked("workspace_uncertain_effect")

    async with database.sessions.begin() as db:
        session, turn = await locked_scope(database, db, scope, status="running")
        await database.swarms.authorize(db, turn, scope=scope)
        await database.goals.authorize(db, turn)
        if json.loads(session.workspace or "null") != reference or session.workspace_scope != scope.workspace_key:
            raise preparation_blocked("workspace_checkpoint_invalid")
        from .maintenance_workspace import maintenance_workspace

        if maintenance_workspace(session.snapshot) is not None:
            raise preparation_blocked("workspace_checkpoint_invalid")
        # Human control clears Session.snapshot. It cannot resurrect an older
        # active checkpoint merely because the corresponding event still exists.
        try:
            await require_settled(db, session.id)
        except ApplicationError:
            if alive:
                return CheckpointSave()
            raise
        point = active_workspace(session.snapshot)
        context, manifest = await product_bindings(db, scope, reference) if runtime.product else (None, None)
        usable = point is not None and (
            point.session_id == session.id and point.turn_id == scope.turn_id
            and point.scope == scope.workspace_key and point.workflow_id == workflow_id
            and point.workspace == reference and (point.product_context, point.restoration_manifest) == (context, manifest)
        )
        if not alive and not usable:
            raise preparation_blocked("workspace_checkpoint_missing")
        expected = session.snapshot
        await db.execute(delete(ComputerOperation).where(ComputerOperation.session_id == session.id))
        db.add(ComputerOperation(session_id=session.id, identity=operation_id, expires_at=int(time.time()) + 240))
        session.revision += 1
        revision = session.revision

    continuation = None
    replacement = reference
    try:
        if alive:
            try:
                image = await database.answers.snapshots.capture(
                    TypeAdapter(WorkspaceRef).validate_python(reference), require_live=True,
                )
            except Exception:
                # Snapshotting is a read of the computer, not a task action.
                # An unsuccessful capture must not discard a prior backup.
                logger.warning("Active filesystem checkpoint was unavailable.", exc_info=True)
                async with asyncio.timeout(20):
                    alive = await live_sandbox(reference)
                if alive:
                    async with database.sessions.begin() as db:
                        session, _ = await locked_scope(database, db, scope, status="running")
                        if session.revision == revision and session.snapshot == expected:
                            session.save_warning = SAVE_WARNING
                    return CheckpointSave()
                if not usable:
                    raise preparation_blocked("workspace_checkpoint_missing") from None
            else:
                if not image:
                    raise preparation_blocked("workspace_checkpoint_missing")
                point = ActiveWorkspace(
                    session_id=scope.session_id, turn_id=scope.turn_id, scope=scope.workspace_key,
                    workflow_id=workflow_id, workspace=reference, graph=saved_graph.reference,
                    image_id=image, product_context=context, restoration_manifest=manifest,
                )
        if not alive:
            assert point is not None
            files = WorkspaceCheckpoint(
                session_id=scope.session_id, turn_id=scope.turn_id, scope=scope.workspace_key,
                workspace=reference, image_id=point.image_id,
                conversation_digest=point.graph, result_fingerprint=point.graph,
            )
            async with asyncio.timeout(180):
                sandbox = await restored_sandbox(runtime, scope, files)
                if await sandbox.poll.aio() is not None:
                    raise preparation_blocked("workspace_checkpoint_missing")
            replacement = {"provider": "modal", "id": sandbox.object_id}
            continued = restored_graph(graph, replacement)
            continuation = await runtime.objects.put(TypeAdapter(AgentCheckpoint).dump_json(continued))
            point = point.model_copy(update={"workspace": replacement})

        async with database.sessions.begin() as db:
            session, turn = await locked_scope(database, db, scope, status="running")
            await database.swarms.authorize(db, turn, scope=scope)
            await database.goals.authorize(db, turn)
            operation = await db.get(ComputerOperation, session.id)
            if (
                operation is None or operation.identity != operation_id or operation.expires_at <= time.time()
                or session.revision != revision or session.snapshot != expected
                or json.loads(session.workspace or "null") != reference
                or session.workspace_scope != scope.workspace_key
            ):
                raise preparation_blocked("workspace_checkpoint_invalid")
            await db.delete(operation)
            await db.flush()
            await require_settled(db, session.id)
            if runtime.product and await product_bindings(db, scope, reference) != (context, manifest):
                raise preparation_blocked("workspace_authority_changed")
            if replacement != reference:
                if runtime.product:
                    await preserve_restoration(db, session.id, reference["id"], replacement["id"], scope.workspace_key)
                desktop = await db.get(ComputerDesktop, session.id)
                if desktop is not None:
                    desktop.workspace_id, desktop.version = replacement["id"], ""
                    desktop.control_client, desktop.control_epoch = "", new_id()
                await db.execute(delete(ComputerLease).where(ComputerLease.session_id == session.id))
            session.workspace = json.dumps(replacement)
            assert point is not None
            session.snapshot = point.model_dump_json()
            session.save_warning = RECOVERY_STATUS if continuation else None
            session.revision += 1
            db.add(Event(
                session_id=session.id, turn_id=scope.turn_id, kind=BOUNDARY_KIND, identity=identity,
                content=database.security.seal({
                    "graph": graph_ref, "workspace": replacement, "continuation": continuation,
                    "source": reference, "files": point.graph,
                }),
            ))
        return CheckpointSave(continuation)
    finally:
        async with database.sessions.begin() as db:
            await db.execute(delete(ComputerOperation).where(
                ComputerOperation.session_id == scope.session_id, ComputerOperation.identity == operation_id,
            ))
