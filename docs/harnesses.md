# Agent harnesses

New web sessions, Slack threads and saved automations default to **Claude Agent
SDK** (`claude-agent-sdk`). `AGENT_HARNESS` overrides the default. Explicitly
selected harnesses remain available: Hermes, Codex, OpenCode, Deep Agents and
Tool Loop. All configured models remain selectable with every harness. The configured
`AGENT_MODEL` is preserved, including GPT-6 Astra. The gateway must support the
selected model and tool calls through the runtime's native API (Messages for
Claude Agent SDK); selecting it does not establish provider compatibility.

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

Each app turn uses the saved, scrubbed conversation and tool receipts. It creates
a fresh SDK session rather than resuming an unfiltered native transcript that
could retain private tool payloads from an earlier requester. Completed external
actions are recorded and must not be replayed after a checkpoint or failure.

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

Claude Agent SDK owns proactive compaction within a task. At session startup the
authenticated `/context/window` endpoint supplies the selected model's input
budget, reserving output, injected context and tool-result headroom. The harness
sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` and an 80% compaction threshold (scaled for
windows below the SDK's 100k setting minimum). The SDK may impose a smaller
window for an unfamiliar model. Both inherited compaction-disable flags are
cleared. Native `compact_boundary` events appear in activity, while the native
summary remains private to the SDK. The durable journal still preserves original
public receipts for restart recovery. No generation limit is added or lowered.

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

As a fallback for Claude, and for the fresh-session LiteLLM adapters, the relay returns a typed
context signal and blocks further calls from that native session. After the SDK
closes and all tool receipts settle, the adapter forces journal compaction and
starts a fresh session with a bounded reference and the original current request.
The journal is available for non-chat tasks too. User stop/steering wins over
recovery, pending tools prevent it, and task deadlines are not reset. Repeated
rejections without task progress must reduce input and stop after at most three
repairs. New completed work can trigger further compactions for long tasks.
Hermes receives the standard `context_length_exceeded` error and retains its
native bounded overflow-compression path; its native transcript is not rewritten
by Moyai. Temporal continues to checkpoint lifecycle references, not prompts.

To verify native SDK compaction against a live gateway with only synthetic local
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
with a retry message instead of silently dropping older context. Unfinished tool
calls block automatic continuation. Missing, corrupt, mismatched or invalid
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
are `hermes`, `codex`, `opencode`, `deepagents` and `tool-loop`.

## Other runtimes and extension

`sandbox/harness_registry.py` is the single catalog for API validation, UI model
choices, Slack selection and adapter creation. Each adapter implements
`HarnessAgent`: `validate`, `run_conversation`, `interrupt` and `close`.
`sandbox/agent.py` owns workspace preparation, shared prompts, goals, waits,
checkpointing and delivery.

Codex, OpenCode, Deep Agents and Tool Loop continue using `litellm.aagent_session`
and the named bindings in `sandbox/harness_bindings.py`. The pinned beta source
is `2cee61626d9581bc22bbdeefb1924f854f50d427`; the tested PyPI wheel alone does not
contain that API. Claude Agent SDK stays pinned at `0.2.163`. No upstream source
is vendored. Add a new registry definition and lifecycle adapter to extend Moyai.

## Verification

```sh
uv run pytest -q tests/test_claude_sdk.py tests/test_harnesses.py tests/test_harness_gateway.py tests/test_spend.py
uv run pytest -q tests/test_context_store.py tests/test_context_gateway.py tests/test_context_lifecycle.py tests/test_claude_sdk_transport.py
node --test tests/test_harness_picker.cjs tests/test_automation_editor.cjs
```

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
and follows up from saved receipts. `SMOKE_HARNESS` selects the runtime. Do not
run agent-generated shell commands on the web host or a developer's computer.

A PR and local checks do not update the hosted agent. Production rollout requires
deploying the web worker and rebuilding/preparing the sandbox image. Existing
sessions and saved automations remain on their stored runtime.
