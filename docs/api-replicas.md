# API instances with independent lifecycles

`MOYAI_RUNTIME_ROLE=api` serves browser requests without starting coordinator
jobs or execution workers. Multiple API instances of the **same build and shared
configuration** may overlap. Inference stays on the separate broker; the existing
singleton coordinator keeps webhook processing, wake dispatch, Slack delivery,
schedulers, identity/environment jobs, trace export, session titles and deletion
cleanup.

This is an opt-in prerequisite for seamless releases. Different build SHAs still
fail admission, and coordinator/broker replacements still require their singleton
owner to exit. No production service, edge route, or deployment workflow is
changed automatically. The production GitHub Action currently supports the
three-service coordinator/broker/worker topology only; do not add API services to
production and then use that action unchanged.

## Prerequisites

- PostgreSQL, Temporal, verified shared object storage and explicit stable keys.
- `MOYAI_SEPARATE_BROKER=true` and `MOYAI_SCHEMA_MODE=verify` on all roles.
- Offline schema migration to revision 2. This adds the durable session-title
  inbox and a partial index for pending deletion scans; it preserves saved
  sessions, keys and runtime policy. Follow
  [schema migrations](schema-migrations.md), including stopping **all** runtime
  owners before `python -m app.schema_migrations --apply`.
- One coordinator publishes the shared policy first, then one broker and the
  existing workers. API instances join without rewriting that policy or running
  schema initializers. Build SHA, keys, storage, public URL, Temporal target,
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

## Same-build replacement rehearsal

1. Start the replacement API with the same build/config and a fresh ephemeral
   directory. Verify readiness, login, a saved session and a saved file.
2. Keep the old API alive while moving **new** workspace traffic to the ready
   replacement. Route hooks and broker traffic as above throughout.
3. Drain the old API's existing requests before stopping it. Platform termination
   deadlines still matter: browser event streams/computer sockets must reconnect,
   and long uploads or audio transcription may be interrupted if forced past the
   drain deadline. This PR does not implement an edge traffic switch or a new
   drain protocol.
4. Verify the coordinator, worker executions and broker streams stayed alive;
   confirm queued title/deletion work was handled by the coordinator.

For a different build, config fingerprint or schema revision, use a coordinated
maintenance transition that includes every API owner. Do not bypass the
fingerprint. Remaining release work is a reviewed compatibility contract for
mixed-build overlap, readiness/routing/drain automation, and a production
continuity rehearsal with real sessions.

## Local verification

Set `MOYAI_TEST_POSTGRES_URL` to a disposable PostgreSQL database, then run:

```sh
uv run --frozen --python 3.13 pytest -q tests/test_api_lifecycle.py tests/test_schema_migrations.py
uv run --frozen --python 3.13 python -m scripts.broker_restart_probe \
  --replicated-api --hold-seconds 4 --output /tmp/moyai-api-overlap
```

The probe starts real PostgreSQL-backed coordinator, broker and two API server
processes plus local Temporal. It switches a synthetic HTTP client's requests to
the ready API before stopping the old one, checks signed-session continuity, and
holds one native Responses stream through that handoff. The coordinator and
broker keep their PIDs and the model call completes once. The model is a local
HTTP fixture: this verifies lifecycle isolation, not production load balancing,
real provider performance, cloud sandbox execution or S3 transfers. The JSON
receipt and timestamped terminal recording identify that scope explicitly.
