# Explicit PostgreSQL schema migrations

This is the database prerequisite for overlapping app replacements. It does not
allow overlapping coordinators or mixed builds yet. The exact-build fingerprint,
coordinator/broker ownership, and existing coordinated release drain remain in
force. Production is not opted in by merging this change.

`MOYAI_SCHEMA_MODE=auto` remains the default for existing installations and SQLite.
For an explicitly migrated PostgreSQL schema, use `MOYAI_SCHEMA_MODE=verify` on
**every role**. Constructors then verify the code-owned schema revision and skip
all component schema setup, legacy backfills and default insertion. Normal
application data writes and runtime-policy publication still work. Startup also
rejects accidental DDL through the application database adapter.

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

   It prints `{"schema_revision": 2, "status": "ready"}` on success. An empty
   schema must already exist, and requires `MOYAI_DATABASE_INITIALIZE=true`.
   Existing schemas do not need that flag. No credentials are printed.
4. Set `MOYAI_SCHEMA_MODE=verify` on the coordinator, broker and workers. Start
   the coordinator first, then the matching broker and workers. Verify their
   health and an ordinary saved session before reopening ingress.

The current GitHub production release workflow does **not** run this command or
activate verify mode. Keep production in auto mode until its maintenance release
is wired to invoke the migrator with all runtime owners stopped. In particular,
a normal pre-deploy hook that runs while the old app is live will be rejected.

## Failure and rollback

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
release supporting the same revision. A pre-feature release or an older schema
revision requires a separately reviewed database restore/rollback procedure.

## Changing the schema

Keep schema setup and legacy backfills in each component's `initialize_schema`
function (core Store/database methods use `_initialize_schema`). Add new
components to the dependency-ordered registry in `app/schema_migrations.py`.
Do not place DDL, migration backfills or seed writes in ordinary constructors.

Increment `SCHEMA_REVISION` when any initializer changes and **append**, never
rewrite, that revision's digest in `app/schema_versions.json`. The manifest test
prints the expected normalized source digest. CI also checks that every component
initializer is registered. Revisions must be contiguous. Compatibility across
different schema revisions is deliberately not assumed.

## Local proof

Point `MOYAI_TEST_POSTGRES_URL` at a disposable PostgreSQL database, then run:

```sh
python -m pytest -q tests/test_schema_migrations.py
python -m scripts.schema_migration_probe --output /tmp/moyai-schema-proof
```

The probe creates/drops only its own randomly named test schema. It records real
migration and constructor results in JSON and a terminal cast. It does not run
Temporal, inference, or external provider requests; it proves schema admission
and restart behavior, not production traffic handoff.
