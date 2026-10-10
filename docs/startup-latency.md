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
