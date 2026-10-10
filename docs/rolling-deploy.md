# Compatible rolling deployments

After the four-service rolling topology has been activated and tested, choose
**GitHub → Actions → Deploy production → Run workflow → main → rolling**.
Run with **Only check configuration** first. A successful preflight writes nothing.

This mode replaces the worker, broker, coordinator and API in that order. Users
can continue sending messages and admitted work stays under its existing durable
ownership. Each replacement must become healthy, and its predecessor must retire,
before the next role changes. The controller never drains the global session queue,
cancels turns, changes concurrency, migrates the database, or changes edge routes.

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
  without autoscaling or command overrides. Every service needs Render's
  **300-second shutdown allowance**. Use the normal repository Docker startup.
- Actual running commits equal their saved `MOYAI_BUILD_SHA` settings. Every role
  implements the correlated rolling health headers. Set environment variable
  `MOYAI_API_SERVICE_ID` in GitHub's `moyai-production` environment to the activated
  API service; retain the existing deployment and read-only SSH credentials.

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
equal the live contracts and exact schema revision. Same protocol revisions are
a reviewed compatibility declaration, not proof that arbitrary code is safe.
Review both old→new and new→old durable data/workflow behavior and run the actual
release pair through ongoing-work and rollback tests before keeping revisions
unchanged. Incompatible schema or runtime-setting changes need a maintenance plan.

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

Render's shutdown window is finite. API and broker requests get the configured
270-second application drain within the 300-second platform allowance. A stream
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
and observed ownership replacements, without environment secrets.

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
completion, unknown writes, external edits and failed ownership transfer.
`tests/release/test_rolling_probe.py` uses real local PostgreSQL locks/contracts
and validates HTTP process correlation. These tests do not deploy Render or
establish production latency. The first activated production release still needs
an observed continuous-traffic rehearsal with bounded inference streams.

```sh
MOYAI_TEST_POSTGRES_URL=postgresql://... uv run --frozen --python 3.13 pytest -q tests/release
```
