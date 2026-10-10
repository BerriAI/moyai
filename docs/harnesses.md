# Agent harnesses

New web sessions, Slack threads and saved automations choose a default from the
resolved model ID's provider prefix: **Codex SDK** (`codex`) for `openai/` and
**Claude Agent SDK** (`claude-agent-sdk`) for `anthropic/`. This includes GPT-6 Astra,
GPT-6.1 Sol and future models added to the catalog or configured as `AGENT_MODEL`;
no version-specific routing entry is needed. Model aliases and display names are
resolved before choosing the SDK. Other provider prefixes and unqualified gateway
aliases retain the Claude SDK fallback. An explicitly configured `AGENT_HARNESS`
overrides these model defaults; remove an old `AGENT_HARNESS=claude-agent-sdk` setting
to enable automatic pairing. The new-session picker shows the effective automatic
choice and supports an explicit override. Explicitly
selected harnesses remain available: Hermes, Codex, OpenCode, Deep Agents,
Tool Loop and Pi. Regular model choices remain selectable with every harness.
**GPT-6 Astra Ultrafast** is an opt-in processing mode that requires Codex and
automatically selects it, including when `AGENT_HARNESS` sets another default.
An explicit incompatible harness selection is rejected. The configured
`AGENT_MODEL` is preserved, including GPT-6 Astra. The gateway must support the
selected model and tool calls through the runtime's native API (Responses for
Codex, Messages for Claude); selecting it does not establish provider compatibility.

## Codex SDK

`sandbox/codex_harness.py` uses the published Python `openai-codex==0.161.0`
package and its pinned `openai-codex-cli-bin==0.161.0` app-server runtime directly.
It replaces the LiteLLM harness wrapper for the existing `codex` selection.
The SDK requires Python 3.10 or later; Moyai's sandbox uses Python 3.12 or later.
Published runtime wheels support Linux x86_64 and ARM64. Older workspace snapshots
install the pinned SDK/runtime on first use.

Codex runs inside Moyai's isolated machine with an ephemeral thread for every
invocation. Cold starts use a fresh temporary native home; the warm process below
owns a temporary home for its bounded lifetime. Native authentication, transcripts and
summaries are never resumed across requesters. The shared public journal described
below supplies restored context and records completed actions. Moyai keeps ownership
of sessions, memory, skills, delegated workers, permissions, checkpoints and delivery.
Native delegation is disabled with `agents.enabled=false`; native hooks, apps,
memory, bundled skill instructions and automatic project instructions are disabled.
Project runtime configuration is untrusted. Only Moyai's configured MCP server is
added, and inherited provider credentials and runtime switches are removed.

The Moyai MCP server is required, with a 60-second startup timeout. The pinned
runtime waits for its actual tool catalog before sending the first model request;
failed or timed-out MCP discovery ends the turn without inference. Optional MCP
startup previously let Codex begin with native tools only and add connected tools
mid-turn. This could produce false missing-access answers and failed early calls.
Codex uses native `tool_search` to discover MCP schemas and then calls the returned
tool namespace directly. Claude uses native `ToolSearch`, and Hermes uses
`tool_search`/`tool_describe`/`tool_call`.
Shared instructions select the correct discovery path for the active harness.

Native search is always enabled in the pinned runtime for search-capable models;
the old `features.tool_search` flag is a retired no-op. However, Astra and Sol's
bundled `code_mode_only` setting hides the standalone search tool, and that special
tool cannot be invoked inside `functions.exec`. At startup Moyai reads the pinned
binary's full catalog with `debug models --bundled`, changes only the selected
search-capable model from `code_mode_only` to `code_mode`, and supplies it through
`model_catalog_json` in the disposable native home. This retains `functions.exec`
and all original model metadata and prompts while exposing native client search.
The catalog probe receives no provider credentials or broker capability and does
not refresh from the network. Other models retain their original tool mode.
Search covers only the MCP tools authorized for this session; discovery does not
grant additional connection access. Models without search use their actual tool
catalog, including `ALL_TOOLS` when code mode is available.

To reproduce startup and native search using real Codex and MCP with scripted local inference:

