# Compatible API deployments on Render

After the one-time topology activation, use **Actions → Deploy production → main →
release_type: api**. Start with **preflight_only** checked. The selected main commit
must have passing CI and a reviewed API compatibility declaration. This updates
only `moyai-api`; the coordinator, broker and worker keep their deployments,
runtime ownership, queues and in-flight work.

Merging this workflow does not create a service, migrate the database, change
Cloudflare routing or activate the topology. The current three-service topology
continues using `release_type: coordinated`. Once `MOYAI_API_SERVICE_ID` is set,
the coordinated mode refuses to run: backend changes need a separately planned
maintenance transition including every API owner.

## One-time activation

Do this as a planned production transition, after both the API compatibility
support and this workflow are merged. Adding `moyai-api` is a new paid Render
service and requires the account owner's cost approval before creation.

1. Follow [schema migrations](schema-migrations.md) to migrate to the revision
   required by the selected source (revision 3 when introduced). Drain and stop
   **all** runtime owners first. Schema migration is offline; do not add it as a
   pre-deploy command to the API service. Preserve the existing keys and data.
2. Start the coordinator, broker and existing worker on the same reviewed build,
   all with `MOYAI_SCHEMA_MODE=verify` and `MOYAI_SEPARATE_BROKER=true`. The
   coordinator must publish the API compatibility contract first. Verify normal
   sessions and shared file reads/writes before continuing.
3. Create the private API service with the configuration below. Privately copy the
   existing shared, auth, provider and feature settings; never regenerate signing
   or encryption keys. Start in maintenance staging while configuring it. Use the
   reviewed commit containing this workflow's health identity and shutdown support
   for the initial API deployment. After activation both release flags must be
   false. The API's ephemeral directory is a cache; uploaded/saved files must
   already be verified in shared object storage.

   | Setting | Required value |
   | --- | --- |
   | Name / type / workspace | `moyai-api` / private service / same workspace as existing roles |
   | Region / source | Oregon / `BerriAI/moyai`, `main` |
   | Runtime / Dockerfile / context | Docker / `./Dockerfile` / `.` |
   | Start command / pre-deploy command | Both empty; use the image entrypoint; no sandbox builds or migrations |
   | Instances / autoscaling / auto-deploy | One / disabled / off |
   | Persistent disk | None |
   | Maximum shutdown delay | 300 seconds (`serviceDetails.maxShutdownDelaySeconds` in the Render API) |
   | `MOYAI_RUNTIME_ROLE` | `api` |
   | `MOYAI_SCHEMA_MODE` / `MOYAI_DATABASE_INITIALIZE` | `verify` / `false` |
   | `MOYAI_SEPARATE_BROKER` / `TEMPORAL_ENABLED` | `true` / `true` |
   | `MOYAI_BUILD_SHA` | Full immutable deployed commit |
   | `RENDER_MIGRATION_STAGE` / `MAINTENANCE_DRAIN` | `false` / `false` once activated |
   | `DATA_DIR` | Dedicated ephemeral directory, e.g. `/var/data/moyai` |

   Budget for two API instances during overlap, each with up to
   `MOYAI_DATABASE_POOL_SIZE + 1` database connections. Keep the existing tunnel
   connector independent of API deployments.
4. Verify the API's authenticated session access and saved files, and check HTTP
   `/health`, including build/owner headers, from the private network. Route
   `/broker` and its descendants directly to the existing broker, `/hooks` and
   its descendants to the coordinator, and all other workspace traffic to the
   **API service's stable private address**. Keep existing Access policies. Do
   not address an individual container or change these routes on each release.
5. Add `MOYAI_API_SERVICE_ID` as a **variable** in the GitHub environment
   `moyai-production`. The existing deployment API and SSH credentials need access
   to the new service. Run `release_type: api`, `preflight_only: true`; require a
   `preflight_passed` receipt before the first release.
6. Rehearse a reviewed compatible API change against production: keep a real
   session/model request active, deploy the API, verify the stream and worker
   continue, check signed-session/file access, and test event/socket reconnects.
   Record latency/errors and confirm the old API owner exits. Local simulations
   do not establish Render load-balancer behavior or production performance.

## Release checks and native overlap

The workflow pins the commit selected at button press and waits for CI. Before
any mutation it inspects all four services, requires exactly one runtime owner
per role, verifies HTTP readiness and saved environment digests over authenticated
SSH, and computes the candidate's **effective Settings** in an isolated process.
Its API protocol, schema and shared-settings fingerprint must match the running
coordinator's published contract. A different API build SHA is allowed; background
builds must still match one another.

