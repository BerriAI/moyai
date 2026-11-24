# Kubernetes fleet runtime

The default Helm installation uses one `Recreate` pod with shared installation
state. It does not provide continuous application upgrades. Independent
generations can instead run the API, broker, coordinator and worker in separate
Deployments behind a stable ingress. Normal compatible releases can then warm
the new generation, move HTTP and Temporal routing, checkpoint running work,
and retire the old generation only after its durable drain receipts complete.

The private release controller owns resource creation, image qualification,
registry bindings, routing, connection budgets and retirement. The application
entrypoint is:

```sh
python -m moyai.kubernetes_fleet prepare --snapshot /installation/installation.json
python -m moyai.kubernetes_fleet serve api --port 8787
```

`prepare` runs in an init container and copies a completed installation snapshot
from a read-only Secret to a private per-pod directory. Every pod keeps the same
installation keys, database/schema, object prefixes, Temporal namespace and
public origin. It does not initialize an installation or migrate application
schemas. `check ROLE` reports the image's contracts without starting clients,
workers or migrations.

The controller creates each Deployment at zero replicas, registers its actual
Deployment UID, then starts it. The namespace, Deployment name/UID and Pod UID
come from the reviewed pod template and Kubernetes Downward API. Admission also
checks the image's embedded revision and the registered worker deployment/build.
Restarting a sealed generation cannot admit new work.

Each generation's Deployment keeps an immutable image and uses `Recreate`.
The release controller creates a *new* set of Deployments for the next version;
it does not run `rollout restart` or replace the image in the serving generation.
There are normally four role pods, with eight during source/candidate overlap,
plus the stable ingress pods. Eight pods alone do not establish this routing,
registration or drain behavior.

Use readiness checks to withdraw a draining role. Do not use `/health` for a
liveness probe: the role becomes unready while accepted requests finish. The
container's 300-second termination grace is a final shutdown allowance; it is
not a substitute for observing drain receipts before scaling down.

After a separately qualified maintenance migration, the Helm chart can retain
its existing public and broker Services while selecting the stable ingress:

```yaml
application:
  mode: externalFleets
  fleetIngress: moyai-fleet-ingress
```

This setting removes the singleton application Deployment. Switch it only after
the singleton has drained and the registered replacement and both ingress pods
have been verified. Database, Temporal, installation PVC and public Service
identities remain managed by the chart. The ingress must expose named `http`
and `broker-http` ports; its broker listener must not expose the UI or private
controller endpoints.

Enabling worker versioning on an existing unversioned installation changes its
shared runtime contract. The first migration needs a maintenance boundary with
zero old owners; changing `Recreate` to `RollingUpdate` does not perform it.
Subsequent releases must pass exact-image compatibility and capacity checks.
Incompatible protocol/schema changes still need their reviewed migration path.

Reserve database capacity for the source, candidate, a fresh rollback fleet,
process replacement, routing watchers and existing clients. The controller
checks this envelope before creating a generation. `config.MOYAI_DATABASE_POOL_SIZE`
sets each role's bounded pools; `postgresql.maxConnections` sets the bundled
server limit (default 100). Qualify pool changes with the workload. Raising the
server limit requires a memory review and restarts bundled PostgreSQL, so do
that during the initial maintenance boundary, before overlapping application
generations. External PostgreSQL capacity remains its operator's responsibility.
