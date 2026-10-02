"""Temporal holds the clock; only IDs and execution status enter its history."""
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class AutomationWorkflow:
    @workflow.run
    async def run(self, automation_id: str, revision: int, run_id: str = ''):
        if not run_id:
            result = await workflow.execute_activity(
                'launch_automation',
                args=[automation_id, revision, workflow.info().workflow_id,
                      (workflow.info().start_time + timedelta(minutes=15)).isoformat()],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=60)),
            )
            run_id = result.get('run_id', '')
            if not run_id:
                return result
        # Keep the schedule action open while its session works or awaits input.
        # Temporal's SKIP policy then prevents overlapping scheduled actions.
        for _ in range(1000):
            finished = await workflow.execute_activity(
                'automation_finished', run_id, start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=60)),
            )
            if finished:
                return {'run_id': run_id}
            await workflow.sleep(15)
        workflow.continue_as_new(args=[automation_id, revision, run_id])
