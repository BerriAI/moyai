# Tracing and observability

[Documentation](README.md) · [Project overview](../README.md)

> This guide retains the detailed reference material from the original README.
> Dated acceptance reports describe past checks, not a current deployment or test result.

## Control-plane memory

Moyai shares Modal clients across workspace monitoring, billing, and connection
checks. Modal 1.6.0 retains factory-created clients through SDK shutdown hooks;
creating a client on every poll steadily increases the control plane's memory.
The application reuses one client per explicit credential pair and closes them
after its consumers stop. Changed credentials select a different client without
closing handles still used by existing operations. SDK shutdown hooks retain
those distinct credential identities until process exit.

This prevents polling from accumulating clients. It does not make in-flight
inference durable across server restarts; interrupted inference remains a
separate recovery concern. Compare Render memory with process-start timestamps
when investigating a broker failure, rather than attributing every interruption
to a model or tool error.

## Cloud request failures and recovery

The sandbox emits an `error` event with `data.phase=broker_failure`. Its structured
fields preserve HTTP status, original upstream status, client request ID,
allowlisted response IDs (including Render and model ledger IDs), exception and
underlying exception types, numeric errno, request timing, and partial-response
state. Diagnostic records exclude request/response bodies, capability values,
full URLs and arbitrary headers. Provider error bodies are not diagnostic logs.

Search Render application logs for the event's `request_id`. Broker requests log
`broker_request_started` and `broker_request_finished` or `broker_request_failed`,
including the response status, byte count and elapsed time. The same generated ID
travels in `X-Moyai-Request-ID`. Use the response's Render ID to correlate with edge
request logs. An edge failure without an application start record narrows the
failure boundary; it does not establish a particular proxy reset or timeout cause.

Before agent execution, the initial GitHub checkout's repository/PR metadata
reads reconnect through temporary HTTP or network failures. Each metadata lookup
keeps its 90-second timeout within a 180-second reconnect window; the local caller
waits another 10 seconds for the relay result. Exhaustion emits a
`repository_metadata` startup marker for the existing bounded durable startup
recovery. This applies only to `github_checkout` and `github_repository` metadata
inside repository preparation. Permanent HTTP rejections, later Git operations,
tool writes and metadata calls after startup retain their existing failure policy.

When a transient model failure arrives before any response and Codex still has
running tools, it keeps the native client and thread alive. After a bounded
backoff, a continuation on that same thread inspects the existing tool sessions,
waits for finite work, and can stop an unneeded preview server. Completions from
all earlier native turns still become durable receipts; the relay does not resend
the failed POST or restart tools. A receipt grace period alone cannot finish a
long-running preview server.

With Temporal enabled, a failure with settled tool receipts can instead continue
after the SDK exits and the filesystem checkpoint succeeds. The restored public
journal must match the saved epoch and sequence before a fresh SDK invocation
starts. Live and checkpoint-based recovery share the original user turn's limit
of three continuations, delayed by 2, 4 and 8 seconds. The count survives context
handoffs, rotation and worker replacement, retaining the requester, model and
budgets. Loss of the native process with unresolved tools remains terminal.

Partial streams, uncertain tool transport, permanent upstream rejections,
unknown process outcomes and failed checkpoints do not qualify. An upstream
401/403 wrapped in a gateway 502 remains terminal. Pending receipts prohibit a
cold restart; only the still-live Codex thread may continue with its existing
tools. Cancellation and the original task timeout still apply. Hermes receives
diagnostics but its legacy history does not support this automatic recovery
protocol. Arbitrary external writes and inference billing do not have a generic
exactly-once guarantee; accepted inference may be billed after its connection is
lost.

Run the local fault-injection proof with:

```sh
uv run pytest -q --tb=line tests/test_codex_sdk_transport.py::test_native_transport_recovery_preserves_running_commands
uv run pytest -q --tb=line tests/test_claude_sdk_transport.py::test_real_sdk_recovers_broker_failure_from_cold_tool_receipts
uv run pytest -q --tb=line tests/test_durable.py tests/test_temporal_integration.py
```

