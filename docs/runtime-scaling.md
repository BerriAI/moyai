# Capacity and execution worker scaling

Moyai can use one coordinator and multiple Temporal execution workers with
PostgreSQL and shared object storage. The default remains `standalone`, with
100 occupied sandboxes, 1,000 unfinished sessions and 8 model requests. Higher
budgets are now configurable; this does not provision provider quota or replicas.

## What each limit controls

| Setting | Default | Meaning |
| --- | ---: | --- |
| `MAX_CONCURRENT_RUNS` | 100 | Global occupied sandbox reservations, including provisioning, cleanup and retained warm machines. |
| `MAX_PENDING_RUNS` | 1,000 | Unfinished sessions, including active work and queued work; this is not just the waiting queue. |
| `MAX_CONCURRENT_MODEL_REQUESTS` | 8 | Concurrent requests through the coordinator's model gate. Full foreground requests receive the existing explicit, unbilled queue response. |
| `MOYAI_DATABASE_POOL_SIZE` | 8 | Connections per process, plus one separate process-ownership connection. Range 1–256. |
| `TEMPORAL_WORKER_ACTIVITIES` | 120 | Activities executing per worker process. An activity can observe a live sandbox for about 20 seconds. |
| `TEMPORAL_WORKFLOW_CACHE_SIZE` | 200 | Cached workflows per worker; eviction causes replay, not session loss. |
| `TEMPORAL_DISPATCH_BATCH_SIZE` | 200 | Maximum wake-outbox entries processed in each coordinator batch. |
| `TEMPORAL_DISPATCH_CONCURRENCY` | 10 | Concurrent wake deliveries within that batch. |

The three global budget settings have no artificial upper ceiling. They remain
admission controls: 3,000 active sandboxes do not require 3,000 simultaneous model
calls or database connections. Sandbox CPUs affect work inside a sandbox. Add
execution workers when Temporal activity queue latency is the bottleneck; increase
model capacity only with upstream RPM/TPM and measured coordinator headroom.

## Supported topology

- `MOYAI_RUNTIME_ROLE=standalone`: one API plus co-located execution worker, with
  SQLite or PostgreSQL. Its exclusive database ownership excludes cluster roles.
- `MOYAI_RUNTIME_ROLE=coordinator`: one API, model gate, wake dispatcher, and
  singleton recovery/background jobs. It does not consume Temporal activities.
- `MOYAI_RUNTIME_ROLE=worker`: consumes the same Temporal task queue, skips global
  startup recovery/background jobs, and serves readiness at `/health` only.
  Application HTTP and WebSocket traffic is rejected. Route all browsers,
  sandboxes and webhooks to the coordinator's public URL.

Run one server process per container. A worker can be replicated independently.
There must still be exactly one coordinator; a second coordinator is refused.
This is not support for arbitrary API replicas or uninterrupted coordinator
deployments. The single API/model broker remains a capacity and availability
boundary that must be measured before a 3,000-live-session production commitment.

All roles must use the same PostgreSQL schema, encryption/session keys, shared
object destination, public URL, Temporal target/namespace/task queue, global
budgets and application build. A stored fingerprint rejects mismatches. Set the
same full `MOYAI_BUILD_SHA` on every process. Pool sizes and per-worker concurrency
may differ. Keep other runtime/provider settings aligned too: the fingerprint
does not validate every application setting or contact the external services.

Session and global admission ownership use renewable 30-second database-clock
leases. They release pool connections between operations. Lease loss cancels an
activity, and every write transaction carrying a lease checks its token/expiry.
Transactions lock the lease row until commit so replacement ownership cannot
overlap an acknowledged old write. Crash takeover waits for expiry and Temporal
retry. Existing sandbox launch journals remain responsible for external-call
recovery; a lease cannot undo an already-issued provider request.

Session state saves, status updates, event inserts and wake acknowledgements use
per-session write locks. Distinct sessions can write concurrently. Cross-session
decisions, permissions, queue claims and other legacy writes retain the exclusive
schema lock. This reduces serialization without claiming all database contention
has been removed. Several synchronous database paths still occupy worker event
loops; use scheduling diagnostics to find remaining stalls.

## Rollout after the PostgreSQL cutover

1. Complete and validate the [standalone PostgreSQL cutover](postgres-runtime.md)
   first. Preserve the current file disk and original keys. Do not combine a
   database migration with a capacity increase or role change.
