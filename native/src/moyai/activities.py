"""Application I/O boundaries around native agent execution."""

from __future__ import annotations

import logging

from pydantic import TypeAdapter
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_harness import ModalSandbox
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import CompletedTurn, PreparedTurn, SteeringInput
from .images import hydrate
from .runtime import current

logger = logging.getLogger(__name__)

SNAPSHOT_WARNING = (
    "The answer was saved, but the computer snapshot could not be confirmed. "
    "The previous saved workspace remains authoritative."
)


@activity.defn(name="moyai_prepare_turn_v1")
async def prepare_turn(turn_id: str) -> PreparedTurn:
    try:
        runtime = current()
        prepared = await runtime.database.prepare(turn_id)
        prepared.images = await runtime.database.input_images(turn_id, session_id=prepared.scope.session_id)
        await hydrate(runtime, prepared)
        return prepared
    except PermissionError as error:
        raise ApplicationError(str(error), type="PermissionDenied", non_retryable=True) from error


@activity.defn(name="moyai_save_answer_v1")
async def save_answer(result: CompletedTurn) -> bool:
    return await current().database.save_result(
        result.scope, conversation=result.conversation, output=result.output,
        workspace=result.workspace, usage=result.usage, deferred=result.deferred,
    )


@activity.defn(name="moyai_save_computer_v1")
async def save_computer(turn_id: str, reference: dict | None) -> None:
    runtime = current()
    if not reference:
        await runtime.database.settle(turn_id)
        return
    ref = TypeAdapter(WorkspaceRef).validate_python(reference)
    if ref.provider != "modal":
        # Local workspaces are used only by deterministic supplementary tests.
        await runtime.database.settle(turn_id)
        return
    try:
        backend = ModalSandbox().backend(ref)
        sandbox = await backend.get_sandbox()
        image = await sandbox.snapshot_filesystem.aio(timeout=120)
    except Exception:
        logger.warning("Computer snapshot settlement failed; the answer remains saved.", exc_info=True)
        # The received answer is already committed. Never replace it with a save failure.
        await runtime.database.settle(
            turn_id,
            warning="The answer was saved, but the computer snapshot failed. "
                    "The previous saved workspace remains authoritative.",
        )
        return
    await runtime.database.settle(turn_id, snapshot=image.object_id)


@activity.defn(name="moyai_settle_answer_v1")
async def settle_answer(turn_id: str) -> None:
    """Settle an accepted answer after snapshot activity failure or timeout."""
    await current().database.settle(turn_id, warning=SNAPSHOT_WARNING)


@activity.defn(name="moyai_fail_turn_v1")
async def fail_turn(turn_id: str, message: str) -> None:
    await current().database.fail(turn_id, message)


@activity.defn(name="moyai_stopped_turn_v1")
async def stopped_turn(turn_id: str, conversation: str | None) -> None:
    await current().database.stopped(turn_id, conversation)


@activity.defn(name="moyai_prepare_steering_v1")
async def prepare_steering(parent_id: str, turn_id: str) -> SteeringInput:
    runtime = current()
    prepared = await runtime.database.prepare_steering(parent_id, turn_id)
    prepared.images = await runtime.database.input_images(turn_id)
    await hydrate(runtime, prepared)
    return prepared


@activity.defn(name="moyai_steering_receipt_v1")
async def steering_receipt(turn_id: str, enqueue_id: str | None) -> None:
    await current().database.steering_receipt(turn_id, enqueue_id)


ACTIVITIES = [
    prepare_turn, save_answer, save_computer, settle_answer, fail_turn, stopped_turn,
    prepare_steering, steering_receipt,
]

