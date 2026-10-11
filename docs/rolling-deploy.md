# Compatible rolling deployments

After the four-service rolling topology has been activated and tested, choose
**GitHub → Actions → Deploy production → Run workflow → main → Run workflow**.
Leave the default **release_type: rolling** selected and **preflight_only** unchecked
to deploy. The action selects the latest main commit at button press; later pushes
do not change that release.

Every deployment already waits for the selected commit's CI and runs the full
preflight before changing any service. **preflight_only** is an optional check
without deployment, useful after configuration changes. It is not a required
separate run. An incompatible schema, contract or shared configuration is rejected
before deployment and needs a separately planned transition.

This mode replaces the worker, broker, coordinator and API in that order. Users
can continue sending messages and admitted work stays under its existing durable
ownership. Each replacement must become healthy, and its predecessor must retire,
before the next role changes. The controller never drains the global session queue,
cancels turns, changes concurrency, or changes edge routes. A reviewed additive
schema plan can run before the first replacement, once every live role supports
the online migration protocol described below. This supports future declared
expansions; it does not make arbitrary schema changes safe to run online.

## Activation prerequisites

Rolling deployment cannot bootstrap its own ownership protocol. Complete the
separately planned initial migration and routing activation before enabling it:

- PostgreSQL on the exact required verified schema, all four coordinator-published
  compatibility contracts, shared object storage and stable shared keys.
- Dedicated API traffic, `/broker/*` traffic to the broker, and `/hooks/*` traffic
  to the coordinator. Preserve existing access controls. Routing must already
  have been checked with real sessions; this controller does not alter it.
- `MOYAI_SCHEMA_MODE=verify`, `MOYAI_COORDINATOR_HANDOFF=true`,
  `MOYAI_SEPARATE_BROKER=true` and `TEMPORAL_ENABLED=true` on all four roles;
  `MOYAI_DATABASE_INITIALIZE=false`, staging off and maintenance drain off.
- One private Oregon Render service per role, each diskless, auto-deploy off,
  without autoscaling or start-command overrides. Worker and coordinator may
  retain the exact pre-deploy command
  `/app/.venv/bin/python /app/build_workspace_image.py`; API and broker must have
  no pre-deploy command. This hook prepares Modal images without starting runtime
  jobs or migrating the database. The controller preserves the hook and rejects
  other commands or edits during a release. Every service needs Render's
  **300-second shutdown allowance**. Use the normal repository Docker startup.
- Actual running commits equal their saved `MOYAI_BUILD_SHA` settings. Every role
  implements the correlated rolling health headers. Set environment variable
  `MOYAI_API_SERVICE_ID` in GitHub's `moyai-production` environment to the activated
  API service; retain the existing deployment and SSH credentials. SSH runtime
  probes are read-only; an approved online schema plan uses the same private
  connection to the coordinator's installed migration engine.

The controller rejects the old singleton-broker/whole-cluster build contract,
local disks, unverified schemas, staged services, mismatched saved build labels,
and extra owners. It does not repair these conditions during a release. Do not
change a build label to disguise the commit that is actually running.

## Compatibility and checks

The dispatched `main` commit is immutable for the entire run. Its Docker startup,
PostgreSQL runtime, production-release rehearsal, other reported checks, and
commit statuses must pass. The two protocol/controller workflows must have
successful **push** runs on that exact commit. Both run on every push to main,
including UI-only and documentation-only commits; their pull-request path filters
remain unchanged. A missing run still fails closed.

The checked-out candidate computes its API and three background role contracts
in an isolated process using each service's actual saved environment. All must
equal the live contracts. The physical schema must either match the candidate or
have an explicit compatible online plan. Same protocol revisions are
a reviewed compatibility declaration, not proof that arbitrary code is safe.
Review both old→new and new→old durable data/workflow behavior and run the actual
release pair through ongoing-work and rollback tests before keeping revisions
unchanged. Breaking schema or runtime-setting changes need a maintenance plan.

## Online additive schema changes

The initial bridge requires verified schema **17** and keeps its existing role
fingerprints unchanged. Deploy that bridge to **all four roles with no schema
change** before attempting a later expansion. A piped probe recognizes an older
image without the engine, but permits it only for a same-schema release. It does
not infer support from candidate code or a healthy HTTP response. This feature
does not automatically migrate a pre-17 installation or activate role routing.

For a later schema revision, the exact selected candidate must contain a complete
reviewed chain in `app/online_schema_migrations.json`, matching its versioned
schema digests. Every running role must advertise protocol 1, the same schema
compatibility revision as the candidate, and a required revision no newer than
the live database. Shared role contracts, runtime policy and concurrency remain
unchanged. Missing steps, unsupported operations, capability gaps and schema
downgrades stop before any database or service write.

