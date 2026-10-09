# Diagnosing a slow session start

Moyai's API, Temporal activity callbacks and background jobs share one Python
event loop. A synchronous SQLite writer-lock wait on that loop also delays
heartbeats, dispatch and unrelated requests. Increasing sandbox CPUs does not
fix that control-plane delay.

The control endpoint now handles database operations in a thread, waits for
outstanding writes before propagating cancellation, and revalidates the sandbox
capability after reading the request body. Empty receipt lists and control
polls without pending input do not acquire SQLite's writer lock. A positive
read hint is rechecked in the existing transaction before input is locked.
An input that arrives after a negative hint is picked up on the next poll.

Wake-outbox reads and acknowledgements also run off the event loop. A failed
wake delivery remains pending for retry without restarting the healthy Temporal
worker or cancelling unrelated activities. Actual worker failure still reconnects.

## Local reproduction

```
uv sync --frozen --group dev
uv run python scripts/scheduling_stall_demo.py
```

This exercises the real ASGI broker endpoint and SQLite database. An independent
writer holds the database for one second, while a 10 ms heartbeat measures event
loop delay. It runs both an empty control poll and a real queued correction.
No Temporal, sandbox or model service is called by this probe.

With main at `3390021`, the original probe measured 961 ms of event-loop delay.
The first run with this change measured 8 ms for an empty poll and 5 ms with a
correction. The correction still waits for the database writer; other tasks can
run during that wait. These are local contention results, not production latency
or evidence of 3,000-session capacity.

## Timings in service logs

Scheduling records use logger `uvicorn.error.moyai.scheduling` and JSON version 1.
Correlate by `run_id`, message, activity and workflow execution IDs where present.
The platform log timestamp gives the observation time.

| Event / field | Meaning |
| --- | --- |
| `session_wake_delivered` | Temporal acknowledged a wake and its outbox revision was marked delivered; duration includes that acknowledgement write. |
| `session_wake_batch` | Dispatch batch size, failures and elapsed time, including its local concurrency wait. |
| `session_turn_claimed.queue_wait_ms` | Message creation to durable claim, including any intentional wait behind an existing turn or for capacity. Includes follow-ups. |
| `session_activity.schedule_to_start_ms` | Temporal's current-attempt activity scheduling-to-start interval, using server timestamps. This excludes workflow-task scheduling/replay; inspect Temporal history for that interval. |
| `session_activity.duration_ms` | Time executing one activity, labelled with the phase at entry. `idle` includes admission/claim/prepare, `provision` includes environment preparation and provider find/create/initialization, `install` covers runtime/file setup, `launch` starts the supervisor. Retries have separate attempt IDs. |
| `event_loop_lag` | A one-second probe ran at least 500 ms late. Includes process CPU consumed across all threads during the observed interval; it does not attribute CPU to a particular request. |
| `slow_database` | A connection/transaction took at least 100 ms, including commit/close. Includes code locations and whether it ran on the event loop. Globally limited to one warning per ten seconds; this is a diagnostic sample, not a full query histogram. |
| Broker `first_response_body_ms` | Time from receipt of the broker request until the first nonempty response body. Included in the existing finished/failed record. For model streams this can be an SSE metadata event, not necessarily a generated token; a failed HTTP response is not a successful model response. |

Normal monitor activities can intentionally wait 20 seconds and are omitted from
activity timing logs unless their scheduling wait is at least 500 ms or they fail.
Logs never include SQL text/parameters, prompts, request/response bodies, tokens,
exception messages or frame locals. Workflow command sequences are unchanged.

## Verify after deployment

Confirm the deployed commit first. Compare message-to-claim and startup-phase
latencies under similar traffic before and after deployment, separately for cold
starts, warm reuse and follow-ups. Correlate delayed wakes with Temporal's
workflow-task scheduled/started/timed-out history, activity scheduling delays,
event-loop lag and slow database call sites. Use those observations to choose
the next hot path to move or optimize.

This change does not raise concurrency limits or provision additional CPUs.
Other synchronous database/processing paths still share the event loop. The
earlier production workflow-task timeout is evidence of a scheduling stall,
but this local reproduction does not prove the control endpoint caused that
specific incident.
