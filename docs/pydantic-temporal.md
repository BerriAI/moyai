# Default durable agent engine

New sessions use **PydanticAI + Temporal** for every configured model. The Python
settings default and Render blueprint both select `pydantic-ai`; there is no pilot
or opt-in flag. Claude and OpenAI models still run through the existing LiteLLM
broker. Their native agent SDK subprocesses are not used on this path.

The harness stays thin. It exposes discovery and one generic workspace invocation;
repository workflows, coding guidance, app procedures and delivery instructions
remain in `agent/skills/*/SKILL.md` and the permission-scoped personal skill library.
Only the catalog is in the initial prompt. Relevant instructions load on demand.

| Owner | Responsibility |
| --- | --- |
| PydanticAI | Typed model/tool protocol, tool validation, cancellation snapshots and conversation execution |
| Temporal | Persisted model/tool activity outcomes, bounded retries, workflow replay and worker recovery |
| Moyai broker | Background input compaction, model pinning, budgets, spend, permissions, skill/memory scope, approvals and tool authorization |
| Moyai session controller | Input ownership, Stop, child/credential waits, goals, answer settlement and computer lifecycle |
| Moyai computer receipts | Stable operation identities, launch reconciliation and unknown-write outcomes |

`PydanticTurnV1` runs on trusted workers. Temporal credentials and provider keys
never enter the sandbox. Model calls use PydanticAI's official OpenAI Responses
adapter against the sealed, run-scoped Moyai broker. LiteLLM handles provider
translation. A selected model must support Responses and tool calls on that gateway;
local scripted-provider tests do not establish live provider compatibility.

The existing session controller starts each agent segment with a stable workflow
ID. Its activity pool uses the normal task queue; agent workflows and activities
use `<TEMPORAL_TASK_QUEUE>-agents-v1`. These are two pollers in the same worker
process, so computer-waiting tools cannot consume every controller activity slot.
A computer is acquired only when needed. Every 50 model requests, the agent yields
at a completed tool boundary to checkpoint and bound workflow history.
The broker is the sole compaction owner, using the same background flow as the
native harnesses ([details](harnesses.md)). At 75% of the verified
input budget it summarizes an older, closed prefix while fitting model requests
and tools continue. Later requests adopt a valid summary plus every item appended
since the snapshot. Instructions, first/latest user requests, unresolved tool groups
and opaque media stay in order. Only a request that no longer fits waits for
compaction, before acquiring a model slot; foreground work can preempt maintenance.
The blocking PydanticAI Harness compactor is deliberately not installed.

Compaction projects the provider input without rewriting PydanticAI's typed history.
Summary failure preserves the original history and the last valid projection.
The private summary cache is bounded and local to the gateway process, not a Temporal
activity/checkpoint: a gateway restart may regenerate a summary from the preserved
history. Temporal reuses completed model/tool activities, so a worker restart does
not repeat their completed writes. Native compaction's capability, turn, requester,
model and prefix checks still fence adoption. The full typed transcript continues
to grow in encrypted storage; this change does not make it an unlimited context.
The broker still rejects an input that cannot safely be reduced to fit.

## Recovery guarantees and limits

Completed Temporal activities are reused on replay. Model requests have three
attempts; a lost provider acknowledgment can still cause another inference charge.
Arbitrary tool writes have one activity attempt. A failure after dispatch stops
with an explicit reconciliation message rather than automatically repeating an
external action. Existing broker outboxes and computer receipts remain authoritative.
Temporal is not an exactly-once guarantee for external services.

User steering is checked at model/tool boundaries. PydanticAI cancellation and
message repair close unexecuted calls before applying a correction. Requester/model
changes yield to the existing capability handoff. Stop revokes the capability and
cancels the workflow; five-second activity heartbeats carry cancellation to workers.
An already-dispatched external action may still finish and must be reconciled.

The full public journal is independent of working-context compaction and remains
readable with `history_read`. Raw provider reasoning and private tool payloads are
excluded. Typed history is reused only within the original turn/scope. After a
private-context segment, continuation rebuilds from the public journal and reloads
currently authorized skill/memory context, matching the existing checkpoint policy.
New user turns never inherit another requester's private typed transcript.

All Temporal payloads and encoded failure attributes are encrypted and offloaded.
Temporal stores small references, avoiding its per-payload size limit. Shared
object storage holds ciphertext in distributed deployments; standalone installations
use `DATA_DIR/temporal-payloads`. This directory is outside workspace artifacts.
Keep payload objects and the original encryption key for the entire retained or
archived Temporal history. Missing objects or keys fail closed. Do not configure
object expiration shorter than history retention. Include local payloads in backups.
Ephemeral hosts using `CHECKPOINT_DIR` require shared object storage and explicit
encryption/session keys. Periodic SQLite/volume snapshots cannot durably acknowledge
each Temporal payload, so this configuration fails at startup without those settings.

## Release requirements

This is a code change with runtime configuration requirements, not a deployment
performed by the PR. It requires a reachable Temporal service, stable
`ENCRYPTION_KEY`/`SESSION_SECRET`, the existing broker, and durable payload storage.
Distributed roles already require shared object storage. Local setup must set
`TEMPORAL_ENABLED=true`; there is no nondurable/native fallback for `pydantic-ai`.

Schema revision remains 13 (inherited from main). API, broker, worker and
coordinator protocol revisions advance to 16 because old roles cannot execute the
new harness or decode the new
payloads. Drain and coordinate the upgrade of all roles using the existing release
procedure. Preserve current credentials and storage. Verify the default with both
Claude and OpenAI on the deployment's gateway before reopening traffic. The Render
blueprint sets the new default; installations with explicit legacy `AGENT_HARNESS`
settings must replace them with `pydantic-ai` as part of the same release.

Persisted native harness IDs and their adapters remain for existing sessions and
explicit legacy configurations. They are not silently migrated mid-turn. Existing
ID-only Temporal histories remain readable. Once this release writes encoded
history, rolling back to a release without the codec is unsupported; retain codec
support in any rollback build. No production settings, secrets, or deployments
are changed by running the local proof.

## Local verification

```sh
uv sync --frozen --python 3.13
uv run --frozen --python 3.13 pytest -q tests/test_pydantic_runtime.py
uv run --frozen --python 3.13 python -m scripts.pydantic_temporal_probe --output /tmp/moyai-durable-proof
```

The tests use a real local Temporal server and PydanticAI graph. They cover worker
restart without repeating a completed write, uncertain writes, replay, background compaction,
steering, Stop, scope fencing, private payload storage, the real skill broker and
answer settlement. The provider-transport test exercises the installed official SDK
against a local sealed Responses endpoint. Provider responses and cloud computers
are controlled test fixtures; live model quality and cloud-provider behavior require
deployment qualification.

The focused compaction proof uses the installed Responses SDK, sealed transport,
real broker, PydanticAI graph and a local Temporal server. It holds a scripted
summary open while two model/tool steps complete, verifies only an oversized third
request waits, then restarts the worker and checks exact tail adoption and replay:

```sh
uv run --frozen --python 3.13 pytest -q -s tests/test_pydantic_runtime.py::test_background_compaction_keeps_working_preserves_tail_and_replays
```
