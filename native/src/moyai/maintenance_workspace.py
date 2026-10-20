"""Preserve a settled graph and its files before a maintenance continuation.

Restoration enters a fresh SDK run. Recorded preparations, activity inputs, tool
results and the old run's workspace object are never rewritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import delete, select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .answer_settlement import transient_checkpoint_error
from .contracts import PreparedTurn
from .history_handoff import AgentCheckpoint, CheckpointSave, save_history_checkpoint
from .models import ComputerOperation, Event, Session, Turn, now
from .product_continuation_errors import preparation_blocked
from .product_drain import DrainDecision
from .product_drain_models import ProductDrainReceipt, ProductDrainState
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
    from sqlalchemy.ext.asyncio import AsyncSession

    from .runtime import Runtime

VERSION = "moyai-maintenance-workspace-continuation-v1"
CHECKPOINT_KIND = "maintenance_workspace_checkpoint"
RESUME_KIND = "maintenance_workspace_resume"
CAPTURE_BEGIN = "maintenance_capture_begin"
CAPTURE_RESULT = "maintenance_capture_result"


class MaintenanceWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["maintenance-workspace-v1"] = "maintenance-workspace-v1"
    identity: str
    session_id: str
    turn_id: str
    scope: str
    workflow_id: str
    workflow_run_id: str
    boundary: str
    generation: int
    intent_fingerprint: str
    preparation_digest: str
    agent_checkpoint: str
    workspace: dict[str, str] | None
    image_id: str | None
    product_context: str | None = None
    restoration_manifest: str | None = None


@dataclass
class MaintenanceResume:
    generation: int
    paused: bool
    checkpoint: AgentCheckpoint | None = None


def maintenance_workspace(value: str | None) -> MaintenanceWorkspace | None:
    if not value:
        return None
    try:
        return MaintenanceWorkspace.model_validate_json(value)
    except ValidationError:
        return None


def preparation_digest(runtime: Runtime, turn: Turn) -> str:
    if not turn.preparation:
        raise preparation_blocked("workspace_checkpoint_missing")
    return hashlib.sha256(runtime.security.unseal(turn.preparation)).hexdigest()


def uncertain_capture() -> ApplicationError:
    return ApplicationError(
        "The filesystem capture outcome is unknown. Inspect its saved receipt; no new capture was dispatched.",
        type="MoyaiMaintenanceCaptureUncertain", non_retryable=True,
    )


async def record_capture(runtime: Runtime, identity: str, image_id: str) -> None:
    """Record a factual provider reply even if authority was revoked afterward."""
    if not image_id:
        raise uncertain_capture()
    async with runtime.database.sessions.begin() as db:
        started = await db.scalar(select(Event).where(
            Event.kind == CAPTURE_BEGIN, Event.identity == CAPTURE_BEGIN + ":" + identity,
        ).with_for_update())
        if started is None:
            raise uncertain_capture()
        existing = await db.scalar(select(Event).where(Event.identity == CAPTURE_RESULT + ":" + identity))
        if existing is not None:
            if json.loads(runtime.security.unseal(existing.content))["image_id"] != image_id:
                raise uncertain_capture()
            return
        db.add(Event(
            session_id=started.session_id, turn_id=started.turn_id, kind=CAPTURE_RESULT,
            identity=CAPTURE_RESULT + ":" + identity,
            content=runtime.security.seal({"image_id": image_id}),
        ))


@activity.defn(name="moyai_maintenance_boundary_v1")
async def maintenance_boundary(turn_id: str, boundary: str) -> DrainDecision:
    """Observe intent; a graph without its filesystem is never a safe park."""
    from .runtime import current

    runtime = current()
    service = runtime.product_drain
    if service is None:
        return DrainDecision(0, False)
    info = activity.info()
    if not info.workflow_id or not info.workflow_run_id:
        raise PermissionError("Maintenance requires a workflow execution.")
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, turn_id)
        if turn is None or not turn.preparation:
            raise PermissionError("Maintenance requires the original preparation.")
        prepared = TypeAdapter(PreparedTurn).validate_json(runtime.security.unseal(turn.preparation))
    await runtime.database.authorize_scope(prepared.scope)
    async with runtime.database.sessions.begin() as db:
        state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        turn = await db.get(Turn, turn_id)
        if state is None or turn is None or turn.workflow_id != info.workflow_id:
            raise preparation_blocked("workspace_checkpoint_missing")
        if state.paused:
            await service._park(
                db, turn.id, state.generation, workflow_id=info.workflow_id,
                workflow_run_id=info.workflow_run_id, boundary=boundary, phase="blocked",
            )
        return DrainDecision(state.generation, state.paused)


@activity.defn(name="moyai_save_maintenance_checkpoint_v1")
async def save_maintenance_checkpoint(
    graph: AgentCheckpoint, generation: int, boundary: str,
) -> CheckpointSave:
    from .runtime import current

    info = activity.info()
    if not info.workflow_id or not info.workflow_run_id:
        raise PermissionError("Preservation requires a workflow execution.")
    return await preserve(
        current(), graph, generation=generation, workflow_id=info.workflow_id,
        workflow_run_id=info.workflow_run_id, boundary=boundary,
    )


async def preserve(
    runtime: Runtime, graph: AgentCheckpoint, *, generation: int,
    workflow_id: str, workflow_run_id: str, boundary: str,
) -> CheckpointSave:
    database, scope = runtime.database, graph.prepared.scope
    service = runtime.product_drain
    if service is None:
        raise RuntimeError("Workspace preservation requires maintenance authority.")
    # The existing saver checks provider-valid conversation, current authority
    # and pending effects, then writes encrypted immutable bytes.
    saved_graph = await save_history_checkpoint(replace(
        graph, prepared=replace(graph.prepared, workspace=graph.workspace),
        deployment_handoff=None, allow_history_only=False,
    ))
    if saved_graph.reference is None:
        raise preparation_blocked("workspace_uncertain_effect")
    identity = CHECKPOINT_KIND + ":" + hashlib.sha256(json.dumps([
        scope.turn_id, workflow_id, workflow_run_id, boundary, generation, saved_graph.reference,
    ]).encode()).hexdigest()
    operation_id = str(uuid5(NAMESPACE_URL, identity))
    capture_id = identity.split(":")[-1]
    image = None
    dispatch_capture = False
    async with database.sessions.begin() as db:
        state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        session, turn = await locked_scope(database, db, scope, status="running")
        await database.swarms.authorize(db, turn, scope=scope)
        if turn.workflow_id != workflow_id:
            raise PermissionError("The maintenance checkpoint belongs to another workflow.")
        existing = maintenance_workspace(session.snapshot)
        if existing is not None:
            if existing.identity != identity:
                raise preparation_blocked("workspace_checkpoint_missing")
            event = await db.scalar(select(Event).where(
                Event.identity == identity, Event.turn_id == turn.id, Event.session_id == session.id,
                Event.kind == CHECKPOINT_KIND,
            ))
            if event is None:
                raise preparation_blocked("workspace_checkpoint_missing")
            saved_reference = json.loads(runtime.security.unseal(event.content))["checkpoint"]
            return CheckpointSave(reference=saved_reference)
        if state is None or not state.paused or state.generation != generation:
            raise PermissionError("The maintenance intent changed before preservation.")
        await require_settled(db, session.id)
        if any(await service._unsettled(db, turn.id)):
            raise preparation_blocked("workspace_uncertain_effect")
        reference = json.loads(session.workspace or "null")
        if reference != graph.workspace or (reference and reference.get("provider") not in {"modal", "lambda"}):
            raise preparation_blocked("workspace_checkpoint_missing")
        if reference and session.workspace_scope != scope.workspace_key:
            raise PermissionError("The workspace authority changed.")
        digest = preparation_digest(runtime, turn)
        context, manifest = await product_bindings(db, scope, reference) if runtime.product and reference else (None, None)
        expected, revision, intent = session.snapshot, session.revision, state.request_fingerprint
        await service._park(
            db, turn.id, generation, workflow_id=workflow_id,
            workflow_run_id=workflow_run_id, boundary=boundary, phase="blocked",
        )
        if reference:
            # The public snapshot API supplies no caller-owned idempotency key.
            # A crash after dispatch must not allocate another unknown image.
            begins = list(await db.scalars(select(Event).where(
                Event.turn_id == turn.id, Event.kind == CAPTURE_BEGIN,
            )))
            for begun in begins:
                key = begun.identity.removeprefix(CAPTURE_BEGIN + ":")
                completed = await db.scalar(select(Event).where(
                    Event.identity == CAPTURE_RESULT + ":" + key,
                ))
                if completed is None:
                    raise uncertain_capture()
                if key == capture_id:
                    original = json.loads(runtime.security.unseal(begun.content))
                    if original != {
                        "workspace": reference, "revision": revision,
                        "snapshot": expected, "preparation": digest,
                    }:
                        raise preparation_blocked("workspace_checkpoint_missing")
                    image = json.loads(runtime.security.unseal(completed.content))["image_id"]
            if image is None:
                db.add(Event(
                    session_id=session.id, turn_id=turn.id, kind=CAPTURE_BEGIN,
                    identity=CAPTURE_BEGIN + ":" + capture_id,
                    content=runtime.security.seal({
                        "workspace": reference, "revision": revision,
                        "snapshot": expected, "preparation": digest,
                    }),
                ))
                dispatch_capture = True
            await db.execute(delete(ComputerOperation).where(ComputerOperation.session_id == session.id))
            lease_seconds = database.settings.lambda_checkpoint_timeout + 120 if reference["provider"] == "lambda" else 240
            db.add(ComputerOperation(
                session_id=session.id, identity=operation_id, expires_at=int(time.time()) + lease_seconds,
            ))
    try:
        if reference and dispatch_capture:
            image = await database.answers.snapshots.capture(
                TypeAdapter(WorkspaceRef).validate_python(reference), require_live=True,
            )
            await record_capture(runtime, capture_id, image)
        point = MaintenanceWorkspace(
            identity=identity, session_id=scope.session_id, turn_id=scope.turn_id, scope=scope.workspace_key,
            workflow_id=workflow_id, workflow_run_id=workflow_run_id, boundary=boundary,
            generation=generation, intent_fingerprint=intent, preparation_digest=digest,
            agent_checkpoint=saved_graph.reference, workspace=reference, image_id=image,
            product_context=context, restoration_manifest=manifest,
        )
        reference_to_point = await runtime.objects.put(point.model_dump_json().encode())
        async with database.sessions.begin() as db:
            state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
            session, turn = await locked_scope(database, db, scope, status="running")
            await database.swarms.authorize(db, turn, scope=scope)
            operation = await db.get(ComputerOperation, session.id)
            if (
                state is None or not state.paused or state.generation != generation
                or state.request_fingerprint != intent or session.snapshot != expected
                or session.revision != revision or json.loads(session.workspace or "null") != reference
                or preparation_digest(runtime, turn) != digest
                or (reference and (
                    operation is None or operation.identity != operation_id or operation.expires_at <= time.time()
                ))
            ):
                raise PermissionError("The workspace changed during maintenance preservation.")
            if runtime.product and reference and await product_bindings(db, scope, reference) != (context, manifest):
                raise PermissionError("The workspace context changed during preservation.")
            if any(await service._unsettled(db, turn.id)):
                raise preparation_blocked("workspace_uncertain_effect")
            session.snapshot = point.model_dump_json()
            db.add(Event(
                session_id=session.id, turn_id=turn.id, kind=CHECKPOINT_KIND, identity=identity,
                content=runtime.security.seal({"checkpoint": reference_to_point}),
            ))
            await service._park(
                db, turn.id, generation, workflow_id=workflow_id,
                workflow_run_id=workflow_run_id, boundary=boundary, phase="parked",
            )
        return CheckpointSave(reference=reference_to_point)
    finally:
        async with database.sessions.begin() as db:
            await db.execute(delete(ComputerOperation).where(
                ComputerOperation.session_id == scope.session_id, ComputerOperation.identity == operation_id,
            ))


@activity.defn(name="moyai_load_maintenance_checkpoint_v1")
async def load_maintenance_checkpoint(
    turn_id: str, reference: str, previous_run_id: str,
) -> MaintenanceResume:
    from .runtime import current

    info = activity.info()
    if not info.workflow_id or not info.workflow_run_id:
        raise PermissionError("Restoration requires a workflow execution.")
    try:
        return await resume(
            current(), turn_id, reference, workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id, previous_run_id=previous_run_id,
        )
    except Exception as error:
        # The frozen image and stable provider creation identity permit another
        # observation of that same restore, not an unrelated launch or tool retry.
        if transient_checkpoint_error(error):
            raise ApplicationError(
                "The saved workspace restore is awaiting provider confirmation.",
                type="MoyaiMaintenanceRestorePending", non_retryable=True,
            ) from error
        raise


async def resume(
    runtime: Runtime, turn_id: str, reference: str, *,
    workflow_id: str, workflow_run_id: str, previous_run_id: str,
) -> MaintenanceResume:
    database = runtime.database
    point = MaintenanceWorkspace.model_validate_json(await runtime.objects.get(reference))
    graph = TypeAdapter(AgentCheckpoint).validate_json(await runtime.objects.get(point.agent_checkpoint))
    scope = graph.prepared.scope
    if (
        point.turn_id != turn_id or scope.turn_id != turn_id or scope.session_id != point.session_id
        or scope.workspace_key != point.scope or point.workflow_id != workflow_id
        or point.workflow_run_id != previous_run_id or workflow_run_id == previous_run_id
        or graph.workspace != point.workspace or graph.prepared.workspace != point.workspace
    ):
        raise PermissionError("This is not the preserved execution's direct continuation.")
    service = runtime.product_drain
    if service is None:
        raise RuntimeError("Workspace restoration requires maintenance authority.")
    resume_identity = f"{RESUME_KIND}:{turn_id}:{point.generation}"
    async with database.sessions() as db:
        published = await db.scalar(select(Event).where(Event.identity == resume_identity))
        published_content = published.content if published is not None else None
    saved = json.loads(runtime.security.unseal(published_content)) if published_content else None
    continued_saved = (
        TypeAdapter(AgentCheckpoint).validate_json(await runtime.objects.get(saved["checkpoint"]))
        if saved is not None else None
    )

    async def validate(db: AsyncSession) -> tuple[Session, ProductDrainState, ProductDrainReceipt]:
        state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        session, turn = await locked_scope(database, db, scope, status="running")
        await database.swarms.authorize(db, turn, scope=scope)
        receipt = await db.get(ProductDrainReceipt, (scope.turn_id, point.generation), with_for_update=True)
        if (
            state is None or state.generation < point.generation
            or turn.workflow_id != workflow_id or receipt is None or receipt.phase != "parked"
            or receipt.workflow_id != workflow_id or receipt.boundary != point.boundary
            or receipt.workflow_run_id not in {previous_run_id, workflow_run_id}
            or preparation_digest(runtime, turn) != point.preparation_digest
        ):
            raise preparation_blocked("workspace_checkpoint_missing")
        return session, state, receipt

    async with database.sessions.begin() as db:
        session, state, receipt = await validate(db)
        prior = await db.scalar(select(Event).where(Event.identity == resume_identity))
        if prior is not None:
            if prior.content != published_content or saved is None or continued_saved is None:
                raise ApplicationError(
                    "The restore committed concurrently; observe its saved receipt.",
                    type="MoyaiMaintenanceRestorePending", non_retryable=True,
                )
            if saved["workflow_run_id"] != workflow_run_id or saved["source"] != reference:
                raise PermissionError("The maintenance continuation receipt changed.")
            if json.loads(session.workspace or "null") != continued_saved.workspace:
                raise preparation_blocked("workspace_checkpoint_missing")
            return MaintenanceResume(saved["generation"], False, continued_saved)
        if maintenance_workspace(session.snapshot) != point or json.loads(session.workspace or "null") != point.workspace:
            raise preparation_blocked("workspace_checkpoint_missing")
        # Only the direct successor may take over the park. It has not yet
        # constructed an SDK run or dispatched a model/tool operation.
        receipt.workflow_run_id = workflow_run_id
        if state.paused:
            if state.generation != point.generation or state.request_fingerprint != point.intent_fingerprint:
                raise preparation_blocked("workspace_checkpoint_missing")
            return MaintenanceResume(state.generation, True)
        if state.generation <= point.generation:
            raise preparation_blocked("workspace_checkpoint_missing")
        await require_settled(db, session.id)
        if any(await service._unsettled(db, turn_id)):
            raise preparation_blocked("workspace_uncertain_effect")
        if runtime.product and point.workspace and await product_bindings(db, scope, point.workspace) != (
            point.product_context, point.restoration_manifest,
        ):
            raise PermissionError("The preserved workspace authority changed.")
        revision, generation = session.revision, state.generation
    workspace = point.workspace
    if workspace:
        if not point.image_id:
            raise preparation_blocked("workspace_checkpoint_missing")
        if workspace["provider"] == "modal":
            async with asyncio.timeout(180):
                if not await live_sandbox(workspace):
                    saved_files = WorkspaceCheckpoint(
                        session_id=point.session_id, turn_id=point.turn_id, workspace=workspace,
                        scope=point.scope, result_fingerprint=point.identity, conversation_digest=point.identity,
                        image_id=point.image_id,
                    )
                    sandbox = await restored_sandbox(runtime, scope, saved_files)
                    if await sandbox.poll.aio() is not None:
                        raise preparation_blocked("workspace_checkpoint_missing")
                    workspace = {"provider": "modal", "id": sandbox.object_id}
        elif workspace["provider"] == "lambda":
            from pydantic_ai.workspaces import WorkspaceUnavailableError

            from .lambda_control import LambdaControl
            control = LambdaControl(runtime.settings)
            try:
                alive = await (await control.get(workspace["id"])).alive()
            except WorkspaceUnavailableError:
                alive = False
            if not alive:
                restored = await control.create("maintenance-restore:" + point.identity.split(":")[-1], checkpoint=point.image_id)
                workspace = {"provider": "lambda", "id": restored.id}
        else:
            raise preparation_blocked("workspace_checkpoint_missing")
    continued_scope = replace(scope, history_segment=scope.history_segment + 1)
    continued = replace(
        graph, prepared=replace(graph.prepared, scope=continued_scope, workspace=workspace),
        workspace=workspace,
    )
    continued_ref = await runtime.objects.put(TypeAdapter(AgentCheckpoint).dump_json(continued))
    async with database.sessions.begin() as db:
        session, state, receipt = await validate(db)
        if (
            state.paused or state.generation != generation or session.revision != revision
            or maintenance_workspace(session.snapshot) != point
            or json.loads(session.workspace or "null") != point.workspace
            or receipt.resumed_generation is not None
        ):
            raise PermissionError("Maintenance changed during workspace restoration.")
        await require_settled(db, session.id)
        if any(await service._unsettled(db, turn_id)):
            raise preparation_blocked("workspace_uncertain_effect")
        if runtime.product and point.workspace:
            if await product_bindings(db, scope, point.workspace) != (point.product_context, point.restoration_manifest):
                raise PermissionError("The preserved workspace authority changed.")
            if workspace is not None and workspace != point.workspace:
                await preserve_restoration(db, session.id, point.workspace["id"], workspace["id"], scope.workspace_key)
        segment_identity = f"history_segment:{continued_scope.root_run_id}"
        segment = await db.scalar(select(Event).where(Event.identity == segment_identity))
        if segment is None:
            db.add(Event(
                session_id=session.id, turn_id=turn_id, kind="history_segment", identity=segment_identity,
                content=runtime.security.seal({"run_id": continued_scope.root_run_id}),
            ))
        elif (
            segment.turn_id != turn_id or segment.session_id != session.id
            or json.loads(runtime.security.unseal(segment.content)) != {"run_id": continued_scope.root_run_id}
        ):
            raise PermissionError("The continuation history segment changed.")
        db.add(Event(
            session_id=session.id, turn_id=turn_id, kind=RESUME_KIND, identity=resume_identity,
            content=runtime.security.seal({
                "source": reference, "checkpoint": continued_ref,
                "workflow_run_id": workflow_run_id, "generation": generation,
            }),
        ))
        session.workspace = json.dumps(workspace) if workspace else None
        session.snapshot = None
        session.revision += 1
        receipt.resumed_generation, receipt.resumed_at = generation, now()
    return MaintenanceResume(generation, False, continued)


MAINTENANCE_ACTIVITIES = [maintenance_boundary, save_maintenance_checkpoint, load_maintenance_checkpoint]