2. Migrate and verify legacy archives, captures and frozen handoffs to private
   shared object storage. New shared uploads alone are insufficient. The
   [storage-maintenance CLI](object-storage.md) uses the configured PostgreSQL
   database for `plan`, `migrate` and `verify`; it never reads the retained SQLite
   copy. Run it on the original disk, retain a managed Postgres backup, and keep
   the source files until verification and restored downloads pass. Distributed startup rejects local
   artifact files without a remote manifest, but an empty/wrong data directory
   cannot reveal files left on another disk. Verify inventory from the original
   disk and restored downloads before removing it.
3. In staging, configure the same stable keys, object destination, Temporal queue,
   coordinator public URL, build SHA and global budgets on every role. Test a real
   upload/download, broker call and saved workspace across two workers.
4. Drain admissions and stop the standalone process. Start one coordinator, then
   workers of the same build. Verify worker `/health`, a new turn, a follow-up,
   cancellation and recovery when a worker stops. Keep the API only on the
   coordinator. Start with existing budgets, then increase in measured steps.
5. Replicas of the same build/policy may overlap during worker replacement.
   For build/policy changes, drain and stop old workers before changing the
   coordinator; restart workers with the new configuration. Old-policy workers
   are fenced. Schema upgrades still occur at startup: incompatible code/schema
   upgrades require a coordinated maintenance window.

Use a direct database connection or session-preserving proxy, not transaction-mode
pooling. Budget worst-case connections as `sum(pool_size + 1)` across all processes,
including overlapping replacement workers, plus administration and provider
reserve. Larger pools can worsen database contention. For example, one coordinator
and four workers with pools of 16 need up to 85 connections before reserves.

An **illustrative staging capacity target**, not production sizing evidence:

```dotenv
MAX_CONCURRENT_RUNS=3000
MAX_PENDING_RUNS=10000
MAX_CONCURRENT_MODEL_REQUESTS=256
MOYAI_DATABASE_POOL_SIZE=16
TEMPORAL_WORKER_ACTIVITIES=256
TEMPORAL_WORKFLOW_CACHE_SIZE=1000
TEMPORAL_DISPATCH_BATCH_SIZE=1000
TEMPORAL_DISPATCH_CONCURRENCY=25
```

The required worker count depends on activity duration and arrival rate. For a
target of `R` activities/second and mean duration `D` seconds, at least `R × D`
activity slots are occupied on average; add headroom and measure tail latency.
For model requests with `T` average seconds per call and `C` active slots, an
idealized upper bound is `60 × C / T` requests/minute, also bounded by provider
RPM/TPM and memory/network. Size from actual tokens and latencies per model.

No infrastructure autoscaler is installed here. Scale worker replicas using
Temporal schedule-to-start delay and available activity slots, bounded by the
database connection budget. Modal/provider sandbox quota and upstream model
quota must be approved independently. Warm reuse and image preparation address
cold-start latency even when admission has spare capacity.

## Observe and reproduce

An authenticated administrator can read `GET /api/admin/capacity`: occupied
reservations, unfinished sessions, durable phases, live execution leases,
coordinator model activity/waiting counts, explicit model-queue responses and
that process's pool statistics. It contains no prompts, credentials or user IDs.
Lease count is active ownership, not the number of healthy replicas. Model queue
counters reset on process restart and do not measure provider-side throttling.

Use a disposable PostgreSQL database via `MOYAI_TEST_POSTGRES_URL`:

```sh
uv run --frozen pytest -q tests/test_runtime_scaling.py
uv run --frozen python scripts/runtime_capacity_probe.py --sessions 3000 --workers 4
```

Do not add `--postgres-backend` to the distributed suite: it deliberately opens
multiple stores against one schema. The probe creates 3,020 real session rows
through the submission gate, then runs real admission through four OS processes.
It requires exactly 3,000 occupied reservations and 20 queued, rejects pending
overflow, and exercises 3,000 synthetic model tasks with 256 slots. Its sandbox
steps and model completions are synthetic and incur no provider calls. Tests also
exercise lease expiry, a killed process, stale-write fencing, overlapping writes
and real Temporal worker recovery. These establish admission/ownership behavior,
not 3,000 real sandboxes, streaming responses, or production throughput.

## Startup measurements after #328

The coordinator now drains successful full wake batches immediately. Previously
it slept two seconds after each 200-row batch, adding 28 seconds of deliberate
pauses to a 3,000-wake burst. Idle polling and failed batches still back off for
two seconds; failed wakes remain in the outbox. A partial index limits each poll
to undelivered rows, rather than scanning retained session history.

Admission database calls and the activity's initial state read run off the event
loop. Cancellation waits for an in-flight claim to finish before releasing its
session/admission leases. This keeps ownership intact without pausing every other
activity during a database lock or pool wait. Other synchronous database paths
remain, especially inside execution phases; watch `slow_database` records with
`on_event_loop=true` before raising per-worker activity concurrency.

