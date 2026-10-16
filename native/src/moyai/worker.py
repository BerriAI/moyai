"""Native Temporal worker and idempotent application-to-workflow dispatch."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager

from temporalio.client import WorkflowExecutionStatus, WorkflowHistory
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import (
    Interceptor,
    Plugin,
    Replayer,
    ReplayerConfig,
    Worker,
    WorkerConfig,
    WorkflowReplayResult,
)

from .activities import ACTIVITIES
from .execution import ExecutionAuthority
from .product_drain import ProductDrainError, ProductDrainService, drain_checkpoint
from .runtime import Runtime, configure
from .settings_runtime import SETTINGS_ACTIVITIES, SETTINGS_WORKFLOWS, settings_worker_identity
from .workflow import TurnWorkflow

logger = logging.getLogger(__name__)


class ProductDrainPlugin(Plugin):
    """Register the existing canonical owner before SDK polling; never replace it."""

    def __init__(self, service: ProductDrainService):
        self.service = service

    def configure_worker(self, config: WorkerConfig) -> WorkerConfig:
        return config

    async def run_worker(self, worker: Worker, next: Callable[[Worker], Awaitable[None]]) -> None:
        await self.service.register_worker()
        await next(worker)
        # Exact canonical backend absence retires this record. No TTL, reconnect,
        # early release or cancellation-prone cleanup can authorize resume.

    def configure_replayer(self, config: ReplayerConfig) -> ReplayerConfig:
        return config

    def run_replayer(
        self, replayer: Replayer, histories: AsyncIterator[WorkflowHistory],
        next: Callable[
            [Replayer, AsyncIterator[WorkflowHistory]],
            AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]],
        ],
    ) -> AbstractAsyncContextManager[AsyncIterator[WorkflowReplayResult]]:
        return next(replayer, histories)


def make_worker(runtime: Runtime) -> Worker:
    if runtime.client is None:
        raise RuntimeError("Connect to Temporal before creating the worker.")
    configure(runtime)
    TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
    product_workflows = []
    product_activities = []
    if runtime.product_automations is not None:
        from .product_automations import PRODUCT_AUTOMATION_ACTIVITIES, PRODUCT_AUTOMATION_WORKFLOWS
        product_workflows.extend(PRODUCT_AUTOMATION_WORKFLOWS)
        product_activities.extend(PRODUCT_AUTOMATION_ACTIVITIES)
    interceptors: list[Interceptor] = [ExecutionAuthority(runtime.database)]
    identity = settings_worker_identity()
    review = getattr(runtime, "product_memory_review", None)
    if review is not None:
        from .product_memory_review import (
            MEMORY_REVIEW_ACTIVITIES,
            MEMORY_REVIEW_WORKFLOWS,
            MemoryReviewWorkflow,
            ReviewAuthority,
        )
        MemoryReviewWorkflow.__pydantic_ai_agents__ = [review.agent]
        product_workflows.extend(MEMORY_REVIEW_WORKFLOWS)
        product_activities.extend(MEMORY_REVIEW_ACTIVITIES)
        interceptors.append(ReviewAuthority(review))
        identity += "-memory-review-v1-"
    drain = getattr(runtime, "product_drain", None)
    plugins = [ProductDrainPlugin(drain)] if drain is not None else []
    return Worker(
        runtime.client,
        identity=identity,
        task_queue=runtime.settings.temporal_queue,
        workflows=[TurnWorkflow, *SETTINGS_WORKFLOWS, *product_workflows],
        activities=[*ACTIVITIES, drain_checkpoint, *SETTINGS_ACTIVITIES, *product_activities],
        plugins=plugins,
        interceptors=interceptors,
        max_concurrent_activities=32,
        max_concurrent_workflow_tasks=16,
    )


async def dispatch_once(runtime: Runtime) -> int:
    client = await runtime.connect()
    drain = getattr(runtime, "product_drain", None)
    if drain is not None:
        if getattr(runtime.database, "product_drain", None) is not drain:
            raise ProductDrainError("Maintenance admission is not bound to this runtime.")
        await drain.notify(runtime)
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
    if runtime.product_automations is not None:
        try:
            # Keep automation RPC outages from delaying manual input/Stop.
            # The persisted pending intent and deterministic workflow ID make
            # another dispatcher pass safe after an unacknowledged RPC.
            async with asyncio.timeout(2):
                await runtime.product_automations.dispatch_pending(limit=5)
        except TimeoutError:
            logger.warning("Automation synchronization remains pending.")
    review = getattr(runtime, "product_memory_review", None)
    if review is not None:
        try:
            # Dispatch only durable intent. Temporal owns review execution and
            # deduplicates the workflow if an acknowledgment is lost.
            async with asyncio.timeout(2):
                await review.dispatch_pending(runtime, limit=5)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background review cannot fail manual dispatch
            logger.warning("Memory-review synchronization remains pending.")
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

