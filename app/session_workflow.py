"""Version 1 session orchestration: IDs and small status flags only in history."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class SessionWorkflow:
    def __init__(self):
        self.revision = 0

    @workflow.signal
    def wake(self):
        self.revision += 1

    @workflow.run
    async def run(self, run_id: str):
        dirty = True
        for _ in range(150):
            if not dirty:
                revision = self.revision
                await workflow.wait_condition(lambda: self.revision != revision)
            before = self.revision
            busy = await workflow.execute_activity(
                'advance_session', run_id,
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2),
                                         maximum_interval=timedelta(seconds=60)),
            )
            # New Activity results only; historical boolean results follow the
            # original command sequence when replayed by upgraded workers.
            if busy in ('capacity', 'children'):
                await workflow.sleep(5)
            dirty = busy or self.revision != before
        # Re-check the authoritative inbox after rollover. Signals that arrived
        # during the final Activity have already committed their DB message.
        workflow.continue_as_new(run_id)
