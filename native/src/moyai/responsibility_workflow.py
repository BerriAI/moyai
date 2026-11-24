"""Temporal owns responsibility timers, activity retries and history rollover."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


@dataclass
class ResponsibilityRun:
    identity: str
    generation: int
    # A small value exercises real Continue-As-New in the integration tests.
    max_wakes: int = 500

    @property
    def workflow_id(self) -> str:
        return f"moyai-responsibility-{self.identity}-{self.generation}"


@dataclass
class ResponsibilityStep:
    finished: bool = False
    wake_at: str | None = None


@activity.defn(name="moyai_responsibility_step_v1")
async def responsibility_step(request: ResponsibilityRun) -> ResponsibilityStep:
    from .runtime import current

    return await current().database.responsibilities.tick(request.identity, generation=request.generation)


@workflow.defn(name="MoyaiResponsibilityV1")
class ResponsibilityWorkflow:
    def __init__(self) -> None:
        self.dirty = True

    @workflow.signal
    def changed(self) -> None:
        self.dirty = True

    @workflow.run
    async def run(self, request: ResponsibilityRun) -> None:
        for _ in range(max(1, request.max_wakes)):
            self.dirty = False
            state = await workflow.execute_activity(
                responsibility_step, request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1), maximum_interval=timedelta(seconds=30),
                ),
            )
            if state.finished:
                return
            if workflow.info().is_continue_as_new_suggested():
                break
            delay = max(0, (datetime.fromisoformat(state.wake_at) - workflow.now()).total_seconds()) if state.wake_at else None
            try:
                await workflow.wait_condition(lambda: self.dirty, timeout=delay)
            except TimeoutError:
                pass
        # Signals only set a coalesced hint. The database ledger is read again
        # on the new run, including signals delivered during this rollover.
        await workflow.wait_condition(workflow.all_handlers_finished)
        workflow.continue_as_new(request)
