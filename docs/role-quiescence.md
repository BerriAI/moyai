# Planned role retirement

Distributed API, broker and coordinator processes can stop accepting work and
finish their accepted operations before an operator retires the process. This is
an optional process control mechanism. It does not provision services, move
ingress traffic, establish release compatibility or authorize worker retirement.

Start the supported product entrypoint with a private local control socket:

```sh
python -m moyai.product_start serve --host 0.0.0.0 --port 8787 \
  --control-socket /tmp/moyai-release/control.sock
```

The socket directory must be owned by the process user and inaccessible to other
users (mode 0700); the socket has mode 0600. Startup refuses to replace an existing
socket. Each container/process needs its own socket. There is no HTTP admin
endpoint and no reusable fleet-wide bearer credential.

On Render, the existing `docker_start` / `render_start` entrypoint enables
`/tmp/moyai-release/control.sock` by default. `MOYAI_ROLE_CONTROL_SOCKET` can
select a different private path; an explicit empty value disables this control
and cannot qualify for a registered fleet rollout. No Docker start-command
override is needed. This retains the qualified listener overlap and graceful
shutdown budgets. The socket binds before role pollers/background tasks start,
and stays available through their final drain.

`python -m moyai.deployment_capabilities --render` reports the installed
source's control protocol, worker configuration, shutdown settings and configured
connection bounds without connecting to a service. The bounds include the four
separate application pools, canonical owner, bounded restore locks, and dedicated
broker/coordinator connections. They exclude operator/ingress clients, connection
recovery overlap, schema migration and platform surge. Budget those separately
using current PostgreSQL limits and observed non-fleet clients.

Read the actual process identity before preparing a release operation:

```sh
python -m moyai.product_role_control status \
  --socket /tmp/moyai-release/control.sock > /tmp/role-status.json
```

The identity includes the application build, role, schema, compatibility
fingerprints, PostgreSQL owner PID and backend start time, OS process PID and a
fresh process nonce. A status response is not evidence that ingress stopped
routing to this process.

Before quiescing, the release controller must commit traffic away from the old
slot and verify that every serving ingress instance has applied the new route.
Keep sufficient ready replacement capacity. Record the exact source identity
and a stable request ID in the release journal. For an unmanaged local role,
the process control command is:

```sh
python -m moyai.product_role_control quiesce \
  --socket /tmp/moyai-release/control.sock \
  --expected /tmp/role-status.json --request-id release-123-old-api
```

The reply acknowledges initiation. Inspect status until the same identity and
request ID report `state: drained`. A lost reply or timeout does not cancel
quiescence and is not permission to retry the operation against a different
process. An identical request reconciles the same operation; a different request
or a stale identity is rejected. Drain failure is not automatically retried.
There is no resume command: recovery starts and qualifies replacement capacity.

For a registered fleet, the controller first seals the service-generation role
in the separately provisioned `moyai_fleet_admission` metadata schema. Startup
holds a shared role-row lock through exact-owner registration; the seal takes
an exclusive lock. It captures prior admitted instances and prevents later
restarts from starting role pollers/background tasks. Existing instances observe
the recorded request and drain cooperatively. A direct socket quiesce request
cannot bypass that durable seal or change its request ID.

Active roles can self-register replacement process lifetimes against their bound
service/build; normal recovery does not need a continuously running deployment
controller. Registration checks both the canonical PostgreSQL owner PID and
backend start time. A new process has a fresh nonce and cannot reuse an earlier
drain receipt. The runtime cannot redirect its registry to an arbitrary schema.
On Render, the image's baked `/app/build-revision` must match the declared build
before role polling starts. A platform Git SHA, when present, must match too.
Image-backed services do not need a Git SHA; their controller must qualify the
exact image digest and source provenance. An environment build label alone
cannot make a different image join an older worker version.
Absent/disabled metadata remains unmanaged for compatibility, but candidates
with `MOYAI_FLEET_REQUIRED=true` fail closed. Startup never creates metadata.

Adopting a live unmanaged fleet is a separate operation: the controller must
verify support on every running role, pre-register exact current owners, lock the
fleet row, then check the complete canonical-owner inventory before enabling it.
Startup reads take that same row's shared lock, so an unmanaged start cannot slip
between the activation check and commit. The activation controller still needs
its own qualification; this document does not authorize implicit adoption.

During quiescence:

- Health and new application requests return unavailable. Requests already
  admitted finish; a slow request can keep the process draining indefinitely.
- An API event stream closes its reader without cancelling the agent workflow,
  emitting a terminal event or resetting its last delivered event ID. The
  existing EventSource reconnects through ingress and resumes from that ID.
- The broker finishes accepted requests and their tracked cleanup before
  closing maintenance. Its canonical owner remains live through settlement.
  An uncertain provider operation retains its existing uncertain receipt.
- The coordinator stops polling new work, finishes accepted delivery units and
  receipt writes, and retains its singleton lock until background drain ends.
  Pending inbox/outbox work remains durable for the successor. Planned drain
  never reposts an uncertain Slack send. Ownership failure still uses the
  existing failure path; this mechanism does not bypass ownership checks.

Only after a successful drain may the release controller request normal process
termination and independently verify the exact old PostgreSQL owner disappeared.
Unmanaged workers reject this quiesce command. Registered workers stop their
pollers only after a durable role seal carries matching Temporal deployment
drain evidence. Their retirement separately requires routing, checkpoint
migration and all-workflow deployment drainage.
Drained API/broker/coordinator status alone never proves that a worker is safe
to stop.

Protocol tests use local transports for external-send failure injection. They
are supplementary to actual-app baseline/final rehearsals. Production Render
orchestration, multi-instance ingress qualification, schema compatibility and
initial unversioned-worker adoption remain separate release gates.
