# Durable inference receipts (staged; disabled by default)

Render currently owns the gateway HTTP connection. If it shuts down after the
request is billed but before saving the response, Temporal can resume the session
but cannot reconstruct an unrecorded response header. This change moves that
connection to a trusted Modal function that outlives the Render web process.

## Lifecycle

1. The sandbox relay generates one ID per local model request. It buffers the
   complete broker response and retains this ID across reconnects to Render.
2. Render atomically admits a job and records its original key, model, session,
   message and requester in SQLite. The encrypted input is the durable dispatch
   inbox; it contains no gateway credential. Duplicate input IDs return the same
   job, and changed input with the same ID is rejected.
3. A Temporal workflow recovers each job independently of session cancellation.
   The normal HTTP path can dispatch/await that same job immediately, without
   waiting for the workflow's recovery polling interval.
4. A separately deployed, trusted Modal function atomically claims the job in
   Modal's durable Dict and makes one non-streamed gateway call using the existing
   Moyai virtual key. It saves the final cost headers immediately, then saves the
   complete answer and usage as an encrypted durable receipt. Receipts up to 1 MiB
   are available from the Dict immediately; the function continues archiving to
   a dedicated Volume independently of the caller. Larger receipts use the Volume
   directly. All upstream automatic retries are disabled.
5. Render imports the receipt by updating the original accounting row in one
   transaction. Repeated receipt imports never add another charge. Temporal
   retries retrieval and import after Render returns, even if the session stopped.
   The input envelope is removed after import to avoid retaining a growing copy
   of the entire prompt for every model call.
6. The relay receives the saved response (adapted to SSE when requested). The
   same job ID cannot invoke another inference.

Temporal history contains only job IDs and completion flags. Prompts, private
skills, outputs and receipts are encrypted before crossing the Modal control
plane or entering the receipt Volume. The trusted service does not run repository
code or agent tools and receives no Slack, GitHub or Google credentials. Hermes
sandboxes never receive the gateway key or receipt encryption key.

## Latency and limits

Dispatch, the durable claim, receipt writes and retrieval add real latency. A cold
Modal container adds startup latency. Spend-dashboard reconciliation and the
Volume archive do not need to finish before delivery, but the first durable Dict
write does. Generation was already buffered before this change to obtain the
final LiteLLM cost header.

The initial prototype used filesystem commits for every marker and took 5–7 s
warm in a synthetic cloud test. The revised hot path uses the durable Dict; see
`scripts/check_inference_modal.py` for a reproducible, no-LLM cloud check. These
measurements are not a production latency guarantee. A synthetic four-call check
on October 1, 2026 measured 0.35–0.64 s warm and 3.47 s on the first cold call
from a local caller, with zero real gateway requests. It verified that each
receipt was available before its archive finished and a duplicate made no new
synthetic gateway call. Render overhead, large payloads and load were not measured.

The trusted function handles up to four concurrent calls per container, scales
to zero after an idle minute, and permits at most 25 containers (100 calls).
No GPU is needed; inference runs at the existing gateway. Session sandboxes are
separate. These are maximum limits, not preallocated capacity.

The normal path checks the durable result every 250 ms and also observes Modal
function completion. It does not wait for the archive for small receipts. The
fallback Temporal poll is every 10 seconds while a request is unresolved; it does
not delay the normal response. Unknown outcomes are checked hourly. The existing model-concurrency
limit applies to durable pending jobs, including across Render restarts.

A job is eligible for first submission for one hour. Modal's atomic Dict claims
expire after seven days of inactivity, so an expired claim cannot revive an old
submission: the authenticated envelope has already expired. A lost spawn
acknowledgment can create duplicate Modal function invocations, but only one wins
the claim and may call the gateway. The Dict also holds received cost headers
and small results, with the same seven-day inactivity expiry. Completed Volume
archives remain available beyond that interval. If the worker dies during archive
creation, its Dict receipt is still recoverable for seven inactive days; a longer
outage without an archive cannot be promised recovery. Recovery checks stop after
30 days, leaving unresolved costs explicitly unknown.

This is not exactly-once external inference. If the inference worker itself dies
after billing but before any receipt is committed, the job remains unknown and
is not blindly sent upstream again. Exact recovery of that case requires a
provider/gateway idempotency-and-result API, a reliable billing callback, or
reconciliation with its authoritative ledger. `x-litellm-call-id` is correlation,
not an assumed gateway idempotency guarantee. No gateway changes or spend-log
permissions are required or made here.

The older missing $0.251452 charge is not backfilled by this change.

## Staged rollout

1. Keep `DURABLE_INFERENCE_ENABLED=false` during deployment preparation.
2. Generate a dedicated Fernet key and store it privately as
   `INFERENCE_ENCRYPTION_KEY` in Render and in Modal secret `moyai-inference-v1`.
   That Modal secret must contain only `INFERENCE_ENCRYPTION_KEY`,
   `LITELLM_API_KEY` (the current Moyai key) and `LITELLM_API_BASE`.
3. In Modal workspace `litellm`, deploy `deploy_inference.py`. This creates app
   `moyai-inference`, Volume `moyai-inference-results-v1` and Dict
   `moyai-inference-ledger-v1`. Do not run `deploy_modal.py`, which deploys the
   legacy web app. Session sandboxes still belong to `hermes-workspace`.
4. Deploy the matching Render code. Keep `TEMPORAL_ENABLED=true`. Confirm both
   `SessionWorkflow` and `InferenceWorkflow` are registered with the worker.
5. Let active turns using the older relay finish before enabling admissions.
   Enable `DURABLE_INFERENCE_ENABLED=true` and start a fresh session turn so its
   sandbox receives the new relay and protocol flag. Verify a controlled request
   across a web-worker restart, exact cost import and one upstream call.
6. Keep the independent Modal service running during web deploys. Drain its
   active calls before worker upgrades or rotating its gateway credential.
   Jobs bind the original key hash/base; a mismatch is rejected before billing.

For rollback, disable new admissions but retain this code version, Temporal,
receipt storage and the encryption key until existing jobs recover. Returning to
a pre-protocol server while new relays are active is unsafe: it would not honor
request IDs. Do not delete claims/results or rotate the receipt encryption key
while jobs remain recoverable. Volume/SQLite receipts need a coordinated retention
policy before automated pruning; this version does not silently delete them.

References: [Temporal activity idempotency](https://docs.temporal.io/activity-definition#idempotency),
[Temporal async activity completion](https://docs.temporal.io/develop/python/asynchronous-activity-completion),
[AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
