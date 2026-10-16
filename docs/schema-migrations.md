# Explicit PostgreSQL schema migrations

The canonical product requires revision **21**, with compatibility floor **19**.
The Pydantic application and Harness have separate receipts; see the
[deployment contract](pydantic-deployment.md). Use the guarded maintenance release
for current native upgrades. The retained online migration engine and its ledger
support historical schema compatibility; they do not make native rolling releases
qualified.

`MOYAI_SCHEMA_MODE=auto` remains the default for existing installations and SQLite.
For an explicitly migrated PostgreSQL schema, use `MOYAI_SCHEMA_MODE=verify` on
**every role**. Constructors then verify the code-owned schema requirement and skip
all component schema setup, legacy backfills and default insertion. Normal
application data writes and runtime-policy publication still work. Startup also
rejects accidental DDL through the application database adapter.
An exact clean revision is admitted when any incoming online receipt agrees with
the binary's known source and target digests. That receipt retains its historical
compatibility value even after a coordinated baseline increase; startup does not
need to admit the earlier schema below the new baseline. An older bridge can also
admit a newer physical revision only when a complete committed receipt chain proves
compatible additive steps from its own known schema digest. A newer number alone
is insufficient.

## First activation (coordinated maintenance)

1. Back up PostgreSQL. Use the merged target release and the same PostgreSQL
   schema, encryption/signing keys, model default and organization settings as
   the application. Preserve any existing shared files and credentials.
2. Drain work and stop all application, worker and broker database owners using
   the existing coordinated maintenance procedure. Do not cancel user work just
   to make the migration run. The migrator refuses to run while any runtime
   owner or another migrator holds the database; it does not terminate them.
3. In the target release environment, run:

   ```sh
   python -m app.schema_migrations --apply
   ```

   It prints `{"schema_revision": 21, "status": "ready"}` on success. An empty
   schema must already exist, and requires `MOYAI_DATABASE_INITIALIZE=true`.
   Existing schemas do not need that flag. No credentials are printed.
4. Set `MOYAI_SCHEMA_MODE=verify` on every role, including the dedicated API.
   Use the same reviewed release and shared settings for this first activation.
   Start the coordinator to publish the contracts, then broker, workers and API.
   Activate role routing and coordinator handoff following the production deployment runbook;
   verify health and an ordinary saved session before reopening ingress.

The API-only and rolling release modes do **not** perform this first migration
or topology activation. Complete the separately coordinated maintenance
transition before enabling them. A normal pre-deploy hook that runs while the old
app is live will be rejected. Once this activation is complete, deploy the bridge
at the same schema revision to all four roles before using online expansions.
An installation still on an earlier revision, such as revision 14, first needs
the one-time maintenance adoption of revision 21; the empty online registry
does not provide a migration path for it.

## Private-session upgrade

Revision 19 adds immutable private ownership and personal Slack grants. It also
advances the compatibility baseline to 19: older application code treats all
sessions as shared and cannot safely overlap once private sessions exist.

Use the coordinated maintenance steps above for an existing revision-18 deployment:
back up the database, drain work, stop **all four roles** (including the dedicated
API), run `python -m app.schema_migrations --apply` from the target release, and
start every role at that release with `MOYAI_SCHEMA_MODE=verify`. Confirm the
migration prints revision 19, health checks pass, and an ordinary saved session
opens before reopening ingress. Verify that a private test session returns 404
for a second individual account.

The rolling and API release actions reject this compatibility change. The original
coordinated action covers the older two/three-service topology and does not perform
this four-role migration. Do not use it to bypass the compatibility check.

## Reviewed online expansions

The following describes the retained canonical online engine. Current native
releases still use maintenance. Keep `MOYAI_SCHEMA_MODE=verify`. The controller computes the plan from the immutable
candidate checkout, verifies every live role's installed protocol support, and
applies the plan before replacing any service. `preflight_only` validates the
entire plan and records it while making no database or service writes.

`app/online_schema_migrations.json` is the explicit reviewed registry. Each step
names its source and target revision, their digests from `schema_versions.json`,
the unchanged compatibility revision, and a list of declarative operations.
Steps must be contiguous. The bridge initially has an empty registry and retains
compatibility revision 19, preserving the existing role fingerprints.

Protocol 1 accepts only:

- New tables with explicit columns, fixed supported data types and optional
  primary keys.
- Nullable columns without defaults on existing tables.
- Plain indexes on tables created in the same migration step.

There is no arbitrary SQL, table/column removal or rename, type replacement,
new required column on an existing table, default expression, foreign key,
existing-table index build, or data backfill. A revision needing any of these
must use a separately reviewed maintenance transition. Structural admission does
not replace application review: old and new readers/writers must tolerate the
expanded shape and preserve their shared runtime contracts.

