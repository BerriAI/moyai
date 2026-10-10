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
