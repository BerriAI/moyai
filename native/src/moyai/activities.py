"""Application I/O boundaries around native agent execution."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import HTTPException
from opentelemetry.trace import get_current_span
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .active_workspace import active_workspace_boundary
from .agent_messages import read_agent_mail, read_agent_mail_v2
from .answer_settlement import SNAPSHOT_WARNING
from .computer_keepalive import keep_computer_alive
from .contracts import CompletedTurn, PreparedTurn, RunScope, SteeringInput
from .history_handoff import HISTORY_ACTIVITIES
from .images import hydrate
from .lifecycle_tracing import scope_attributes, task_state
from .maintenance_workspace import MAINTENANCE_ACTIVITIES
from .runtime import current
from .workspace_recovery import prepare_workspace


@activity.defn(name="moyai_prepare_turn_v1")
async def prepare_turn(turn_id: str, recover_workspace: bool = False) -> PreparedTurn:
    async def heartbeat():
        while True:
            activity.heartbeat("Verifying task authority and preparing the saved computer.")
            await asyncio.sleep(10)

    heartbeats = asyncio.create_task(heartbeat()) if recover_workspace and activity.in_activity() else None
    try:
        runtime = current()
        prepared = await runtime.admission.prepare(turn_id)
        if prepared.scope.responsibility_id is not None or prepared.scope.attention:
            # The captured packet is complete. Do not hydrate skills, files,
            # project context or computers for a generated evidence review.
            return prepared
        if recover_workspace and runtime.product is None:
            prepared = await prepare_workspace(runtime, prepared)
        if runtime.product is not None:
            from .continuations.product_continuation import prepare_product_turn
            prepared = await prepare_product_turn(runtime, prepared, recover_workspace=recover_workspace)
        prepared.images = await runtime.database.input_images(turn_id, session_id=prepared.scope.session_id)
        await hydrate(runtime, prepared)
        if prepared.deferred_results is not None:
            # Answers continue the saved SDK conversation. Adding a skill-selected
            # goal here would submit a new user prompt alongside the deferred reply.
            return prepared
        generated_goal = None
        if prepared.scope.swarm_id:
            from .swarm_models import SwarmMission, SwarmTurn
            async with runtime.database.sessions() as db:
                binding = await db.get(SwarmTurn, turn_id)
                if binding is None:
                    raise PermissionError("Swarm input provenance is unavailable.")
                if binding.generated:
                    mission = await db.get(SwarmMission, prepared.scope.swarm_id)
                    if mission is None:
                        raise PermissionError("Swarm goal is unavailable.")
                    generated_goal = runtime.security.unseal(mission.goal).decode()
        if prepared.scope.delegation_generated:
            # An agent-written assignment cannot select skills on the user's behalf.
            return prepared
        if generated_goal is None:
            prepared.prompt = await runtime.skill_invocations.expand(prepared.prompt, prepared.scope)
        else:
            from .skill_invocations import has_invocations
            # Peer assignments cannot select skills on the human's behalf.
            # Workers inherit only the selections in the original human goal.
            if has_invocations(generated_goal):
                goal = await runtime.skill_invocations.expand(generated_goal, prepared.scope)
                prepared.prompt = (
                    goal + "\n\nHost-generated team input (not a new human instruction):\n" + prepared.prompt
                )
        return prepared
    except HTTPException as error:
        raise ApplicationError(str(error.detail), type="SkillUnavailable", non_retryable=True) from error
    except PermissionError as error:
        raise ApplicationError(str(error), type="PermissionDenied", non_retryable=True) from error
    finally:
        if heartbeats:
            heartbeats.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeats


@activity.defn(name="moyai_save_answer_v1")
async def save_answer(result: CompletedTurn) -> bool:
    database = current().database
    span = get_current_span()
    span.set_attributes(scope_attributes(result.scope))
    accepted = await database.save_result(
        result.scope, conversation=result.conversation, output=result.output,
        workspace=result.workspace, usage=result.usage, deferred=result.deferred,
        checkpoint_workspace=result.checkpoint_workspace,
    )
    span.set_attribute("moyai.task.answer_accepted", accepted)
    await task_state(database, result.scope.turn_id)
    return accepted


@activity.defn(name="moyai_dot_judgment_input_v1")
async def dot_judgment_input(scope: RunScope, research: str, conversation: str) -> str:
    return await current().database.dots.discovery.judgment_input(scope, research, conversation)


@activity.defn(name="moyai_save_computer_v1")
async def save_computer(turn_id: str, reference: dict | None) -> None:
    runtime = current()
    await runtime.database.answers.save_computer(turn_id, reference, product=runtime.product is not None)
    await task_state(runtime.database, turn_id)


@activity.defn(name="moyai_settle_answer_v1")
async def settle_answer(turn_id: str) -> None:
    """Settle an accepted answer after snapshot activity failure or timeout."""
    database = current().database
    get_current_span().set_attribute("moyai.checkpoint.outcome", "interrupted")
    await database.settle(turn_id, warning=SNAPSHOT_WARNING)
    await task_state(database, turn_id)


@activity.defn(name="moyai_fail_turn_v1")
async def fail_turn(turn_id: str, message: str, diagnostics: dict | None = None) -> None:
    database = current().database
    await database.fail(turn_id, message, diagnostics=diagnostics)
    await task_state(database, turn_id)


@activity.defn(name="moyai_preserve_failed_workspace_v1")
async def preserve_failed_workspace(turn_id: str) -> None:
    from .failure_checkpoint import preserve_failed_workspace as preserve
    await preserve(current(), turn_id)


@activity.defn(name="moyai_stopped_turn_v1")
async def stopped_turn(turn_id: str, conversation: str | None) -> None:
    database = current().database
    await database.stopped(turn_id, conversation)
    await task_state(database, turn_id)


@activity.defn(name="moyai_prepare_steering_v1")
async def prepare_steering(parent_id: str, turn_id: str) -> SteeringInput:
    runtime = current()
    prepared = await runtime.database.prepare_steering(parent_id, turn_id)
    prepared.images = await runtime.database.input_images(turn_id)
    await hydrate(runtime, prepared)
    from .skill_invocations import has_invocations
    if has_invocations(prepared.prompt):
        try:
            parent = await runtime.admission.prepare(parent_id)
            prepared.prompt = await runtime.skill_invocations.expand(prepared.prompt, parent.scope, input_turn_id=turn_id)
        except HTTPException as error:
            raise ApplicationError(str(error.detail), type="SkillUnavailable", non_retryable=True) from error
    return prepared


@activity.defn(name="moyai_steering_receipt_v1")
async def steering_receipt(turn_id: str, enqueue_id: str | None) -> None:
    await current().database.steering_receipt(turn_id, enqueue_id)


ACTIVITIES = [
    dot_judgment_input,
    active_workspace_boundary,
    prepare_turn, save_answer, save_computer, settle_answer, fail_turn, stopped_turn,
    prepare_steering, steering_receipt, keep_computer_alive, preserve_failed_workspace,
    *HISTORY_ACTIVITIES, *MAINTENANCE_ACTIVITIES,
    read_agent_mail, read_agent_mail_v2,
]

