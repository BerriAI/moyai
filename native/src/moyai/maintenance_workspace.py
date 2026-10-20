"""Preserve a settled graph and its files before a maintenance continuation.

Restoration enters a fresh SDK run. Recorded preparations, activity inputs, tool
results and the old run's workspace object are never rewritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .answer_settlement import transient_checkpoint_error
from .contracts import PreparedTurn
from .history_handoff import AgentCheckpoint, CheckpointSave, save_history_checkpoint
from .models import Event, Session, Turn, now
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
WAIT_NOTICE_SECONDS = 2
logger = logging.getLogger(__name__)
WAIT_MESSAGES = {
    "workspace_in_use": "Maintenance is waiting for computer control and the current computer action to settle.",
    "workspace_uncertain_effect": "Maintenance is waiting for an existing action or model-request receipt to settle. No action will be replayed.",
    "capture_unknown": "Maintenance is blocked because the filesystem capture outcome is unknown. Inspect the saved capture receipt; another snapshot will not be dispatched.",
    "storage_unavailable": "Maintenance is waiting for a provider or storage availability check. The task remains at its settled boundary.",
    "intent_changed": "Maintenance changed while files were being preserved. The saved intent is being checked again.",
    "restore_pending": "Workspace restoration is waiting for the provider to confirm the saved computer. No task action is being replayed.",
}


def wait_reason(error: BaseException) -> str | None:
    if not isinstance(error, ApplicationError):
        return None
    if error.type == "MoyaiMaintenanceCaptureUncertain":
        return "capture_unknown"
    if error.type == "MoyaiMaintenanceIntentChanged":
        return "intent_changed"
    if error.type in {"ProductPreparationBlocked", "MoyaiMaintenanceWait"} and len(error.details) == 1:
        code = error.details[0]
        if isinstance(code, str) and code in {"workspace_in_use", "workspace_uncertain_effect", "storage_unavailable"}:
            return code
    return None


def intent_changed() -> ApplicationError:
    return ApplicationError(
        WAIT_MESSAGES["intent_changed"], type="MoyaiMaintenanceIntentChanged", non_retryable=True,
    )


async def publish_wait(runtime: Runtime, turn_id: str, code: str) -> None:
    try:
        async with asyncio.timeout(WAIT_NOTICE_SECONDS), runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None:
                return
            session = await db.get(Session, turn.session_id, with_for_update=True)
            if session is not None and session.active_turn == turn_id and session.status == "running":
                session.save_warning = WAIT_MESSAGES[code]
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - a display notice must not alter the safety decision
        # Display metadata cannot change the original safety decision. The log
        # retains the fixed reason even while the session store is unavailable.
        logger.warning("Maintenance notice unavailable: %s (%s)", code, type(error).__name__)


async def capture_available(runtime: Runtime, reference: dict[str, str]) -> bool:
    if reference["provider"] == "modal":
        return await live_sandbox(reference)
    if reference["provider"] == "lambda":
        from pydantic_ai.workspaces import WorkspaceUnavailableError

        from .lambda_control import LambdaControl
        try:
            return await (await LambdaControl(runtime.settings).get(reference["id"])).alive()
        except WorkspaceUnavailableError:
            return False
    raise preparation_blocked("workspace_checkpoint_invalid")


class MaintenanceWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["maintenance-workspace-v1"] = "maintenance-workspace-v1"
    phase: Literal["capturing", "captured"] = "captured"
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
        else:
            session = await db.get(Session, turn.session_id, with_for_update=True)
            if (
                session is not None and session.active_turn == turn.id
                and session.save_warning in WAIT_MESSAGES.values()
            ):
                session.save_warning = None
        return DrainDecision(state.generation, state.paused)


@activity.defn(name="moyai_save_maintenance_checkpoint_v1")
async def save_maintenance_checkpoint(
    graph: AgentCheckpoint, boundary: str,
) -> CheckpointSave:
    from .runtime import current

    info = activity.info()
    if not info.workflow_id or not info.workflow_run_id:
        raise PermissionError("Preservation requires a workflow execution.")
    runtime = current()
    try:
        return await preserve(
            runtime, graph, workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id, boundary=boundary,
        )
    except Exception as error:
        if isinstance(error, ApplicationError) and error.type == "MoyaiHistoryAuthorizationDenied":
            raise preparation_blocked("workspace_authority_changed") from error
        reason = wait_reason(error)
        if reason is None and transient_checkpoint_error(error):
            # Provider capture errors are converted to uncertain_capture below.
            # Only known idempotent storage/read operations reach this branch.
            reason = "storage_unavailable"
        if reason is not None:
            await publish_wait(runtime, graph.prepared.scope.turn_id, reason)
            if wait_reason(error) is not None:
                raise
            raise ApplicationError(
                WAIT_MESSAGES[reason], reason, type="MoyaiMaintenanceWait", non_retryable=True,
            ) from error
        if isinstance(error, (PermissionError, HTTPException)):
            raise preparation_blocked("workspace_authority_changed") from error
        raise


async def saved_checkpoint(runtime: Runtime, db: AsyncSession, point: MaintenanceWorkspace) -> CheckpointSave:
    event = await db.scalar(select(Event).where(
        Event.identity == point.identity, Event.turn_id == point.turn_id,
        Event.session_id == point.session_id, Event.kind == CHECKPOINT_KIND,
    ))
    if event is None:
        raise preparation_blocked("workspace_checkpoint_missing")
    return CheckpointSave(reference=json.loads(runtime.security.unseal(event.content))["checkpoint"])


async def preserve(
    runtime: Runtime, graph: AgentCheckpoint, *,
    workflow_id: str, workflow_run_id: str, boundary: str,
) -> CheckpointSave:
    """Own the capture until its result is known, even across intent changes.

    Session.snapshot is the durable computer fence. CAPTURE_BEGIN/RESULT record
    the external operation independently of maintenance intent. Only a confirmed
    result can release that fence or publish a park for the current generation.
    """
    database, scope = runtime.database, graph.prepared.scope
    service = runtime.product_drain
    if service is None:
        raise RuntimeError("Workspace preservation requires maintenance authority.")
    saved_graph = await save_history_checkpoint(replace(
        graph, prepared=replace(graph.prepared, workspace=graph.workspace),
        deployment_handoff=None, allow_history_only=False,
    ))
    if saved_graph.reference is None:
        raise preparation_blocked("workspace_uncertain_effect")
    async with database.sessions() as db:
        session = await db.get(Session, scope.session_id)
        if (
            session is None or json.loads(session.workspace or "null") != graph.workspace
            or (graph.workspace and session.workspace_scope != scope.workspace_key)
        ):
            raise preparation_blocked("workspace_checkpoint_invalid")
        held = maintenance_workspace(session.snapshot)
        state = await db.get(ProductDrainState, 1)
        needs_capture = held is None and state is not None and state.paused
    if graph.workspace and needs_capture:
        async with asyncio.timeout(30):
            if not await capture_available(runtime, graph.workspace):
                raise preparation_blocked("workspace_checkpoint_missing")
    image = None
    dispatch_capture = False
    async with database.sessions.begin() as db:
        state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        session, turn = await locked_scope(database, db, scope, status="running")
        await database.swarms.authorize(db, turn, scope=scope)
        if turn.workflow_id != workflow_id:
            raise PermissionError("The maintenance checkpoint belongs to another workflow.")
        if state is None:
            raise preparation_blocked("workspace_checkpoint_invalid")
        reference = json.loads(session.workspace or "null")
        if reference != graph.workspace or (reference and reference.get("provider") not in {"modal", "lambda"}):
            raise preparation_blocked("workspace_checkpoint_invalid")
        if reference and session.workspace_scope != scope.workspace_key:
            raise PermissionError("The workspace authority changed.")
        digest = preparation_digest(runtime, turn)
        context, manifest = await product_bindings(db, scope, reference) if runtime.product and reference else (None, None)
        generation, intent = state.generation, state.request_fingerprint
        identity = CHECKPOINT_KIND + ":" + hashlib.sha256(json.dumps([
            scope.turn_id, workflow_id, workflow_run_id, boundary, generation, saved_graph.reference,
        ]).encode()).hexdigest()
        pending = MaintenanceWorkspace(
            phase="capturing", identity=identity, session_id=scope.session_id,
            turn_id=scope.turn_id, scope=scope.workspace_key, workflow_id=workflow_id,
            workflow_run_id=workflow_run_id, boundary=boundary, generation=generation,
            intent_fingerprint=intent, preparation_digest=digest,
            agent_checkpoint=saved_graph.reference, workspace=reference, image_id=None,
            product_context=context, restoration_manifest=manifest,
        )
        existing = maintenance_workspace(session.snapshot)
        if existing is not None:
            # A new generation may adopt confirmed files at this same frozen
            # boundary; it may not adopt another graph, execution or authority.
            if existing.model_copy(update={
                "identity": identity, "generation": generation, "intent_fingerprint": intent,
                "phase": "capturing", "image_id": None,
            }) != pending or (
                existing.generation == generation and existing.intent_fingerprint != intent
            ) or existing.generation > generation:
                raise preparation_blocked("workspace_checkpoint_invalid")
            if existing.phase == "captured":
                return await saved_checkpoint(runtime, db, existing)
            if reference:
                result = await db.scalar(select(Event).where(
                    Event.identity == CAPTURE_RESULT + ":" + existing.identity.split(":")[-1],
                ))
                if result is None:
                    raise uncertain_capture()
                image = json.loads(runtime.security.unseal(result.content))["image_id"]
        if not state.paused:
            # A lost provider reply stays fenced above. Confirmed capture has no
            # remaining writer, so cancellation can safely resume the live graph.
            if existing is not None:
                session.snapshot = None
            session.save_warning = None
            return CheckpointSave()
        await require_settled(db, session.id)
        if any(await service._unsettled(db, turn.id)):
            raise preparation_blocked("workspace_uncertain_effect")
        await service._park(
            db, turn.id, generation, workflow_id=workflow_id,
            workflow_run_id=workflow_run_id, boundary=boundary, phase="blocked",
        )
        if existing is None:
            session.snapshot = pending.model_dump_json()
            if reference:
                db.add(Event(
                    session_id=session.id, turn_id=turn.id, kind=CAPTURE_BEGIN,
                    identity=CAPTURE_BEGIN + ":" + identity.split(":")[-1],
                    content=runtime.security.seal(pending.model_dump_json()),
                ))
                dispatch_capture = True
        expected = session.snapshot
    if reference and dispatch_capture:
        try:
            image = await database.answers.snapshots.capture(
                TypeAdapter(WorkspaceRef).validate_python(reference), require_live=True,
            )
        except Exception as error:
            raise uncertain_capture() from error
        # Persistence failures cannot turn a confirmed reply into a new dispatch.
        await record_capture(runtime, identity.split(":")[-1], image)
    point = pending.model_copy(update={"phase": "captured", "image_id": image})
    reference_to_point = await runtime.objects.put(point.model_dump_json().encode())
    async with database.sessions.begin() as db:
        state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        session, turn = await locked_scope(database, db, scope, status="running")
        await database.swarms.authorize(db, turn, scope=scope)
        if state is None or (state.generation == generation and state.request_fingerprint != intent):
            raise preparation_blocked("workspace_checkpoint_invalid")
        if not state.paused or state.generation != generation:
            raise intent_changed()
        if maintenance_workspace(session.snapshot) == point:
            return await saved_checkpoint(runtime, db, point)
        if (
            session.snapshot != expected or json.loads(session.workspace or "null") != reference
            or (reference and session.workspace_scope != scope.workspace_key)
            or preparation_digest(runtime, turn) != digest
        ):
            raise preparation_blocked("workspace_checkpoint_invalid")
        if runtime.product and reference and await product_bindings(db, scope, reference) != (context, manifest):
            raise PermissionError("The workspace context changed during preservation.")
        await require_settled(db, session.id)
        if any(await service._unsettled(db, turn.id)):
            raise preparation_blocked("workspace_uncertain_effect")
        session.snapshot = point.model_dump_json()
        session.save_warning = None
        db.add(Event(
            session_id=session.id, turn_id=turn.id, kind=CHECKPOINT_KIND, identity=identity,
            content=runtime.security.seal({"checkpoint": reference_to_point}),
        ))
        await service._park(
            db, turn.id, generation, workflow_id=workflow_id,
            workflow_run_id=workflow_run_id, boundary=boundary, phase="parked",
        )
    return CheckpointSave(reference=reference_to_point)


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
        if transient_checkpoint_error(error) or wait_reason(error) == "intent_changed":
            await publish_wait(current(), turn_id, "restore_pending")
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
        or point.phase != "captured"
    ):
        raise PermissionError("This is not the preserved execution's direct continuation.")
    service = runtime.product_drain
    if service is None:
        raise RuntimeError("Workspace restoration requires maintenance authority.")
    resume_identity = f"{RESUME_KIND}:{turn_id}:{point.generation}"
    continued_scope = replace(scope, history_segment=scope.history_segment + 1)

    def continuation(workspace: dict[str, str] | None) -> AgentCheckpoint:
        workspace = dict(sorted(workspace.items())) if workspace is not None else None
        return replace(
            graph, prepared=replace(graph.prepared, scope=continued_scope, workspace=workspace),
            workspace=workspace,
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
            or (point.workspace is not None and session.workspace_scope != scope.workspace_key)
        ):
            raise preparation_blocked("workspace_checkpoint_missing")
        return session, state, receipt

    async def committed(
        db: AsyncSession, event: Event, session: Session, state: ProductDrainState,
        receipt: ProductDrainReceipt, workspace: dict[str, str] | None,
    ) -> MaintenanceResume:
        """Reconcile at either boundary without repeating any provider action."""
        saved = json.loads(runtime.security.unseal(event.content))
        expected = continuation(workspace)
        expected_ref = runtime.security.object_id(TypeAdapter(AgentCheckpoint).dump_json(expected))
        if (
            event.kind != RESUME_KIND or event.turn_id != turn_id or event.session_id != scope.session_id
            or saved.get("source") != reference or saved.get("workflow_run_id") != workflow_run_id
            or saved.get("checkpoint") != expected_ref or saved.get("generation") != state.generation
            or saved.get("intent") != state.request_fingerprint or state.paused
            or receipt.resumed_generation != state.generation or receipt.resumed_at is None
            or receipt.workflow_run_id != workflow_run_id
            or (workspace is not None and session.workspace_scope != scope.workspace_key)
            or (point.workspace is not None and workspace is None)
            or json.loads(session.workspace or "null") != workspace or session.snapshot is not None
        ):
            raise preparation_blocked("workspace_checkpoint_invalid")
        if runtime.product and point.workspace and await product_bindings(db, scope, point.workspace) != (
            point.product_context, point.restoration_manifest,
        ):
            raise PermissionError("The preserved workspace authority changed.")
        return MaintenanceResume(state.generation, False, expected)

    async with database.sessions.begin() as db:
        session, state, receipt = await validate(db)
        prior = await db.scalar(select(Event).where(Event.identity == resume_identity))
        if prior is not None:
            return await committed(db, prior, session, state, receipt, json.loads(session.workspace or "null"))
        if maintenance_workspace(session.snapshot) != point or json.loads(session.workspace or "null") != point.workspace:
            raise preparation_blocked("workspace_checkpoint_missing")
        # Only the direct successor may take over the park. It has not yet
        # constructed an SDK run or dispatched a model/tool operation.
        receipt.workflow_run_id = workflow_run_id
        await require_settled(db, session.id)
        if any(await service._unsettled(db, turn_id)):
            raise preparation_blocked("workspace_uncertain_effect")
        if runtime.product and point.workspace and await product_bindings(db, scope, point.workspace) != (
            point.product_context, point.restoration_manifest,
        ):
            raise PermissionError("The preserved workspace authority changed.")
        if state.paused:
            if state.generation == point.generation and state.request_fingerprint != point.intent_fingerprint:
                raise preparation_blocked("workspace_checkpoint_missing")
            await service._park(
                db, turn_id, state.generation, workflow_id=workflow_id,
                workflow_run_id=workflow_run_id, boundary=point.boundary, phase="parked",
            )
            return MaintenanceResume(state.generation, True)
        if state.generation <= point.generation:
            raise preparation_blocked("workspace_checkpoint_missing")
        generation, resume_intent = state.generation, state.request_fingerprint
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
    continued = continuation(workspace)
    continued_ref = await runtime.objects.put(TypeAdapter(AgentCheckpoint).dump_json(continued))
    async with database.sessions.begin() as db:
        session, state, receipt = await validate(db)
        prior = await db.scalar(select(Event).where(Event.identity == resume_identity))
        if prior is not None:
            return await committed(db, prior, session, state, receipt, workspace)
        if state.paused or state.generation != generation or state.request_fingerprint != resume_intent:
            raise intent_changed()
        if (
            maintenance_workspace(session.snapshot) != point
            or json.loads(session.workspace or "null") != point.workspace
            or receipt.resumed_generation is not None
        ):
            raise preparation_blocked("workspace_checkpoint_invalid")
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
                "intent": resume_intent,
            }),
        ))
        session.workspace = json.dumps(workspace) if workspace else None
        session.snapshot = None
        session.save_warning = None
        session.revision += 1
        receipt.resumed_generation, receipt.resumed_at = generation, now()
    return MaintenanceResume(generation, False, continued)


MAINTENANCE_ACTIVITIES = [maintenance_boundary, save_maintenance_checkpoint, load_maintenance_checkpoint]
