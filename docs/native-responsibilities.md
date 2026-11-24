# Private responsibilities (first Dot slice)

Dot owns an ongoing responsibility separately from any conversation or worker.
In this first slice, a person explicitly selects a private native web chat, an
objective, a model, a check interval and a maximum number of reviews. Existing
activity is baselined silently. New completed replies can produce a private,
quoted finding and a suggested next step without another prompt to Dot.

Open **Dot** in the workspace rail or Settings. Add a responsibility, choose its
source, then continue using that source chat normally. Findings appear in Dot's
inbox. Pause revokes an outstanding assignment; Resume catches up on activity
since the last reconciled review without resetting the review budget.

This is the durable observation/assignment/inbox foundation, not general
personal planning. It does not discover new sources, monitor Slack, initiate
external actions, or enable a watch by default.

## Execution and persistence

- `Responsibilities` owns enrollment, controls, review admission and
  reconciliation. One Temporal entity workflow owns the lifecycle of each
  responsibility generation. There is no process-local responsibility timer,
  due-row scanner or long-running model session per person.
- A completion sequence is appended under native answer settlement's session
  fence. Enrollment takes the same fence before establishing its baseline.
  A turn that was still saving at enrollment cannot disappear behind a
  message-ID watermark. Only completed accepted answers from owner-authored,
  direct web requests are eligible. Generated turns and approval chains are
  excluded. Imported, delegated, swarm and evaluator chats cannot be sources.
- Enrollment, source completion, review settlement and controls append wake
  intents in the same transaction as their domain changes. The existing native
  dispatcher delivers the outbox using Temporal Signal-With-Start, deterministic
  IDs (`moyai-responsibility-<id>-<generation>`) and `REJECT_DUPLICATE`.
  It acknowledges only the event IDs sent in that RPC. Unacknowledged intents
  remain saved; bounded scan rounds advance only past attempted groups, so an
  unavailable head or cancellation cannot starve later delivery. Delivery has
  a two-second coordinator budget after manual
  input and Stop handling. This is transactional transport, not a second
  scheduler or workflow engine.
- The workflow executes a domain step, then waits for a signal or a Temporal
  durable timer. Source cadence is a not-before time when new evidence exists;
  an unchanged source waits indefinitely without model calls or recurring
  step activities. Native settlement signals reconcile finished reviews
  independently of source cadence. An assignment-expiry timer remains in
  place. Transient step failures use the SDK's Activity RetryPolicy, and
  Continue-As-New bounds workflow history. Signals are coalesced hints: every
  step, including the first step after rollover, reads committed database state.
- Each step transactionally reserves a review, creates a private audit session
  and admits a native turn. The existing native dispatcher remains the sole
  starter of its `TurnWorkflow`; its deterministic identity and native
  settlement own review execution and recovery. Controller arguments and
  signals carry identity and generation, not evidence, prompts or results.
  An on-time accepted result retains its native settlement path after assignment
  expiry; expiry prevents new work, not completion of an already accepted save.
  The controller waits for settlement without admitting another review.
  A stuck accepted save relies on existing native recovery and operational
  monitoring; this slice does not add a new pending-save-age alert.
- A closed current controller cannot be silently restarted with the same
  identity. A rejected wake marks the responsibility blocked with a recovery
  reason; explicit Resume advances its generation. A generation-fenced recovery
  step can still reconcile an already accepted result, including after the last
  review is spent. It preserves pending native settlement, never admits another
  review and does not charge the budget again. Resume is rejected under the
  worker Session fence while an accepted result still awaits reconciliation,
  so it cannot discard that result or charge again for the same evidence.
  Stale generations cannot admit work or publish findings.
- `moyai_responsibility` is a separate `NativeOutput` agent. Its input contains
  the objective and at most five new messages, each capped at 6,000 characters.
  It has no functional tools, account selection, conversation history, skills,
  computer, private instructions or memory injection. Generated admission
  ignores gateway mentions in source text. Preparation bypasses hydration.
- Each review permits at most three model requests, 1,500 output tokens per
  request, 90 seconds per request with no provider activity retries, and a
  five-minute assignment lifetime. The review count is reserved before
  dispatch and is not refunded for uncertain, failed or canceled work.
  This is a review limit, not a dollar-denominated spending cap.
- Only an accepted completed native turn can reconcile. The decision must be
  typed, and every citation must quote an exact substring of its saved packet.
  This verifies quotation provenance; it does not verify the source's claims
  against an external system. `wait` advances the cursor silently. `notify`
  writes an inbox receipt in the same transaction as advancing the cursor.
  Repeated identical captured message packets are deduplicated per
  responsibility, independently of the model's chosen quotation. This is
  exact packet deduplication, not semantic deduplication across different text.

## Authority

