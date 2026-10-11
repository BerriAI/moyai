"""Local Temporal queue/slot probe with synthetic activity duration, no providers.

Uses Moyai's actual SessionWorkflow and separate SDK worker runtimes. Workers
share this process; this measures queueing and slots, not multicore scalability.
"""
import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from temporalio import activity
from temporalio.client import Client
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.session_workflow import SessionWorkflow


async def measure(sessions=200, slots=8, duration=.25):
    measurements = []
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        for replicas in (1, 2, 4):
            waits, active, peak = [], 0, 0
            complete = asyncio.Event()
            finished = 0

            @activity.defn(name='advance_session')
            async def step(run_id: str) -> bool:
                nonlocal active, peak, finished
                info = activity.info()
                waits.append(max(0, (info.started_time - info.current_attempt_scheduled_time).total_seconds() * 1000))
                active += 1
                peak = max(peak, active)
                try:
                    await asyncio.sleep(duration)
                    finished += 1
                    if finished == sessions:
                        complete.set()
                    return False
                finally:
                    active -= 1

            queue = 'moyai-probe-' + uuid4().hex
            handles = []
            async with AsyncExitStack() as stack:
                for number in range(replicas):
                    client = await Client.connect(env.client.service_client.config.target_host,
                        runtime=Runtime(telemetry=TelemetryConfig()), identity=f'probe-{number}')
                    await stack.enter_async_context(Worker(client, task_queue=queue,
                        workflows=[SessionWorkflow], activities=[step],
                        max_concurrent_activities=slots, max_cached_workflows=max(200, sessions),
                        graceful_shutdown_timeout=timedelta(seconds=1)))
                started = time.monotonic()
                launches = asyncio.Semaphore(20)

                async def launch(number):
                    async with launches:
                        handle = await env.client.start_workflow(SessionWorkflow.run, str(number),
                            id=f'{queue}-{number}', task_queue=queue, start_signal='wake')
                        handles.append(handle)

                try:
                    await asyncio.gather(*(launch(number) for number in range(sessions)))
                    await asyncio.wait_for(complete.wait(), 120)
                    waits.sort()
                    assert peak <= replicas * slots and finished == sessions
                    measurements.append({'worker_instances': replicas, 'activity_slots_each': slots,
                        'sessions': sessions, 'synthetic_activity_seconds': duration,
                        'peak_activities': peak, 'completed': finished,
                        'elapsed_ms': round((time.monotonic() - started) * 1000, 2),
                        'schedule_to_start_p50_ms': round(waits[(len(waits) - 1) // 2], 2),
                        'schedule_to_start_p95_ms': round(waits[int((len(waits) - 1) * .95)], 2)})
                finally:
                    await asyncio.gather(*(h.terminate('Local probe complete') for h in handles))
    return {'local_temporal': True, 'synthetic_activities': True, 'no_provider_calls': True,
            'measurements': measurements}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sessions', type=int, default=200)
    parser.add_argument('--slots', type=int, default=8)
    parser.add_argument('--activity-seconds', type=float, default=.25)
    parser.add_argument('--output', type=Path, help='Write JSON here; the local server can also print diagnostic logs.')
    args = parser.parse_args()
    if not 1 <= args.sessions <= 3000 or not 1 <= args.slots <= 512 or not 0 < args.activity_seconds <= 20:
        parser.error('Use 1–3000 sessions, 1–512 slots, and an activity duration in (0,20].')
    result = json.dumps(asyncio.run(measure(args.sessions, args.slots, args.activity_seconds)), indent=2)
    if args.output:
        args.output.write_text(result + '\n')
    else:
        print(result)
