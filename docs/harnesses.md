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
oversized first message containing an entire restored journal. Moyai therefore
bounds the saved-history reference to 48,000 UTF-8 bytes when starting a fresh
session. Small histories remain verbatim. Larger histories keep the original
request, latest user correction and recent message/tool excerpts; the complete
scrubbed journal remains in `/session/.moyai-history.jsonl` for targeted reads,
outside repositories and downloadable artifacts. The current request and durable
conversation are never shortened. Omitted
history is explicitly marked, and the agent must check relevant instructions and
receipts before repeating external actions. The same bound applies to the other
fresh-session LiteLLM harnesses; Hermes retains its native history handling.
This bound reserves space for other input; it is not a model-specific token
limit or a guarantee that arbitrary attachments and tool catalogs will fit.

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
uv run pytest -q tests/test_claude_sdk_transport.py
node --test tests/test_harness_picker.cjs tests/test_automation_editor.cjs
```

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
