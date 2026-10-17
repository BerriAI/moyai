# Pydantic core deployment boundary

Current images run only `MOYAI_CORE=pydantic` (the default), using `/app/.venv`.
The root `uv.lock` pins the runtime and shared product services together. The
canonical `app` package contains identity, storage, accounting, schema setup and
historical formats; it contains no executable legacy engine.

The **Deploy production** workflow defaults to maintenance with Pydantic selected.
Preflight checks real role admission, provisioned schemas, current ownership and
drain. There is no qualification bypass. See [production deployment](production-deploy.md)
for operator steps and [schema migrations](schema-migrations.md) for canonical receipts.

## Configuration contract

`moyai.product_config.load_product_config(environment)` reads only the supplied
environment snapshot. It returns `ProductConfig` and rejects any retired core selector.
It does not connect to a database, create files, initialize keys, or load `.env`.

```python
config = load_product_config(os.environ)
settings = Settings(_env_file=None, **config.native_overrides())
runtime = Runtime(settings, product_config=config)
source_settings = config.legacy_settings()  # app.config.Settings
```

The source settings retain the original product keys, schema, object destination,
aliases and validators. The native settings use the same PostgreSQL database with
a separate execution schema and explicit native key. The native data directory is
`source_settings.data_dir / "pydantic"`; the source data directory remains separate.

| Environment setting | Existing-product requirement |
| --- | --- |
| `MOYAI_PRODUCT_MODE` | `existing`; `standalone` preserves the fresh native application |
| `MOYAI_RUNTIME_ROLE` | Explicit `api`, `coordinator`, `broker`, or `worker` |
| `MOYAI_DATABASE_URL` | Explicit PostgreSQL URL; no `DATABASE_URL` alias or SQLite fallback |
| `MOYAI_DATABASE_SCHEMA` | Original product schema |
| `MOYAI_NATIVE_SCHEMA` | Distinct non-system schema; maps to `Settings.database_schema` |
| `MOYAI_SCHEMA_MODE` | `verify` |
| `MOYAI_NATIVE_SCHEMA_MODE` | `verify`; maps to `Settings.native_schema_mode` |
| `MOYAI_DATABASE_INITIALIZE` | Explicit `false` |
| `SESSION_SECRET` | Original product session signing secret |
| `ENCRYPTION_KEY` | Original **raw Fernet** key; no regeneration |
| `MOYAI_NATIVE_ENCRYPTION_KEY` | Separate explicit native key input, at least 32 characters |
| `MOYAI_NATIVE_KEY_FORMAT` | `sha256-text-fernet-v1` |
| `MOYAI_NATIVE_OBJECT_FORMAT` | `fernet-hmac-sha256-v1` |
| `OBJECT_STORAGE_BUCKET`, `OBJECT_STORAGE_PREFIX` | Explicit original product destination |
| `MOYAI_NATIVE_OBJECT_PREFIX` | Separate nonoverlapping object prefix |
| `OBJECT_STORAGE_ENDPOINT`, `OBJECT_STORAGE_REGION` | Existing product values and validation |
| `OBJECT_STORAGE_ACCESS_KEY_ID`, `OBJECT_STORAGE_SECRET_ACCESS_KEY`, `OBJECT_STORAGE_SESSION_TOKEN` | Existing explicit credentials, or the existing AWS credential chain |
| `TEMPORAL_ENABLED` | `true` |
| `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE` | Explicit existing deployment destination |
| `TEMPORAL_TLS`, `TEMPORAL_API_KEY` | Original product values; product TLS default remains `true` |
| `TEMPORAL_TASK_QUEUE` | Original queue retained for its original histories |
| `MOYAI_NATIVE_TASK_QUEUE` | Distinct from both the original queue and its `-agents-v1` queue |
| `MOYAI_SEPARATE_BROKER`, `MOYAI_COORDINATOR_HANDOFF` | `true` for distributed roles |

