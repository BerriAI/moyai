# Native Deep Agents SDK

New sessions default to `deepagents-native` (**Deep Agents SDK**). This path calls
`deepagents.create_deep_agent` directly. It does not use `LiteLLMAgent`,
`litellm.aagent_session`, the old `workspace_call` bridge, or `MoyaiControl`.

The graph is in `app/native_agent.py`. It can run outside Moyai's durable host
with injected application services. The SDK owns its model/tool loop, notebook
file operations, summarization, and `task` subagents. LangChain's
`TodoListMiddleware` supplies `write_todos`. Moyai contributes current-input
control, public progress, and typed application capabilities.

## Thin runtime, rich skills and context

The graph contains execution mechanics. Task procedures remain in versioned
`agent/skills/*/SKILL.md` files and requester-authorized saved skills, loaded on
demand through typed capabilities. Coding, research and delivery behavior can
change without editing the graph or introducing another SDK adapter.

Shared platform instructions live in `agent/prompts/`; the native-specific
orientation is `agent/prompts/deepagents.md`. The broker supplies current memory,
skill discovery, repository context and conversation references. Skill references
use canonical capability names, mapped to the advertised `moyai_` tools by the
runtime orientation. Skills cannot override permission checks or authorize writes.
Cancellation, identity fences, action receipts and checkpoint validation remain
enforced in code.

## Tools and state

- SDK file tools use `StateBackend`, a conversation notebook. They never read
  the API server's filesystem. Real computer operations use
  `moyai_computer_*` capabilities and the existing isolated computer service.
- Every authorized application tool has its own `moyai_` name and JSON schema.
  Arguments are validated before dispatch; the broker checks current grants and
  request ownership again. No model-supplied scope or capability is accepted.
- SDK subagents inherit the authorized tools and model service. They share
  request limits and the external-action lock. Their intermediate model text
  is not published as the parent answer. SDK subagents are in-graph work;
  Moyai's separately persisted agent groups remain separate capabilities.
- Messages, files and todos survive continuation segments. Completed-turn
  checkpoints also resume on a later message through the existing encrypted
  native-session store. Actor/model/gateway changes, private-context taint,
  inconsistent input history, and workspace recovery invalidate reuse; the
  next turn reconstructs authorized context from the public journal.
- Public activity shows planning, notebook work and delegation. Notebook
  contents and delegated intermediate text are excluded from these events.

## Durable hosting

`app/native_agent_workflow.py` hosts the SDK graph in a new
`NativeDeepAgentsTurnV1` workflow. It uses the public `TemporalModel` contract,
with model calls dispatched to explicit scoped activities.
`app/native_agent_activities.py` connects those activities to Moyai's broker,
gateway, journal and encrypted checkpoints. The shared application services in
`app/deepagents_runtime.py` remain responsible for permissions, model streaming,
computer receipts, goals, credential waits and public delivery.

Model, tool, and control capacity are separate. A blocked tool does not consume
the model or inbox slots needed by other chats. External tool activities have
one attempt: an ambiguous write is surfaced for inspection instead of replayed.
Temporal records completed activity results and reconstructs the native graph
after worker replacement. Stop revokes the current capability; Send now cancels
obsolete generation and fences undispatched application actions.

The SDK and its experimental Temporal hosting plugin remain pinned in the
lockfile. Changes to their behavior require replay tests.

## Setup and rollout

Use the existing Temporal and LiteLLM configuration, with:

```dotenv
AGENT_HARNESS=deepagents-native
TEMPORAL_ENABLED=true
```

Existing `deepagents-temporal`, `deepagents`, Codex, Claude, and other sessions
retain their persisted engine. No database rewrite migrates an active graph.
The API, coordinator, worker and broker protocol revisions advance to **19**:
old roles cannot execute the new workflow or authorize its persisted engine ID.
Use the coordinated maintenance deployment for this transition, after draining
old work. The PR does not deploy or change production settings.

## Local demonstration and checks

```sh
uv sync --frozen
uv run --frozen python -m scripts.native_agent_demo
```

Open `http://127.0.0.1:8799/demo-login`, choose **Cloud session**, and send:

> Verify native Deep Agents planning, notebook and delegation.

This runs the actual Moyai UI, Deep Agents graph, Temporal server, Responses
transport and authorized history tool. Only model replies are scripted.
It needs no provider or sandbox credentials and does not make external writes.
The temporary notebook and demo database are removed when the process exits.

```sh
uv run --frozen pytest -q tests/test_native_agent.py
```

The suite covers SDK planning/files/delegation, tool schema validation, full
HTTP intake, steering, Stop, concurrent chats, private checkpoint invalidation,
worker restart, encrypted history replay, and ambiguous-write handling.
The same tests run in the SQLite and PostgreSQL CI jobs.
