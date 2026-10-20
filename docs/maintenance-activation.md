# Maintenance activation

The optional `moyai-maintenance-activation-v1` protocol lets a private release
controller activate an already staged native service. It removes a second
provider deployment; it does not change the rolling release policy.

The controller must pause execution, verify drain completion, replace every old
runtime with a staging listener, prove zero owners, and explicitly migrate and
verify both schemas. It then activates coordinator, broker, worker and API in
that order, checking exact new owners and health before explicitly resuming.
The protocol is not a migration or resume command.

Each staging deployment must receive a new 32-character lowercase hexadecimal
`MOYAI_MAINTENANCE_TOKEN`, even when redeploying the same commit. The staged
listener exposes an owner-only Unix socket, by default
`/tmp/moyai-maintenance/control.sock`. `MOYAI_MAINTENANCE_SOCKET` can select an
absolute path in a private per-container directory. Do not mount that directory
from another container.

Run `python -m moyai.maintenance_activation status` inside the exact staged
container to obtain its protocol, build, role, service, instance and staging
token. The `activate` subcommand accepts an `ActivationRequest` JSON object on
stdin: the complete expected identity, a unique request ID, and the canonical
schema, native schema, paused generation and pause request fingerprint.

Before activation, the controller invokes
`python -m moyai.maintenance_activation provision` with the `ActivationGuard` on
stdin. This explicit step verifies the pause and takes the canonical zero-owner
lock before creating the independently versioned `moyai_maintenance_activation_v1`
release table. It verifies the exact table shape and never repairs a mismatch.
Normal startup never creates this table. Application and rolling schema migration
versions remain unchanged.

Acceptance rechecks the canonical schema and locks the exact native pause while
committing a receipt. A conflicting request for an accepted token is rejected.
The listener then exits gracefully and execs the ordinary entrypoint,
which retains schema, role ownership, dependency and lifecycle checks.

A private local receipt avoids database reads on later probes. A fresh instance
with the original deployed staging environment recovers its durable receipt,
including after execution has resumed. A new staging token cannot reuse that
receipt. Startup and probe errors remain failures; missing or inaccessible
receipt storage is never treated as permission to activate. Ordinary deployments
must clear an old token before entering staging.

Active `/health` responses include `X-Moyai-Maintenance-Activation` and
`X-Moyai-Maintenance-Instance`. They supplement the existing build, role and
database-owner headers. A controller must bind all of them to its recorded
request and stop on changed ownership.

If a connection is lost during activation, acceptance may already be durable.
Inspect the recorded request, durable receipt and actual health/ownership before
proceeding. Do not repeat the mutation under a new identity, clear receipts, roll
back a migrated schema, or implicitly resume. The runtime provides no automatic
partial-release recovery; that remains the release controller's responsibility.

Receipt rows are small release metadata, not customer content. They must remain
available while a provider can restart the corresponding deployment. No automatic
receipt pruning is included.

For an isolated local rehearsal, the existing `MOYAI_PRODUCT_LOCAL_REHEARSAL=true`
mode binds this entrypoint to loopback and supplies identity from
`MOYAI_LOCAL_FLEET_SERVICE_ID`. Its existing checks reject Render metadata and
non-loopback dependencies. These identities never qualify a production preflight.