The installed engine validates the plan and its source against a local schema
digest or verified receipt chain. A dedicated online migration lock prevents two
plans running together, while a shared runtime lock excludes the offline
migrator without excluding active application owners. DDL, durable step receipts
and the new schema revision commit in one transaction. The lock timeout is
250 ms, the statement timeout is five seconds, and the transaction budget is
20 seconds. PostgreSQL 17 and newer enforce that total at the server; the engine
also checks a monotonic deadline between operations. Statement or validation
failures inside the transaction roll the whole plan back. A lost commit
acknowledgement remains uncertain and does not imply rollback. No background
jobs, provider clients or schema initializers are started by this path.

After commit, all four existing service identities must still be healthy with
the same policy/contracts and the target schema before replacement begins. An
uncertain response stops the release without retry or rollback. Inspect the
receipt and database state; a fresh action can observe a complete compatible
target and resume replacements without executing the plan again. A partial
receipt chain, dirty state or unknown schema drift is rejected.

## Maintenance migration failure and rollback

The exclusive database ownership connection lasts through all schema functions,
backfills and the final receipt. The migrator commits a dirty marker before any
schema changes; only a complete run marks the revision ready. It creates the
schema for optional Temporal and tracing features even if they are disabled.
It never constructs app/provider clients, starts background jobs, cancels turns,
or changes the cluster's runtime fingerprint.

If a migration fails, startup refuses the dirty/incompatible schema. Fix the
cause and rerun the same release's command with all owners stopped. Component
transactions can already have committed; the command is retryable, not one
large atomic database rollback. Do not clear the dirty marker manually.

A migrator refuses schemas with a newer revision. After the first explicit
migration, auto mode refuses the schema: keep verify mode for rollbacks to a
release supporting the same revision or a verified compatible expansion. A
pre-feature release or a breaking schema downgrade requires a separately
reviewed database restore/rollback procedure. Do not drop the online ledger to
force admission by an older binary.

## Changing the schema

Keep canonical feature schema setup and historical backfills in
`app/schema_initializers.py`; Store, Security and Spend retain their core methods.
Register components in dependency order in `app/schema_migrations.py`.
Do not place DDL, migration backfills or seed writes in ordinary constructors.

Increment `SCHEMA_REVISION` when any initializer changes and **append**, never
rewrite, that revision's digest in `app/schema_versions.json`. The manifest test
prints the expected normalized source digest. CI also checks that every component
initializer is registered. Revisions must be contiguous. For an online expansion,
also append the reviewed declarative step and keep `SCHEMA_COMPATIBILITY_REVISION`
unchanged only after verifying mixed-version reads/writes. Missing declarations
fail closed. A breaking change advances the compatibility revision and uses the
maintenance procedure; compatibility across revisions is never inferred. When
raising the compatibility baseline, remove the old compatibility's steps from
the candidate's online registry. Preserve their committed database receipts and
versioned source digests; neither history should be deleted to permit startup.

For example, a reviewed future step can add one optional field without rewriting
existing rows. Its `operations` entry would be:

```json
{"kind": "add_column", "table": "runs", "name": "optional_note", "type": "TEXT", "nullable": true}
```

Wrap operations in a step with `from_revision`, `to_revision`, `compatibility`,
`source_digest`, and `target_digest`, using the actual entries from
`schema_versions.json`. The plan digest is computed by the planner, not entered
manually. Types describe PostgreSQL directly: use `BIGINT` for a column whose
SQLite initializer says `INTEGER`. Each schema PR must test its declared DDL
against the target initializer, as well as old/new reads and writes; the source
digest binds the declaration to that revision but does not prove DDL equivalence.

## Local proof

Point `MOYAI_TEST_POSTGRES_URL` at a disposable PostgreSQL database, then run:

```sh
python -m pytest -q tests/test_schema_migrations.py
python -m pytest -q native/tests/test_canonical_schema_migrations.py
python -m pytest -q tests/test_online_migrations.py tests/release
python -m scripts.online_migration_probe --output /tmp/moyai-online-migration
```

The canonical regression creates/drops only its own randomly named test schema.
It verifies migration, historical session preservation and no retired-engine imports. It does not run
Temporal, inference, or external provider requests; it proves schema admission
and restart behavior, not production traffic handoff.

The online probe keeps a separate worker process writing and renewing its lease
through a synthetic future expansion. It reconnects an old-source reader, kills
a migration process after real uncommitted DDL, and verifies rollback and an
explicit inspected retry. The required production release rehearsal runs these
checks and saves their receipts. This remains a local PostgreSQL proof; a real
Render deployment with an active session is still required for production
continuity qualification.