`config.core_identity` is the code-owned `pydantic-v1`, not an operator-selected
compatibility claim. A candidate role fingerprint extends the existing role
fingerprint with core/dependency versions, native schema, queue, key and object
formats. Only ordinary coordinator activation publishes these contracts, after
the release has verified that all four source owners retired. The release never
edits policy rows directly. Native runtime and schema upgrades use maintenance,
including same-core changes. A [compatible API-only release](production-deploy.md#compatible-apiui-updates)
preserves every activated contract, both schemas' receipts and the running
maintenance generation while replacing only the API. It performs no migration
or drain. Coordinated and all-role rolling releases still reject a native source.

Native execution protocol 2 records split activity queues, bundled skill startup,
and on-demand workspace acquisition. It retains the `pydantic-v1` core identity
but changes every native role fingerprint. The v14 application migration
(`2026-10-16-product-execution-v14`) adds only an execution-compatibility receipt;
it performs no physical DDL and preserves the v1–v13 receipts and application data.
Apply it through the existing maintenance release: all source owners must retire
before native provisioning, and all candidate roles must verify before resume.

Legacy raw objects (`s3:<destination-id>:<sha256>`) remain readable through the
product adapter. Native encrypted/HMAC-addressed objects use their separate
prefix and explicit key. These formats are not interchangeable. Changing a
destination or key requires the existing verified migration procedure, not a
configuration fallback. See [object-storage.md](object-storage.md).

## Prepare the first production cutover

The workflow does not generate or derive any encryption key, object prefix,
schema name or Temporal queue. Before dispatch, the release operator must:

1. Preserve the original PostgreSQL URL/schema, raw `ENCRYPTION_KEY`,
   `SESSION_SECRET`, S3 bucket/prefix/credentials, public URL, Temporal destination
   and original task queues on all four services. Keep the existing recovery
   point and source build receipt.
2. Prepare identical explicit `MOYAI_PRODUCT_MODE=existing`,
   `MOYAI_NATIVE_SCHEMA`, `MOYAI_NATIVE_SCHEMA_MODE=verify`,
   `MOYAI_NATIVE_ENCRYPTION_KEY`, `MOYAI_NATIVE_KEY_FORMAT=sha256-text-fernet-v1`,
   `MOYAI_NATIVE_OBJECT_PREFIX`,
   `MOYAI_NATIVE_OBJECT_FORMAT=fernet-hmac-sha256-v1` and
   `MOYAI_NATIVE_TASK_QUEUE` on each service. Use a separately named native
   schema, nonoverlapping object prefix and versioned native queue. For an existing
   native destination, retain its exact key and formats; never infer them from
   the source key or bucket. Keep local rehearsal disabled.
3. Make those prepared values part of each **running source process**
   environment while its core remains `legacy`. Merely editing Render's saved
   environment is insufficient: the source process digest must match it. Use a
   reviewed source-build restart/operations window, preserving all original
   runtime settings, and return to four healthy matching source deployments
   before dispatch. Do not set the source `MOYAI_CORE=pydantic` manually.
4. Review the native destination. An empty schema is an explicit provisioning
   plan. A populated schema needs both known application migration receipts and
   a Harness receipt matching the installed SDK versions and current
   columns/indexes/sequences. Partial or unknown installations require recovery;
   the release refuses to stamp them as valid. Provisioning never copies source
   sessions, credentials, objects or Temporal histories.
   Candidate preflight also checks PostgreSQL's rendered index identifiers:
   some long schema names cause distinct generated indexes to shorten to the
   same name. Such a configuration fails before source drain. Select a different
   verified-empty namespace during preparation. A populated destination requires
   a reviewed index migration; do not rename its schema or change its receipts
   to bypass the check.
5. Pause existing source automations through the existing application before
   switching cores. Let the original coordinator confirm the current definition
   revision and installed schedule format with an empty synchronization error,
   and let its pending event inbox settle while the old worker remains
   available. The release never pauses or cancels these on the operator's behalf.
   The source definitions/history remain available for their explicit native
   migration and re-enablement after cutover.
6. Dispatch the existing workflow on the selected `main` commit with
   `release_type=maintenance`, `execution_core=pydantic`, `preflight_only=true`.
   Inspect `core_migration`, `schema_migration`, `native_migration` and the source
   identities in the saved release receipt. First adoption requires the native
   CI workflow to have succeeded on that exact main commit, alongside the
   existing Docker, runtime and release CI.

After review, dispatch the same selected candidate with `preflight_only=false`.
A newer main commit is a new candidate and must pass its own checks. For an existing native deployment, `execution_core=preserve` keeps Pydantic.
A legacy source must explicitly select Pydantic for this candidate; preserving
its old engine fails preflight before any service changes.

The underlying entrypoint is:

```sh
.venv/bin/python -m scripts.release.deploy \
  --release-type maintenance --core pydantic --preflight --receipt release-receipt.json
```

It retains the existing GitHub-main, Render topology and credential checks.
Do not run it against production during local qualification.

## Maintenance ordering and drain

For a legacy source, the controller redeploys the source worker with drain enabled
using its original core and `AGENT_HARNESS`. For a native source, it first writes
a generation-bound in-place pause and keeps the original worker polling until
the SDK reaches verified safe boundaries; only then does it replace that worker.
During a core switch, it also requires every unfinished
source run—including queued work and saved waiting phases—to settle. Native
counts include waiting/unknown turns, unresolved effects and pending or
unknown inference attempts. Queued native requests remain preserved during a
same-core deployment. Pre-existing `native_queued` and `native_parked` work must
reach zero before a core switch; a legacy worker cannot execute those native
histories. Once the candidate native policy owns admission, new native inputs can
queue while waiting for the final explicit resume.
Long-running work or uncertain external outcomes
can block the window; the release never cancels, replays or declares them settled.
Core retirement also checks canonical automation inbox rows with
`native_pending`/`native_dispatched` status (or an unknown `native_` status),
plus native automation bindings.
A binding's pause is confirmed only when its state is `paused` or `deleted`,
`sync_state=ready`, and `synced_generation=generation`.

The current automation protocol does **not** persist every Temporal sync, tick
and event workflow/run identity. A confirmed schedule pause and zero SQL work
therefore do not establish that earlier control workflows ended. Until the
producer fence and exact workflow-drain receipt are qualified,
`native_automation_history` counts retained bindings and blocks retiring the
native core whenever any exist. Same-core maintenance preserves them. The
release does not erase history, cancel workflows, pause schedules automatically,
or treat a visibility-list result as a quiescence receipt.

The runtime drain contract is explicit: redeploying the worker with
`MAINTENANCE_DRAIN=true` must stop new native execution cluster-wide while
permitting admitted work to settle. Coordinator dispatch and scheduled admission
must observe that boundary; skipping worker startup alone is insufficient.
`native_runs` counts all turn states except `queued`, `completed`, `failed`,
`stopped`, and `resumed`, less starting/running/saving turns with a matching
current-generation, paused, unresumed `parked` receipt for that exact workflow.
Those receipts are counted separately as `native_parked`; they remain forbidden
when retiring the native core. A waiting turn can park only at the qualified
`deferred_waiting` boundary, with an accepted result and a nonempty saved
conversation as well as the same generation/workflow checks. The runtime
establishes that receipt after checking the completed Temporal result. Other
waiting and unknown states block.
The release does not manufacture a drain receipt or turn a runtime failure into
zero work. Qualification must exercise this worker-to-coordinator drain handoff,
including preserving queued input through worker replacement.

For original Temporal schedules, `scripts/release/automation_probe.py` runs in
the legacy interpreter. It reads the tracked schedule IDs and source pause
metadata in a bounded read-only transaction, verifies each schedule is remotely
paused and still targets `AutomationWorkflow` on the original queue, and checks
the metadata and source process identity again. The controller repeats that
check after all source owners retire and before candidate activation; a changed
receipt or unknown remote response stops the release.

This establishes **paused admission with preserved original control histories**,
not terminal retirement of every old Temporal execution. The original API is
staged before its other roles, all source producers then lose ownership, and
the canonical paused/revision checks reject late scheduled admission. Existing
control histories stay on their original queue; the new core never polls or
replays them. The release receipt records
`legacy_control_disposition=preserved_on_original_queue_with_paused_admission`.
The actual application qualification must prove that late old/current-revision
control executions cannot create a run after the source pause.

For a same-core native upgrade, staging first replaces and retires the API.
The controller then starts worker, broker and coordinator replacements as one
batch. Render builds and replaces those services concurrently. Each service
retains its own saved request, deployment identity, source check and exact
owner-retirement check. All three must finish before the zero-owner migration
boundary. Core switches retain sequential API, worker, broker and coordinator
staging. Each candidate
receives the explicit `MOYAI_CORE` selection only after its stage/build flags are
saved and the drain has been verified. Its previous selector value (including
absence) is recorded before the one PUT. `AGENT_HARNESS` is preserved for a
Pydantic cutover ; current candidates cannot switch back to a retired engine. The old process keeps its
independent environment snapshot until it retires.

After all four owners retire, any required source offline migration runs through
the existing guard. Native provisioning then acquires the canonical **exclusive
runtime ownership lock** and rechecks source revision, policy, role contracts,
all four ownership inventories and source/native drain counts. That lock stays
held across the native application's and Harness's separate transactions and
the subsequent verification command. A receipt change after preflight stops
the migration. The native migration lock alone is not used as proof of drain.

Same-core native activation proceeds coordinator → broker → worker and API
together. The persisted maintenance generation keeps execution paused throughout.
Native worker startup with `MAINTENANCE_DRAIN=false` never automatically resumes
that generation, so this path keeps the flag false and avoids restarting the old
and new workers merely to toggle it. After all four candidates are healthy and
every prior owner has retired, the controller issues one explicit resume CAS.
This performs eight Render deployments in five batches instead of ten sequential
deployments. Core switches retain coordinator → broker → drained worker → API →
final worker undrain and their existing core-retirement checks.
Active native health must return exactly `{"status":"ok"}`, the existing
build/role/owner/rolling headers and `X-Moyai-Core: pydantic-v1`; coordinator and
broker leadership headers retain their existing meanings. Staged health owns no
runtime and retains `{"status":"ok","mode":"migration_staging"}`.

Native ORM and Harness transactions retain the canonical shared `726940`
runtime fence. A subordinate connection binds a shared session marker
`(726948, canonical_owner_pid)` before acquiring that fence. The runtime verifies
the parent's exact backend start, live shared owner lock and admitted policy;
it does not mark the canonical owner or rebind a connection to another parent.
The release probe reports valid subordinate fences separately from process
owners. An unmarked holder remains an owner; orphan, self, chained, ambiguous,
wrong-namespace and role-owning markers fail the observation. A subordinate
cannot supply HTTP process identity. The exclusive migration guard still sees
every raw fence, including a child transaction whose parent has retired.

## Native maintenance control contract

`scripts/release/native_control.py` invokes the installed native interpreter:

```sh
/app/.venv/bin/python -m moyai.product_drain request
/app/.venv/bin/python -m moyai.product_drain status
/app/.venv/bin/python -m moyai.product_drain resume
```

Each command reads at most 16 KiB of JSON from stdin. The request includes
`protocol=1`, `core=pydantic-v1`, `native_schema`, `task_queue`, and for writes:
`expected_worker` (PID, timezone-aware backend start, actual source policy,
worker policy), `expected_generation`, and a bounded unique `request_key`.
The release records this intent before requesting a write. A compatible
coordinator replacement can retain the previous cluster policy; the request
uses that actual admitted policy, not a recomputed Settings fingerprint.

Write receipts bind the new generation, paused state, request fingerprint,
requester identity and source policy. Status additionally reports queued,
parked, unsafe-turn, effect and inference-request counts and the exact live
registered workers. The legacy SQL probes independently verify the generation
and safe-park receipts in their read-only snapshot. An unknown response stops;
it is never converted into a successful pause/resume or automatically retried.

The reviewed native migration seeds generation zero paused. At the first
cutover, the release verifies that bootstrap while all roles are staged, before
activating the coordinator. An in-place pause retains the old worker until work
parks; an explicit resume rejects a live draining worker or unresolved effects
and requests. Historical retirement checks remain fail-closed; the current release CLI does
not offer a native-to-legacy target.

## Explicit schema provisioning

The release transport runs these existing commands with
`/app/.venv/bin/python`, from `/app`, in the staged coordinator. The image
and workflow install the one root workspace environment; product support is included.
Direct operator use requires the same reviewed maintenance/ownership boundary;
these commands alone are not a deployment controller.

Canonical `app.schema_migrations.migrate` uses that same interpreter through the
existing maintenance transport. Its centralized `app/schema_initializers.py`
registry needs no retired executor imports. The extraction preserves the original
component identities, revision 21 and historical manifest digest.

```sh
.venv/bin/python -m moyai.product_start provision --apply
.venv/bin/python -m moyai.product_start verify
```

The first command checks the source product's migration receipt in a bounded
read-only transaction. It then calls the reviewed native app-schema migration,
installs/verifies the canonical source-continuation fence, and calls
`NativeStores.start(provision=True)`. SDK provisioning uses public
read operations and records SDK versions plus a column/index/sequence digest.
It does not start a Runtime, agent, worker, dispatcher, or user bootstrap.
Application and SDK receipts are separate; a partially completed provisioning
attempt does not make ordinary startup pass both checks.
The source-continuation fence must also verify before a native role publishes or
joins the canonical runtime policy; a native-only schema fixture does not prove
that canonical source migration or fence.

Normal existing-product startup verifies both receipts. Native application
schema verification does not run `create_all`. **The pinned Harness stores still
execute their stock lazy, idempotent schema DDL on first use.** There is no public
SDK flag to disable this and no private-method monkeypatch. Runtime credentials
therefore still need the permissions required by that SDK behavior. Provisioning
and shape verification are not a claim of strict no-DDL operation.

The CLI contract consumed by `scripts/release/native_migration.py` is:

| Command | Required JSON fields on successful exit |
| --- | --- |
| `provision --apply` | `core="pydantic-v1"`, configured `native_schema`, nonempty `app_schema` revision, `harness_schema="provisioned"` |
| `verify` | `core="pydantic-v1"`, configured `native_schema`, `app_schema="verified"`, `harness_schema="verified"` |

Additional fields such as `deployment_readiness` do not grant readiness.
Failures exit nonzero without secrets. The wrapper captures output, never retries
a potentially applied command, and returns a receipt only after both manifests
match the candidate and the guarded source state is unchanged. Public maintenance
probes run in `/app/.venv/bin/python`; they use psycopg/SQL and do not import the
native SDK or depend on `scripts/` being installed inside the image.

## Failure and rollback

All ambiguous PUT/POST/SSH outcomes stop at their recorded intent. A fresh run
rejects a partially staged baseline. Reconcile the receipt and actual processes
before recovery; do not retry a failed provisioning call just because the staged
HTTP listener is healthy. The source and native receipts remain separate, so a
partially provisioned Harness cannot hide behind a successful application receipt.

For concurrent replacements, inspect every entry in `batches[].deployments`.
`prepared` means no deployment request was sent; `requested` with no ID means
the POST outcome is unknown. A requested peer can finish after another member
fails or the workflow stops. Batch failures never cancel peers, retry writes,
start migration, or resume execution. Reconcile every requested service and
its actual process/owner before recovery.

After the v14 receipt is applied, use roll-forward recovery with a candidate that
understands protocol 2. The maintenance controller inventories every native
receipt and rejects a v13-only candidate during preflight, before any pause or
deployment transition, including a same-core forward revert. Do not remove the
v14 receipt or re-stamp earlier receipts to permit old workers. Protocol 2's role
fingerprints also prevent protocol 1 roles from joining the published contract.
Replaying old histories on new code establishes forward compatibility only; it
does not qualify new histories on the v13 worker. This boundary depends on the
supported release controller: the old runtime verifier checks its expected
receipts individually, so bypassing the controller is not a qualified rollback.

Current images cannot select a legacy engine. Retain earlier source images and
their release receipts for recovery assessment, but an earlier image is safe only
when its schema, histories and ownership protocols remain compatible. Use
roll-forward recovery once the native execution receipt excludes that image.
Never replay old Temporal histories on the native queue, delete data to satisfy
a probe, or rotate encryption keys to recover.

## Local existing-product rehearsal

Explicit local mode permits isolated integration on loopback resources. It does
not qualify the installation or approve a production deployment:

```sh
export MOYAI_CORE=pydantic
export MOYAI_PRODUCT_MODE=existing
export MOYAI_PRODUCT_LOCAL_REHEARSAL=true
export MOYAI_RUNTIME_ROLE=standalone
export MOYAI_SEPARATE_BROKER=false
export MOYAI_COORDINATOR_HANDOFF=false
export TEMPORAL_TLS=false
# Supply the remaining required source/native configuration above.
.venv/bin/python -m moyai.product_start serve --host 127.0.0.1 --port 8896
```

This path requires loopback PostgreSQL, Temporal, public URL and S3 endpoints,
rejects Render markers, and only binds a loopback listener. It starts the real
native API, dispatcher and Temporal worker. It uses explicit existing schemas;
it does not initialize a new product installation. Use isolated test data and
the authorized local E2E provider launcher when testing model behavior.

Use a free isolated listener and never restart a user's active local instance.
Disposable local schemas do not require production backup procedures. Keep
independently owned test services separate from services used by other active
qualification runs; coordinate before resetting a shared S3 service.
Local `api` and `worker` roles can also run separately. API does not run dispatcher
or worker loops. `check` reports configuration and dependency validity only,
with `readiness=not_checked`. Four-role tests must exercise the real qualified
coordinator/broker lifecycle; standalone rehearsal cannot establish those claims.

The existing `moyai dev` command also accepts this explicit standalone local
rehearsal configuration. It starts a local Temporal server using the effective
native namespace and `<source DATA_DIR>/pydantic/temporal.db`. Only that dev path
creates the local Temporal directory, with mode `0700`. In existing mode,
`moyai user` rejects native account creation, and direct `moyai serve`/`worker`
direct callers to `product_start` so role flags cannot be bypassed.

## Application qualification and focused release checks

- Wire native authority into the broker admission/accounting contract. A native
  RunScope cannot be passed to legacy `/broker/{run}` routes as a fabricated run.
- Map the original model selection, concurrency and unlimited iteration policy
  explicitly; prototype model-call limits are not equivalent.
- Preserve coordinator leader ownership, broker maintenance ownership, API
  owner identity, health headers, admission fences and drain behavior.
- Rehearse the integrated release path through actual source and candidate
  application deployments, including mixed-version boundaries and rollback.
- Verify restoration, old object reads, authorization revocation and native
  workflow recovery through the actual locally deployed application.

Run focused supplementary tests with a disposable PostgreSQL URL:

```sh
uv sync --frozen --python 3.13
MOYAI_TEST_POSTGRES_URL=postgresql://... \
  uv run --frozen --python 3.13 pytest -q tests/release
```

These tests exercise the real release controller with simulated Render responses,
real PostgreSQL guard/drain queries, and (when the native interpreter is installed)
the real Harness schema receipt across supported old/new image layouts. They do not replace the
required baseline/final locally deployed Moyai UI/service proof, real configured
model/Modal execution, recording and screenshot. Deployment acceptance requires that evidence for the actual target environment.
