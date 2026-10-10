# Session startup latency

New-session submission immediately displays the submitted message, retains uploads
and the idempotency key until acknowledgement, and opens the accepted conversation
without waiting for the sidebar. Leaving during the request preserves the next
draft and does not navigate the user back. Failed saves offer retry and edit.

The Temporal outbox wakes its local dispatcher after transaction commit. PostgreSQL
NOTIFY also wakes a coordinator for writes from another process; rollback emits no
notification. A dedicated LISTEN connection reconnects independently, and the
existing two-second durable poll remains the fallback. Notifications contain only
the database schema; message content and credentials stay out of them.

Blocking database work in session creation/detail, tool discovery, and durable
startup phases runs in worker threads. Received event batches finish persistence
before cancellation releases ownership. Runtime upload and independent launch-spec
reads overlap; both must finish before agent launch.

## Overlap source context and cold acquisition

Cold Temporal turns now load Slack source context and attachments concurrently
with the existing workspace acquisition path. Warm, prepared-pool and demo turns
retain their previous ordering. This removes a sequential wait when both source
loading and provisioning take time. It does **not** start the agent before its
sandbox: native tools still run only inside that sandbox.

The existing session guard, capacity reservation, provider name, environment pin
and snapshot selection still own acquisition. A `context_pending` flag in the
durable session JSON survives provider completion. Only successful completion of
both branches clears it. If a worker stops after the machine is saved, the next
worker repeats the idempotent source reads before writing the launch spec. Agent
launch and broker capability activation still occur after that barrier.

Cancellation or a source error cancels source loading and joins acquisition before
releasing the session guard, including repeated activity cancellation. A returned
machine is recorded for ordinary cleanup; a lost acknowledgement retains the
existing provider-name reconciliation path. This does not add provider-level
exactly-once guarantees or solve a provider create that remains ambiguous after a
hard process crash. No agent actions are retried by this change.

Starting acquisition earlier can start billable compute earlier, including when
source preparation later fails. A ready machine can wait for slower source I/O.
No compute-cost reduction is claimed. No setting or database schema changes are
required. Deploy matching builds using the existing coordinated rollout. To roll
back, finish or stop active turns and let their cleanup settle before reverting
all roles together; older code does not understand the new pending-context marker.

### Controlled measurements and demo

The same real durable controller was run from baseline `945983c766ee608ad51e5eefd1df086d7957a00f`
and the candidate with identical provider/source fixtures. Each sample creates a
fresh SQLite store and a cold session. The timer starts immediately before input
persistence and stops at the agent launch boundary; API delivery, Temporal queue
time, SDK startup, model inference, and final response are not measured.

| Fixture: source 300 ms, provider 500 ms | Cold samples | Median | p95 (nearest rank) | Range |
| --- | ---: | ---: | ---: | ---: |
| Baseline sequential | 20 | 846.75 ms | 861.12 ms | 837.64–862.32 ms |
| Candidate overlap | 20 | 549.12 ms | 556.49 ms | 540.33–573.64 ms |

Every sample reached one launch with one fixture machine and complete source
context. These local distributions demonstrate removal of the sequential wait;
they are not production model-response percentiles. No live model/cloud run or
cost measurement was performed for this prerequisite. Warm reuse is regression
tested but not included in these cold measurements.

```sh
uv run --frozen python scripts/startup_overlap_probe.py --samples 20 --output /tmp/candidate.json
# Run the same probe against an unchanged baseline checkout:
uv run --frozen python scripts/startup_overlap_probe.py --source /path/to/baseline --samples 20 --output /tmp/baseline.json
# Local diagnostic UI: real controller, simulated external I/O, 3s/4s waits:
uv run --frozen python scripts/startup_overlap_probe.py --serve 8979 --baseline /path/to/baseline
# Open http://127.0.0.1:8979 and click Run comparison.
```

The remaining work to start the real agent without an execution sandbox is
described in [Agent startup separation](agent-startup.md).

## Optional prepared workspaces

`SANDBOX_PREPARED_POOL_SIZE` defaults to **0**. Leave it at 0 for the first rollout.
Enabling it requires Temporal, Modal, and a size smaller than `MAX_CONCURRENT_RUNS`.
`SANDBOX_PREPARED_IDLE_SECONDS` defaults to 300 (30–3600).

The pool creates clean Modal machines and syncs the runtime, without a task, agent
process, run token, identity, or connected-app credentials. It serves only fresh
sessions without a snapshot, repository, selected or default environment, parent, side chat,
or Slack source. Used workspaces never return to the pool. Runtime changes invalidate
old entries; the fingerprint includes Python adapters and Hermes patches.

A shared lease serializes pool maintenance. Every reservation counts toward the
existing workspace limit, including creation and cleanup. Atomic assignment
transfers ownership to the durable session journal and public workspace reference
in one transaction. Only a new response that can claim the prepared machine gets
admission credit; computer wake and expired claims use ordinary capacity checks.
Admission reclaims ready pool machines before stopping existing idle workspaces,
skips uncertain cleanup entries, and does not wait on an ongoing pool build.

A lost create acknowledgement is reconciled by its durable name. If creation is
ambiguous and lookup still says absent during cleanup, the reservation remains
counted and lookup retries. It is deliberately not forgotten: a late cloud create
could otherwise leave a billable machine untracked. A permanently absent ambiguous
reservation can consume capacity until operational reconciliation; inspect the
provider before clearing it. Pool disabling, expiry and build changes only reclaim
unassigned entries. Provider cleanup failures retain reservations for retry.

Preparation adds idle compute cost and only removes provisioning/runtime-file work
on a pool hit. Agent startup and model latency still remain. Coordinator and worker
must have matching code and pool settings because they participate in the shared
runtime fingerprint. Activation is a separate configuration/cost decision.

## Reproduce the UI check

```sh
uv run python scripts/startup_latency_demo.py --port 8892
```

Open `http://127.0.0.1:8892/demo/startup-login`, type a message, and press Enter.
This localhost-only demo uses real UI/session APIs and simulated execution, with
3 seconds added to create, 2 to detail, and 8 to sidebar requests. It does not call
a model, SSO, Modal, or connected apps. The diagnostic overlay is demo-only.
`--frontend-root /path/to/baseline/app/static` serves another frontend against the
same API and delay fixture.

A matched 1440×900 browser sample against `af31f1f` showed message display moving
from 13,337 ms to 8 ms, and a usable conversation from 13,337 ms to 5,123 ms. These
are controlled local samples, not production percentiles. Real cloud measurements
must compare submission, durable claim, provisioning, installation, launch, first
model request and first response after the coordinated deployment.

Regression coverage includes committed cross-process wakes and rollback on real
PostgreSQL, two-worker pool assignment, capacity, lost creation ACKs, delayed
appearance during cleanup, saved workspace preservation, cancellation persistence,
parallel install preparation, permanent setup errors, retries and navigation away.
