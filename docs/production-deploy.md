# Deploy production

After dedicated API activation, use `release_type: api` and follow
[compatible API deployments](api-deploy.md). The rest of this page describes
`release_type: coordinated` for the original two/three-service topology.

Use **GitHub → Actions → Deploy production → Run workflow → main** after the
one-time setup below. One button coordinates `moyai-private` and `moyai-worker`,
plus `moyai-broker` after the separate-broker topology is activated.
It selects the main commit at button press, waits for its CI, and deploys that
exact commit to every active role. Later pushes do not change the selected release.
Start a new run to deploy newer changes; re-running an old job keeps its old
commit and cannot roll a newer production version backward.

This workflow is manual. Merging code does not deploy production. Do not use
Render's separate **Deploy latest commit** buttons while it is running.

## One-time setup

1. Merge the workflow into `main` so GitHub shows its Run workflow button.
2. Create the GitHub environment **`moyai-production`** in this repository's
   Settings → Environments. Restrict its deployment branches to **main only**
   (selected branches, exact `main` rule). Keep the existing main branch review
   protections. Configure a required environment reviewer if your release policy
   calls for one.
3. Add these **environment secrets**, using credentials authorized for the
   Litellm Render workspace:

   | Secret | Purpose |
   | --- | --- |
   | `RENDER_DEPLOY_API_KEY` | Read the app, worker and activated broker and their deployment history; update their three release flags and create deployments. Render API keys inherit their account's access; use the least-privileged available deployment account. |
   | `RENDER_DEPLOY_SSH_KEY` | Dedicated, non-interactive SSH private key, including its header/footer and newlines. Add its public key to the intended Render account. SSH is used only for runtime checks over Render's authenticated SSH endpoint. |

   Never put either credential in workflow YAML, PR comments, artifacts, or logs.
   No separate GitHub token, database exposure, public management endpoint, or
   new paid service is needed for coordinated mode. Keep Render auto-deploy **off** for every service.
4. Run the workflow on `main` with **Only check configuration (no deployment)**
   checked. It verifies CI, all active Render services, SSH access, running build IDs,
   local health, database ownership, and matching shared settings without writing
   to Render or the database. Its receipt must say `preflight_passed`.
5. Run it again with that option unchecked for the first coordinated deployment.