Local results on PostgreSQL 18.6, with no provider calls:

| Probe | Before | After |
| --- | ---: | ---: |
| Last wake of 3,000, default batch 200/concurrency 10, simulated 5 ms Temporal RPC | 30.51 s | 2.33 s |
| Worker event-loop lag during a real 700 ms writer lock | 702.58 ms | 1.22 ms |

These are observations on one developer machine, not production latency targets.
The admission operation still waits for the lock (~736 ms); other work can run
during that wait. Tests also hold a real pool connection, verify cancellation
ownership, retry failed deliveries, and check the wake index against 30,000
historical rows. Existing real Temporal tests cover worker replacement, retained
sandbox identity, offline follow-ups, and workflow replay.

The actual local Temporal server, with 200 synthetic 250 ms activities and eight
slots per worker, measured p95 schedule-to-start delays of 5.54 s / 2.54 s / 1.16 s
with 1 / 2 / 4 worker instances. Peaks were exactly 8 / 16 / 32 activities and all
200 completed in each run. These workers have separate SDK runtimes but share a
process; the experiment validates queue capacity, not multi-CPU efficiency.
A second 1,024-activity probe accepted 256 slots per worker and completed with
1 / 2 / 4 workers. It was arrival-limited (peak 196 / 279 / 405), so it does not
demonstrate saturated throughput at 1,024 simultaneous activities.

The separate PostgreSQL admission probe used real OS processes. All five cases
reserved exactly 3,000 sessions, queued 20, and rejected pending overflow:

| Worker processes | Maximum pool per process | Admission phase |
| ---: | ---: | ---: |
| 1 | 4 | 17.23 s |
| 2 | 4 | 16.80 s |
| 4 | 4 | 17.80 s |
| 4 | 8 | 18.77 s |
| 4 | 16 | 18.99 s |

These single-run samples overlapped some local regression work. They provide no
evidence that a larger pool speeds this workload. Admission retains a global
lease; adding workers helps available execution slots, not that serial decision.
These admission probes did not include provider cleanup. Increasing sandbox CPU
does not remove the shared database admission queue.

Reproduce locally with a disposable PostgreSQL URL and Python 3.13:

```sh
uv run --frozen --python 3.13 python scripts/runtime_startup_probe.py --sessions 3000
uv run --frozen --python 3.13 python scripts/runtime_capacity_probe.py --sessions 3000 --workers 4 --pool-size 4
uv run --frozen --python 3.13 python scripts/temporal_queue_probe.py --output temporal-queue.json
uv run --frozen --python 3.13 python scripts/temporal_queue_probe.py --sessions 1024 --slots 256 --activity-seconds .5 --output temporal-queue-256.json
uv run --frozen --python 3.13 pytest -q tests/test_runtime_performance.py tests/test_runtime_scaling.py tests/test_temporal_integration.py
```

## Capacity rollout recommendation

1. Deploy this performance change with existing standalone budgets. Coordinate
   that deploy with the production-cutover owner. This PR does not change any
   deployment setting. The pending-wake index is created at startup; include its
   creation in the normal coordinated startup window.
