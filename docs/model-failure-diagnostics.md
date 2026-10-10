# Model failure diagnostics

Failed answers offer **Failure details** with the recorded SDK category, provider
code, HTTP status, request stage and correlation IDs, when available. Details
belong to the answer's original input. A queued follow-up does not inherit them.

Messages, Responses and Chat Completions use the same safe diagnostic fields.
Authenticated failures before inference admission emit a broker span and a
`broker_failure` event without adding a model request or charge. Stages are
`validation`, `context_preparation`, `admission`, `upstream` and `stream`.
Unauthorized requests remain in request-level broker logs only.

Model and agent spans export an explicit error description and exception event
to all configured trace destinations. Error fields use `moyai.error.*`; model
output remains model output. Request IDs connect the record to broker and gateway
logs. A late broker failure retains its original turn even after another input
has been claimed.

Only known structured provider codes and SDK categories are retained. Unknown
codes stay unknown. Provider bodies, SDK result strings, exception messages and
stderr are excluded. The details can identify a rejection but cannot establish
which part of the accumulated request caused it.

A successful HTTP header followed by a stream failure is recorded as
`response_status`, separate from an HTTP error status. It is response context,
not the cause of the failure. Both Claude and Codex native categories are shown.

These observations do not change retry eligibility, inference accounting or tool
replay. Intermediate failures can remain in the durable event history after a
successful recovery; successful answers do not display them as terminal errors.
