# Coordinator replacement under continuous traffic

`MOYAI_COORDINATOR_HANDOFF=true` opts a verified PostgreSQL cluster into standby
coordinators. Keep it false until the dedicated API and broker routes are active,
all roles run the same compatible contracts, and the selected release's schema
(currently revision 8) is migrated offline. See [rolling deployment](rolling-deploy.md).
The flag belongs to the shared runtime settings on every role. This is an
application protocol; it does not remove Render disk or shutdown restrictions.

With the flag enabled, the coordinator serves `/hooks/*` and `/health` only. Route
ordinary application traffic to the dedicated API and `/broker/*` to the broker.
A replacement proves PostgreSQL and Temporal connectivity before binding its
port. It may report ready while in `standby`: it can durably accept signed hooks,
but it has not run recovery, dispatched wakes, or started background delivery.
This lets Render move hook traffic before stopping the old process.

The old process retains advisory lock 726943 on its runtime-owner PostgreSQL
connection for the entire background lifecycle, including shutdown. It stops its
inbox consumer, drains background components, and lets admitted Slack sends save
their receipts before releasing that lock. The replacement then acquires the
lock, runs recovery once, and starts the consumers. Other workers and inference
requests do not need to become idle. New hook events can accumulate during this
short transfer and are handled by the successor. Requests already admitted to the
old HTTP process must finish before its ASGI shutdown starts; configure Render's
shutdown allowance and Uvicorn drain accordingly.

Every background database operation inherits the coordinator owner context and
checks that the same runtime owner still holds 726943. A lost owner connection or
revoked compatibility record fences further operations and fails readiness. The
process never reconnects or elects itself again after releasing leadership. An
unexpected process or network failure can leave an external action ambiguous;
existing Slack receipts mark those actions uncertain instead of replaying them.
This protocol does not promise exactly-once effects at arbitrary external APIs.

A compatible successor joins the published `runtime_coordinator_policy` without
changing the baseline `runtime_policy` or any API/worker/broker contract. Shared
settings and the exact schema must match. Each background-role fingerprint covers
the source-owned API, worker, broker and coordinator protocol revisions; changing
any of these declares that the existing cluster contract is incompatible. Changing these
contracts requires a separately reviewed migration; supplying a different build
SHA or changing environment variables cannot declare compatibility.

## Hook behavior

- Slack events are size-bounded, signature-checked at intake, committed to a
  PostgreSQL inbox, then acknowledged. Event IDs deduplicate retries across both
  processes. Only the lock holder processes the inbox; saved events do not need
  their original five-minute HTTP signature to remain fresh. Completed payloads
  are cleared and receipts expire after one day. At most 1,000 events remain
  pending, with a retryable response when full. A failed event gets a delayed
  retry so later events can proceed.
- Slack URL verification returns the signed challenge synchronously.
- Slack interactions remain synchronous because modal triggers expire quickly
  and view submissions return field validation errors. A durable per-body claim
  prevents replicas replaying a single-use action. Successful JSON responses are
  cached for a retry; an interrupted or ambiguous action is never auto-replayed.
  The person can reopen the action. Interaction bodies are not stored.
- Automation hooks retain their existing signed, size-bounded, deduplicated
  `automation_events` inbox. Both coordinators can accept them; only the elected
  background coordinator dispatches them.

## Release evidence

`/health` keeps `{"status":"ok"}` and reports `X-Moyai-Build`,
`X-Moyai-Owner` (PostgreSQL backend PID), `X-Moyai-Role: coordinator`,
`X-Moyai-Rolling-Release: 1`, and
`X-Moyai-Coordinator-State: standby|leader|draining|failed|starting`.
A release controller must prove the new process is initially ready, the old
runtime owner eventually disappears, and the new owner alone holds 726943 and
reports `leader`. Standby readiness alone is not a completed handoff.

Tests in `test_coordinator_handoff.py` exercise two full ASGI applications against
real PostgreSQL with provider/Temporal boundaries mocked. They cover delivery to
a standby while the old owner consumes, successor takeover, signature/dedupe
boundaries, synchronous interaction responses, ambiguous actions, blocked
shutdown, compatibility revocation, and fenced background operations.
`test_slack_polling.py` verifies graceful shutdown finishes a blocked admitted send
and records `sent` once. A release still requires real multi-process traffic and
inference drain rehearsal before production activation.