The protocol revision is a reviewed compatibility declaration, not a proof about
arbitrary code. Follow [the compatibility checklist](api-replicas.md#compatibility-contract-and-review)
for durable writes, auth formats, job payloads and feature dependencies. Changes
requiring new worker/broker behavior, shared config, or a schema revision cannot
use this path. The preflight does not certify provider/auth/feature defaults
outside the shared fingerprint or rehearse every candidate automatically.

Only the API's `MOYAI_BUILD_SHA` is changed, then a deployment is requested for the
same exact commit. Both release modes share GitHub concurrency. The controller
rechecks saved environment, live deployment IDs, service configuration and
background ownership before writes. Avoid concurrent dashboard edits/manual
Render deploys: these checks detect observed drift but cannot make multiple
Render calls atomic.

For a diskless service, [Render's deployment lifecycle](https://render.com/docs/deploys)
keeps the old instance serving while the candidate starts, switches new traffic
after readiness, and sends SIGTERM to the old instance after 60 seconds. Private
services use [TCP health checks](https://render.com/docs/health-checks); API startup
waits for Temporal and verified database ownership before binding its port. The
workflow additionally verifies HTTP health, the actual serving build and its
PostgreSQL owner, then waits for the old API owner to disappear. Coordinator,
broker and worker owner identities (PID plus database connection start time)
must remain unchanged throughout the observed rollout.

Render's configured shutdown allowance is 300 seconds. API Uvicorn drains for up
to **270 seconds after SIGTERM**, reserving time for application cleanup. Other
roles retain their existing 20-second timeout. These are finite deadlines:
event streams/computer sockets must reconnect, and an upload/transcription or
other API request exceeding the deadline can be interrupted. Broker model streams
are on the unchanged broker. This path does not make coordinator, worker, broker
or schema upgrades seamless.

## Failure and rollback

The receipt records each intended write before it is sent, the deployment IDs,
previous/current commits and observed process identities. No settings values or
application data are logged. Always inspect the receipt and Render before retrying.

| Observed outcome | Controller behavior |
| --- | --- |
| CI, compatibility, config or preflight failure | No deployment mutations |
| Confirmed build/start failure, original API still healthy and sole API owner | Restore its saved build SHA; keep the original deployment running |
| Confirmed live candidate consistently returns a correlated HTTP 503, background owners/config/contract unchanged | Wait for the first old API to exit, then redeploy the previous verified API commit once and verify readiness and draining |
| SSH/HTTP uncertainty, lost write response, unknown/cancelled deployment, timeout, external change or interruption | Stop with a receipt; no speculative retry or rollback |
| Old owner never drains, rollback fails, or background ownership changes | Fail and require inspection; never cancel sessions or alter background services |

A verified rollback has receipt status `rolled_back` and the GitHub job still
fails: the candidate was not released. Rollback rebuilds the previous commit and
can take time; an unhealthy live candidate can cause an outage until it recovers
or rollback completes. Dependency installation/build failures also remain possible
when rebuilding the old commit. This is not instantaneous automatic failover.

Before manual recovery verify the actual live commit, saved API build, active
Render deployments, contract and all owners. Restore a clean compatible baseline
before another preflight. Backend maintenance must include the API service; do
not bypass the coordinated-mode guard by simply clearing the GitHub variable.

## Local verification

With a disposable `MOYAI_TEST_POSTGRES_URL`:

```sh
uv run --frozen --python 3.13 pytest -q tests/release tests/test_render.py \
  tests/test_api_lifecycle.py tests/test_api_compatibility.py
uv run --frozen --python 3.13 python -m scripts.release.rehearse_api --output /tmp/api-controller
uv run --frozen --python 3.13 python -m scripts.broker_restart_probe \
  --mixed-build-api --drain-api --hold-seconds 4 --output /tmp/api-drain
```

The controller rehearsal simulates Render/SSH/CI and covers success, startup
failure, verified rollback, and ambiguous writes. The process rehearsal uses real
PostgreSQL, Temporal, coordinator/broker/API processes, signed HTTP reads and a
local model fixture. An admitted finite API stream finishes a database read after
SIGTERM while new traffic goes to the replacement and broker inference completes
once. No cloud worker, real provider, S3 transfer or Render traffic switch is
simulated as measured production continuity. JSON receipts and timestamped terminal
recordings are written to the output directories.
