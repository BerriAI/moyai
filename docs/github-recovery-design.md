# GitHub publication recovery: design and evidence

## Scope and evidence

Two independent workers completed: Explore and Regression audit. Existing candidate is rejected as incomplete: its explicit retry path bypassed immediate-recovery validation. Local HTTP reproduction uses real Uvicorn broker, real HTTP client and SQLite with a loopback provider simulator. Unchanged base: a provider-accepted POST followed by 502 produced uncertainty, one POST, no receipt, GET/POST sequence. No production outage was induced. GitHub's original 502 cause remains unknown.

Root cause: `publish` awaited `POST /pulls` with no reconciliation around it, so `ConnectorError` escaped before receipt persistence; old preflight blindly accepted `pulls[0]`. Normal creation and reconciliation need one receipt invariant.

This repo uses SQLite, not Postgres; the rig uses its real storage. No CLAUDE.md/default_branch.py is present in discovered checkout; connected checkout resolved main at 3198e5a. Both git histories are shallow; authenticated history fetch and browser PR search unavailable, so prior-art/revert census is incomplete. Tool fanout pauses the lead and does not offer persistent SendMessage; both named workers returned artifacts instead. These are declared workflow substitutions, not claims of literal LiteLLM rig parity.

## Shape

Problem class: uncertain outcome of a non-idempotent remote write. Established repository precedent: github_followups.sent for comment writes. Candidate A: retry POST after error (rejected: duplicates). Candidate B: one immediate readback only (rejected: subsequent retry bypasses invariants). Chosen C: persisted write-attempt marker, deterministic branch lookup and one shared strict receipt validator. No sleep/poll loop, no new service, no provider write retry.

State machine:
- Unsent: preflight read; confirm a matching PR if found, otherwise mark attempted before sending one POST
- Attempted/unconfirmed: only bounded readback, never POST again for the key
- Confirmed: persisted receipt returned on identical request; authorization and connection checks still apply
- Missing/unavailable/malformed/mismatched: actionable uncertainty, no receipt
- Revoked: stop reads/writes and receipt persistence
- Closed/draft valid matching PR: report actual state as historical publication, never claim currently ready for review
- Crash after marking before sending: conservatively uncertain, manual inspection needed
- Legacy incomplete records: conservatively mark attempted during migration if commit exists

One process deployment already serializes publishers under write_lock; marker is not a distributed locking guarantee. No second parallel-worker architecture introduced.

## Mechanical walk

Reachability: broker `/tools/call` -> GitHub.call write_lock -> publish -> request; real rig exercises this path
Divergence: one receipt validator used by POST acknowledgement and every preflight/error recovery lookup; deletes old inline selection
Rebinding: expected target/base/branch/commit captured once, no mutation during readback
Call-site census: create PR uses publish; update/comment own separate followup state machines, unchanged and regression-tested; sandbox synchronizes valid receipt and delivery consumes stored result
Error shapes: HTTP status, transport and JSON decoding normalize to ConnectorError; structural invalid data explicitly normalizes to ConnectorError; recovery error preserves destination and reason; cancellation is not swallowed
Carry-forward: commit persisted before refs; attempted persisted before PR POST; receipt saved only after shared validation and post-await permission recheck; same key/args reuses row across turns
Hermeticity: loopback-only sockets, dummy credentials, isolated database, existing HTTP fake extended to realistic PR metadata; no live keys, no provider writes; negative cases assert one POST across repeated invocations

## Conditional walk

Retry/external call: four terminal states above, no automatic mutation replay, empty read is not proof of failed creation
Security/trust boundary: existing allowlist/capability/connection-version gate retained before and after remote reads; validator checks head and base repositories, branch and SHA plus canonical URL and receipt field types
New field/lifecycle: add attempted column in owning github_publications initializer; migration preserves receipts and treats old incomplete committed publications conservatively; no secrets, FK/cascade/encryption change
Consolidation: old preflight accepts arbitrary single PR; consciously removed in favor of verified identity. Closed/draft recognized states preserved truthfully, no conversion to ready
Modes/error shapes: POST acknowledgement, initial lookup, immediate recovery, explicit retry and saved receipt enumerated; malformed success JSON drives readback
Persistence/concurrency: SQLite durable marker survives restart; existing process-local write lock retained, multi-pod not claimed
Observability: return original failure plus readback failure and deterministic destination; no raw provider body, credentials or prompts in diagnostics
Tests: positive controls plus mutation against baseline, broker-level 502/timeout/invalid shapes, repeated unresolved retry, revocation and regression suites

No load-bearing query redesign: exact publication primary-key lookup and bounded head/base GitHub lookup unchanged. No performance claim needs a synthetic EXPLAIN benchmark.
