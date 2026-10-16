# Deploy and maintain production

For a new installation, use the [versioned Docker guide](../deploy/docker/README.md).
The existing Render production path has separate
API, coordinator, broker and worker services. Every role runs the same Docker
image and root `.venv`. Pydantic is the only executable core in current images.
A fresh local evaluation starts with the [root guide](../README.md#run-locally).

## Prepare configuration

Preserve the existing service identities, PostgreSQL schema, signing/encryption
keys, object storage, public URL, identity policy, Temporal namespace and original
queues. Apply the additions in the [deployment contract](pydantic-deployment.md#configuration-contract)
to all four services, changing only `MOYAI_RUNTIME_ROLE` per role. That contract
lists each required input and the first-cutover preparation sequence.

Native execution requires a separate schema, encryption key, object prefix and
Temporal queue. Preserve existing native values during upgrades. Do not generate
replacement values for an installation that already has native data. Provisioning
receipts verify both application and Harness storage; configuration validity alone
does not establish readiness.

The old one-service Render blueprints have been retired. Applying a new blueprint
to a running installation would not preserve its data or role ownership. Existing
Render services continue using the Docker entrypoint, their current private routing
and the guarded release workflow. The release controller pins the established
coordinator/worker/broker service IDs; `MOYAI_API_SERVICE_ID` supplies the API ID.
A different deployment needs an explicitly reviewed topology configuration.

## Release from GitHub

Keep Render auto-deploy disabled and use **Actions → Deploy production → Run
workflow → main**. Select **maintenance**, **pydantic**, and initially
**preflight_only: true**. This reads the live topology and checks the exact selected
commit's CI without changing services. Inspect the saved release receipt for
`preflight_passed`, schema plans, core selection and source ownership.

During the planned maintenance window, run the same selected candidate with
**preflight_only: false**. Every run performs preflight again; a later main commit
is a new candidate. The `preserve` core option is valid when the source already
uses Pydantic. Preserving a retired core fails before mutation. Native releases
currently require maintenance; rolling/API/coordinated modes are retained in
source tooling for historical compatibility checks and cannot deploy native roles.

The workflow uses the `moyai-production` GitHub environment, restricted to main,
with these existing inputs:

| Input | Use |
| --- | --- |
| `RENDER_DEPLOY_API_KEY` secret | Read and update the established services and create deployments |
| `RENDER_DEPLOY_SSH_KEY` secret | Private runtime inspection and guarded migration/control commands |
| `MOYAI_API_SERVICE_ID` variable | Existing dedicated API service |
| GitHub workflow token | Read main and its checks; supplied by Actions |

Keep credentials in protected environment settings. Database credentials remain
on the services. Merging a PR does not dispatch this workflow.

## Maintenance sequence

1. Pause admission through the generation-bound native maintenance control and
   let the existing worker reach verified safe boundaries. Unknown effects,
   unresolved billing attempts and unsafe turns block progress. Queued inputs
   remain saved; the controller does not cancel work to satisfy the drain.
2. Stage and retire the API, then stage worker, broker and coordinator together.
   Verify every old process retires before advancing. Staged listeners return
   maintenance responses and own no runtime.
3. With all owners gone, take exclusive runtime ownership, apply any reviewed
   canonical/native migrations, and verify their receipts. No schema fingerprint
   or old receipt is rewritten to force compatibility.
4. Activate coordinator, then broker, then worker and API together with matching
   contracts. The persisted native pause survives worker replacement without
   toggling its drain environment flag. Verify all four roles and ownership,
   then issue one explicit resume. Same-core native upgrades use eight Render
   deployments in five batches.
5. Check active `/health` returns `{"status":"ok"}` and the expected build, role,
   owner and `X-Moyai-Core: pydantic-v1` headers. Confirm existing sign-in, an old
   session/download, a new task and a continuation in the actual application.

The first legacy-to-native adoption has additional source-automation pause and
original-queue preservation checks. Follow the full [deployment contract](pydantic-deployment.md)
for that transition. A maintenance release includes a service interruption.

## Failure and recovery

An uncertain control-plane write or migration result stops the release at its
recorded intent. Inspect the receipt and actual processes before another action;
uncertainty does not prove rollback. Retain database backups, both encryption
keys, object populations and Temporal histories together.

Concurrent members have separate intents under `batches[].deployments`. Peers
already requested can continue replacing after a failure. Inspect every member;
an unconfirmed POST must not be retried, and a partially staged baseline must not
be fed into a fresh release.

Use a compatible roll-forward candidate after native protocol changes. Current
images cannot start a retired engine. Recovering with an earlier image requires
that image's reviewed schema/history/ownership compatibility and release procedure;
changing `MOYAI_CORE` alone is not a rollback. Never erase receipts, bypass fences,
rotate keys or replay original histories on the native queue to make startup pass.

The [verification guide](verification.md) covers local checks. Provider account,
real Render handoff and production workload qualification remain deployment checks.
