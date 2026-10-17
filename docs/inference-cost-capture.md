# Inference cost capture and receipt recovery

Moyai records the final cost reported by the configured gateway. It does not
calculate a replacement price from current model rates or interpret a missing
cost as zero.

## Large responses

Responses terminal events can repeat model output, instructions, and tool
definitions. A terminal event larger than 256 KiB must not lose its usage merely
because that unrelated content is large.

Usage capture incrementally validates JSON and retains only the allowlisted
request identity, model, timestamp, usage counters, and cost fields. Model
content is skipped. The selected metadata remains bounded to 256 KiB, nesting
to 64 levels, and individual numeric tokens to 256 characters. Compressed
responses are decoded in bounded chunks. Invalid JSON and oversized selected
metadata remain explicit coverage errors.

The bytes delivered to the model SDK are unchanged. Capture does not introduce
another model request, gateway lookup, database write, or awaited operation on
the response path. Parsing still takes CPU time proportional to the response.

## Recovering a missing cost

With `LITELLM_SPEND_RECOVERY_ENABLED` enabled, the existing background recovery
worker queries the recorded gateway/key scope for an exact request receipt.
It accepts only validated, unambiguous charge evidence and never repeats
inference to recover a cost.

Receipt lookups denied with HTTP 401 or 403 now retry with exponential backoff
from one minute to one hour. Requests previously parked with
`receipt_access_denied` become eligible again. The gateway credential must
already have permission to read the matching receipts; this change does not
grant it additional permissions or use an administrator credential.

Opaque receipt IDs up to 4,096 characters are accepted. IDs longer than the
existing 200-character reference column are represented there by a SHA-256
reference. Deduplication continues to hash the full original receipt ID
together with its gateway and key scope.

## Coverage and rollout

Failed product turns with no SDK usage and no dispatched attempt in either
durable ledger do not create fictitious missing-cost requests. They remain
visible as turns and are counted as `undispatched_failed_turns`. Standalone or
historical turns without that admission evidence, and any turn with an observed
or uncertain dispatched attempt, retain their unknown-cost treatment.

This change requires no schema migration. Deploy through the normal release
process, then verify that new large responses retain their final cost and
usage. It also works after the separate billing-storage consolidation.

Deploying the fix does not guarantee that every historical missing amount is
recovered. Recovery still needs an exact, accessible gateway receipt and the
original recorded key scope. Ambiguous receipts, absent evidence, mismatched
scope, and unverified zero-valued logs remain unresolved. Historical requests
that bypassed Moyai accounting need a separate, reviewed reconciliation.
