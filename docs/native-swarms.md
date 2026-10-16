# Swarms on Pydantic AI

Choose **Swarm** on the new-session screen, describe one shared goal, select
2–20 workers, and choose a work window. The default is 20 workers, plus the
coordinator, working until explicitly paused or stopped. Selected worker models
rotate across the saved roster; an empty selection inherits the coordinator's
model. Every execution uses Pydantic AI/Harness through the configured LiteLLM
Responses endpoint.

Each member has a persistent native conversation and its own Modal computer.
The team shares a public board, not a filesystem. Open a member from the roster
to inspect its full answer, activity, files, questions, and computer, or to send
it a direct instruction. Member conversations retain the mission's visibility.
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

`SwarmCapability` contributes a registered Pydantic `FunctionToolset`:

| Tool | Behavior |
| --- | --- |
| `swarm_board(after=0)` | Reads the caller's trusted identity, goal, roster, leadership, assignments and paginated public posts. |
| `swarm_claim_lead(plan)` | Atomically claims worker leadership; concurrent callers receive the actual winner. |
| `swarm_assign(member_id, task)` | Queues bounded work in an existing worker conversation. |
| `swarm_publish(message)` | Persists a public finding and makes it eligible for a coalesced notification. |

Tools are scoped by the server-authored `RunScope`, never by a model-selected
mission or owner. Mutation receipts use native run/tool-call identities and
commit with their effects. Nested native delegation is unavailable inside a
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
`worker_models`, `visibility`, and `budget_seconds` (0 means until stopped;
finite windows are 60–2,592,000 seconds). It returns the coordinator session and
initial turn. `GET /api/sessions/{id}/swarm` reads the mission; `after` selects a
chronological board cursor. `POST /api/swarms/{id}/control` accepts an action
(`pause`, `resume`, `stop`, `time`) and the current control revision. Only `time`
accepts `budget_seconds`. All endpoints use ordinary sign-in, CSRF, and origin
protection.

Existing-product deployments require the additive
`2026-10-16-native-swarms-v15` schema migration and native protocol revision 3
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
