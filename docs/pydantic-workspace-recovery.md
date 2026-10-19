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

## Maintenance pauses

Protocol 6 introduces `moyai-maintenance-workspace-continuation-v1`. New
executions reach a settled SDK `ModelRequestNode` before parking. They freeze
steering, save the existing agent continuation, and capture the live filesystem.
The envelope binds both saves to the session, turn, original workflow run,
boundary, pause generation, preparation digest and current authority. The drain
receipt stays blocked until capture and publication finish. A failed capture
does not certify a safe park.

The workflow then continues as new. Its direct successor takes ownership of the
parked receipt and waits without constructing an agent or using the computer.
The original computer may reach its maximum lifetime during this wait. On
resume, the activity checks the saved binding, permissions, maintenance state,
effects, inference requests and computer-control fences again. It prefers the
original live computer. Only confirmed provider expiry or disappearance permits
restoration from the exact saved image, using the existing idempotent creation
path.

Resume creates a new immutable continuation with the verified workspace in both
`AgentCheckpoint.workspace` and `PreparedTurn.workspace`. Application publication
and the resume receipt are committed together before the successor constructs a
fresh SDK run. Lost replies reuse that receipt. Original preparations and
Temporal activity inputs remain unchanged; no SDK private workspace field is
modified. Conversation, tool results, usage, retry budgets, pending steering and
stream state follow the existing history-handoff protocol. Human input cannot
invalidate a held maintenance savepoint.

This saves filesystem bytes, not guest-process memory or external-system state.
It does not replay process starts or external effects. A missing or expired image
refuses restoration; it never authorizes an empty computer.

### Initial adoption and older executions

The native compatibility protocol changes from 5 to 6. All native role
fingerprints change, while legacy fingerprints and schema manifests remain
unchanged. Use the reviewed contract-adoption path; do not weaken a compatibility
rejection or perform an ordinary mixed-contract rollout.

Existing workflow histories keep their previous command sequence. They do not
retroactively gain workspace preservation. In particular, the old `3d0e936`
runtime's parked receipts are not filesystem backups, including when its
existing history Continue-As-New path has run.

Initial installation requires a verified fleet-wide empty execution boundary:
no admitted old execution owning workspace state that must survive, no uncertain
effects or inference requests, and separate checks for never-prepared tasks and
deferred waits. Keep the API serving and queue new inputs through the reviewed
admission controls, adopt the protocol, verify the new owners, then resume.
Live old executions require their own qualified adoption mechanism; this change
does not supply one. Computers already expired without a current verified
snapshot cannot be reconstructed by this upgrade.

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