```sh
uv run pytest -q tests/test_codex_tool_readiness.py tests/test_codex_tool_search.py tests/test_workspace_diagnostics.py
uv run python scripts/tool_readiness_demo.py --output /tmp/moyai-tool-readiness
uv run python scripts/codex_tool_search_demo.py --output /tmp/moyai-tool-search
```

### Warm runtime and schema caching

With Temporal, `CODEX_RUNTIME_REUSE=true` (default) reuses the native Codex
app-server inside the chat's already-warm sandbox. Prewarming starts when the
sandbox receives the run, overlapping repository, attachment and workspace setup.
It does not allocate a sandbox when someone opens a chat. Non-Temporal runs,
delegated workers and `SANDBOX_IDLE_SECONDS=0` retain the cold path.

A local lease supervisor keeps the process for at most
`min(SANDBOX_IDLE_SECONDS, 300)` idle seconds. Reuse requires the same chat, actor,
model and runtime revision, exclusive ownership, completed tool receipts, background
terminal cleanup and verified unloading of the finished native thread. A failed or
interrupted invocation, owner disconnect, idle expiry or scope change discards the
process. An unavailable lease falls back before inference to the cold launch;
runtime failures after admission do not introduce an automatic turn replay.
Unload, SDK shutdown and lease-release errors disable reuse without changing a
completed answer or replacing the original execution error. Resource cleanup is
independent, and lease cleanup cannot prevent the broker relay from closing.
The task deadline covers startup and execution, then exits before teardown.
Cancellation during teardown waits for owned SDK/lease cleanup before propagating;
the lease remains attached until release succeeds, preserving the entrypoint fallback.
Orphaned native files from filesystem snapshots are removed before workspace
preparation, including on cold runs and delegated workers.
`CODEX_RUNTIME_REUSE=false` disables prewarming and reuse.

The pinned SDK connects through a local JSON-lines/Unix-WebSocket adapter
(`websockets==16.1.1`). Each invocation still starts a new native thread with the
current relay URL, capability, config and permission-filtered MCP catalog.
The supervisor starts without inherited credentials, and per-thread credentials
travel over stdin, not process arguments. MCP remains required before inference.
This reuses the Codex process, not a prior conversation or MCP connection across
capability boundaries.

The reusable process caches the pinned binary's bundled model metadata. The broker
also caches up to 256 immutable connector schema templates, copying them for each
response. Connection selection, policy, token validation and tool authorization
are reevaluated on every catalog request; authorized catalogs are never cached.

Reproduce locally with real Codex and MCP, using scripted inference:

```sh
uv run pytest -q tests/test_codex_runtime.py tests/test_tool_schema_cache.py
uv run python scripts/codex_runtime_demo.py --output /tmp/moyai-runtime --pairs 11
```

The benchmark reports entry-to-first-inference startup separately from the first
warm launch. Its steady medians exclude the first pair for both paths. It excludes
cloud provisioning and model generation and is not an end-to-end production SLA.

### Inference and lifecycle

The SDK receives the run capability and sends Responses requests to Moyai's local
relay using a custom provider. The broker still pins the selected model, authorizes
each request, enforces limits and records spend through LiteLLM Gateway. Native
approval policy is `never`; the enclosing machine supplies filesystem isolation
and connected-app authorization remains in the broker. Runtime request and stream
retries are disabled; only confirmed context rejection can automatically restart
the native session through Moyai's recovery path.

Astra's model catalog selects native **code mode**, even when the corresponding
feature flags are false. The model calls `functions.exec`, which can invoke native
shell/file tools and Moyai MCP tools. Moyai saves receipts from public
`commandExecution`, `fileChange`, `mcpToolCall` and `imageView` lifecycle items.
Code-mode outer call IDs differ from nested tool IDs. Before allowing the next
model request, the relay waits for its output IDs to appear in the ordered native
`rawResponseItem/completed` stream and for all nested receipts to settle. Raw native
client `tool_search_call` and `tool_search_output` events also supply search receipts,
using their call IDs, arguments and discovered schemas. Other raw events contribute
only output IDs; private reasoning and model responses never become public receipts.
A missing receipt blocks the boundary rather than inventing
completion. Corrections, delegation waits and cooperative checkpoints occur at
these boundaries; native in-flight steering is not enabled.

