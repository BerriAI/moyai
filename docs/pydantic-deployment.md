# Pydantic runtime deployment contract

Current images run only `MOYAI_CORE=pydantic` (the default), using `/app/.venv`.
The root `uv.lock` pins the runtime and shared product services together. The
canonical `app` package contains identity, storage, accounting, schema setup and
historical formats; it contains no executable legacy engine.

This guide describes the public runtime interfaces for self-hosted installations.
Managed deployment workflows and operator runbooks live in a private repository. See
[Docker installation](../deploy/docker/README.md) for self-hosting and
[schema migrations](schema-migrations.md) for canonical receipts.

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
compatibility claim. Role fingerprints include core/dependency versions, native
schema, queue, key and object formats. Normal coordinator activation publishes
these contracts. Incompatible schema, framework or protocol changes require a
coordinated maintenance window with all previous runtime owners retired.
Compatible replacements must preserve the activated contracts and schema receipts.

Native execution protocol 2 records split activity queues, bundled skill startup,
and on-demand workspace acquisition. It retains the `pydantic-v1` core identity
but changes every native role fingerprint. The v14 application migration
(`2026-10-16-product-execution-v14`) adds only an execution-compatibility receipt;
it performs no physical DDL and preserves the v1–v13 receipts and application data.
All runtime owners must retire before native provisioning, and every replacement
role must verify the new receipt before execution resumes.

Legacy raw objects (`s3:<destination-id>:<sha256>`) remain readable through the
product adapter. Native encrypted/HMAC-addressed objects use their separate
prefix and explicit key. These formats are not interchangeable. Changing a
destination or key requires the existing verified migration procedure, not a
configuration fallback. See [object-storage.md](object-storage.md).

For the v21 Slack message/provenance cutover, encrypted inbox compatibility and
legacy transcript repair, follow [Slack messages in Moyai](slack-message-integrity.md#upgrade-and-repair).

## Native maintenance control contract

The installed runtime provides the following maintenance interface:

```sh
python -m moyai.product_drain request
python -m moyai.product_drain status
python -m moyai.product_drain resume
```

Each command reads at most 16 KiB of JSON from stdin. The request includes
`protocol=1`, `core=pydantic-v1`, `native_schema`, `task_queue`, and for writes:
`expected_worker` (PID, timezone-aware backend start, actual source policy,
worker policy), `expected_generation`, and a bounded unique `request_key`.
Callers must record this intent before requesting a write. A compatible
coordinator replacement can retain the previous cluster policy; the request
uses that actual admitted policy, not a recomputed Settings fingerprint.

Write receipts bind the new generation, paused state, request fingerprint,
requester identity and source policy. Status additionally reports queued,
parked, unsafe-turn, effect and inference-request counts and the exact live
registered workers. An unknown response does not establish a successful
pause/resume; inspect the persisted state before deciding whether to retry.

The native migration seeds generation zero paused. Verify that bootstrap before
activating the coordinator. An in-place pause retains the old worker until work
parks; an explicit resume rejects a live draining worker or unresolved effects
and requests.

## Explicit schema provisioning

Install the root workspace environment with product support, then run these
commands only after draining work and stopping every runtime owner. Provisioning
requires exclusive canonical runtime ownership; these commands do not coordinate
an application deployment.

Canonical `app.schema_migrations.migrate` uses the same workspace interpreter. Its
centralized `app/schema_initializers.py` registry preserves the original component
identities, revision 21 and historical manifest digest.

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

The public CLI success contract is:

| Command | Required JSON fields on successful exit |
| --- | --- |
| `provision --apply` | `core="pydantic-v1"`, configured `native_schema`, nonempty `app_schema` revision, `harness_schema="provisioned"` |
| `verify` | `core="pydantic-v1"`, configured `native_schema`, `app_schema="verified"`, `harness_schema="verified"` |

Additional fields such as `deployment_readiness` do not grant readiness.
Failures exit nonzero without secrets. Callers must verify both manifests and
preserve the maintenance boundary before allowing startup. A lost command response
requires inspection; it does not prove that provisioning was rolled back.

## Recovery compatibility

Application and Harness receipts remain separate, so a partially provisioned
Harness cannot hide behind a successful application receipt. Inspect both before
retrying provisioning. Never delete or rewrite a receipt to admit an older binary.
Earlier images are suitable for recovery only when their schemas, histories and
ownership protocols remain compatible. See the private operations runbooks for
managed deployment recovery.

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

## Local runtime checks

Run focused checks with disposable PostgreSQL databases configured through
`MOYAI_TEST_POSTGRES_URL` and `MOYAI_TEST_POSTGRES`:

```sh
uv run --frozen pytest -q tests/test_render.py tests/test_render_shutdown.py tests/test_online_migrations.py
uv run --frozen pytest -q native/tests/test_product_execution_version.py native/tests/test_product_drain.py
```

These tests cover runtime admission, maintenance generations, preserved migration
receipts, startup and shutdown. Qualify provider execution, authorization, object
reads and workflow recovery through the actual self-hosted application as well;
see [verification](verification.md).
