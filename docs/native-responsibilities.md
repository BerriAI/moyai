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

- `Responsibilities` owns enrollment, controls, due checks and reconciliation.
  There is no process-local timer or long-running model session per person.
- A completion sequence is appended under native answer settlement's session
  fence. Enrollment takes the same fence before establishing its baseline.
  A turn that was still saving at enrollment cannot disappear behind a
  message-ID watermark. Only completed accepted answers from owner-authored,
  direct web requests are eligible. Generated turns and approval chains are
  excluded. Imported, delegated, swarm and evaluator chats cannot be sources.
- The existing dispatcher scans indexed due rows, transactionally reserves a
  review, creates a new private audit session and admits a native turn.
  `TurnWorkflow` and its deterministic workflow ID still own execution and
  recovery from an unacknowledged start. Each responsibility has independent
  active work and due state.
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
  Repeated identical quoted evidence is deduplicated per responsibility.

## Authority

Enrollment pins the individual owner, native auth version and original product
grant. Workers have no connected accounts. Owner, source visibility/archive/
deletion and original authority are rechecked at wake, preparation, activity
execution, product inference admission, answer publication and inbox/audit read.
Pause advances the generation and stops the assignment under the existing
native session fence. A late result cannot advance the responsibility or
publish a finding.

Audit sessions are excluded from ordinary chat lists and private history
recall. They cannot accept new messages, queued edits or approval resumptions.
Their saved answers do not enqueue personal memory or skill learning. The
source itself must remain accessible even when reading a copied audit result.

The v28 application migration and frozen online expansion add four tables.
Runtime protocol 9 prevents older production participants from interpreting a
responsibility assignment as an ordinary tool-enabled turn. This release must
use the existing coordinated release boundary; do not mix incompatible workers
on a standalone queue.

## API

All routes require the ordinary authenticated session and CSRF policy. Payloads
are Pydantic models; the TypeScript client is generated from OpenAPI.

| Operation | Route |
| --- | --- |
| Enroll / list | `POST` / `GET /api/responsibilities` |
| Pause / resume with expected generation | `POST /api/responsibilities/{id}/control` |
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
5. Wait another check cycle. Confirm no duplicate findings or extra reviews.
   Exercise Pause, Resume and Mark reviewed. Verify source access loss hides
   evidence and blocks future work.
6. Record the real UI and inspect the page and enrollment dialog at 1440, 768
   and 320 pixels. Fixture browser checks are supplementary.

Regression checks include `test_responsibilities.py`, the API contract,
admission/history/settlement coverage, the three PostgreSQL schema suites,
`npm run api:check`, `npm run typecheck`, `npm test`, `npm run build` and
`npm run test:ui`.
