# API instances with independent lifecycles

`MOYAI_RUNTIME_ROLE=api` serves browser requests without starting coordinator
jobs or execution workers. Multiple API instances with the **same declared API
protocol, schema and shared configuration** may overlap even when build SHAs
differ. Inference stays on the separate broker; the existing
singleton coordinator keeps webhook processing, wake dispatch, Slack delivery,
schedulers, identity/environment jobs, trace export, session titles and deletion
cleanup.

This is an opt-in prerequisite for seamless API releases. Coordinator/broker/worker
builds must still match, and coordinator/broker replacements still require their singleton
owner to exit. No production service, edge route, or deployment workflow is
changed automatically. The production GitHub Action has a separate `api` release mode; follow
[API deployment activation](api-deploy.md) before adding this service or selecting
that mode. Coordinated mode refuses the activated four-service topology.

## Prerequisites

- PostgreSQL, Temporal, verified shared object storage and explicit stable keys.
- `MOYAI_SEPARATE_BROKER=true` and `MOYAI_SCHEMA_MODE=verify` on all roles.
- Offline schema migration to revision 3. Revision 2 adds the durable session-title
  inbox and a partial index for pending deletion scans; revision 3 adds the API
  compatibility policy. These preserve saved
  sessions, keys and runtime policy. Follow
  [schema migrations](schema-migrations.md), including stopping **all** runtime
  owners before `python -m app.schema_migrations --apply`.
- One coordinator publishes the shared policy first, then one broker and the
  existing workers. API instances join without rewriting that policy or running
  schema initializers. API protocol/schema revisions, keys, storage, public URL, Temporal target,
  limits and prepared-pool settings must match. Keep provider/auth/feature
  settings aligned too; the fingerprint is not an exhaustive config validator.
- One server process per container. Include each API and overlapping replacement
  in the database connection budget: up to `MOYAI_DATABASE_POOL_SIZE + 1` each.

Existing standalone and coordinator deployments retain their default behavior.
Only execution workers prepare sandboxes. This change does not enable or resize
the production warm pool.

## Route each request to its owner

Keep the public hostname and existing Cloudflare Access policies. Ordered routes:

| Path | Destination |
| --- | --- |
| `^/broker(/.*)?$` | Existing private inference broker |
| `^/hooks(/.*)?$` | Singleton coordinator |
| All remaining workspace traffic | Ready API instance(s) |

The API rejects `/hooks` and `/hooks/*` with 503, including signed Slack and
automation events. It also rejects broker requests. The edge must route these
directly to their owner, not forward them through an API process. Keep the tunnel
connector independent of API deployments. The coordinator still serves APIs for
compatibility, but should receive only hooks after the routing cutover.

API startup connects to Temporal and verifies database ownership before Uvicorn
binds its TCP port. An unsuccessful startup exits after
`TEMPORAL_STARTUP_TIMEOUT_SECONDS` (default 60). `/health` returns 503 if Temporal
readiness or database ownership is lost. Check HTTP health during rollout even
when the platform only checks TCP; dependency loss does not close an already
bound port.

Title requests and deletion intents are committed to PostgreSQL. API shutdown
does not cancel their processing on the coordinator. The coordinator polls for
new work; no traffic affinity is needed for signed sessions or this handoff.
Deletion returns 202 until cleanup completes. A confirmed/ambiguous title model
attempt is not automatically replayed after a coordinator crash. Titles may keep
the original prompt fallback in that case. Other coordinator jobs continue using
their existing durable queues.

## Compatibility contract and review

`app/runtime_compatibility.py` owns `API_PROTOCOL_REVISION`. It is source-controlled;
there is no environment override or arbitrary SHA allowlist. The API fingerprint
includes this revision, the exact schema revision and every existing shared-policy
setting except `MOYAI_BUILD_SHA`. The coordinator publishes it atomically with its
exact-build runtime policy, only in verified-schema, separate-broker mode, after
component construction succeeds. A missing/stale contract rejects even a same-build
API. APIs cannot publish a contract and bind their transactions to the coordinator's
policy, not their own build fingerprint.

