"""Native Temporal worker and idempotent application-to-workflow dispatch."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import (
    Interceptor,
    Worker,
)

from .activities import ACTIVITIES
from .dispatch_wakeups import DispatchWakeups
from .execution import ExecutionAuthority
from .models import Turn
from .product_drain import ProductDrainError, drain_checkpoint
from .runtime import Runtime, configure
from .settings_runtime import SETTINGS_ACTIVITIES, SETTINGS_WORKFLOWS, settings_worker_identity
from .worker_capacity import ActivityCapacity, WorkerPools
from .workflow import TurnWorkflow

logger = logging.getLogger(__name__)


def make_worker(runtime: Runtime) -> WorkerPools:
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
    interceptors: list[Interceptor] = [ExecutionAuthority(runtime.database), ActivityCapacity()]
    agent_plugins = [AgentPlugin(runtime.agent)]
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
        agent_plugins.append(AgentPlugin(review.agent))
        identity += "-memory-review-v1-"
    drain = getattr(runtime, "product_drain", None)
    activities = [*ACTIVITIES, drain_checkpoint, *SETTINGS_ACTIVITIES, *product_activities]
    legacy = Worker(
        runtime.client,
        identity=identity,
        task_queue=runtime.settings.temporal_queue,
        workflows=[TurnWorkflow, *SETTINGS_WORKFLOWS, *product_workflows],
        activities=activities,
        interceptors=interceptors,
        max_concurrent_activities=runtime.settings.temporal_worker_activities,
        max_cached_workflows=runtime.settings.temporal_workflow_cache_size,
    )
    pools = tuple(Worker(
        runtime.client, identity=identity,
        task_queue=f"{runtime.settings.temporal_queue}-{pool}",
        activities=activities, plugins=agent_plugins, interceptors=interceptors,
        max_concurrent_activities=runtime.settings.temporal_worker_activities,
        max_cached_workflows=runtime.settings.temporal_workflow_cache_size,
    ) for pool in ("control", "model", "tools"))
    # All pollers belong to the same canonical process owner. Register once,
    # before any poller can execute; retirement still follows owner disappearance.
    return WorkerPools((legacy, *pools), register=drain.register_worker if drain is not None else None)


@dataclass
class DispatchState:
    starting_after: tuple[str, str] | None = None
    starting_through: tuple[str, str] | None = None
    backlog: bool = False


async def dispatch_once(runtime: Runtime, state: DispatchState | None = None) -> int:
    state = state if state is not None else DispatchState()
    state.backlog = False
    client = await runtime.connect()
    drain = getattr(runtime, "product_drain", None)
    if drain is not None:
        if getattr(runtime.database, "product_drain", None) is not drain:
            raise ProductDrainError("Maintenance admission is not bound to this runtime.")
        await drain.notify(runtime)
    batch_size = runtime.settings.temporal_dispatch_batch_size
    if state.starting_through is None:
        # Freeze each scan round before claiming more work. Later arrivals
        # cannot extend it indefinitely and starve retries near the beginning.
        state.starting_through = await runtime.database.dispatch_boundary()
    claimed = await runtime.database.claim_batch(batch_size)
    starting = await runtime.database.starting_turns(
        limit=batch_size, after=state.starting_after, through=state.starting_through,
    )
    slots = asyncio.Semaphore(runtime.settings.temporal_dispatch_concurrency)

    async def start(turn: Turn) -> bool:
        async with slots:
            try:
                async with asyncio.timeout(10):
                    await client.start_workflow(
                        TurnWorkflow.run, turn.id, id=turn.workflow_id,
                        task_queue=runtime.settings.temporal_queue,
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
            except WorkflowAlreadyStartedError:
                # The deterministic ID also reconciles a lost acknowledgment.
                pass
            except (RPCError, TimeoutError):
                logger.warning("Temporal did not acknowledge dispatch; the saved request will be reconciled.")
                return False
            return True

    results = await asyncio.gather(*(start(turn) for turn in starting))
    dispatched = sum(results)
    full_page = len(starting) == batch_size
    later = (
        bool(await runtime.database.starting_turns(limit=1, after=state.starting_through))
        if not full_page and state.starting_through is not None else False
    )
    selected = {turn.id for turn in starting}
    newly_claimed_outside_page = any(turn.id not in selected for turn in claimed)
    if full_page:
        state.starting_after = starting[-1].created_at, starting[-1].id
    else:
        state.starting_after = state.starting_through = None
    state.backlog = all(results) and (
        len(claimed) == batch_size or full_page or later or newly_claimed_outside_page
    )
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


async def dispatcher(runtime: Runtime, *, wakeups: DispatchWakeups | None = None) -> None:
    state = DispatchState()
    async with wakeups or DispatchWakeups(runtime.settings) as listener:
        while True:
            # Clear before reading durable rows, so a commit during/after the
            # scan cannot be lost between deciding to wait and actually waiting.
            listener.event.clear()
            try:
                await dispatch_once(runtime, state)
            except asyncio.CancelledError:
                raise
            except Exception:
                state.backlog = False
                logger.exception("Dispatch is temporarily unavailable; saved inputs remain queued.")
            if state.backlog:
                await asyncio.sleep(0)  # Yield to HTTP/owner monitoring, without a polling delay.
            else:
                await listener.wait()


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