The Codex test keeps a real running command and preview server alive through an
injected HTTP 502, then collects their receipts in the same native thread.
The Claude test uses the real bundled Claude SDK, encrypted broker, MCP and SQLite,
with local model responses and an injected HTTP 502. One synthetic publication
survives a cold restore and completes with its action count still one. Temporal
tests use a real local server and simulated sandbox provisioning.

## Agent Traces in LiteLLM

Moyai can send the same sanitized spans to **LiteLLM Lens, Raindrop, Langfuse,
LangSmith, and Braintrust concurrently**. See the [tracing comparison and rollout
guide](tracing-comparison.md) for configuration, verified dashboard links,
and the differences found during integration.

Set `LITELLM_TRACE_ENDPOINT=https://gateway-dev.litellm-sandbox.ai/v1/traces` and a
dedicated `LITELLM_TRACE_API_KEY` in Render's private environment. Both are required;
leave the key empty to disable export. The inference gateway and its key remain
configured separately through `LITELLM_API_BASE` and `LITELLM_API_KEY`.

The control plane sends standard OTLP/HTTP protobuf with the OpenTelemetry SDK.
Each response produces an agent span containing the task and final
answer, with child model and tool spans including timing, status, token counts,
and bounded tool inputs/results. Follow-ups have separate trace IDs and a shared
session ID. Active delegated agents attach beneath their coordinator; machine
renewals retain the current turn's trace identity. Sandbox events never receive
the trace credential and trace payloads are separate from public chat activity.
The service name remains `moyai`; workers use their saved agent labels.
Each turn keeps its original trace, parent and name across recovery.
Turns started from Slack set `agent.source.type=slack`, `agent.source.url`
(the thread permalink) and `agent.source.title` (the thread's first message)
on the agent span, so Lens shows a "Slack thread" link at the top of the trace.

Native Messages and Responses model spans include the last five user text
messages, public assistant text and requested tool names, for both JSON and
streaming responses. Streaming deltas are assembled before redaction; completed
Responses snapshots replace those deltas so text is exported once. Failed and
incomplete snapshots preserve any text and tool names already received. Text capture
is bounded at 16,000 characters per input/output aggregate; overflowing text is
omitted as a whole. Tool arguments and results remain in their separate tool
spans. Internal context-compaction calls export usage and status only. Capture
uses the existing inference connection and does not add a network hop.

System prompts, loaded skills, private reasoning, images and credential-tool
payloads are excluded. Known credentials and common secret fields are redacted;
ordinary task/tool text is sent to the configured gateway. Text is capped at
16,000 characters per field. Moyai writes encoded spans to a SQLite outbox on
Render's persistent disk before export. A background worker retries delivery after
outages and restarts using the same IDs. It keeps delivery receipts and removes
acknowledged payloads. The gateway must deduplicate by trace/span ID if it accepted
a batch but its acknowledgment was lost. Pending payloads occupy disk until delivery;
back up and protect that disk with the rest of the workspace data. A crash before
capture, a lost disk, or a model response that never reaches Moyai can still leave
gaps; this outbox does not recover missing inference responses or billing receipts.

The destination must enable `general_settings.tracing.store: clickhouse` and
`CLICKHOUSE_URL`. ClickHouse is required for Agent Traces itself. A Lens worker
is only needed for automated investigations. After deployment, run a short task
that uses a file or terminal tool, open **Logs → Agent Traces**, and look for
`moyai`; verify the task, tool result and final answer in the trace tree.

## Agent Traces in Raindrop

The same spans can also go to [Raindrop](https://www.raindrop.ai/docs/platform/issues/), so its
issue detection runs over Moyai conversations next to LiteLLM Lens. Set `RAINDROP_WRITE_KEY`
in Render's private environment to turn it on, and optionally `RAINDROP_PROJECT_ID` to route
into a specific Raindrop project. Spans are sent as OTLP/HTTP protobuf to
`https://api.raindrop.ai/v1/traces`. Override it with `RAINDROP_TRACE_ENDPOINT` if needed.

LiteLLM and Raindrop each have their own outbox table on the same disk, `trace_outbox` and
`trace_outbox_raindrop`, with independent retries and receipts. If Raindrop is down, LiteLLM
delivery keeps going and is never resent, and the other way around. Either destination works
on its own. Every span carries `traceloop.association.properties.convo_id` (the Moyai session)
and `traceloop.association.properties.event_id` (the turn's trace ID), which Raindrop uses to
group spans into conversations. The same redaction rules apply, and the write key is redacted
from trace text like the other credentials.

Each top-level turn also sends one interaction to `/v1/events/track`, through its
own durable `trace_outbox_raindrop_events` queue. Its `event_id` matches the turn's
trace ID. Raindrop needs this interaction for Events, Signals, and Issues; OTLP
spans alone do not create it. The Query API uses a separate read credential.

After deploying, run a short task, then open Raindrop and look for the `moyai` service
in Events. Issues show up once Raindrop has enough conversations to cluster.

## Agent Traces in Langfuse

Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in Render's private environment,
with `LANGFUSE_BASE_URL=https://us.cloud.langfuse.com` for the US project. Both keys
are required; leaving either empty disables Langfuse only. Set
`LANGFUSE_TRACING_ENVIRONMENT=production` on Render (local default: `development`).
Other HTTPS Langfuse regions and self-hosted base URLs are supported.

Moyai exports its existing sanitized OpenTelemetry spans directly to
`/api/public/otel/v1/traces`, using Basic authentication and Langfuse's v4 ingestion
header. No additional SDK or sandbox credentials are needed. Agent turns appear
as **agent** observations, model requests as **generations** with model/token usage,
and tools as **tool** observations. Follow-ups have separate traces grouped by the
Moyai session ID; delegated agents retain their existing parent/child hierarchy.
Every observation carries the environment, `moyai` tag, and a link to its chat.
System prompts, private reasoning, images and private tool payloads remain excluded.

Langfuse has its own persistent `trace_outbox_langfuse` table, retries and receipts.
An outage does not block LiteLLM/Raindrop delivery or agent responses. Keys are
redacted from trace text and never committed or passed into agent sandboxes.
After deployment, run a short task that uses a terminal/file tool, then find
**moyai** in Langfuse and verify the task, generation, tool output and final
answer. Check the session view for follow-up turns. Existing completed turns are
not backfilled.

## Agent Traces in LangSmith and Braintrust

Set `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT=moyai` to enable LangSmith.
`LANGSMITH_ENDPOINT` defaults to `https://api.smith.langchain.com`; set
`LANGSMITH_WORKSPACE_ID` if the key needs explicit workspace routing.

Set `BRAINTRUST_API_KEY` and `BRAINTRUST_PARENT=project_id:<project-id>` to enable
Braintrust. `project_name:moyai` also works, but an ID survives project
renames. `BRAINTRUST_API_URL` defaults to `https://api.braintrust.dev`.

Both use `/otel/v1/traces`, with independent persistent outboxes. Set
`TRACE_ENVIRONMENT=production` on Render. Follow-up turns share a session/thread
identifier, and delegated agents preserve their parent span. No additional
vendor SDK or global tracer provider is installed. Leaving a destination's key
empty disables that destination. New destinations do not backfill old turns.

Run `uv run python -m scripts.check_trace_exports --send --live-model` with the
configured environment to create labeled verification data in every enabled
backend. This makes two small model requests using a temporary SQLite database;
it does not start a production chat. Inspect `verification.json` in the reported
directory for trace IDs and delivery receipts, then check stored dashboard data.