Every API transaction checks both policies. Contract revocation rejects subsequent
reads/writes and makes `/health` fail. Writes lock the contract row through commit;
revocation waits for writes already admitted. Read-only projections retain their
consistent transaction snapshot. Runtime owners still hold the shared database
ownership lock, so changing the coordinator build/config or migrating the schema
requires draining **all** owners, including APIs.

Equal protocol numbers are a **reviewed compatibility declaration**, not an automatic
proof that arbitrary source changes are safe. Before retaining the revision:

- Check both directions: new APIs must read existing state and produce writes that
  the old coordinator, broker and workers understand; the old API must also read
  state produced by the new API during overlap and rollback.
- Review durable run/message/wake state, title/deletion inboxes, serialized job/tool
  payloads, attachment references, broker capabilities, encryption and signed-session
  formats. Schema changes also require a schema revision and offline migration.
- Keep auth/provider/feature configuration aligned. A new feature that depends on
  new worker/broker behavior needs a coordinated release even if its tables exist.
- Run regression tests and rehearse the actual candidate against the intended old
  release before routing traffic. Bump `API_PROTOCOL_REVISION` for an incompatible
  change; use maintenance deployment when compatibility is uncertain.

This change does not add Temporal build routing, coordinator ownership transfer,
broker stream handoff or rolling schema migration. Initial activation from revision
2 requires an offline migration and a contract-publishing coordinator. Revision-2
API processes cannot participate in this first transition.

## Compatible API replacement rehearsal

1. Start the reviewed compatible replacement API with the same shared config and a fresh ephemeral
   directory. Verify readiness, login, a saved session and a saved file.
2. Keep the old API alive while moving **new** workspace traffic to the ready
   replacement. Route hooks and broker traffic as above throughout.
3. Drain the old API's existing requests before stopping it. Platform termination
   deadlines still matter: browser event streams/computer sockets must reconnect,
   and long uploads or audio transcription may be interrupted if forced past the
   drain deadline. The [API release workflow](api-deploy.md) uses Render native
   overlap and verifies process retirement; initial edge routing is a one-time step.
4. Verify the coordinator, worker executions and broker streams stayed alive;
   confirm queued title/deletion work was handled by the coordinator.

For an incompatible protocol, changed shared config or schema revision, use a
coordinated maintenance transition that includes every API owner. Do not bypass
the fingerprint. Remaining release work includes coordinator ownership transfer,
broker draining and a production continuity rehearsal with real sessions.
The API-only workflow does not make background-service or schema upgrades seamless.

## Local verification

Set `MOYAI_TEST_POSTGRES_URL` to a disposable PostgreSQL database, then run:

```sh
uv run --frozen --python 3.13 pytest -q tests/test_api_lifecycle.py tests/test_api_compatibility.py tests/test_schema_migrations.py
uv run --frozen --python 3.13 python -m scripts.broker_restart_probe \
  --mixed-build-api --hold-seconds 4 --output /tmp/moyai-api-overlap
```

The probe starts real PostgreSQL-backed coordinator, broker and two API server
processes plus local Temporal. It switches a synthetic HTTP client's requests to
the ready API before stopping the old one, checks signed-session continuity, and
holds one native Responses stream through that handoff. The coordinator and
broker keep their PIDs and the model call completes once. The model is a local
HTTP fixture: this verifies lifecycle isolation, not production load balancing,
real provider performance, cloud sandbox execution or S3 transfers. The JSON
receipt and timestamped terminal recording identify that scope explicitly.

`--mixed-build-api` copies the current application source, adds a synthetic read-only
HTTP route to the candidate, and derives distinct build identifiers from those source
trees. The route returns 404 on the old API and 200 on the candidate. A third source
tree with a bumped protocol is rejected before readiness. This is a controlled
compatibility fixture; it does not certify future releases. `--replicated-api`
retains the same-build rehearsal.
