"""Native Temporal worker and idempotent application-to-workflow dispatch."""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from .activities import ACTIVITIES
from .execution import ExecutionAuthority
from .runtime import Runtime, configure
from .settings_runtime import SETTINGS_ACTIVITIES, SETTINGS_WORKFLOWS, settings_worker_identity
from .workflow import TurnWorkflow

logger = logging.getLogger(__name__)


def make_worker(runtime: Runtime) -> Worker:
    if runtime.client is None:
        raise RuntimeError("Connect to Temporal before creating the worker.")
    configure(runtime)
    TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
    return Worker(
        runtime.client,
        identity=settings_worker_identity(),
        task_queue=runtime.settings.temporal_queue,
        workflows=[TurnWorkflow, *SETTINGS_WORKFLOWS],
        activities=[*ACTIVITIES, *SETTINGS_ACTIVITIES],
        interceptors=[ExecutionAuthority(runtime.database)],
        max_concurrent_activities=32,
        max_concurrent_workflow_tasks=16,
    )


async def dispatch_once(runtime: Runtime) -> int:
    client = await runtime.connect()
    await runtime.database.claim_next()
    dispatched = 0
    for turn in await runtime.database.starting_turns():
        try:
            await client.start_workflow(
                TurnWorkflow.run,
                turn.id,
                id=turn.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
        except WorkflowAlreadyStartedError:
            # Temporal remains the execution authority after dispatch acknowledgment loss.
            pass
        except RPCError:
            logger.warning("Temporal did not acknowledge dispatch; the saved request will be reconciled.")
            continue
        dispatched += 1
    for turn, parent in await runtime.database.pending_steering():
        if parent.status not in {"starting", "running"}:
            if turn.status == "steering":
                await runtime.database.steering_receipt(turn.id, None)
            else:
                await runtime.database.steering_uncertain(turn.id)
            continue
        # Persist intent before the RPC. If its acknowledgment is lost and the
        # parent closes, absence of an enqueue receipt is not permission to rerun.
        await runtime.database.signal_attempted(turn.id)
        try:
            await client.get_workflow_handle(parent.workflow_id).signal(TurnWorkflow.steer, turn.id)
        except RPCError:
            continue
    for turn in await runtime.database.pending_stops():
        handle = client.get_workflow_handle(turn.workflow_id)
        try:
            description = await handle.describe()
            if description.status == WorkflowExecutionStatus.RUNNING:
                await handle.cancel()
            else:
                await runtime.database.stopped(turn.id)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                # A Stop admitted before workflow creation never needs to start the run.
                await runtime.database.stopped(turn.id)
    return dispatched


async def dispatcher(runtime: Runtime) -> None:
    while True:
        try:
            await dispatch_once(runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Dispatch is temporarily unavailable; saved inputs remain queued.")
        await asyncio.sleep(0.5)


async def reconcile_workflows(runtime: Runtime) -> None:
    """Reflect terminal Temporal outcomes after an app write/ack failed.

    This never resumes a snapshot or starts an agent; Temporal stays the sole
    execution authority. It only settles stale application status records.
    """
    while True:
        try:
            client = await runtime.connect()
            for turn in await runtime.database.unsettled_turns():
                try:
                    description = await client.get_workflow_handle(turn.workflow_id).describe()
                except RPCError:
                    continue
                if description.status == WorkflowExecutionStatus.CANCELED:
                    await runtime.database.stopped(turn.id)
                elif description.status in {
                    WorkflowExecutionStatus.FAILED, WorkflowExecutionStatus.TIMED_OUT,
                    WorkflowExecutionStatus.TERMINATED, WorkflowExecutionStatus.COMPLETED,
                }:
                    await runtime.database.fail(
                        turn.id,
                        "Execution ended before the application could confirm settlement. "
                        "Inspect the saved activity before repeating an action.",
                        only_unsettled=True,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Execution status reconciliation is temporarily unavailable.")
        await asyncio.sleep(5)

