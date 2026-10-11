# Default durable agent engine

New sessions use **Deep Agents + Temporal** for every configured model. The Python
settings default and Render blueprint both select `deepagents-temporal`; there is no pilot
or opt-in flag. Claude and OpenAI models still run through the existing LiteLLM
broker. Their native agent SDK subprocesses are not used on this path.

The harness stays thin. It exposes discovery and one generic workspace invocation;
repository workflows, coding guidance, app procedures and delivery instructions
remain in `agent/skills/*/SKILL.md` and the permission-scoped personal skill library.
Only the catalog is in the initial prompt. Relevant instructions load on demand.

| Owner | Responsibility |
| --- | --- |
| Deep Agents | Library-owned model/tool graph, typed messages and tool validation |
| Temporal | Persisted model/tool activity outcomes, bounded retries, workflow replay and worker recovery |
| Moyai broker | Background input compaction, model pinning, budgets, spend, permissions, skill/memory scope, approvals and tool authorization |
| Moyai session controller | Input ownership, Stop, child/credential waits, goals, answer settlement and computer lifecycle |
| Moyai computer receipts | Stable operation identities, launch reconciliation and unknown-write outcomes |

`DeepAgentsTurnV2` runs on trusted workers. Temporal credentials and provider keys
never enter the sandbox. Model calls use LangChain's OpenAI Responses
adapter against the sealed, run-scoped Moyai broker. LiteLLM handles provider
translation. A selected model must support Responses and tool calls on that gateway;
local scripted-provider tests do not establish live provider compatibility.

The existing session controller starts each agent segment with a stable workflow
ID. Four independent activity pools run in the worker process: session controllers
on the normal task queue, graph/model work on `<TEMPORAL_TASK_QUEUE>-agents-v1`,
tools/computer acquisition on that queue's `-tools` suffix, and preparation/control/
publication on its `-control` suffix. The queue suffix is a routing identifier;
the workflow type itself is V2. Readiness requires every pool to be running.
Computer waits and slow tools cannot consume controller, model, or control slots.
Controller acquisition of a busy session lease yields promptly instead of occupying
a slot while waiting. A computer is acquired only when needed; an explicit manual
wake also runs on the tool pool. Every 50 model requests, the agent yields
at a completed tool boundary to checkpoint and bound workflow history.
The broker is the sole compaction owner, using the same background flow as the
native harnesses ([details](harnesses.md)). At 75% of the verified
input budget it summarizes an older, closed prefix while fitting model requests
and tools continue. Later requests adopt a valid summary plus every item appended
since the snapshot. Instructions, first/latest user requests, unresolved tool groups
and opaque media stay in order. Only a request that no longer fits waits for
compaction, before acquiring a model slot; foreground work can preempt maintenance.
Deep Agents' built-in summarization middleware is excluded through its public
`HarnessProfile` API. Filesystem result eviction is disabled as well, so the
broker is the only component that rewrites model input.

Compaction projects the provider input without rewriting Deep Agents' typed history.
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

Send Now first persists the input and returns an HTTP receipt. A workflow signal
wakes an independent control monitor, which journals the attributed input in
Temporal and acknowledges that the running agent accepted it. Acceptance is not
a claim that the model has already generated a response to it. If a model request
is active, middleware cancels it and reissues with the correction in the same
graph/turn. The selected model, original task and completed tool results remain.
This uses HTTP Responses cancellation and reissue; it does not implement native
OpenAI WebSocket `response.steer` or preserve interrupted private reasoning.

Public assistant text streams incrementally through sanitized snapshots. The UI
coalesces them into one growing message; only completed commentary enters the
public journal and downstream delivery. Interrupted partial snapshots remain in
the event history, but are not promoted to a completed graph message. Late chunks
from an obsolete input or stopped capability cannot publish. Finalization checks
the inbox again so a correction during answer publication continues the same turn.

External actions in a parallel tool batch execute sequentially. The control monitor
can accept a correction while a dispatched tool is running; its result is retained
before the graph continues. Accepted input cancels queued computer operations, and
trusted input identities fence later broker dispatch. Cancelled calls receive paired
tool results; later unstarted calls are suppressed. Requester/model changes yield
to the existing capability handoff. Stop revokes the capability and requests workflow
cancellation immediately, before controller cleanup; five-second activity heartbeats
carry cancellation to workers. Already-dispatched external actions may still finish;
saved broker/computer receipts remain authoritative and uncertain writes require
reconciliation.

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

## Library configuration

The implementation uses the official `temporalio-deepagents==0.0.1` integration:
`create_temporal_deep_agent`, `DeepAgentsPlugin`, and `tool_as_activity`. Model
calls and I/O tools are individual activities; the graph executes in the workflow.
It does not wrap an entire agent run in one retryable activity.

The integration is **experimental**. Deep Agents is pinned to **0.7.15**, the
compatible release for the integration's LangSmith constraint; the lockfile pins
the full set. Upgrades require replay and recovery qualification. The current
upstream stack emits deserialization deprecation warnings and can emit coroutine
cleanup warnings when a worker evicts a running graph. Restart, cancellation and
replay are covered locally; production memory/lifecycle qualification remains a
release requirement.

Only the authorized `workspace_tools` and `workspace_call` bridge is advertised.
Moyai's existing catalog provides files, shell, planning, skills, memory and child
agents. Native Deep Agents filesystem/subagent tools are disabled to avoid a
second permission/computer lifecycle. Scope and operation IDs are injected by
trusted middleware, never accepted from model arguments. Task procedures remain
in skills; the workflow contains no repository-specific instructions.

