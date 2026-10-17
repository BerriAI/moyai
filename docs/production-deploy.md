# Deploy and maintain production

For the staged migration of the manual workflow into private
`BerriAI/moyai-ops`, see [private deployment setup](../deploy/github/README.md).
The existing workflow remains active until that guide's explicit cutover.

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
uses Pydantic. Preserving a retired core fails before mutation. Schema, framework
and incompatible protocol changes require maintenance. Compatible native API/UI
updates can use the API-only procedure below; compatible four-role updates can
use the rolling procedure. The historical `coordinated` mode rejects native services.

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

## Compatible API/UI updates

Select **api**, **preserve**, and initially **preflight_only: true** on the chosen
main commit. Native API preflight requires the exact commit's Docker, native
runtime, PostgreSQL runtime and release-controller CI. It verifies all four
running role identities, their health, native application/Harness receipts and
the unpaused maintenance generation. The API must be diskless, use the reviewed
Docker entrypoint without command overrides, and have a 300-second shutdown
allowance. Render private services use TCP checks: the native Uvicorn listener
opens only after application lifespan startup completes. The release CI tests
this socket boundary and shutdown draining; post-switch probes additionally
verify HTTP readiness and the exact native process identity.

The candidate must have exactly the same canonical schema revision, native
migration manifests, framework versions and API/background compatibility
contracts as the running cluster. A new schema migration, core change, or
incompatible protocol must use maintenance; the API path never provisions,
migrates, pauses or resumes execution.

Run **api**, **preserve**, **preflight_only: false** after inspecting the receipt.
Only the API build and deployment change. Render warms the replacement and
switches traffic; the controller verifies its build, core and process identity,
then waits for the previous API owner to retire. Coordinator, broker and worker
builds, owners, contracts and native receipts must remain unchanged. Their
background execution stays admitted throughout the replacement.

This supports compatible API/UI releases, not a full-cluster blue/green switch.
Render draining is bounded; long-lived connections may reconnect. Qualify real
traffic, authenticated requests, streams and active tasks through the actual
application before treating an installation as uninterrupted.

A confirmed startup failure can retain the verified old API and restore its
saved build label. An explicitly unhealthy candidate can trigger one verified
API rollback while native receipts and background owners remain unchanged.
Ambiguous writes, unknown readiness, changed contracts or background failures
stop for inspection without a speculative retry or rollback.

## Compatible four-role rolling updates

Select **rolling**, **preserve**, and initially **preflight_only: true** on the
chosen main commit. This requires an already-running Pydantic cluster, verified
canonical/native schemas, four distinct healthy role owners, one registered
native worker and an unpaused native maintenance generation. Exact-commit
Docker, native runtime, PostgreSQL runtime and release-controller CI must pass.

Both the running worker and candidate must use the qualified 240-second total
worker shutdown contract. The Render entrypoint supplies the 240-second default
while preserving explicit settings, and the pollers start their shutdowns
together so active work can use that shared window. The ready replacement
handles new workflow/control polling while old activities drain. A source with
the earlier zero-second default or sequential pool shutdown is rejected before
any changes: adopt the new entrypoint and shutdown protocol through maintenance
first. Qualify longer activities separately; an already-dispatched model request
is never replayed to hide an interrupted stream.

For first adoption, use the maintenance procedure to install the shared worker
shutdown protocol. Then inspect a rolling preflight receipt: `worker_shutdown`
must report `moyai-worker-shared-drain-v1` and `seconds: 240`. Selecting rolling
does not retrofit that behavior into an already-running older worker.

The candidate must retain every role compatibility contract, canonical schema
revision, native application migration manifest and Harness/framework version.
This path performs no schema migration, global drain, pause or resume. Use
maintenance when any of those contracts change.

After checking the preflight receipt, run with **preflight_only: false**. The
controller replaces **worker → broker → coordinator → API**, one role at a time.
Unaffected owners must stay healthy and identical. The selected role can overlap
only its own replacement; every old process must retire before the next role.
Native worker registration must match the exact canonical process identity and
contracts before a worker is ready. Historical registrations are not live owners.
Broker maintenance leadership and coordinator leadership must transfer to the
replacement before advancing. A compatible coordinator joins the existing
cluster policy without rewriting it.

Render's private-service TCP readiness uses the reviewed native lifespan startup
gate. HTTP probes then verify the selected build, core, role, process and
leadership. Native schema receipts and the maintenance generation remain fixed
throughout. Queued and running work do not need to reach a global zero-work
boundary for this release.

The platform's shutdown deadline remains finite. Qualify active model/tool
activities and stream reconnection for the installation's workload; this
controller does not extend that deadline or replay ambiguous external effects.
A failed or uncertain deployment stops with its recorded intent. Previously
updated compatible roles can remain on the new build. Inspect each live build,
saved environment and owner before recovery; do not blindly retry a lost write
or feed a mismatched baseline into a new release.

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
   or old receipt is rewritten to force compatibility. Native maintenance uses
   this offline boundary for canonical schema upgrades even when the compatibility
   floor is unchanged; it does not require an additive online migration plan.
   Schema provisioning does not run the separate
   [billing history backfill and physical consolidation](billing-storage.md).
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

## Finish physical billing consolidation

First deploy the canonical/native billing schemas, retain a current database
backup, and complete both backfills using the [billing storage procedure](billing-storage.md).
Then select **maintenance**, **pydantic** (or **preserve** on a native cluster),
and **consolidate_billing: true** in the deployment workflow. Start with
**preflight_only: true** to inspect the saved billing comparison.

This option requires an existing native cluster with the candidate's schema
receipts already provisioned. Preflight reads the storage receipt, verifies both
backfills and mirrors, checks source table definitions and compares every mapped
field. It performs no backfill or financial-data mutation. Ordinary releases
default this option to false.

The actual release drains and stages all four roles, verifies zero owners, and
then records one billing cutover intent before invoking the installed migrator.
The migrator rechecks the release's policy, ownership, safe-work counts, native
receipts and paused generation while holding its exclusive canonical lock. Its
atomic cutover verifies the data again and replaces the two original stores with
compatibility views. Activation follows only a confirmed one-store receipt.
Unsupported constraints or dependent objects can still reject the final DDL;
the transaction then retains the original stores.

An explicit request performs this maintenance sequence even when the selected
build is already deployed. If that build already has a verified consolidated
ledger, the workflow only verifies it. An uncertain cutover response leaves the
services staged for inspection and never retries or restores tables automatically.
The CLI equivalent is `--release-type maintenance --consolidate-billing`.

## Failure and recovery

An uncertain control-plane write or migration result stops the release at its
recorded intent. Inspect the receipt and actual processes before another action;
uncertainty does not prove rollback. Retain database backups, both encryption
keys, object populations and Temporal histories together.

Completed steering inputs use the terminal `included` state. They do not require
their own workflow checkpoint; their parent turn and any unsettled effects or
model requests remain independently checked. For older v1 workers, the release
helper corrects the known terminal-state enumeration inside its maintenance CLI
process before running status or a guarded control command. It does not edit
installed files, saved turns or receipts, and rejects unknown maintenance
contracts. Running an older installed CLI directly retains its original
classification, so recovery must use the qualified release helper.

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