2. Run and verify the PostgreSQL-compatible shared-file migration added by
   [#330](https://github.com/BerriAI/moyai/pull/330) before
   starting distributed workers. Verify archives, browser captures and frozen
   handoffs from an empty replacement disk, with identical keys and object
   destination. All user/sandbox traffic still requires one coordinator.
3. Start staging at 100 occupied sandboxes, two workers, 120 activity slots per
   worker, pools of 8, and the currently configured production model budget.
   Preserve explicit overrides (for example, 32 model requests) rather than
   resetting them to the default of 8. Keep dispatch batch
   200/concurrency 10 initially: the artificial batch pauses are already removed.
   Two workers plus one coordinator use at most 27 DB connections; budget 36
   during one overlapping replacement, plus administration/reserve. Four-slot
   pools passed admission tests, but mixed execution/provider traffic still
   needs measurement before reducing the default eight.
4. Increase active work through 100, 250, 500, 1,000 and 3,000 only after measuring
   schedule-to-start p95, event-loop lag, database pool waits, first model token,
   provider startup/429s, error rate, and coordinator CPU/memory. Start with p95
   activity queue delay below 1 s and event-loop lag below 100 ms as staging
   acceptance targets, not measurements already achieved in production. Exercise
   stop, follow-up, upload/download and one-worker failure at every step.

For a 3,000-session staging trial, explicitly set `MAX_CONCURRENT_RUNS=3000`
and `MAX_PENDING_RUNS=10000`; the unchanged 1,000 pending default would reject
the larger workload. Consider workflow caches of 512 per worker after measuring
replay and memory; cache eviction is safe but adds replay work. Change shared
budgets/builds through the coordinated drain described above, since #328 fences
workers with a different policy. This is separate from scaling replicas of the
same build. Infrastructure autoscaling is still external: use sustained Temporal
queue delay and occupied slots to request another replica, bounded by DB and
provider budgets, and drain a worker before scale-down.

For planning, a monitoring activity holds a slot for roughly 20 seconds and is
immediately rescheduled. Fully running sessions can therefore approach one
occupied activity slot each; warm/idle sessions with durable timers do not.
At 70% planned utilization, use `ceil(active_sessions / (slots_per_worker * .7))`:

| Simultaneously running sessions | Slots per worker | Planning worker count |
| ---: | ---: | ---: |
| 100 | 120 | 2 |
| 500 | 256 | 3 |
| 3,000 | 256 | 17 |

Those larger counts are arithmetic targets, not tested production sizing. To
retain the same 30% headroom after losing one worker, add another worker. With
17 workers at pool 8 and one coordinator at pool 16, budget 170 connections;
two overlapping replacement workers and 20 reserved connections bring that to
208. A 256-connection database leaves margin for that topology only if other
clients fit within the remaining budget. More connections do not create more
database CPU or I/O capacity. Keep session-preserving connections for ownership.

Model capacity must be sized separately per upstream model. At 20 seconds per
request, 256 slots allow at most 768 requests/minute before RPM/TPM limits; for
an illustrative 10,000 input + 2,000 output tokens/call that is 9.216M tokens/min
(validate the provider's separate token buckets and caching rules). Serving
3,000 simultaneous model calls at that duration would require roughly 9,000 RPM
and 108M aggregate tokens/min plus coordinator/network memory headroom. Foreground
requests currently receive an explicit unbilled 429 with `Retry-After: 3` when
the model gate is full. Three thousand live sandboxes do not imply all 3,000 must
call a model at once. Measure their actual model duty cycle and per-model usage
before moving from 8 to 32/64/128/256 slots.

Provider requirements for 3,000 occupied sandboxes include at least that many
concurrent sandbox reservations, creation/termination burst allowance, image
availability and shared-storage bandwidth. Modal currently requests 2 vCPU and
4 GiB per sandbox by default: 3,000 is up to 6,000 requested vCPU and about
11.7 TiB RAM. Actual pricing/allocation rules depend on the provider. Confirm
quota, region capacity and cold/warm startup p95 directly; these local probes
do not query quotas, create paid sandboxes, call models, or certify that load.

Use existing `session_turn_claimed` queue wait, `session_wake_batch` delivery,
`session_activity` phase duration/schedule-to-start and `slow_database` logs to
separate the delays. Compare cold `provision` and `install` phases with warm
reuse before changing sandbox CPU. No current production trace sample or quota
verification is included in these local results.

## Admission during slow sandbox cleanup

Warm-sandbox eviction releases global admission while waiting for provider
termination. The victim remains protected by its session execution lease. Global admission is held for the final capacity
check and the new reservation, so an unrelated session can reclaim another idle
sandbox or claim a slot freed elsewhere. The initiating admission still waits
for its own cleanup; this does not speed up the provider's termination call.

The `warm_cleanup` journal stays occupied until termination succeeds. After
cleanup, admission checks capacity again under the global lock before reserving
anything: another worker may have taken the freed slot. Busy sessions and idle
sessions with queued follow-ups remain protected. Cancellation, a lost response,
or a worker crash retains the cleanup journal for retry under a new session lease.
The same admission guard covers new turns, computer wakes, environment readiness,
checkpointed steering, credential resumes and child-agent handoffs.

A local PostgreSQL probe with two worker stores and a simulated five-second
termination measured unrelated admission at **5,064.86 ms before / 20.00 ms after**.
The second admission finished before the slow cleanup; both slots remained
accounted for, and an extra session stayed queued. These stores share a process;
separate tests kill an OS worker during cleanup and recover after lease expiry.
Tests also cover SQLite, standalone PostgreSQL, separate worker connections,
cancellation and a competing session taking the reclaimed slot. Provider calls
are simulated, so this is not production sandbox-startup or throughput evidence.

```sh
# Use the disposable MOYAI_TEST_POSTGRES_URL described above.
uv run --frozen --python 3.13 python scripts/admission_cleanup_probe.py
uv run --frozen --python 3.13 pytest -q tests/test_admission_cleanup.py
```

There are no new capacity settings, database tables or Temporal workflow commands.
Deploy through the existing coordinated process and preserve configured limits.
Global reservation decisions still serialize, and full queues retain the existing
five-second retry. Worker replicas, provider quotas and model throughput remain
separate sizing decisions.
