# Known tool rejections

An HTTP 403 alone does not prove that a tool was never dispatched. A handler may
commit an action and then fail an access recheck. Such responses remain uncertain.

The actual broker `PrivateExportDenied` and `ToolRejected` guards now emit an
additive response signal:

- `X-Moyai-Tool-Rejection-V1`: one of those two exact guard types.
- `X-Moyai-Tool-Request-ID`: the submitted 32-character tool request identity.

The status and response body stay unchanged. The broker strips these reserved
headers from ordinary HTTP exceptions, so an upstream error cannot impersonate
one of the guards. DeepAgents accepts the signal only on `/tools/call` and only
for the matching request, including a request transported in multiple parts.
It never reads an error body to decide dispatch status.

The resulting non-retryable `ToolNotDispatched` failure carries sanitized
exception/stage/status/identity facts through Temporal. Broker, runtime and
terminal records agree on rejected, not dispatched, and not retryable. Ordinary
403s and failures after a write remain `UncertainToolOutcome`; no POST is replayed.
Confirmed receipt persistence remains outside the dispatch exception handler.

## Correlation

No new UI identity field is required. Within the same explicit `turn_id`, runtime
and broker `tool_failure.request_id` is the request hash. A corresponding
ActivityReporter `call_id` is `<request-hash>:<model-call-id>`. An alias is safe to
use only when it identifies one unique call in that turn; an existing terminal
receipt remains authoritative.

## Actual local application proof

Run the same workflow through the actual baseline and final Moyai applications
with normal service authentication, real Temporal and a configured real model.
These scenarios do not require cloud computers or any secret to be submitted:

1. **Ordinary session:** have the model invoke `computer_handoff` once with
   `{"reason":"   "}`. The JSON string reaches the handler, whose stripped
   validation raises `ToolRejected` before a handoff event or computer wake.
2. **Private session:** `credentials_request` is advertised even though ordinary
   export tools are hidden. Have the model request a synthetic generic credential
   with `{"provider":"generic","name":"local-rejection-proof","format":"env",
   "reason":"Verify private guard only; no secret will be supplied.",
   "request_key":"local-rejection-proof"}`. The existing credential sink raises
   `PrivateExportDenied` before creating a credential request or audit entry.
   A private-session-capable individual identity is required. If the test uses
   the app's normal session signer to bootstrap a synthetic individual identity,
   identify that substitution explicitly; it does not verify Google SSO.

Verify each tool is actually invoked by the real model, then compare the broker,
runtime, final diagnostics and visible history. Before: a known rejection is
followed by an uncertain runtime failure. After: all records agree on rejection
and no dispatch, with the same HTTP status and no side effects. TestClient,
mock transports and direct fixture stores are supplementary regression coverage,
not substitutes for that deployment proof. The parent task owns credentials,
services, captures, and proof on the final integrated revision.