This integration uses the SDK's `AsyncCodexClient` and the pinned app-server's
experimental `experimentalRawEvents` thread option, passed through its supported
dictionary request API. The generated high-level thread API does not expose that
option. An SDK/runtime upgrade must reverify native tool schemas, event ordering
and receipt identity with the transport tests below.

Codex receives the selected model's context budget and an 80% native compaction
threshold. With Moyai's custom provider, the pinned runtime summarizes through
ordinary `/v1/responses`; it does not require a separate `/responses/compact`
endpoint. Native summaries remain private. Confirmed pre-generation context
rejection uses the same bounded journal-recovery path as Claude, preserving the
original request, deadlines and completed receipts. The gateway must support both
Responses and Chat Completions for durable summary generation. For Astra it must
also preserve native Responses items such as `additional_tools`, tool namespaces,
custom tool calls/outputs, client `tool_search_call`/`tool_search_output` items and
streamed completion events.

Transient model connection failures with pending tools continue in the same live
native thread, preserving existing command sessions under the original turn
retry limit. See [cloud recovery](observability.md#cloud-request-failures-and-recovery).

When context rejection ends a Codex turn, the adapter compacts the same native
thread while commands remain alive. Only explicit native compaction can pass
the relay's context-pressure latch; authentication, model-call limits and the
original deadline still apply. Overflow during compaction uses the pinned
runtime's SSE error contract so Codex can trim history and retry. Completion
events from earlier turns remain attached to their original calls, and private
native summaries never enter the public journal. After verified compaction the
same thread continues; failed compaction still permits a fresh runtime only
when all tool outcomes are confirmed. User Stop retains priority.

After a terminal failure, the adapter allows up to 10 seconds for queued and
late command receipts before closing the native client, within the original
task deadline. This grace period issues no model or tool calls. Stop permits
saving already-queued receipts but cancels the wait; unknown outcomes continue
to block automatic restart.

## Claude Agent SDK

`sandbox/claude_harness.py` calls the pinned Python **`ClaudeSDKClient`** directly.
It does not use LiteLLM's harness wrapper or import/patch Hermes. The shared
sandbox image retains Hermes and other runtimes for existing sessions and
explicit selections. Claude runs inside that same isolated Modal machine.
Older project snapshots install the pinned SDK on first use if it is missing
or outdated; this does not initialize Hermes or the LiteLLM harness wrapper.

The SDK receives only the run capability for inference and calls Moyai's loopback
relay. The relay forwards native Messages requests to the configured LiteLLM AI
Gateway. Provider keys remain on the web worker. Moyai pins the selected model,
enforces run authorization and limits, and streams native response bytes unchanged.

Claude uses a finite native file/shell tool allowlist and `dontAsk` permissions.
Only the broker-authorized `moyai` MCP server is loaded, with strict MCP config
and no user/project settings sources. Connected-app authorization remains in the
broker. Private memory, credential tool payloads and reasoning do not enter
public activity or traces.

MCP tool definitions load on demand through the SDK's native `ToolSearch` tool.
Moyai explicitly enables it for the loopback gateway and includes it in both
the native tool list and the permission allowlist; the environment flag alone
does not enable discovery with a restricted tool list. Core file/shell tools
remain available immediately. Search loads selected MCP schemas, and calls
still pass through the same broker authorization and activity hooks.
The gateway must preserve `defer_loading` and `tool_reference` blocks for the
selected model. See [SDK tool search](https://code.claude.com/docs/en/agent-sdk/tool-search).

SDK `PreToolUse`, `PostToolUse` and `PostToolUseFailure` hooks save tool receipts
and publish activity. Before the next inference, the relay lets Moyai checkpoint,
wait for credentials/delegated work, or apply a correction at a complete tool
boundary. In-flight redirect is a Hermes capability; Claude uses boundary
steering. Stop revokes the capability and terminates the isolated machine.

Claude, OpenCode and Pi can resume a compatible, successfully completed native
conversation on the next chat turn, including after the sandbox process exits.
The SDK receives only the new request; it owns its existing conversation instead
of receiving the public journal again inside a new user message. Completed
external actions remain recorded and must not be replayed after a failure.

Native reuse has three independent gates:

- Compatibility: the harness, installed runtime version, adapter instructions,
  working directory, effective model and gateway configuration must match
- Privacy: the broker requires the exact current requester. A turn that injected
  personal memory, skill directory entries, loaded skills, skill excerpts or search descriptions cannot
  publish reusable native state, even if those selections are later removed
- Restart: the saved public journal epoch and append position must match the
  restored filesystem. The preceding canonical turn must have completed, with no
  intervening metadata reply, failed turn or changed delivered steering input

Automatic memory and the initial skill directory use this same privacy gate.
They can avoid discovery round trips, but more turns may lose native session
reuse. The public journal still supports continuation. A smaller core prompt
does not establish lower end-to-end latency; measure with representative
libraries and follow-up turns. This changes inference context, not sandbox
startup or deployment topology.

The core prompt keeps task continuity, progress, trust boundaries and delivery
requirements. Skill saving, memory capture and credential procedures live in
their tool schemas/descriptions; the detailed 1Password procedure is supplied
with `credentials_run`. Runtime tool discovery determines when those schemas
enter the model request. Full request size still depends on the selected skills,
notes, tools and history, not just the core prompt.

Run `uv run python -m scripts.initial_context_demo --output /tmp/moyai-context-demo --delay 3`
to record first-inference recall, direct skill loading and immediate revocation.
Open the generated `recording.html` to replay the timestamped output. This is a
real local broker demonstration using disposable identities and a synthetic
inference upstream; it does not evaluate model quality. Add `--baseline REF` to
compare core Codex prompt characters against another local Git revision.

The broker stores one encrypted, bounded native record per run, separately from
public history, activity and downloadable artifacts. `/context/native` accepts
only authenticated, encrypted broker requests. Invocation leases fence late
uploads and invalidations; current capability and scope are checked again when
saving. Upload happens after answer receipt and has a short timeout. Failure to
save only loses reuse. Native state is limited to approximately 2 MB, with at
most 256 regular files for CLI runtimes; oversized state uses the public journal.

Native plaintext lives outside `/workspace` and is removed before attachments,
project preparation or agent startup, including when starting a cloned child.
Each SDK uses an isolated config/cache directory and fresh relay credentials.
Claude uses the pinned SDK's public `SessionStore` protocol; OpenCode and Pi use
LiteLLM's public resume API and its persisted native files. Missing, corrupt or
incompatible state selects a fresh session before inference. An ambiguous SDK
failure never automatically retries the task. Interrupted turns, intermediate
goal iterations, Codex, Deep Agents and Tool Loop retain public-journal recovery;
Hermes retains its existing separate history flow.

Malformed native state and confirmed context recovery can publish a replacement
after a fresh SDK attempt completes. Replacement admission atomically retires the
old candidate and lease while retaining the turn's privacy, canonical-input and
checkpoint checks. Compaction must succeed before context recovery admits that
replacement; ordinary failures do not restart the task.

The SDK supports automatic compaction within a session, but cannot compact an
oversized first message containing an entire restored journal. Chat sessions now
keep an append-only, scrubbed public journal in `/session/context.sqlite3`, outside
repositories and downloadable artifacts. A bounded working summary, its coverage
cursor, and recent journal excerpts form the next runtime's history. The current
request remains verbatim. The database travels with the existing Modal filesystem
checkpoint; Temporal still carries only session IDs and small lifecycle flags.

Before starting a runtime and after it finishes at a complete tool boundary,
Moyai updates the summary using only entries after its saved cursor. Each request
contains at most 24,000 bytes of new excerpts and a 12,000-byte prior summary.
It retains goals, constraints, decisions, completed-action receipts and unfinished
work. Summary and cursor commit atomically after a complete, bounded response.
Raw receipts remain available through bounded reads, for example:

```sh
python /opt/workspace-runner/context_store.py --after 40 --limit 5
# For the next 4,000-character slice of record 41:
python /opt/workspace-runner/context_store.py --after 40 --limit 1 --offset 4000
```

The authenticated `/context/compact` broker route uses the run's currently
selected model through LiteLLM Chat Completions. It has no tools and does not
inject personal memory, skills, attachments or native transcripts. Authorization,
concurrency, model-request limits and spend accounting apply as usual. Summaries
are model-generated reference data, not higher-priority instructions or proof
that an action succeeded. Check original records before repeating external writes.

The summary request does not impose an output-token cap. Generation uses the
selected model's gateway configuration; the saved summary's byte budget is
separate. Normal Messages, Responses and Chat Completions requests preserve an
explicit runtime output limit, and leave an omitted limit to the gateway. Moyai
does not inject an 8,192-token default or clamp requests to 16,000 tokens.

Every Messages, Responses and Chat Completions request now passes a context
budget check **after** model pinning, private memory, skills and attachments are
applied, and **before** inference admission/accounting. The selected deployment's
input ceiling and shared context window must leave room for the unchanged output
allowance, plus 10% input headroom (at least 512 tokens). Omitted output limits
reserve the configured effective default, or conservatively the model maximum;
the check does not insert a generation limit. An impossible allowance is reported
explicitly rather than silently clamped.

Limits come from `MODEL_CONTEXT_LIMITS` or the gateway's `/model/info`, cached
for five minutes by gateway, credential and model. A routed alias uses the minimum
limits of all its deployments. Metadata `max_tokens` alone is not a context
window. When only `max_input_tokens` is available, output is conservatively
reserved inside that limit. Missing/invalid limits pause inference with a
configuration error. Configure verified limits for private model aliases; model
names such as Astra and GLM do not imply Claude's context window.

Moyai's gateway owns background model-input compaction for Codex, Claude Agent
SDK, Hermes, OpenCode, Deep Agents, Tool Loop and Pi. At 75% of the verified input
budget it snapshots an older, closed prefix and starts tool-free summarization.
Fitting model requests continue using the existing history while that summary
runs. A later request uses the completed summary plus every item appended after
the captured prefix. A summary covers a contiguous span, keeping work on its
original side of retained instructions and media. Later compactions can extend
that summary or summarize a later span after a retained record. Native
processes and tools keep running; the gateway does not replace the SDK's own
transcript or restart its session.

Prefix hashes and the originating capability, turn, requester and selected model
fence adoption. Divergent child/title histories cannot adopt an unrelated result.
System/developer instructions, first/latest user requests, unresolved tool groups
and opaque media groups remain verbatim. Private reasoning is omitted from the
summary input. Native tool responses can contain still-running process handles;
the summary preserves those handles and their continuation steps. Private
snapshots and summaries stay in a bounded in-memory cache, outside the public
journal, background journal jobs, activity text and content traces. Stop, scope
changes and idle expiry retire this cache and cancel its background work.
Requests checking or waiting for compaction keep their scope alive through idle
cleanup and cache pressure. Idle expiry starts after the last such request exits;
Stop, scope changes and shutdown still invalidate it and wake waiting requests.
If every cache slot is in use, new requests use their checked original input and
new summaries may be declined without evicting another active request's history.

If a request cannot fit, it waits for relevant compaction before taking a model
slot, then validates the resulting input again. Summary failure preserves the
original history and the last valid projection. If reduction cannot make the
request fit, the existing typed context-recovery path remains available.
Summaries share ordinary model concurrency, request limits and usage accounting;
foreground work can preempt maintenance. Output allowances are unchanged.

The authenticated `/context/window` capability enables supported native controls:
Claude automatic compaction, Hermes proactive compression, OpenCode auto/prune,
Pi automatic compaction and Deep Agents summarization middleware yield to this gateway owner. Older
gateways retain their existing controls. Codex has no supported disable switch;
its usage-based native counter sees the projected input, while emergency native
recovery remains available. Deep Agents' public middleware profile is process-wide;
if a running process loses the capability, it requires a restart to restore native
compaction. SDK transcripts still grow in memory and remain
subject to the existing 5 MiB transport ceiling. This is not an unlimited raw
transcript or restart-persistent private-history store.

Ordinary text requests use a local UTF-8 estimate, or the previous response's
input usage plus a conservative byte estimate of appended messages. Reuse is
scoped to the run, protocol and model, expires after five minutes, and requires
unchanged instructions, schemas and message prefix. Cache read/write tokens count
as input; cache-control placement alone does not invalidate reuse. Only hashes
and byte lengths are retained. Changed context, missing usage and cold workers
fall back to estimating the full request. Provider-truncated input is not reused.

When the estimate reaches 80% of the input budget, or new images need counting,
the gateway's `/utils/token_counter?call_endpoint=true` estimates a serialization
of the complete request content, schemas and opaque native items, with images
passed separately as images. This intentionally adds JSON/protocol headroom and
does not rewrite the inference payload. Generic tokenizer counts cannot reduce
the conservative local estimate for text; provider API counters can. If
counting is unavailable, text uses that byte estimate; images require a working
provider API counter rather than a guessed cost for their URL or base64 text.
Unsupported audio/file/video blocks fail explicitly. Provider-injected content
and tokenizer approximations can still differ: a structured pre-generation
context rejection also requests reduction; arbitrary errors/timeouts do not.

As a fallback for the durable SDK adapters, the relay returns a typed
context signal and blocks further calls from that native session. After the SDK
closes and all tool receipts settle, the adapter forces journal compaction and
invalidates native reuse and starts a fresh session with a bounded reference and
the original current request.
The journal is available for non-chat tasks too. User stop/steering wins over
recovery, pending tools prevent it, and task deadlines are not reset. Repeated
rejections without task progress must reduce input and stop after at most three
repairs. New completed work can trigger further compactions for long tasks.
Hermes receives the standard `context_length_exceeded` error and retains its
native bounded overflow-compression path; its native transcript is not rewritten
by Moyai. Temporal continues to checkpoint lifecycle references, not prompts.

To verify the legacy native SDK fallback against a live gateway with only synthetic local
files and the Read tool, set `GATEWAY_BASE_URL` and `GATEWAY_API_KEY`, then run:

```sh
uv run python -m scripts.claude_compaction_smoke --model openai/gpt-6-astra --output report.json
```

This uses a disposable local broker/database and makes billed provider calls.
It lowers the compaction window only for the probe, verifies at least two native
compactions, the original codeword, and exactly-once reads, and requires zero
remote count calls and zero custom-recovery calls. Output allowances remain the
SDK's own values. Deterministic real-SDK tests also cover Claude and GLM aliases.

Summary generation uses the same budget check. Oversized batches are split into
prefixes; each response returns `through_seq`, so the store advances only over
records actually summarized. Forced compaction can cover the full remaining tail
and request a smaller saved summary after a model change. No record is deleted.
If even the summary plus one excerpt cannot fit, recovery pauses with the last
valid summary and receipts intact instead of recursively attempting compaction.
Partial batches require the client's `cursor_protocol: 1` capability. An older
sandbox that cannot consume returned cursors pauses instead of skipping records
during a rolling deployment; updated clients can still read old full-batch replies.

An oversized, empty or incomplete summary is regenerated from the same original
records with a more concise target, for up to three attempts. The code never
cuts off a summary to fit. Transient gateway failures can also retry; refusals
and permanent upstream rejections cannot. Every attempt rechecks authorization,
counts toward request limits and records usage. Activity records include the
failure category, attempt, request ID and summary byte count when available,
without storing rejected text or private reasoning. These retries only generate
summaries; they cannot restart a runtime or replay a task action.

A failed recovery leaves the last good summary, cursor and all receipts intact.
Before the next task invocation, compaction must catch up; exhausted recovery pauses work
with a retry message instead of silently dropping older context. An unresolved
tool from a stopped invocation does not block a later turn: its unknown outcome
is shown separately from the summary so the agent can answer and investigate.
Original receipts and pending records are retained; the runtime does not replay
the action or invent a completion. The agent must inspect original records,
workspace state and external receipts before repeating an affected action.
Saved tool IDs are scoped to each invocation so reused native IDs cannot settle
older pending calls. Currently executing tools still block cooperative renewal.
Missing, corrupt, mismatched or invalid
checkpoint state is not silently treated as an empty conversation. A fresh child
or an explicit stale-filesystem recovery starts from its own canonical fallback.
Same-session requester/model changes keep only the public context; private memory
is resolved separately for the active requester on each normal inference.

Existing `conversation.json` files migrate once. Subsequent invocations read
indexed previews and append new rows rather than loading/rewriting the complete
journal. Healthy saved sessions also omit the redundant full chat fallback from
their launch specification. Goal continuations use the same store. The other
fresh-session LiteLLM adapters use this path too; Hermes retains its native
history handling. Non-chat adapter calls retain the 48,000-byte excerpt fallback.

This bounds restored history input and steady-state journal processing, not total
disk usage, snapshot storage or inference cost. Large requests, system prompts,
attachments and tool catalogs still need to fit the chosen model. GPT-6 Astra and
GLM 5.3 routing are covered with fixtures; live provider compatibility and summary
quality require provider verification. The gateway must support both the runtime's
native API and Chat Completions for the chosen model.

## Prompt caching and accounting

[The SDK enables prompt caching automatically](https://code.claude.com/docs/en/agent-sdk/cost-tracking#track-cache-tokens).
Moyai explicitly overrides inherited `DISABLE_PROMPT_CACHING` and family-specific
disable flags. It keeps the normal five-minute Anthropic cache policy; this change does
not opt into the higher write cost of a one-hour cache.

The authenticated Messages gateway preserves both block-level and top-level
`cache_control`. SDK-managed system/tool prefixes and conversation prefixes can
be reused within the provider's TTL and minimum token threshold. A setting being
enabled does not guarantee a hit on a short or changing prompt. Other providers
use their own cache policy through the gateway; this change does not enable
response caching or reuse a prior answer.

`model_requests`, the spend API and traces retain `cache_read_input_tokens` and
`cache_creation_input_tokens`. Messages `input_tokens` excludes those tokens;
Moyai adds them exactly once to its total prompt count. Responses accounting
continues to treat cached tokens as included in `input_tokens`. Dollar amounts
still come from the gateway; missing cost is never guessed from local SDK prices.

## Existing sessions and automations

Existing sessions retain their persisted harness, including old Hermes sessions.
Start a new thread/session to use the new default; native histories are not
hot-switched. Follow-ups, side chats and delegated workers retain the selected
harness. Existing automation definitions without a harness retain Hermes, while
new automations save the configured default. The automation editor can explicitly
switch a saved workflow to Claude Agent SDK and a configured model; saving pauses it for
review through the existing workflow.

New Slack threads can explicitly choose a runtime:

```text
@Moyai harness claude-agent-sdk
Read the repository
```

The task line is optional. A harness-only command starts no compute. Other IDs
are `hermes`, `codex`, `opencode`, `deepagents`, `tool-loop` and `pi`.

## Pi

Choose **Pi** in the new-session engine picker, or start a Slack thread with
`@Moyai harness pi`. Pi uses Chat Completions through the configured gateway;
the selected model must support that API and tool calls. Its native file and shell
tools run in the isolated workspace. Connected-app tools use Moyai's authorized
MCP bridge.

Workspace images include Pi `1.1.0` and its private Node `22.23.3` runtime. An older
saved workspace installs those versions on first use without replacing the
project's Node runtime. The LiteLLM runtime also upgrades its pinned source and
MCP dependency automatically. If an installation fails, the turn stops before
launching Pi; retry after restoring package-download access, or rebuild the
workspace image.

Moyai owns background compaction when the gateway advertises that capability.
Pi's automatic compaction yields to it, and Pi's agent/provider retries are
disabled. A confirmed context rejection can rebuild from the saved public
receipts only after tool outcomes settle. Failed summaries and uncertain tool
outcomes stop recovery. Compatible completed conversations can resume native
state on the next turn with fresh broker credentials; invalid snapshots fall
back to public context.

## Other runtimes and extension

`sandbox/harness_registry.py` is the single catalog for API validation, UI model
choices, Slack selection and adapter creation. Each adapter implements
`HarnessAgent`: `validate`, `run_conversation`, `interrupt` and `close`.
`sandbox/agent.py` owns workspace preparation, shared prompts, goals, waits,
checkpointing and delivery.

OpenCode, Deep Agents, Tool Loop and Pi use `litellm.aagent_session`, or
`litellm.aagent_resume` for eligible OpenCode and Pi conversations, with the named
bindings in `sandbox/harness_bindings.py`. The pinned beta source
is `36f96259f08d449bdc996ed36919c47b39ce527f`; the tested PyPI wheel alone does not
contain that API. Claude Agent SDK stays pinned at `0.2.163`; the native Codex
Python SDK and its bundled runtime are pinned at `0.161.0`. No upstream source
is vendored. Add a new registry definition and lifecycle adapter to extend Moyai.
The isolated harness environment uses MCP `2.2.0`; the controller retains its
separate locked dependencies.

## Verification

```sh
uv run pytest -q --tb=line tests/test_codex_sdk.py tests/test_codex_sdk_transport.py tests/test_claude_sdk.py tests/test_harnesses.py tests/test_harness_gateway.py tests/test_spend.py
uv run pytest -q --tb=line tests/test_context_store.py tests/test_context_gateway.py tests/test_context_lifecycle.py tests/test_claude_sdk_transport.py
node --test tests/test_harness_picker.cjs tests/test_automation_editor.cjs
```

With `uv`, Git and npm installed, run the native Pi contract suite:

```sh
scripts/test_pi_runtime.sh
```

This creates a disposable runtime environment and installs the pinned source,
Pi and OpenCode. Missing Pi dependencies fail the suite. Tests run the actual
Pi process against the real broker, encrypted native-state store and MCP bridge,
with scripted local inference. They cover file and app tools, cold resume with
renewed credentials, invalid snapshots, provider errors, truncated streams,
Stop, deadlines, call budgets and context-recovery failures. Shared harness tests
exercise background compaction while tools continue and steering at request
boundaries. The Pi CI job also builds the production workspace image. These
checks make no external model calls and do not establish every provider's
compatibility, model quality or production latency.

The `native-resume` cases in `test_claude_sdk_transport.py` exercise the real
pinned Claude SDK, MCP tool transport, encrypted broker endpoint and SQLite.
They verify the same native session ID after cold restoration, a new-request-only
SDK prompt, and one execution of a completed tool. Provider responses are local
fixtures; these checks do not establish production response latency.

`test_codex_sdk_transport.py` runs the actual pinned app-server and MCP bridge with
a local synthetic Responses server. It exercises Astra's native code-mode shell,
file patch and MCP calls; interruption after saved receipts; provider failure
without replay; repeated native compaction; and a fresh requester invocation using
only public saved context. It also verifies project-config isolation and private
summary exclusion. These tests make no provider calls and do not establish live
Astra/Opus gateway compatibility or model task quality.

`uv run python -m scripts.context_checkpoint_demo` demonstrates repeated cold
filesystem restores and summary-failure recovery using synthetic data and a
deterministic summarizer. It makes no provider calls. The real SDK/MCP continuation
test above separately verifies that the completed echo action is not repeated.

For a **live read-only tool-search probe**, securely provide `GATEWAY_BASE_URL`
and `GATEWAY_API_KEY`, optionally `SMOKE_MODEL` (default GPT-6 Astra), then run:

```sh
uv run python -m scripts.claude_tool_search_smoke
```

This exercises the real SDK, MCP bridge, authenticated relay, and gateway with
native shell/file tools disabled. A disposable broker has no connected accounts;
only discovery and its read-only `model_list` tool may execute. It asserts that
the first request contains no MCP definitions, discovery precedes execution,
and the selected tool runs once. Requests are billed. `SMOKE_EAGER=1` measures
the previous eager configuration; `SMOKE_REPORT` saves sanitized request metrics.

For a **live, tool-free caching probe**, securely provide `GATEWAY_BASE_URL` and
`GATEWAY_API_KEY`, optionally `SMOKE_MODEL` and `SMOKE_ROOT`, then run:

```sh
uv run python -m scripts.claude_cache_smoke
```

This runs the actual SDK adapter, authenticated relay, gateway and accounting
against three synthetic requests. It disables all tools, makes billed inference
calls, prints provider cache counts, writes `verification.json`, and fails unless
a cache read is reported. It does not exercise coding tools or production Slack.

For full coding-tool verification, run `python -m scripts.harness_smoke` **inside
an isolated container or Modal sandbox**, with the same gateway credentials.
It creates, reads and executes a test program, calls a read-only workspace tool
and follows up after reopening the durable context store. `SMOKE_HARNESS` selects
the runtime and `SMOKE_MODEL` selects the configured model. The native Codex/Astra
and Claude/Opus 5.5 pairings both passed this live gateway probe. Native compaction
remains covered by the fixture-backed tests
described above. Do not
run agent-generated shell commands on the web host or a developer's computer.

A PR and local checks do not update the hosted agent. Production rollout requires
deploying the web worker and rebuilding/preparing the sandbox image. Existing
sessions and saved automations remain on their stored runtime.