The new harness ID is `deepagents-temporal`. The older `deepagents` sandbox
adapter is retained for saved sessions. PR development versions that used
`pydantic-ai` or `DeepAgentsTurnV1` are not a supported in-flight upgrade: drain
those test workflows before switching. No released native session is silently rewritten.

## Release requirements

This is a code change with runtime configuration requirements, not a deployment
performed by the PR. It requires a reachable Temporal service, stable
`ENCRYPTION_KEY`/`SESSION_SECRET`, the existing broker, and durable payload storage.
Distributed roles already require shared object storage. Local setup must set
`TEMPORAL_ENABLED=true`; there is no nondurable/native fallback for `deepagents-temporal`.

Schema revision is 17 (inherited from main). API, broker, worker and
coordinator protocol revisions advance to 16 because old roles cannot execute the
new harness or decode the new
payloads. Drain and coordinate the upgrade of all roles using the existing release
procedure. Preserve current credentials and storage. Verify the default with both
Claude and OpenAI on the deployment's gateway before reopening traffic. The Render
blueprint sets the new default; installations with explicit legacy `AGENT_HARNESS`
settings must replace them with `deepagents-temporal` as part of the same release.

Persisted native harness IDs and their adapters remain for existing sessions and
explicit legacy configurations. They are not silently migrated mid-turn. Existing
ID-only Temporal histories remain readable. Once this release writes encoded
history, rolling back to a release without the codec is unsupported; retain codec
support in any rollback build. No production settings, secrets, or deployments
are changed by running the local proof.

## Local verification

```sh
uv sync --frozen --python 3.13
uv run --frozen --python 3.13 pytest -q tests/test_deepagents_runtime.py tests/test_deepagents_acceptance.py tests/test_deepagents_workers.py
uv run --frozen --python 3.13 python -m scripts.deepagents_temporal_probe --output /tmp/moyai-durable-proof
```

The tests use a real local Temporal server and Deep Agents graph. They cover worker
restart without repeating a completed write, uncertain writes, replay, background compaction,
steering, Stop, scope fencing, private payload storage, the real skill broker and
answer settlement. Session compatibility checks also cover queued followups and
requester/model changes, credential waits, parent/child completion, tool discovery,
lazy computer acquisition with actual local file writes and saved operation receipts,
and screenshot content preserved through the graph and conversation checkpoint.
The provider-transport test exercises the installed official SDK
against a local sealed Responses endpoint. The HTTP acceptance suite runs the
full app, real Temporal and installed graph with controlled provider/tool transports.
It covers generation, running-tool and computer-startup steering; finalization;
Stop; stale dispatch; incremental text; and another chat progressing with a single
activity slot per pool. Cloud computers and external connector behavior still
require deployment qualification. PostgreSQL scheduling CI runs all three Deep
Agents suites. The scaling/performance suites provide their own database schemas
and must run without the `--postgres-backend` fixture override.

The Lens subprocess benchmark remains a **native SDK compatibility lane**. CI
explicitly selects `MOYAI_EVAL_HARNESS=codex` for both base and candidate; its local
broker uses that same harness. It cannot execute `deepagents-temporal` and must not
be presented as a live model-quality evaluation of this migration. Without an
explicit supported native selection, the driver rejects the durable default rather
than silently substituting another engine. The Temporal tests above run separately
on SQLite and PostgreSQL. A live provider and cloud-computer evaluation of the
durable engine remains a release requirement beyond the limited live probe below.

The focused compaction proof uses the installed Responses SDK, sealed transport,
real broker, Deep Agents graph and a local Temporal server. It holds a scripted
summary open while two model/tool steps complete, verifies only an oversized third
request waits, then restarts the worker and checks exact tail adoption and replay:

```sh
uv run --frozen --python 3.13 pytest -q -s tests/test_deepagents_runtime.py::test_background_compaction_keeps_working_preserves_tail_and_replays
```


## Live steering qualification

The opt-in probe uses native Codex app-server `turn/steer` as the comparison,
not Moyai's Codex adapter. Both use `openai/gpt-6-astra`, medium reasoning and
the same synthetic prompt/correction through the development gateway. It sends
the correction after the first visible text and records receipt, redirected text
and completion separately. It incurs model usage and requires the Codex CLI for
the native lane. It reads only `LITELLM_DEV_API_KEY`; it never substitutes or
overwrites the production `LITELLM_API_KEY`.

```sh
uv run --frozen --python 3.13 python -m scripts.deepagents_steering_probe \
  --output /tmp/moyai-live-steering \
  --gateway https://gateway-dev.litellm-sandbox.ai/v1 \
  --model openai/gpt-6-astra
```

An optional `--env-file /path/to/local.env` reads the development key when the
launching process has not loaded it. Use a fresh output directory for every run.
The 2026-10-11 local qualification completed three trials per engine plus one
per engine verifying the reusable script; all eight kept the same turn and
adopted the correction. Across all four, Deep Agents acceptance was 168–606 ms
(median 183 ms; HTTP receipt 29–311 ms), with 3.8 s median to redirected text and
8.2 s median to completion. Native Codex acknowledged in about 1 ms, with
50.5 s median to redirected text and 54.1 s to completion on the HTTP path.

These are four tool-free trials per engine, different harness system prompts,
different local intake transports and a fixed run order. They establish this
observed capability, not universal speed or model-quality parity. A separate
gateway WebSocket probe connected and started a response but returned
`response.steer.failed` / `response_not_found` for steering. Its cause was not
established. Direct OpenAI WebSocket steering parity is therefore unqualified.
Live Claude, real cloud computers/connectors, sustained multi-replica load and
the experimental integration's production memory/lifecycle behavior remain
release requirements. No production deployment was performed by these probes.
