# Four-service maintenance deployment

Use this path for the planned Deep Agents upgrade when the API, worker, broker
and coordinator are already separate services. PR #400 advances the runtime
protocol from 17 to 18. The deployed schema 18 also needs an offline migration to
19; its compatibility floor advances to 19 and there is no online migration plan.
The rolling workflow correctly refuses this transition.

In **Actions → Deploy production → Run workflow**, select **main** and
**release_type: maintenance**. First use **preflight_only: true** to inspect the
selected commit and live topology without changes. During the agreed maintenance
window, run with **preflight_only: false**. The workflow pins main at button press,
waits for that exact commit's CI, and performs preflight again before any write.
Merging this tooling alone does not deploy anything.

## Preconditions

- All four services must run the same previous build with healthy runtime
  ownership, staging and drain disabled, verified schema mode, separate broker
  and coordinator handoff enabled. The dedicated API ID comes from the existing
  `moyai-production` environment variable `MOYAI_API_SERVICE_ID`.
- Keep Render auto-deploy disabled. One release owner must avoid dashboard edits
  and separate manual deployments until the workflow finishes.
- Preserve database, object storage, signing/encryption keys, Temporal settings,
  permissions and capacity configuration. Only `MOYAI_BUILD_SHA`,
  `MAINTENANCE_DRAIN` and `RENDER_MIGRATION_STAGE` are changed.
- The candidate's effective default must be `deepagents-temporal` on every role.
  A stored `AGENT_HARNESS` override retaining a native engine stops preflight;
  review that configuration explicitly before retrying. Saved sessions retain
  their own harness selections.
- Arrange database recovery according to the existing backup policy before an
  offline schema change. The controller does not create a backup or provide an
  automatic downgrade path.

## Sequence and service interruption

1. Redeploy the existing worker build with drain enabled. Wait for saved execution
   boundaries, no pending/running model requests, and no live execution leases.
   Queued messages remain saved; the controller does not cancel user work.
2. Stage the candidate API, worker, broker and coordinator, in that order. Each
   staged service serves maintenance health without running the application.
   Verify each old process has retired using its database PID and start time.
   The API is unavailable during staging; callers must retain and retry their
   requests after service resumes.
3. Verify that **all runtime owners have exited**. For an offline schema change,
   invoke the staged coordinator's installed migrator once over private SSH.
   It takes exclusive runtime ownership and rechecks schema, contracts and drain
   safety under that lock before even marking the schema dirty. Confirm the new
   clean schema, unchanged stored contracts and still-empty ownership inventory.
4. Activate the candidate coordinator, broker, drained worker and API. Normal
   coordinator startup publishes the new protocol; the controller never rewrites
   compatibility fingerprints. Verify source, health, contracts and exact
   ownership after every replacement.
5. Release the worker drain with one final deployment, then verify all four
   services and preserved configuration. Queued execution can now resume.

This sequence uses ten Render deployments. It includes a service interruption;
it is not a blue/green or zero-downtime upgrade. Later compatible releases can
return to the default [rolling deployment](rolling-deploy.md) path.

## If a phase fails

Download the **production-release** artifact and inspect the last phase, every
recorded environment/deployment intent, and `schema_migration`. A lost response
can mean a write succeeded. The controller never retries an ambiguous mutation,
automatically rolls back, cancels sessions or resumes a staged worker on failure.

| Boundary | Required inspection |
| --- | --- |
| CI/preflight, no mutations | Fix the failed check or configuration. Production is unchanged. |
| Worker drain | New execution may be paused on the old build. Inspect active work and leases; do not cancel work to force the drain. |
| Role staging | Verify which old processes remain and which services are staged. Keep incompatible candidate roles inactive until every old owner is gone. |
| Offline migration | Inspect the schema revision and dirty marker on the private database. An unconfirmed response must be reconciled even if Render reports healthy staging. Do not rerun the deployment blindly. |
| Candidate activation | Keep the worker drained. Determine the actual schema, installed source and activated contracts, then finish activation with the receipt's exact candidate commit. |
| Final worker release | Work may already have resumed. Check saved action receipts and actual runtime ownership before any corrective deployment. |

After schema 19 is applied, an old schema-18 binary is not a safe rollback.
Restore from an approved backup or complete the matching candidate deployment
under an explicit recovery plan. A fresh maintenance run rejects a partial
baseline instead of inferring how to recover it.

GitHub concurrency serializes this workflow; it cannot lock the Render dashboard.
Control-plane checks detect observed edits but are not an atomic lock against
another operator.

## Local validation

The release tests cover ambiguous writes, failure at each deployment boundary,
exact owner retirement and real PostgreSQL read-only drain snapshots. Run against
a disposable PostgreSQL database; tests create and delete isolated schemas:

```sh
MOYAI_TEST_POSTGRES_URL=postgresql://... uv run --frozen --python 3.13 pytest -q tests/release
```

Run the real controller with synthetic Render, SSH, CI and migration responses:

```sh
uv run --frozen --python 3.13 python -m scripts.release.rehearse_maintenance --output /tmp/maintenance-rehearsal --pace 0.3
```

The rehearsal records successful ordering, an old owner that will not retire, and
an uncertain migration response. All three use synthetic data and no credentials.

These tests do not establish production traffic recovery, native SDK continuation
or provider latency. Keep those application qualifications separate from the
deployment controller receipt.
