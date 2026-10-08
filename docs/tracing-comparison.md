# Parallel tracing: deployment and comparison

Verified October 6, 2026. All five destinations receive the same sanitized OTLP
spans and IDs. The comparison uses Moyai's existing OpenTelemetry SDK and durable
exporter, not five competing auto-instrumentation libraries. No new dependencies
are required. Native SDK capabilities below describe available product features;
this integration specifically exercises their OTLP ingestion paths.

## Where Moyai lives and how it deploys

- Repository: [BerriAI/moyai](https://github.com/BerriAI/moyai).
- [Render service](https://dashboard.render.com/web/srv-daunqr8473hc73buottg):
  in the Litellm workspace, Oregon, Python 3.13.7.
- App: Render hosts FastAPI/UI,
  the model/tool broker, and the Temporal worker. Modal's `hermes-workspace`
  hosts agent sandboxes. Temporal uses queue
  `moyai-sessions-v1`.
- One Render instance, 1 CPU / 2 GB RAM, with SQLite and artifacts at
  `/var/data/moyai` on a 1 GB persistent disk. Keep one instance.
- Build: `pip install uv==0.11.17 && uv sync --frozen --no-dev`.
  Start: `.venv/bin/python render_start.py`. Health check: `/health`.
- Render is configured for `main`, with automatic deployment off. The live
  version before this change was `d4a5eec351ed467060630d65d1f93b6d01f8dca1`, from
  `codex/langfuse-tracing` (PR #68). This release starts from that deployed commit
  and incorporates the existing `main` changes.

The deployed version already had durable Lens, Raindrop OTLP, and Langfuse
exporters. Langfuse was receiving production observations. Raindrop was missing
interaction events, so its Events/Signals experience was not populated by spans
alone. This release adds those events and the LangSmith/Braintrust destinations.

## Configuration

Credentials belong in Render's private environment. `.env.example` documents
local defaults, and `render.yaml` carries deployment configuration. Inference
stays on `https://gateway.litellm-sandbox.ai/v1`; Lens uses the separate dev trace
gateway. All credentials stay on the Render control plane, outside sandboxes.

For gateway-dev, set `LITELLM_TRACE_ENDPOINT` to
`https://gateway-dev.litellm-sandbox.ai/lens-ingest/v1/traces`. In **Lens > Traces >
Set up tracing**, generate a dedicated tracing key and save it as
`LITELLM_TRACE_API_KEY` in Render's private environment. The old gateway
`/v1/traces` route and model/virtual keys no longer accept trace exports.
Other deployments should copy their full **Traces endpoint** from the same
setup screen, preserving any path prefix. Restart Moyai after changing these
settings; its pending Lens outbox will retry against the configured endpoint.

| Destination | Required settings | Protocol and routing |
| --- | --- | --- |
| Lens | `LITELLM_TRACE_ENDPOINT`, `LITELLM_TRACE_API_KEY` | OTLP protobuf, Bearer, `/v1/traces` |
| Raindrop | `RAINDROP_WRITE_KEY`; optional `RAINDROP_PROJECT_ID` | OTLP protobuf `/v1/traces` plus JSON `/v1/events/track`; Bearer; project slug header |
| Langfuse | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | OTLP protobuf, Basic auth, `/api/public/otel/v1/traces`, ingestion version 4 |
| LangSmith | `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT`; optional `LANGSMITH_WORKSPACE_ID` | OTLP protobuf, `x-api-key`, `Langsmith-Project`, optional `X-Tenant-Id`, `/otel/v1/traces` |
| Braintrust | `BRAINTRUST_API_KEY`, `BRAINTRUST_PARENT` | OTLP protobuf, Bearer, `x-bt-parent=project_id:...`, `/otel/v1/traces` |

Use `TRACE_ENVIRONMENT=production` and
`LANGFUSE_TRACING_ENVIRONMENT=production` for the application. Verification uses
`verification` in both. LangSmith uses project `moyai`; Braintrust uses
project `03def8d1-b4cf-431e-8907-828a0ae42f97` in Moe's Org. Langfuse uses the
existing US project `cmuvztlwz059bad0c138utb1q`. Raindrop uses Production/default.

Each destination has an independent SQLite queue and background task. An outage
or slow receiver does not block the other receivers or network-bound delivery
on the request path. Retries retain IDs, respect Retry-After, and survive restart.
Acknowledged payloads are removed; compact receipts prevent replay duplication.
Receiver deduplication is still needed if an acknowledgment is lost. Existing
completed turns are not backfilled. Pending payloads consume persistent disk.

## Verified comparison

Final marker: `moyai-comparison-e88cacb4e1c8`.
Session: `47bad3e3737a4de7a62ff520a55c0128`.

- Initial turn: `62a19a88e83ef1bc705c53392eaa491c`, five spans: coordinator,
  delegated worker, two file tools (one intentional error), and a model request.
- Follow-up: `47ab8b5eaf994987ff622f86c24a3c81`, two spans: coordinator and model.
- All five queues acknowledged all seven spans; Raindrop acknowledged both
  interaction events. Read-back verified hierarchy, model input/output, usage,
  and error status. Both model answers were `10`.
- Model usage: 46 input / 4 output tokens in the first turn; 32 / 16 in the
  follow-up. LangSmith, Braintrust, and Langfuse estimated $0.00066 and $0.00112.
  These are catalog estimates, not exported gateway billing receipts.

The check uses the real tracing code, two actual gateway model calls, real local
file reads, and a deliberately missing file. Delegation and completion are
driven by the verification harness in an isolated temporary database. It does
not launch a production Hermes/Modal conversation. The sample's local session
URL is metadata for that temporary store, not a hosted chat link.

Dashboard entry points (search the marker or session above):

- [Lens](https://gateway-dev.litellm-sandbox.ai/ui/lens?trace=62a19a88e83ef1bc705c53392eaa491c)
- [Raindrop](https://app.raindrop.ai/events?org=aaede0ab&event=62a19a88e83ef1bc705c53392eaa491c&tab=tree)
- [LangSmith](https://smith.langchain.com/o/a8e37b69-252c-4f08-a589-c09784f432e1/projects/p/4d325963-780d-4479-b0e9-0667d39072a8)
- Braintrust (project logs view)
- [Langfuse](https://us.cloud.langfuse.com/project/cmuvztlwz059bad0c138utb1q/traces)

LangSmith derives run UUIDs from OTLP span IDs and keeps the original trace ID
in `OTEL_TRACE_ID` metadata. Match on that metadata or the shared thread ID,
rather than expecting its displayed trace UUID to equal the OTLP trace ID.

## Setup tradeoffs and gaps

| Platform | Strengths and supported features | Setup pains and observed limitations |
| --- | --- | --- |
| Lens | Fits the existing LiteLLM gateway; agent trees, timing, errors, token usage, and automated investigations. Plain OTLP integrates with the existing capture code. | Requires ClickHouse for traces and a separate worker for investigations. Trace write access does not imply read access. Tool content needs GenAI tool argument/result attributes. This deployment's inference and trace gateways differ; recorded gateway cost requires receiver access to the matching owned spending records, and trace-based estimates require receiver-side pricing. |
| Raindrop | Interaction/conversation views, tool errors, signals, issue clustering, and feedback workflows. Native SDKs can simplify interaction association. | Two ingestion paths are required: OTLP spans and interaction events with matching IDs. Write and Query API keys are separate; the supplied key succeeded for writes and returned 401 for Query API reads, so verification used the signed-in UI. Hosted event ingestion returned 200 with IDs despite docs describing 204; both are supported. Workflow/agent spans appeared with an `llm` badge in its tree. Issue quality needs representative production traffic; a smoke test cannot establish it. |
| Langfuse | Explicit agent/tool/generation types, sessions, token/cost accounting, prompts, scores, and evals; supports cloud and self-hosting. Existing integration needed little deployment work. | Correct region plus public/secret key pair; v4 metadata must be carried on every observation. Legacy trace reads returned 410, so use Observations API v2 with explicit fields. Custom usage-detail keys can silently suppress price calculation; standard GenAI token fields correctly produced costs. |
| LangSmith | Good thread/turn navigation, typed runs, token/cost estimates, datasets, annotation, evals, and tracing across frameworks. Key plus project header was sufficient. | Its GenAI importer required message `parts`; legacy `content` alone returned 200 but lost model text. An ERROR status without an exception event appeared successful. Both are now mapped correctly. Child spans need their exported parent, and UI IDs differ from OTLP IDs. |
| Braintrust | OTLP/OpenInference mapping preserved hierarchy, messages, tools, errors, and token counts; estimated costs, SQL/BTQL, datasets, experiments, scorers, and production analysis are available. | Requires explicit project routing (`x-bt-parent`); project IDs are safer than names across renames. The UI/query model distinguishes root traces, spans, and event/log children. It decodes JSON-looking scalar outputs (e.g. `10` becomes a number), and cost estimates can populate asynchronously. |

All five receive bounded, sanitized content. System prompts, private reasoning,
images, loaded skills, credential tools, and personal memory tool payloads are
excluded. Model inputs include the last five user messages rather than the full
raw conversation; tool arguments/results have their own spans. Secret fields
and configured tracing keys are redacted. Trace user IDs use the turn author's
SSO email, including Slack accounts linked to Google sign-in. Internal account
IDs and authorization are unchanged. Accounts without an SSO email retain a
stable hashed fallback; previously exported traces are not rewritten. Model JSON
is bounded without cutting its syntax. Timing currently measures full requests,
not time to first token. Separate reasoning-token breakdowns, gateway billing,
feedback, online evaluators, and automated investigations are not configured by
this tracing change. That shared capture policy bounds what any platform can
show; missing private content is not a platform ingestion failure.

## Release and operation

The release is prepared for a manual Render deployment. The new LangSmith and
Braintrust credentials/routing and production environment label are saved in
Render and were read back successfully without changing other environment
variables. Saving them did not deploy the service.

After the release is merged, wait for active turns to settle and use Render's
**Manual Deploy → Deploy a specific commit** with the merged SHA. The persistent
disk causes a stop-before-start restart. Verify `/health`, then run a short
production chat plus follow-up and locate the shared session in all dashboards.
The library tests and verification run establish export behavior before rollout;
production traffic starts using it only after that deployment.

To repeat the isolated check with existing environment credentials:

```sh
uv run python -m scripts.check_trace_exports --send --live-model
```

Without `--live-model`, observations are explicitly labeled fixtures. The script
returns the output directory, trace IDs, and per-queue receipts; it exits nonzero
when a configured receiver has not acknowledged delivery. It does not supply
dashboard read credentials or mistake an HTTP acknowledgment for stored data.
For Langfuse use `/api/public/v2/observations` with bounded timestamps and
`fields=core,basic,io,model,usage`; for LangSmith use `/runs/query`; for Braintrust
query `project_logs('<project-id>')` by session metadata. Raindrop Query API needs
its separate Query key; Lens read APIs need log-read permission.

To disable one destination, clear only its key and redeploy. Pending rows remain
on disk and resume when re-enabled. Rolling back to the previous application
commit leaves the new queue tables harmlessly in place; preserve the disk.

Validation: the 54 tracing/configuration checks and all 128 JavaScript tests
passed. The full Python suite reported 950 passed, 2 skipped, and 2 failures.
Both failures reproduced in an untouched archive of baseline `d2c0e21`:
`test_context_recovers_mention_files_and_only_imports_prior_files_from_invoked_thread[True]`
in `tests/test_slack_files.py`, and
`test_real_temporal_startup_retry_timer_survives_worker_replacement` in
`tests/test_temporal_integration.py`. They are existing failures, not changes
introduced by this tracing release.

## Primary documentation consulted

- [Lens first trace](https://docs.litellm.ai/docs/proxy/lens/first-trace),
  [deployment](https://docs.litellm.ai/docs/proxy/lens/deployment),
  [API](https://docs.litellm.ai/docs/proxy/lens/api).
- [Raindrop OTLP](https://raindrop.ai/docs/sdk/opentelemetry),
  [Python SDK](https://raindrop.ai/docs/sdk/python),
  [HTTP interactions](https://raindrop.ai/docs/sdk/http-api),
  [Query API](https://query.raindrop.ai/v1/docs).
- [Langfuse OpenTelemetry](https://langfuse.com/integrations/native/opentelemetry),
  [read APIs and field groups](https://langfuse.com/docs/api-and-data-platform/features/public-api).
- [LangSmith OpenTelemetry mappings](https://docs.langchain.com/langsmith/trace-with-opentelemetry).
- [Braintrust OTLP](https://www.braintrust.dev/docs/integrations/sdk-integrations/opentelemetry/send-traces-and-logs),
  [attributes](https://www.braintrust.dev/docs/integrations/sdk-integrations/opentelemetry/attributes),
  [query request schema](https://braintrust.dev/docs/kb/btql-post-endpoint-payload-and-response-schema).


## Model usage and cost correlation

Moyai exports [OpenTelemetry GenAI attributes](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/registry/attributes/gen-ai.md) for observed input/output tokens and cache-read/cache-write tokens. Input totals include cached tokens: native Anthropic input, cache reads and cache writes are summed; OpenAI Chat Completions and Responses totals already include cached input. Unknown counters are omitted and explicit zero is preserved. These fields are pricing inputs, not an invoice amount.

`gen_ai.response.id` and `gen_ai.response.model` contain observed provider response values. `litellm.call_id` contains the gateway's `x-litellm-call-id` response header when present; a local Moyai request UUID is never substituted. Receivers can correlate gateway charges using these IDs under their own ownership rules.

OpenAI tier uses the [standard `openai.response.service_tier` attribute](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/registry/attributes/openai.md). Anthropic billing details that do not yet have semantic conventions use these provider-specific extensions when returned:

- `anthropic.response.service_tier`
- `anthropic.usage.cache_creation.ephemeral_5m_input_tokens`
- `anthropic.usage.cache_creation.ephemeral_1h_input_tokens`

The same metadata is exported for Chat Completions, Responses and Messages, including streaming and context compaction. Native response content and private reasoning remain excluded from traces.
