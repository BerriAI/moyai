"""Independent billing recovery; no model data or secrets in Temporal history."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class InferenceWorkflow:
    @workflow.run
    async def run(self, job_id: str):
        for _ in range(150):
            done = await workflow.execute_activity('advance_inference', job_id,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(initial_interval=timedelta(seconds=2), maximum_interval=timedelta(seconds=60)))
            if done is True:
                return
            await workflow.sleep(done.get('retry_seconds', 10) if isinstance(done, dict) else 10)
        workflow.continue_as_new(job_id)
