# Trace native Moyai runs in Lens

The native runtime can export its Pydantic AI agent, model and tool spans to
Lens. Tracing is off until both settings are present:

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
names and timings. They do not copy prompts, completions, private memory,
capability content, credential tool arguments, binary attachments or full model
request parameters. Existing gateway model logging supplies the request and
response content according to the gateway's own configuration.

The exporter uses a bounded background queue and a three-second request timeout.
An unavailable collector does not fail or retry model inference. Shutdown
flushes the remaining telemetry. The Pydantic AI Temporal plugin prevents
workflow replay from emitting duplicate spans; telemetry is configured on a
local provider without changing other agents' global instrumentation.

## Receiver compatibility

Lens 0.2.0 predates the gateway-forwarding ownership fix in
[BerriAI/lens#183](https://github.com/BerriAI/lens/pull/183). Use a Lens build
containing that fix before enabling gateway-forwarded native tracing. On 0.2.0,
native spans sent through the gateway are accepted without their owner and
appear separately from the model spans. An HTTP 200 is not sufficient
verification.

After deployment, run a small real Moyai task that calls a tool. Find its exact
turn ID in the `moyai` agent span's metadata, then confirm the same trace
contains the tool and gateway model spans, their input/output, and matched
request cost. Test replay separately and verify that it adds no duplicate spans.
