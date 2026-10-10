# Separate inference from app restarts

This opt-in topology isolates sandbox model/tool traffic from the coordinator's
UI/API lifecycle. It does **not** enable mixed-build rollouts, API replicas or
zero-downtime API updates. All processes still require the same full build SHA
and runtime fingerprint. Keep the default topology until the coordinated cutover
and edge routing below are ready.

## Ownership

Set `MOYAI_SEPARATE_BROKER=true` on **every** process:

| Role | Traffic and work |
| --- | --- |
| `coordinator` | UI, API, uploads, webhooks, Temporal wake dispatch, Slack delivery, identity/environment/automation jobs, trace export, session titles and infrastructure accounting. Rejects `/broker/*` with 503 instead of forwarding. |
| `broker` | `/broker/*` and `/health` only. Owns the model gate, run-scoped tools/credentials/Git/attachments, live context, compaction recovery, memory review and model-cost receipt recovery. Connects to Temporal for automation tools, but never dispatches wakes or consumes activities. |
| `worker` | Temporal execution activities and `/health` only, as before. |

The broker has an exclusive PostgreSQL ownership lock, separate from the
coordinator lock. A second broker is refused before inference recovery runs.
The shared policy includes the split setting; a process configured for the wrong
topology, keys, storage, limits or build is refused. An API restart no longer
interrupts pending model requests, running compaction or executing tool receipts.
Restarting the broker still performs that recovery, because its old connections
have ended. Do not overlap broker instances or turn off the ownership checks.

Chat inference, context maintenance, credential inference and memory review keep
one shared `MAX_CONCURRENT_MODEL_REQUESTS` gate on the broker. Session titles and
audio transcription retain their existing separate limits on the coordinator;
this change does not claim those requests survive an API restart. The API's
`/api/admin/capacity` identifies model counters as `source=separate_broker` and
returns `null` for unavailable process-local counters, rather than a false zero.

## Route directly at the edge

Keep the **same public hostname and existing Access applications**. Sandboxes
already call `<PUBLIC_URL>/broker/<run-id>/...`; do not change their origin,
capabilities, service-token headers or audience. Put a more-specific ingress
rule **before** the current catch-all rule:

```yaml
ingress:
  - hostname: moyai.litellm-sandbox.ai
    path: ^/broker(/.*)?$
    service: http://moyai-broker:10000
    originRequest:
      httpHostHeader: moyai.litellm-sandbox.ai
  - hostname: moyai.litellm-sandbox.ai
    service: http://moyai-private:10000
    originRequest:
      httpHostHeader: moyai.litellm-sandbox.ai
  - service: http_status:404
```

These are example origins; use the exact Render private service hostnames. For a
remotely managed tunnel, apply the equivalent ordered published routes in its
dashboard. A browser/API proxy to the broker would keep inference dependent on
the app process and defeats the separation. Never route the broker through the
app, redirect bearer-bearing requests, expose a public origin, or broaden the
Cloudflare Access policy. The broker still verifies the broker-audience assertion
and the active run capability; it rejects workspace APIs and all WebSockets.
Keep the existing `moyai-tunnel` connector on its independent lifecycle; restarting
the connector alongside the API could still break those connections.

## Coordinated first cutover

1. Merge and test the release without changing production roles. With
   `MOYAI_SEPARATE_BROKER=false`, existing coordinator/worker behavior remains.
   This release adds a fingerprint field even in the default topology, so deploy
   matching coordinator/worker builds together using the existing drained rollout.
2. Prepare one private broker service in the same workspace/region, with the same
   code, PostgreSQL schema, S3 destination, stable keys, public URL, provider
   configuration, Temporal target and limits. It needs no persistent disk.
   Keep auto-deploy off; provision paid compute only after approval. Use
   `RENDER_MIGRATION_STAGE=true` until activation. Preserve the original disk.
3. Drain new execution on the old worker, allow in-flight inference/tool work to
   settle, and stage/stop all old workers and the old coordinator. Retain queued
   turns and never cancel user work just to make the cutover easier.
4. Set the same full `MOYAI_BUILD_SHA` and `MOYAI_SEPARATE_BROKER=true` on all three
   roles. Start one coordinator first to establish the policy, then one broker.
   Verify broker readiness and exactly one broker ownership lock. Preserve
   production budgets (currently 100 runs / 1,000 pending / 32 model requests).
5. Switch the ordered edge routes. Verify employee access, a broker request from
   an existing sandbox, an authorized tool read, a saved file, invalid-capability
   rejection, broker-to-workspace isolation and signed webhook handling. Requests
   incorrectly routed to the coordinator fail closed instead of opening a second
   inference gate.
6. Activate the matching workers and remove the execution drain. Run a real turn,
   then restart **only the coordinator at the same build** during a live model
   request. Check its stream, request accounting, saved result and latency.

With pool size 8, three processes require up to 27 database connections, plus
administrative reserve. One overlapping worker adds 9. This does not authorize
overlapping coordinator or broker instances. Keep Render readiness at `/health`;
broker/worker readiness also requires a connected Temporal client and valid DB
ownership. No production deployment or paid service is created by this PR.

After writes reach this topology, rollback is another drained, coordinated switch
of all roles and the edge route. Keep PostgreSQL/S3 and the original keys; never
resume the retained stale SQLite database. Do not flip just one split flag.

## Reproduce without cloud spend

Use a disposable PostgreSQL database, then run from the repository root:

```sh
export MOYAI_TEST_POSTGRES_URL=postgresql://postgres:local-test@127.0.0.1:5432/moyai_test
uv run --frozen pytest -q tests/test_separate_broker.py tests/test_runtime_scaling.py
uv run --frozen python -m scripts.broker_restart_probe --output /tmp/moyai-broker-probe
```

The probe starts real local Temporal, PostgreSQL-backed coordinator and broker
processes and an HTTP streaming model fixture. It receives the first native
Responses token, verifies the one-call limit, stops the API, starts its
replacement, and receives the final token on the **original** stream. It requires
one provider call and one completed accounting row. The JSON report and cast are
actual execution output. The model fixture incurs no provider spend; this is not
a live Render/Cloudflare or paid-model rollout test.

Remaining work: define and test a code-owned compatibility contract for different
API builds, move schema changes out of ordinary replica startup, coordinate
singleton app jobs, remove API disk dependence, and implement readiness/traffic
handoff plus graceful draining for overlapping API instances.