Enrollment pins the individual owner, native auth version and original product
grant. Workers have no connected accounts. Owner, source visibility/archive/
deletion and original authority are rechecked at wake, preparation, activity
execution, product inference admission, answer publication and inbox/audit read.
Pause advances the generation and stops the assignment under the existing
native session fence. A late result cannot advance the responsibility or
publish a finding.

Audit sessions are excluded from ordinary chat lists and private history
recall. Generic chat reads, debug export, message/control mutations, archive,
delete, link attachment and automation destinations reject audit sessions.
The private **Review activity** dialog reads saved assignments through a
dedicated API, including failed attempts after the active pointer is cleared.
That read holds the original authority and source fences through its transaction.
It reuses the same packet validator as reconciliation and suppresses pending
or rejected decision content. Invalid quotations appear as validation failures.
Their saved answers do not enqueue personal memory or skill learning. The
source itself must remain accessible even when reading a copied audit result.

Publication and inference admission acquire locks in canonical-authority,
native User, responsibility, source Session, worker Session order. Source
deletion/archival cannot commit between the protected source check and the
broker inference receipt.

The v28 application migration and frozen online expansion add four domain
tables. The additive v29 migration adds the wake outbox without changing the
frozen v28 identity. The outbox has no responsibility foreign key: settlement
hooks must not acquire an implicit parent lock while holding a source or
worker Session fence. Hook keys are disjoint from control/start keys.

At coordinator startup, waiting/running generations receive idempotent wake
intents. This adopts existing v28 rows and also deliberately rechecks committed
state after a restart. It preserves assignments, evidence cursors, budgets and
inbox receipts. A stale seed is harmless because step checks the generation;
concurrent enroll/resume already commits its own wake. Deterministic
Signal-With-Start prevents a second chain for the same generation.

Runtime protocol 10 changes the production runtime fingerprint. The existing
production role session factory checks that policy at each transaction: a
stale participant cannot admit, claim or prepare responsibility work, including
cached preparation. Protocol numbering alone does not isolate Temporal queues.
Use the existing coordinated release boundary: fence/drain old production
owners before enabling this version. Standalone queues must not mix incompatible
workers; stop every old poller before starting this release.

## API

All routes require the ordinary authenticated session and CSRF policy. Payloads
are Pydantic models; the TypeScript client is generated from OpenAPI.

| Operation | Route |
| --- | --- |
| Enroll / list | `POST` / `GET /api/responsibilities` |
| Pause / resume with expected generation | `POST /api/responsibilities/{id}/control` |
| Private review activity | `GET /api/responsibilities/{id}/reviews` |
| Private inbox | `GET /api/responsibility-inbox` |
| Mark reviewed | `POST /api/responsibility-inbox/{turn_id}/read` |

## Local verification

Use the actual repository app, isolated data and a configured development model:

```sh
uv sync --group dev --frozen
npm ci
npm run build
# Set the normal local-only workspace password and gateway configuration.
# Tin's scoped MOYAI_E2E_RUNNER may wrap this command when available.
uv run moyai dev --host 127.0.0.1 --port 8793
```

1. On baseline `48cd852b620bb7439927757ef03f3f6ea7d510a3`, verify that the
   enrollment route and Dot page are unavailable.
2. On the candidate, create a private source chat and complete an ordinary
   model turn. Enroll a responsibility in the UI. Confirm zero reviews and
   no inbox result for existing activity.
3. Enroll a second private source. Stop and restart the actual app using the
   same isolated data directory. Confirm both responsibilities remain waiting
   with zero reviews.
4. Send a new meaningful status to each source through the app. Wait for the
   configured checks; do not prompt Dot. Confirm two independent findings
   with exact source quotes. Inspect each review's saved native usage: real
   model requests, zero tools, and a bounded selected evidence packet.
5. Inspect the actual Temporal workflow histories: enrollment creates a
   responsibility controller; source completion wakes it; review settlement
   wakes reconciliation; unchanged sources schedule no recurring activities.
   Wait another check interval. Confirm no duplicate findings or extra reviews.
   Exercise Pause, Resume and Mark reviewed. Verify source access loss hides
   evidence and blocks future work.
6. Record the real UI and inspect the page and enrollment dialog at 1440, 768
   and 320 pixels. Fixture browser checks are supplementary.

Upgrade verification also starts the actual app against isolated v28 data,
checks v29 adoption without resetting saved work, and restarts the final app.
Real Temporal integration tests exercise durable timers across worker restart,
accepted-start/lost-ack recovery, retries, closed-controller recovery, and
Continue-As-New with concurrent evidence; their model fixtures are supplementary
to the actual-app proof.

Regression checks include `test_responsibilities.py`,
`test_responsibility_temporal.py`, the API contract,
admission/history/settlement coverage, the three PostgreSQL schema suites,
`npm run api:check`, `npm run typecheck`, `npm test`, `npm run build` and
`npm run test:ui`.
