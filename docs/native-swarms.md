# Swarms on Pydantic AI

In an ordinary web chat, ask: **“Start a swarm of 20 workers with a two-hour work
window to investigate X.”** The agent can call
`start_swarm(goal, workers=20, budget_seconds=7200)` and return a link to the
persistent team. Launch returns immediately; the team continues in its own
conversations. It inherits the chat's model, visibility and selected accounts.
The original authenticated request and directions travel with the assignment;
the agent's proposed goal is reference data, not new human authorization.
Current-turn attachments receive independent member-scoped references.

The tool is available only in direct web chats. Swarm members, delegated agents,
Slack and automated executions cannot launch another swarm. A retried native
tool call recovers the same team. Stop or new guidance before admission fences
creation; once created, the team has its own Pause/Stop controls at the returned
link. The chat's Stop button does not stop an already-created independent team.

Choose **Swarm** on the new-session screen, describe one shared goal, type any
whole number of at least 2 workers, and choose a work window. There is no fixed
maximum team size. The default is 20 workers, plus the
coordinator, with no automatic time expiry. The coordinator finishes a satisfied
goal after the workers settle; explicitly ongoing goals can continue until
paused or stopped. Selected worker models
rotate across the saved roster; an empty selection inherits the coordinator's
model. Every execution uses Pydantic AI/Harness through the configured LiteLLM
Responses endpoint.

Team size is separate from execution concurrency: the existing Temporal worker
activity and dispatch settings still govern execution capacity. Removing the roster
maximum does not change those settings or provider quotas.

Each member has a persistent native conversation and its own Modal computer.
The mission opens in **Space**, a live constellation of the actual saved team,
adapted from the original swarm space view in #362. Every worker remains reachable,
including larger teams and narrow screens. Nodes show the selected model, current
status, worker lead, and most recent public finding or saved reply among the latest
50 board posts. The shared feed shows the six latest public team updates.
Updates refresh with the existing three-second mission poll; they are not private
reasoning or a token stream. Animated connections indicate active work, not messages.
Reduced motion keeps a static graph that still refreshes when team state changes.

Select a member or reply to open its conversation. **Chat** restores the roster,
full recent board, and mission conversation without discarding the composer draft.
Questions, credential requests, and unsettled effects open the conversation for
review instead of being hidden behind Space. Team controls remain available in
both views, subject to the same owner permissions.

The team shares a public board, not a filesystem. Open a member from the roster
to inspect its full answer, activity, files, questions, and computer, or to send
it a direct instruction. Member conversations retain the mission's visibility.
Saved artifact links can also be previewed from another conversation in the same
mission. Input uploads and unrelated conversations keep their own boundaries.
Only the original owner can direct the team or change its controls.

Workers volunteer to lead, and the first eligible claim wins atomically.
The lead and coordinator can assign focused work to existing teammates.
Findings and saved final answers appear in **Team chat**. The coordinator
synthesizes the team's results in the mission conversation. The interface shows
the latest 50 public updates; complete member conversations remain available.
It does not expose private reasoning or credentials.

## Lifecycle and authority

- Mission creation, the pinned roster, initial inputs, attachment references,
  and dispatch intent commit in one transaction. Retrying the same start request
  recovers the same team.
- The lead receives coalesced notifications for new findings and human
  directions. Once a round settles, a durable 30-second timer can wake the lead
  for useful remaining work on the same goal. This continues within the chosen
  window; the model cannot extend it or create extra roster members.
- **Pause team** and **Stop team** fence new execution and request cancellation
  through the ordinary native Stop dispatcher. Already-admitted external actions
  can finish; controls cannot undo them. Saved results and computers follow the
  existing native checkpoint/recovery contracts.
- **Resume team** waits for cancellation cleanup, then continues the same
  conversations in a new generation. Pending human questions/approvals remain
  pending until answered; they are not automatically approved or discarded.
  Failed work and uncertain effects require human recovery.
- The coordinator can finish a satisfied goal only after workers, queued input
  and uncertain actions settle. Completion becomes durable after its final answer
  is saved. A later human input cancels pending completion; a follow-up to an
  already-completed mission reopens the same team without replaying its old jobs.
- A finite window includes startup, queueing, and pauses. Expiry pauses the
  mission. Resuming an expired finite window renews its duration; an unexpired
  window retains its deadline. Changing the window updates its deadline from
  the current time and does not itself resume a paused team.
