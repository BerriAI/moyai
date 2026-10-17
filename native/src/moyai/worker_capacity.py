"""Versioned activity routing through Temporal's public interceptor API."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import replace
from types import TracebackType
from typing import Literal, Self

import anyio
from temporalio import workflow
from temporalio.worker import (
    Interceptor,
    StartActivityInput,
    Worker,
    WorkerConfig,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

ROUTING_VERSION = "moyai-activity-capacity-isolation-v1"
WORKER_SHUTDOWN_VERSION = "moyai-worker-shared-drain-v1"
ActivityPool = Literal["model", "tools", "control"]


async def finish_cleanup[Result](operation: Awaitable[Result]) -> Result:
    """Retain the caller's cancellation, but never abandon owned poller cleanup."""
    task = asyncio.ensure_future(operation)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def activity_pool(name: str, args: Sequence[object] = ()) -> ActivityPool:
    if name.startswith("agent__"):
        if name.endswith((
            "__model_request", "__model_request_stream",
            "__model_compact_messages", "__model_cancel_suspended_response",
        )):
            return "model"
        if name.endswith((
            "__event_stream_handler", "__get_tools", "__get_instructions", "__validate_args",
        )) or any(
            marker in name for marker in (
                "__capability__step_persistence__", "__capability__moyai_effects__",
                "__capability__moyai_authority__", "__capability__planning__",
                "__capability__memory__load_snapshot",
            )
        ):
            return "control"
        return "tools"
    if (
        name == "moyai_save_computer_v1" and len(args) > 1
        and isinstance(args[1], Mapping) and args[1].get("provider") == "modal"
    ):
        return "tools"
    return "control"


class CapacityOutbound(WorkflowOutboundInterceptor):
    def __init__(self, next: WorkflowOutboundInterceptor):
        super().__init__(next)
        self.model_started = False

    def start_activity(self, input: StartActivityInput) -> workflow.ActivityHandle[object]:
        queue = workflow.info().task_queue
        pool = activity_pool(input.activity, input.args)
        if pool == "model":
            self.model_started = True
        elif not self.model_started and input.activity.endswith("__capability__workspace__call"):
            # The SDK uses one activity type for both startup workspace
            # validation/catalog reads and later commands. Before the first
            # model request these are preparation, not model-selected tools.
            # This state belongs to one workflow and is rebuilt during replay.
            pool = "control"
        # Old histories keep their exact queue. Explicit SDK/tool routing also
        # remains authoritative. New histories record this decision in Temporal.
        if input.task_queue in (None, queue) and workflow.patched(ROUTING_VERSION):
            input = replace(input, task_queue=f"{queue}-{pool}")
        return self.next.start_activity(input)


class CapacityInbound(WorkflowInboundInterceptor):
    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        self.next.init(CapacityOutbound(outbound))


class ActivityCapacity(Interceptor):
    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput,
    ) -> type[WorkflowInboundInterceptor]:
        return CapacityInbound


class WorkerPools:
    """One process owner, with independently bounded Temporal activity pools."""

    def __init__(
        self, workers: tuple[Worker, ...], *, register: Callable[[], Awaitable[None]] | None = None,
    ):
        self.workers = workers
        self.register = register
        self.stack: AsyncExitStack | None = None

    @property
    def is_running(self) -> bool:
        return all(worker.is_running for worker in self.workers)

    def config(self) -> WorkerConfig:
        return self.workers[0].config()

    async def __aenter__(self) -> Self:
        stack = AsyncExitStack()
        entered: list[Worker] = []
        try:
            if self.register is not None:
                await self.register()
            for worker in self.workers:
                await stack.enter_async_context(worker)
                entered.append(worker)
        except BaseException:
            try:
                await finish_cleanup(self._shutdown_workers(entered))
            finally:
                await finish_cleanup(stack.aclose())
            raise
        self.stack = stack
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> bool | None:
        assert self.stack is not None
        try:
            await finish_cleanup(self._shutdown())
        finally:
            result = await finish_cleanup(self.stack.__aexit__(exc_type, exc, traceback))
        return result

    async def run(self) -> None:
        async with self:
            await asyncio.Future[None]()

    async def shutdown(self) -> None:
        await finish_cleanup(self._shutdown())

    async def _shutdown(self) -> None:
        workers = self.workers if self.stack is not None else tuple(
            worker for worker in self.workers if worker.is_running
        )
        await self._shutdown_workers(workers)

    @staticmethod
    async def _shutdown_workers(workers: Sequence[Worker]) -> None:
        # Every poller starts the same bounded grace window together. During
        # rolling replacement, the ready candidate owns new workflow/control
        # polling while these activities finish on their original worker.
        results = await asyncio.gather(
            *(worker.shutdown() for worker in workers),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