The Render API key and SSH key are separate credentials. Merely merging this PR
does not install them or enable deployments. The checked-in Oregon SSH host key
comes from [Render's SSH documentation](https://render.com/docs/ssh); a changed
host key stops the workflow and must be verified against Render before updating.

## What the button does

1. Require the selected commit's Docker startup push workflow to succeed, along
   with all other reported CI checks and commit statuses. Checks belonging to
   this deployment workflow are identified through GitHub's workflow-run
   inventory and excluded, including earlier failed preflights on the same
   commit. A previous deployment failure does not substitute for CI or bypass
   the live-state preflight. Verify both services are
   live at the same build and no other deployment is in progress.
2. Redeploy the **existing worker build** with `MAINTENANCE_DRAIN=true`. Wait for
   active sessions to reach saved boundaries, model requests and leases to clear,
   and the old worker instance to exit. Queued work stays saved; the workflow does
   not cancel sessions.
3. Deploy the new worker in maintenance staging. It serves a health response but
   does not run jobs. Confirm no execution worker still owns the database and
   model requests and execution leases have cleared. A Stop request accepted
   after draining stays saved for the replacement worker; the ownership check
   does not wait for a paused worker to process it.
4. Deploy the app/coordinator at the selected commit and verify its runtime.
5. Activate the worker at the same commit, clear its drain flag, and verify both
   services and database ownership before declaring success.

When all roles already use `MOYAI_SEPARATE_BROKER=true`, the same button also:

- Checks the broker's private service identity, build, shared configuration,
  HTTP health and exclusive database ownership before any writes.
- After the worker stops, stages the new broker and waits for the old broker's
  database ownership, requests and leases to clear before deploying the app.
- Activates the matching broker after the app, checks HTTP readiness and ownership,
  and only then resumes the worker. A failed broker deployment or readiness check
  leaves execution staged for inspection.

This is six Render deployments in the split topology. The button never changes
`MOYAI_SEPARATE_BROKER` or Cloudflare routes and does not perform the first topology
switch. A broker provisioned in maintenance staging is left alone while the app
and worker still use the combined topology. Follow the coordinated cutover in
[separate-broker.md](separate-broker.md) first; all three roles must then be live
on the same build, with staging and drain disabled, before using this button.

The only environment values this workflow changes are `MOYAI_BUILD_SHA`,
`MAINTENANCE_DRAIN`, and `RENDER_MIGRATION_STAGE`. It checks that all other saved
values are preserved, including signing/encryption keys, shared storage, Temporal
settings, and concurrency limits. Each mutation and deploy ID goes into the
nonsecret **production-release** artifact. No-op releases still verify the
running services.

This mode is coordinated deployment. Compatible API updates can use the
[API-only path](api-deploy.md) after its separate topology activation. There are four
Render deployments in the combined topology and six with a separate broker,
so builds and safe draining can take several minutes. New
execution pauses, and the app still restarts. The separate work on broker
isolation, removing the app disk, and overlapping app instances is needed to
remove that interruption.

## If a release stops

Open the Actions job summary, download its release receipt, and inspect both
Render services' live deployments **and saved environment values**. A failed
API request can still have been accepted; an intent with `confirmed: false` or a
deploy with no ID requires checking Render before any retry. The workflow does
not automatically roll back, cancel sessions, or reactivate a staged worker.

| Last phase | Recovery boundary |
| --- | --- |
| CI or preflight, with no recorded writes | Fix the reported problem and start a new run. Missing credentials can fail before a receipt exists. |
| Worker drain | The coordinator remains on its previous build. Inspect any active deployment, then either finish the drain or restore the worker's previous build with drain off. Do not cancel user work just to pass the check. |
| Worker staging / old ownership check | Execution remains paused. Verify the old workers have exited before changing the coordinator. To abandon the release, restore the worker to the still-live coordinator's build and matching flags. |
| Broker staging / old ownership check | The worker remains staged. Verify the broker's live deployment and old ownership before changing the coordinator. Restoring execution requires the broker and worker to match the still-live coordinator. |
| Coordinator deployment | Keep the worker staged. Determine which coordinator commit is actually live and whether its runtime and database policy are healthy. Finish the coordinator deployment before activating a matching worker. |
| Broker activation / readiness | Keep the worker staged. Verify the broker and coordinator are on the selected build with matching configuration, successful HTTP readiness and exactly one broker owner. A Render TCP health check alone is insufficient. |
| Worker activation / final verification | Check live commit IDs, drain/staging flags, health and singleton ownership on both services. Resolve the failing check; do not deploy an arbitrary latest commit to only one service. |

A partial release is deliberately rejected by a fresh run until a clean matching
baseline is restored. An operator must use the receipt's exact old/new SHAs and
account for any schema migration before choosing rollback or completion. A green
Render deployment alone does not establish that application recovery is complete.
The preflight option can validate a restored baseline without starting a release.

GitHub serializes runs of this workflow, but cannot lock the Render dashboard.
It detects dashboard edits before mutations; this is not an atomic lock against
another operator. Keep one release owner and do not run a parallel manual deploy.

## Supported topology and local verification

This version supports one coordinator (`srv-db41eiqj9qps73fpuan0`), one worker
(`srv-db4s0b142hec73epgpt0`) and, when activated, one broker
(`srv-db4tlsajnfac738gigb0`), all private Oregon services using shared Postgres,
S3 and Temporal. It rejects auto-deploy, autoscaling, extra runtime owners,
staged/draining baselines and inconsistent split flags. The read-only probe
verifies the runtime fingerprint schema of the running build, including the
pre-broker and pre-prepared-pool formats during the first upgrade. It does not
accept a legacy fingerprint for a new build. Prepared-pool settings must match
across all roles; omitted settings use the defaults of zero prepared workspaces
and 300 seconds of idle lifetime. Further ownership or compatibility changes need
corresponding release checks; never bypass them to force a deployment.

Run the credential-free rehearsal from the repository root:

```sh
python3 -m scripts.release.rehearse --pace 0.7
python3 -m scripts.release.rehearse --separate-broker --pace 0.7
```

It runs the actual orchestrator against simulated Render, SSH and CI responses,
showing successful release ordering and a coordinator failure that leaves the
worker staged. It never contacts production. Run the unit and integration checks:

```sh
# Use only a disposable PostgreSQL database; tests create and remove isolated schemas.
MOYAI_TEST_POSTGRES_URL=postgresql://... uv run --frozen --python 3.13 pytest -q tests/release
```

The `Production release rehearsal` CI workflow provides its own PostgreSQL 17
service. It tests the real read-only ownership/drain queries as well as simulated
API failures; it does not need production credentials or trigger deployments.
