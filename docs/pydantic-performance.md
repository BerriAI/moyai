# Native runtime performance

The Pydantic execution core retains separate API, coordinator, broker, and
execution services. Within an execution process, Temporal keeps the original
queue available for workflow tasks and historical activity commands, with
separate model, computer/tool, and control activity queues for new executions.
Long computer commands must not consume the capacity needed to admit another
model request, check authority, publish a result, or stop work.

Activity routing is recorded with a Temporal patch marker. Existing histories
retain their original activity names, inputs, and queue; the original queue
continues polling. The entire worker group registers one canonical process owner
before polling and shuts down as one group. This change does not make native
rolling releases supported: use the existing guarded maintenance deployment.

Native participant protocol 2 and the v14 execution-compatibility receipt mark
this upgrade. The receipt adds no application tables, but prevents the supported
release planner from accepting an older v13 candidate after new execution
histories can exist. Earlier receipts remain unchanged. Old-history replay on
new workers is supported; it is not evidence that old workers can consume new
histories. After provisioning v14, use a compatible roll-forward candidate.

## Computer startup

Fresh text-only runs load packaged skills from an immutable catalog captured
before agent construction. They defer the SDK's eager computer acquisition and
Coder repository-context loading until a computer operation is needed.
The actual workspace acquisition and durable operations remain SDK-owned.
Repository instructions are supplied to the model before its first computer
action executes; a parallel batch cannot execute an uninformed write.
Existing workspace references, restoration, and delegated workspace sharing
retain their original authority boundaries.

Temporal patch markers retain the previous startup commands when replaying old
histories. The adapters check the pinned SDK capability shapes and require review
if an SDK upgrade changes those extension contracts.

## Capacity settings

Existing-product configuration passes these settings into the native runtime:

| Setting | Default | Scope |
| --- | --- | --- |
| `TEMPORAL_WORKER_ACTIVITIES` | 120 | Activity slots in each independently polled queue |
| `TEMPORAL_WORKFLOW_CACHE_SIZE` | 200 | Temporal worker workflow cache; zero disables caching |
| `TEMPORAL_DISPATCH_BATCH_SIZE` | 200 | Maximum newly reserved turns and bounded dispatch page |
| `TEMPORAL_DISPATCH_CONCURRENCY` | 10 | Concurrent workflow-start RPCs |
| `MOYAI_DATABASE_POOL_SIZE` | 8 | Connections in each native SQLAlchemy, canonical-reader SQLAlchemy, Harness, and canonical store pool |

SQLAlchemy overflow is disabled. The database setting is **per pool, per process**;
it is not a cluster-wide connection limit. Budget the sum across all deployed
roles and replicas, plus dedicated notification and ownership connections.
The existing broker's `MAX_CONCURRENT_MODEL_REQUESTS` remains a separate shared
model-admission limit.

PostgreSQL producers issue transaction-bound notifications. Rollback discards
them; commit wakes the coordinator. Notifications only shorten the wait:
persisted rows remain authoritative, and bounded polling recovers missed hints
or listener outages. Full successful batches drain without a polling sleep.
Network dispatch happens after the reservation transaction releases its locks.

## Read paths

Established-user GET requests use current product authority and a read-only
native profile projection. They do not update the cached profile or acquire its
writer lock. First projection and mutations retain their locked paths. Native
disable/epoch checks and response-time canonical revocation checks remain active.

Combined history reads release their native connection before rechecking
canonical access, which itself reads native account controls. This permits a
one-connection pool without borrowing recursively from it.

Live streams authorize at admission and after each event/idle wait. Every
delivered event still receives a fresh check; adjacent iterations no longer
repeat the same pre-wait check.

## Qualification

Use a disposable PostgreSQL destination:

```sh
uv sync --project native --frozen --extra product
cd native
MOYAI_TEST_POSTGRES=postgresql://... uv run pytest -q \
  tests/test_pool_budget.py tests/test_product_read_contention.py \
  tests/test_dispatch_capacity.py tests/test_temporal_capacity.py \
  tests/test_bundled_skills_startup.py tests/test_live_stream.py \
  tests/test_product_slack_contention.py
```

The contention checks keep a real PostgreSQL writer or pool connection occupied
while exercising another request. They distinguish waiting for a database
resource from blocking the event loop. Temporal checks cover saturated tool
capacity, control/model progress, and replay with the production interceptor.
These are supplementary tests, not a substitute for actual application proof.

Before release, run baseline and candidate through the supported existing-product
local entrypoint with isolated PostgreSQL, S3, and Temporal, and the scoped E2E
launcher. Exercise the same authenticated reads under writer contention, a burst
of new conversations, a fresh text-only request, and a computer write/read plus
follow-up using a real configured model. Retain exact revisions, raw HTTP/SQL
observations, and real UI recordings. Local samples are not production
percentiles or a claim of fourfold throughput from four worker pools.
