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
and a stable request ID in the release journal, then issue:

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
Workers reject this quiesce command: their retirement separately requires
Temporal routing, checkpoint migration and all-workflow deployment drainage.
Drained API/broker/coordinator status alone never proves that a worker is safe
to stop.

Protocol tests use local transports for external-send failure injection. They
are supplementary to actual-app baseline/final rehearsals. Production Render
orchestration, multi-instance ingress qualification, schema compatibility and
initial unversioned-worker adoption remain separate release gates.
