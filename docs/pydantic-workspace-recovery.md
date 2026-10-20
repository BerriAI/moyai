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
receipt stays blocked until capture and publication finish. Recoverable
preconditions such as a human lease or unsettled receipt wait at that boundary
with a durable timer and a visible explanation. A known intent change is
rechecked, and an unpause can return the still-live graph to execution.
Confirmed expiry before capture, invalid checkpoint bindings and changed
authority fail with a recovery-needed explanation instead of waiting forever.

Capture intent is persisted before calling the provider, and a returned image is
recorded separately before checkpoint publication. The public Modal snapshot API
does not accept a caller-owned idempotency key. A lost reply without an image
receipt therefore stays blocked for inspection; it cannot silently dispatch
another snapshot; the session exposes this concrete blocker. A known image receipt can finish publication after a lost
database or object-store response without capturing again. The new activities
have no blanket automatic retry policy. Restore retries are limited to transient
observations of the same frozen image and stable provider creation identity.

The workflow then continues as new. Its direct successor takes ownership of the
parked receipt and waits without constructing an agent or using the computer.
The original computer may reach its maximum lifetime during this wait. On
resume, the activity checks the saved binding, permissions, maintenance state,
effects, inference requests and computer-control fences again. It prefers the
original live computer. Only confirmed provider expiry or disappearance permits
restoration from the exact saved image, using the existing idempotent creation
path. Restore has a separate bounded deadline that accommodates Lambda's running,
bootstrap and initialization phases; provider maximum lifetimes are unchanged.

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

Stopping the parked task retains its maintenance image and immutable graph
reference. The ordinary new-turn recovery path deliberately does not adopt that
stopped graph, including when the original computer has expired. It reports that
the backup exists and a reviewed recovery is required, preserving both objects
and the session's maintenance envelope. Automatic recovery into a different
authorized turn is outside this change; it must not replay the stopped actions
or silently treat the image as an ordinary completed-answer checkpoint.

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
deferred waits. Queue new inputs through the reviewed admission controls before
retiring incompatible owners, adopting the protocol, verifying the new owners,
and resuming. The existing canonical policy gate requires incompatible owners,
including APIs, to retire before changing the shared contract. This PR does not
provide an uninterrupted HTTP bridge for that initial transition, and it must
not be combined with an independently compatible API-only deployment.

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
