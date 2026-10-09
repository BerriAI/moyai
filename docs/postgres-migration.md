# PostgreSQL migration rehearsal

[Deployment](deployment.md) · [Architecture](architecture.md)

This is the first step toward overlapping Moyai deployments: a verified copy of
the SQLite database into an isolated PostgreSQL schema. **The application still
uses SQLite. This command does not switch the application to Postgres or enable
multiple web instances.** Keep the existing single-instance deployment.

## What the command does

`app.postgres_migration` takes a consistent SQLite backup, including committed
WAL data, without starting the application or running its startup recovery. It
reads the source as its existing filesystem owner and writes temporary snapshots
inside a private temporary directory. It does not copy or print credentials.

The importer creates a **new schema** in PostgreSQL and copies every application
table in one transaction. It preserves primary and foreign keys, unique and check
constraints, explicit indexes, views, defaults, and the GitHub write-access
revocation trigger. SQLite integers become 64-bit Postgres integers, REAL values
remain double precision, and BLOBs become BYTEA. Generated IDs resume above both
the largest retained ID and SQLite's saved sequence, including deleted IDs.

Before committing, it checks the table inventory, column types, row counts, and
a SHA-256 digest of every row's typed contents in primary-key order. This covers
chat text, encrypted connections, permissions, event cursors, execution journals,
inline attachments, and stored object references. The report includes counts and
hashes, never row contents or connection strings. Unknown schema constructs,
changed triggers, invalid foreign keys, mixed column types, or NUL text stop the
operation. New schema features require an explicit migration update.

**This is a point-in-time copy, not replication.** Live source writes can continue
during a rehearsal, but writes after the snapshot will not appear in Postgres.
The existing source files remain authoritative. The importer does not move local
archives, fetch object-storage payloads, or copy encryption/session keys. Preserve
those separately; use the [object-storage migration](object-storage.md) for files.

## Run a rehearsal

Use Python 3.12+ and a disposable PostgreSQL 17 database. The optional dependency
group keeps Postgres drivers and schema tooling out of the normal application
image. Install it on the maintenance host, and run as the owner of `workspace.db`.

```sh
uv sync --frozen --extra postgres-migration
uv run --frozen --extra postgres-migration python -m app.postgres_migration plan \
  --data-dir /path/to/moyai-data
```

The default operation is `plan`. It validates the source snapshot and prints a
JSON inventory without opening a PostgreSQL connection. Use a restored backup
first. A plan reads all rows; allow time and temporary disk space for a full copy.

Set `MOYAI_MIGRATION_DATABASE_URL` through the maintenance environment's secret
store. It must identify the intended rehearsal database; the command deliberately
does not read the app's `DATABASE_URL` or accept a password in its arguments.
Require TLS for a remote database and use a role restricted to that database
with permission to create schemas. Retain production database access controls
for rehearsal copies, which contain the same private data.

```sh
uv run --frozen --extra postgres-migration python -m app.postgres_migration copy \
  --data-dir /path/to/moyai-data --schema moyai_rehearsal_20261009
```

The schema name must start with `moyai_` and be new. Existing schemas are never
cleared, reused, or overwritten. Failed copying or verification rolls back the
transaction. A lost connection during commit can leave its outcome uncertain;
check the schema and verify it before retrying. Use a different schema for a new
rehearsal. Remove disposable schemas only after reviewing their reports.

To compare the destination against an unchanged source backup independently:

```sh
uv run --frozen --extra postgres-migration python -m app.postgres_migration verify \
  --data-dir /path/to/unchanged-backup --schema moyai_rehearsal_20261009
```

Verification is read-only in Postgres. It rechecks table/column inventory and
typed row contents; it is not a general audit of later changes to triggers,
indexes, defaults, or constraints. If the SQLite source has changed since copying,
verification should fail. Copying is atomic rather than incrementally resumable;
a failed large import must restart from a new consistent snapshot.

## Local acceptance test

Start a disposable Postgres service, set `MOYAI_TEST_POSTGRES_URL` to it, and run:

```sh
uv run --frozen --extra postgres-migration pytest -q tests/test_postgres_migration.py
```

The tests initialize the real Moyai schema, populate synthetic Unicode messages,
encrypted credentials, binary attachments, precision-sensitive numbers and deleted
high IDs, then copy into a real Postgres instance. They verify constraints,
permission revocation, concurrent imports, rollback, source snapshot isolation,
destination corruption detection, and redacted error reporting. The dedicated CI
workflow supplies Postgres; without the test URL, local database tests are skipped.

For a short executable demo with the same disposable database:

```sh
uv run --frozen --extra postgres-migration python scripts/postgres_migration_smoke.py
```

It creates synthetic sessions using the actual application schema, runs the plan,
copy and independent verification, proves an existing destination is protected,
and removes only its own randomly named Postgres schema afterward. Add
`--output /path/to/report.json` to save its content-verification report.

## Remaining work before overlapping deployments

1. **Port the application storage boundary.** Replace SQLite-specific runtime SQL,
   transaction locking, schema upgrades and checkpoint assumptions with explicit
   Postgres behavior. Preserve single-instance operation for the first cutover.
2. **Move durable files and establish ownership.** Finish object-storage migration;
   make job claiming and recovery safe across processes. Current startup recovery
   globally interrupts pending model requests and assumes the previous process
   has stopped. Merely pointing two replicas at Postgres would be unsafe.
3. **Separate deployment lifecycles.** Run the browser API, model/tool broker and
   Temporal workers independently, with compatible workflow versions and schema
   migrations while old and new code coexist.
4. **Overlap and drain.** Warm replacement instances, route traffic only after
   readiness, retain spare capacity, drain existing model streams, and reconnect
   browser feeds from persisted event cursors.
5. **Prove continuity under load.** Redeploy while real agents stream, call tools
   and accept follow-ups. Measure response latency and verify no lost responses,
   repeated tool actions or cross-instance recovery interference.

For the eventual database cutover, quiesce every SQLite writer and take a final
verified backup/copy; coordinate that one-time maintenance window separately.
Preserve encryption/session keys and the original database/files. Once the new
system accepts writes, reverting to the old SQLite copy would lose new work; a
rollback then requires reconciling those writes. This PR provides rehearsal
tooling, not that production cutover.
