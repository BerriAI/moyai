# Trace native Moyai runs in Lens

The native runtime exports Pydantic AI agent, model and tool spans, Temporal
workflow/activity spans, and FastAPI request spans to Lens through standard OTLP.
Tracing is off until both settings are present:

```sh
export LITELLM_TRACE_ENDPOINT="https://your-gateway.example/v1/traces"
export LITELLM_TRACE_API_KEY="$LITELLM_API_KEY"
uv run --frozen moyai dev
```

For a gateway connection, use the same gateway key as inference. The gateway
authenticates the export and assigns the spans to that key's owner, so its model
spans and Moyai's native spans share a trace and access scope. Keep the complete
endpoint path, including any deployment prefix. A direct Lens endpoint instead
requires a Lens ingestion key and records a separate ingestion scope.

In a split deployment, configure the endpoint and tracing key on the API,
coordinator and workers through the deployment's secret mechanism. Workers
propagate W3C trace context through Temporal and the authenticated model broker.
The broker forwards only a valid `traceparent`, without forwarding arbitrary
headers or baggage.

Native spans include the agent name, session and turn IDs, model usage, tool
names and timings. Prompt and response capture is off by default. To show
conversation text in Lens, explicitly set this on the API, coordinator and workers:

```sh
export LITELLM_TRACE_INCLUDE_CONTENT=true
```

This records user and assistant text, tool-call arguments and results, and
failure details for new runs. Tool content appears in both the conversation
and the tool's span. Failed calls retain their error message and stack trace;
the exception type and message are also span attributes for receivers that
do not retain events.

System instructions, injected memory and saved private preferences, private
memory/credential tool payloads, reasoning, media, deferral metadata, and full
model request parameters remain excluded. Configured application credentials,
credential fields in structured payloads, and recognizable credentials in text
are redacted. User-supplied text and tool results are still conversation data: enable this only
where the Lens access and retention policy is appropriate for that content.
This does not change the gateway's independent logging policy or backfill old
traces.

Logfire's default scrubbing remains enabled. Tool payloads keep diagnostic prose
such as "authentication failed" while credential values are redacted. Only a valid UUID in the exact
`metadata.moyai_session_id` field is exempted from its `session` pattern, so the
application's session can be correlated without allowing session cookies or
other credential fields through.

HTTP tracing uses the official OpenTelemetry FastAPI instrumentation. It keeps
route templates, methods and response status, without request/response bodies,
headers or raw URLs. Exception types and error status are always retained.
Exception messages, stack traces and status descriptions follow the content
flag and credential redaction policy above. Static assets and health endpoints are
excluded, as are internal ASGI send/receive spans.

## Computer and task lifecycle

Pydantic AI owns agent/model/tool instrumentation, and its Temporal plugin owns
workflow/activity spans, trace propagation and replay suppression. Moyai adds
committed task state to existing activity spans and small domain spans for
computer health checks, checkpoint capture, allocation and desktop startup.
There is no extra agent wrapper, telemetry workflow activity or stored trace
context.

On a computer reconnect, the FastAPI request span contains the recovery outcome.
Its child spans show the old computer's observed state and the provider operation.
A `moyai.computer.recovered` event is emitted only after the replacement commits;
it includes the previous and replacement workspace IDs and whether files were
restored or a fresh computer was allocated. A rejected or failed recovery never
emits that success event.

Later requests have their own trace. Correlate them with the original run using
`metadata.moyai_session_id`, `moyai.turn.id` and, where the original task scope
is available, `gen_ai.conversation.id`. Do not hold a trace open across a human
wait or treat a previous trace as the parent of a new user action.

Waiting for browser login, user input or approval is a normal task state, not
an error. Settlement spans include `moyai.task.state`, `moyai.wait.reason`,
`moyai.computer.keepalive_eligible` and any checkpoint warning. A failed snapshot
does not turn an already saved answer into a failed task. Keepalive activities
record their result and configured idle timeout without repeating agent work.

A health check observes a stopped sandbox when the provider is queried. It does
not establish the exact death time or prove that idle timeout caused it. There
is no new polling service for paused tasks: detecting the moment a sandbox dies
would require provider lifecycle telemetry or an independent observer. Lens
findings and alert rules are separate from this instrumentation.

The exporter uses a bounded background queue and a three-second request timeout.
An unavailable collector does not fail or retry model inference. Shutdown
flushes the remaining telemetry. The Pydantic AI Temporal plugin prevents
workflow replay from emitting duplicate spans; telemetry is configured on a
local provider without changing other agents' global instrumentation.

## Instrumentation contract

Use the framework integrations rather than creating replacement agent, model or
tool spans. Pydantic AI's `InstrumentationSettings` owns their serialization;
Moyai uses format version 6 so tool responses have `role="tool"`. The SDK emits
`gen_ai.operation.name`, `gen_ai.tool.name`, `gen_ai.tool.call.id`,
`gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`. Arguments and results
are opt-in content under the
[OpenTelemetry tool execution conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/execute-tool.md).
Failed spans retain `ERROR` status and `error.type`; successful tool spans do not
gain an error status. Agent usage retains the SDK's aggregate fields so it is not
counted again as model usage.

The application-specific customization is its privacy policy and two compatibility
adaptations:

- Temporal 1.34 suppresses spans when `record_exception` sees Pydantic AI's handled
  `ToolFailed` or `ModelRetry` (including their internal wrappers), mistaking them for workflow-task failures.
  A Logfire exception callback uses OpenTelemetry's own exception serializer for
  those exception families inside workflows. It adds the event to the SDK span and keeps
  Temporal's replay guard and workflow failure semantics intact.
- The current Lens forwarding path does not retain exception events. Moyai
  mirrors the sanitized exception type/message onto the existing span and derives
  `error.type` when absent. The original exception event and stack trace remain
  in OTLP; this creates no extra error spans.

GenAI conventions are still in Development. Exception events follow the installed
SDK's supported format; migration to the newer exception log signal should happen
with SDK and receiver support, not by introducing a separate Moyai telemetry format.
See [Pydantic AI instrumentation settings](https://ai.pydantic.dev/api/models/instrumented/)
and [OpenTelemetry exception migration guidance](https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-spans/).

## Receiver compatibility

Lens 0.2.0 predates the gateway-forwarding ownership fix in
[BerriAI/lens#183](https://github.com/BerriAI/lens/pull/183). Use a Lens build
containing that fix before enabling gateway-forwarded native tracing. On 0.2.0,
native spans sent through the gateway are accepted without their owner and
appear separately from the model spans. An HTTP 200 is not sufficient
verification.

After deployment, run a small real Moyai task that calls a tool. Find its exact
turn ID in the `moyai` agent span's metadata, then confirm the same trace
contains the tool and gateway model spans, permitted conversation text when
enabled, and matched request cost. Reconnect the task's computer and find its
separate request trace through the session ID. Test replay separately and verify
that it adds no duplicate spans.