Preflight validates and records the complete plan and digest without applying
it. A normal deployment records migration intent, then sends the plan once to
the coordinator's installed engine. The engine rechecks the database, owners
and contracts in its migration transaction. Reviewed additive DDL, its receipt
chain and the physical revision commit together. Existing application owners
remain admitted; there is no global worker pause or session cancellation.
Individual DDL operations can still briefly wait for locks or fail their bounded
timeouts: 250 ms to acquire a lock and five seconds per statement, with a
20-second total transaction budget (also enforced by PostgreSQL 17 or newer).
This is not a guarantee of zero latency during schema changes.

Before any service replacement, the controller rereads every role and requires
the same process identities, healthy state, contracts and policy, plus the exact
target database revision. The target then becomes the fixed schema baseline for
the rest of the release. The [schema migration guide](schema-migrations.md)
defines the deliberately narrow additive operations and the maintenance path
for changes outside that set. The bridge ships an empty plan registry; a future
schema change still needs its own reviewed declaration and compatibility tests.

For each selected role, the controller:

1. Confirms all four saved environments, Render topology, live deployments,
   shared contracts and runtime owners still match the verified baseline.
2. Changes only that role's `MOYAI_BUILD_SHA` and deploys the exact selected SHA.
   It records each mutation intent before making the request.
3. Checks unaffected services remain healthy at the same process identities.
   During the selected role's overlap, at most one additional runtime owner is
   permitted. Shared policy and all contracts remain immutable.
4. Correlates candidate HTTP health with its actual build, role and PostgreSQL
   owner PID/start time. For coordinator replacement, readiness may initially
   mean a standby that accepts durable hooks.
5. Waits for the old owner to exit. The new coordinator must then own the leader
   lock; the new broker must own maintenance leadership and be accepting work.
   Only then can the next role deploy.

Active sessions, model requests and leases do not need to reach zero. The shared
model limit remains unchanged while brokers overlap. A process failure may still
produce bounded retry/lease-recovery delay; this is not a zero-latency promise.

Render's shutdown window is finite. The API and broker keep accepting for 30
seconds after SIGTERM for routing convergence, then drain admitted requests for
up to 240 seconds, capped at 270 seconds after the first signal. The coordinator
keeps accepting for 30 seconds, then retains its existing 20-second HTTP drain,
leaving approximately 250 seconds for background cleanup. These waits leave
cleanup headroom within the 300-second platform allowance. The grace reduces a
brief routing/listener race; it is
not a guarantee that private routing always converges within 30 seconds. Validate
the real ingress path during each rollout. Ownership and background handoff still
use the existing lifespan protocols. Worker and standalone shutdown are unchanged. A stream
that outlasts that window can be interrupted and follows the existing durable
failure/uncertainty rules. The controller cannot guarantee uninterrupted streams
of unlimited duration and does not retry ambiguous external work.

## Failure, partial releases and rollback

A failed, cancelled, ambiguous or timed-out control-plane operation stops the
release. There is no automatic deployment retry or speculative rollback. An
environment write or deployment request can have succeeded even if its response
was lost. A failed candidate can also leave the old build serving while the saved
build label names the candidate; that is intentionally rejected by the next run.

Inspect the `production-release` receipt and Render's exact live deployment and
saved environment before any recovery action. The receipt contains per-role
previous SHA/deploy/owner, selected SHA, confirmed/unconfirmed writes, deployments,
observed ownership replacements, and any full schema plan/digest and confirmation,
without environment secrets. If the migration response was lost, its commit is
unknown to this run: no deployment starts, no migration is automatically retried,
and the database is never automatically rolled back. Inspect the committed schema
and ledger. A fresh release can observe an already committed compatible target
and continue the service replacements without reapplying the migration.

Already completed compatible replacements may remain live if a later role fails.
Once every service has a verified healthy baseline and saved labels match reality,
a fresh run can continue the same/newer selected `main` release and skip roles
already there. It checks every role's compatibility before doing so.

Rolling mode only permits forward `main` updates. Re-running an older workflow
cannot deploy backwards. Rollback is a separate reviewed operation using the
receipt's exact prior commits and a verified compatible state; never remove the
schema/policy fences or use an arbitrary Render “latest commit” deployment to
force recovery.

The existing `coordinated` mode remains for its original two/three-service
topology. The older API-only controller retains its narrower topology checks;
use rolling mode for the activated coordinator/broker handoff topology.

## Verification scope

`tests/release/test_rolling_deploy.py` exercises real controller logic against
simulated Render, SSH and GitHub responses, including ongoing work, partial
completion, unknown writes, external edits and failed ownership transfer. It also
checks migration-before-replacement ordering, zero-write preflight, capability
admission, unsafe-plan rejection, lost migration responses and committed-target
recovery. These simulated checks do not establish live deployment continuity.
`tests/release/test_rolling_probe.py` uses real local PostgreSQL locks/contracts
and validates HTTP process correlation. These tests do not deploy Render or
establish production latency. The first activated production release still needs
an observed continuous-traffic rehearsal with bounded inference streams.

```sh
MOYAI_TEST_POSTGRES_URL=postgresql://... uv run --frozen --python 3.13 pytest -q tests/release
```