- Each model/tool activity rechecks mission status, deadline, generation,
  requester, and account grants. Generated wakes inherit the original authority
  and cannot silently adopt changed permissions. A changed requester/account
  context requires a new mission.
- Generated wakes are recorded as system input, separately from human messages.
  Peer instructions and board entries do not grant human authorization.
  Existing external-effect receipts and approval checks still apply.

Connected accounts and models are pinned for the mission. Start another mission
to change them. Files attached at creation receive independent member-scoped
references to the same encrypted objects. Coding work must be coordinated
explicitly because each member has an independent filesystem.

## Native implementation

`SwarmLaunchCapability` exposes `start_swarm(goal, workers=20, budget_seconds=0)`
to ordinary direct web chats. Zero means no time limit; finite windows must be
60–2,592,000 seconds. It uses `SwarmService.start` inside the source chat's
authority/session fence and commits its tool receipt in the same transaction as
the team. It does not add a second execution engine or change concurrency limits.

`SwarmCapability` contributes a registered Pydantic `FunctionToolset`:

| Tool | Behavior |
| --- | --- |
| `swarm_board(after=None, member_offset=0)` | Reads the caller's trusted identity, goal, roster, leadership, assignments and recent public posts. Page posts with `next_after` and `has_more` (use `after=0` for older history). Page the roster separately with `next_member_offset` and `members_has_more`; `member_count` includes the full team. |
| `swarm_read(sequence=0, offset=0)` | Reads complete shared text in bounded pages: sequence 0 is the original goal; other sequences identify board posts. |
| `swarm_claim_lead(plan)` | Atomically claims worker leadership; concurrent callers receive the actual winner. |
| `swarm_assign(member_id, task)` | Queues bounded work in an existing worker conversation. |
| `swarm_publish(message)` | Persists a public finding and makes it eligible for a coalesced notification. |
| `swarm_finish(summary)` | Lets the coordinator finish a satisfied goal after workers settle and its final answer is saved. |

Tools are scoped by the server-authored `RunScope`, never by a model-selected
mission or owner. Mutation receipts use native run/tool-call identities and
commit with their effects. Board previews page roster details as well as public
posts within the Harness output limit; full shared text remains readable in pages.
Nested native delegation is unavailable inside a
swarm so it cannot create untracked workers sharing a member's computer.
Ordinary Chat retains its normal native delegation capabilities.

Every member runs the existing `MoyaiPydanticTurnV1` workflow and Pydantic Agent.
`MoyaiPydanticSwarmV1` only coordinates persisted application intent and timers;
it contains no model/tool loop. It periodically continues as new to bound
Temporal history. Database transactions serialize leadership, assignments,
controls and result handoffs across processes. Temporal workflow IDs reconcile
lost dispatch acknowledgments.

The implementation does not import the retired swarm execution engine. Legacy
mission checkpoints are not converted, and legacy mixed CLI harness selection
is not provided by this native feature.

## API and deployment

`POST /api/swarms` accepts the ordinary message fields plus `workers`,
`worker_models`, `visibility`, and `budget_seconds` (0 means no time limit;
finite windows are 60–2,592,000 seconds). It returns the coordinator session and
initial turn. `GET /api/sessions/{id}/swarm` reads the mission; `after` selects a
chronological board cursor. `POST /api/swarms/{id}/control` accepts an action
(`pause`, `resume`, `stop`, `time`) and the current control revision. Only `time`
accepts `budget_seconds`. All endpoints use ordinary sign-in, CSRF, and origin
protection.

Existing-product deployments require the additive
`2026-10-16-native-swarms-v16` schema migration and native protocol revision 3
through the guarded maintenance release. Earlier migration receipts remain
unchanged. API, coordinator, worker, and broker owners must agree on the new
contract before resuming execution. Do not run an older worker against the new
swarm workflows.

## Verification

```sh
uv run --frozen pytest -c native/pyproject.toml \
  native/tests/test_native_swarms.py native/tests/test_swarm_temporal.py
MOYAI_TEST_POSTGRES=postgresql://local-user@127.0.0.1:5432/postgres \
  uv run --frozen pytest -c native/pyproject.toml native/tests/test_swarm_postgres.py
npm run typecheck
npm run build
```

The Temporal tests use the real SDK and a controlled model, including worker
replacement and history replay. PostgreSQL checks use disposable local databases.
These checks supplement the PR's actual-application before/after proof with real
configured inference and Modal; they do not replace it.
