# Separate inference from app restarts

This opt-in topology isolates sandbox model/tool traffic from the coordinator's
UI/API lifecycle. The base auto-schema topology requires matching builds and
an exclusive broker. The [API replica extension](api-replicas.md),
[verified broker overlap](broker-overlap.md), [compatible workers](worker-releases.md)
and [coordinator handoff](coordinator-handoff.md) provide the mechanisms used by
the full [rolling deployment procedure](rolling-deploy.md). Keep the default
topology until the one-time migration, role routing and activation are ready.

## Ownership

Set `MOYAI_SEPARATE_BROKER=true` on **every** process:

| Role | Traffic and work |
| --- | --- |
| `coordinator` | Webhooks, Temporal wake dispatch, Slack delivery, identity/environment/automation jobs, trace export, session titles and infrastructure accounting. Also UI/API/uploads in the base topology; hooks only after handoff is enabled. Rejects `/broker/*` with 503 instead of forwarding. |
| `broker` | `/broker/*` and `/health` only. Owns the model gate, run-scoped tools/credentials/Git/attachments, live context, compaction recovery, memory review and model-cost receipt recovery. Connects to Temporal for automation tools, but never dispatches wakes or consumes activities. |
| `worker` | Temporal execution activities and `/health` only, as before. |

With `MOYAI_SCHEMA_MODE=auto`, the broker has an exclusive PostgreSQL lock and
a second broker is refused before recovery. In verified-schema mode, compatible
brokers share broker membership, use globally coordinated model slots, and elect
one maintenance owner. See [broker overlap](broker-overlap.md).
The shared policy includes the split setting; a process configured for the wrong
topology, keys, storage, limits, prepared-pool settings or build is refused. Only
execution workers maintain prepared sandboxes; the broker never starts the pool
or subscribes to execution wake notifications. An API restart no longer
interrupts pending model requests, running compaction or executing tool receipts.
An auto-schema broker restart performs global recovery after its exclusive
predecessor exits. A verified broker recovers only requests belonging to proven
dead owners and leaves another live broker's requests untouched. Never disable
ownership checks to force a rollout.

A shared-policy, protocol or schema change requires all runtime owners to stop
before the new coordinator publishes a different contract. The coordinator checks this before schema
writes, keeps joining instances out until component setup finishes, and only
then publishes the new policy. A reviewed compatible build replacement instead
joins the existing contracts without replacing the baseline policy. This needs
verified schema and, for coordinator overlap, `MOYAI_COORDINATOR_HANDOFF=true`.

Chat inference, context maintenance, credential inference and memory review keep
one shared `MAX_CONCURRENT_MODEL_REQUESTS` budget across verified brokers; an
auto-schema broker retains its local gate. Session titles and
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

For the base three-service topology, the
[production deployment button](production-deploy.md) supports subsequent
coordinated releases. It verifies and stages the
broker before updating the coordinator, then verifies the new broker before
resuming the worker. It does not change the split flag or edge route and does
not perform this first topology switch. Clear the broker's `MAINTENANCE_DRAIN`
flag during activation so all three services form a clean release baseline.

With database pool size 8, three processes plus the coordinator's wake listener
require up to 28 database connections, plus administrative reserve. One
overlapping worker adds 9. In verified broker mode, add one dedicated slot-lock
connection per broker (not per model call), plus 9 for each API at pool size 8.
A verified broker replacement therefore adds up to 10 connections. Use the
rolling runbook before enabling coordinator/broker overlap. Render [private services use TCP
health checks](https://render.com/docs/health-checks), not an HTTP health-check
path. The broker therefore waits for Temporal and valid database ownership before
ASGI startup completes and Uvicorn opens its port. After
`TEMPORAL_STARTUP_TIMEOUT_SECONDS` (default 60, range 1–600), unsuccessful startup
exits and releases the ownership lock. Verify `/health` explicitly from the private
network during cutover; broker/worker HTTP readiness also checks Temporal and DB
ownership. A later dependency failure returns 503 but does not close the TCP port;
the existing release checks must observe HTTP readiness. No production deployment
or paid service is created by this PR.

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

For the complete rolling topology, migrate offline to the selected schema
(currently revision 15), activate the dedicated API and its edge route, and enable
coordinator handoff. All participating services need diskless verified shared
files and the configured shutdown allowance. The rolling controller verifies
replacement readiness and old-owner retirement. Broker HTTP draining is bounded
to 270 seconds inside Render's required 300-second allowance; requests exceeding
that deadline may be interrupted, and ambiguous external actions are not replayed.
A successful local rehearsal is evidence of the mechanism, not proof that the
production routing and activation have already been completed.
