# Native workspace checkpoint and restore

Pydantic AI and Temporal retain the conversation, deferred requests, results and
execution history. Moyai separately saves the Modal computer's filesystem at a
settled run boundary. A conversation checkpoint is not a computer snapshot.

## Active turns

New workflows use `moyai-active-computer-keepalive-v1`. Temporal schedules a short
maintenance activity at most every sixty seconds, or one third of the configured
Modal idle timeout. The activity rechecks the current requester, turn, workspace
scope and grants, then runs a fixed `/bin/true` command on the existing computer.
It does not acquire a computer for a text-only turn or create a replacement.
The timer continues during model calls, delegated work, and answer/checkpoint
settlement. It stops on completion, Stop, lost authority, or confirmed computer
termination. History handoff cancels the old timer before continuing as new.
The session-row fence admits both `running` and `saving` atomically and refreshes
the turn after acquiring that fence. A normal transition into saving while a tick
waits for a lock therefore keeps maintenance active. The turn and session must
still agree on their current phase; preparation and checkpoint publication retain
their stricter single-phase fences.

This protects active work from **idle** expiry, not from the configured maximum
computer lifetime, provider loss, or an outage lasting longer than the idle
window. The maximum lifetime is unchanged. Completed and approval-wait checkpoints
retain the verification rules below; maintenance does not make a mid-action
snapshot safe to restore or authorize replaying any action.

## Approval waits

New workflows use the `moyai-workspace-checkpoint-v1` patch. They commit a received
answer or deferred request first, then save the computer. A deferred request stays
in `saving` until that attempt settles, so a fast approval cannot start another
turn while its source computer is being captured. Successful settlement exposes
`waiting` with the original active turn and deferred call IDs.

Snapshot failure, timeout and worker-loss cleanup retain the accepted request and
show a warning. They do not substitute an older snapshot. Settlement and Stop
serialize on the session row; Stop or deletion cannot be undone by a late
checkpoint or fallback callback.

## Restore before execution

Preparation prefers the original live Modal computer. Only confirmed termination
or disappearance permits restoration; authentication and transport failures do
not authorize replacement.

The versioned envelope in the existing `Session.snapshot` field binds an image to
its session, source workspace, scope, accepted source turn/result, saved
conversation hash, and applicable product project/restoration records. Restoration
requires current actor and grant checks, an unchanged preparation and savepoint,
and no uncertain effect or conflicting desktop operation. A bare historical image
ID is insufficient.

Modal creates the restored computer from that exact image using a stable
session/turn/image-derived name. Retries can find an already-created computer
after a lost response. Publication checks current authority again and atomically
saves the new workspace reference and preparation. For an adopted legacy session,
it also inherits the verified restoration receipt, preventing a second legacy
file import from overwriting later edits.

The native agent receives the new explicit `WorkspaceRef` alongside the unchanged
conversation and deferred results. Recovery does not re-execute earlier tools or
change effect outcomes. Temporal remains the execution owner.

## Freshness and limits

- A savepoint is consumed before the SDK can perform new work. It cannot be used
  to roll an already-progressed task back after another computer loss.
- Human control invalidates the preceding savepoint before input. Read-only
  viewing does not invalidate it. Capture refuses an active controller or an
  unsettled operation.
- Filesystem images use the pinned Modal SDK's existing 30-day retention policy.
  An expired/missing image produces an explicit refusal, never an empty computer.
- Restoration preserves saved filesystem bytes. It does not restore process
  memory or guarantee that background processes or browser windows resume.
  Changes made after a checkpoint by an untracked background process are outside
  that saved point; confirmed process starts are not replayed.
- A computer that already expired without a verified savepoint cannot be
  reconstructed by this change. Saved conversations, credential resolutions,
  effect receipts and previously exported artifacts remain available.

No schema version is added: this uses the existing v13 columns and restoration
table. New serialized fields have backward-compatible defaults, and old workflow
histories retain the unpatched activity sequence.

## Verification

`native/tests/test_workspace_recovery.py` covers deferred and completed cold
resume, immutable approvals/results/effects, missing and stale checkpoints,
revocation and Stop during restore/capture, inherited legacy provenance, and
read-only desktop binding. Its PostgreSQL cases overlap real Stop and settlement
transactions and verify that Stop cannot be undone.

The actual-app qualification uses separate local baseline and candidate
deployments, a real configured model, and real Modal idle expiry. It writes files
before a secure credential wait and resumes through the ordinary credential
endpoint. The baseline fails on the dead workspace. The candidate restores files,
completes the authorized setup, retains prior receipts, and exports byte-verified
content without repeating the initial write. Synthetic identities and credentials
are local test inputs; this does not claim live Google SSO or an external webhook
registration.
