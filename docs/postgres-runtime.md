# PostgreSQL runtime

[Deployment](deployment.md) · [Migration rehearsal](postgres-migration.md)

Moyai can read and write its application database in PostgreSQL 17. SQLite remains
the default. For the initial cutover, **keep one application instance and the existing
persistent file storage.** The default standalone mode does not enable rolling
deployments or switch an existing installation automatically. After cutover and
shared file migration, see [execution worker scaling](runtime-scaling.md) for the
opt-in coordinator/worker topology.

## Configuration

Set these through the service's secret store after preparing the target database:

- `MOYAI_DATABASE_URL`: a PostgreSQL connection URL. Use the private database
  endpoint in the service's region and require TLS as prescribed by the provider.
- `MOYAI_DATABASE_SCHEMA`: the exact schema produced by the verified importer,
  such as `moyai_cutover_20261009`. The default is `moyai`.
- Leave `MOYAI_DATABASE_INITIALIZE=false` for a cutover. An empty or unrelated
  schema is refused. For a disposable development installation only, explicitly
  create an empty schema and set this flag to `true` for the first startup.

The ordinary `DATABASE_URL` variable is intentionally ignored. A configured
Postgres connection that fails never falls back to SQLite. The application image
includes the driver and a bounded connection pool; no optional runtime extra is
required. The offline importer still needs the `postgres-migration` extra.

The database role must own the application schema and its objects: startup applies
additive schema upgrades, indexes, SQL helper functions, and the permission
revocation trigger. Use a dedicated database/role and schema, not `public`.
Connections set a schema-specific search path with `pg_catalog`; normal queries
bind values using the driver. Connection errors reported by the application omit
SQL, parameter values, and credentials.

## Transaction and process ownership

Explicit write transactions acquire a schema-scoped PostgreSQL advisory lock
before reading decisions or changing rows. Other writes acquire that lock unless explicitly scoped to a session; scoped
writes share the schema lock and exclusively lock that session. This preserves SQLite's serialized queue claiming, submission deduplication,
revision checks, and permission changes. Writes are never automatically replayed.
Read projections use repeatable-read transactions so history and its event cursor
share one snapshot while other requests can continue writing.

In default standalone mode, each application instance also holds a separate
session advisory lock **before
schema setup and startup recovery**. A second app cannot start against that schema.
If the ownership connection dies, further app transactions are refused; `/health`
returns 503. The process must restart to recover ownership. Standalone `Store`
objects used by tests and explicit maintenance do not own an application lease;
they still serialize writes. Do not use them to run another recovery loop or worker.

The pool defaults to eight connections (`MOYAI_DATABASE_POOL_SIZE`, range 1–256),
with a ten-second acquisition/lock
budget and a thirty-second statement timeout. There is one additional connection
for process ownership. Use a direct PostgreSQL connection or a session-preserving
proxy; transaction-mode poolers cannot preserve the application ownership lock.

Application queries now use explicit IDs, conflict handling, typed JSON fields,
and table inspection. Backend-specific schema handling is limited to Moyai's four
declared column types and generated integer identities; triggers and views have
explicit implementations. New schema features must be exercised in the Postgres
CI suite, including upgrades from an imported database.

## Prepare the production cutover separately

1. Rehearse with a restored production backup and verify every imported table.
2. Preserve `ENCRYPTION_KEY`, `SESSION_SECRET`, the existing data directory, local
   archives/captures, and object-storage access. Postgres only moves database rows.
3. Stop all SQLite writers for a one-time maintenance window; take a final verified
   copy into a new Postgres schema. A previous rehearsal is a stale snapshot.
4. Configure the two Postgres variables and start exactly one application instance.
   Keep Render's disk, replica count, and existing runtime/Temporal settings.
5. Verify health, sign-in, old chats, encrypted connections, attachments, queued
   work, and a new real agent turn before reopening traffic.

`CHECKPOINT_DIR` is incompatible with Postgres. Enable the database provider's
backups/PITR and retain backups of files and encryption/session keys. The SQLite
storage-maintenance CLI refuses to operate when Postgres is configured so it cannot
silently back up or migrate a stale local database. Render skips the old Modal
SQLite bootstrap when a Postgres URL is explicitly configured; the runtime still
validates the target. The existing Modal checkpoint deployment recipe is not a
Postgres deployment recipe.

Keep the original SQLite snapshot for rollback **before new Postgres writes**.
After accepting new writes, simply removing the URL would lose that new work;
rollback requires write reconciliation or restoring the Postgres service.

## Reproduce verification

Use a disposable PostgreSQL 17 database and set `MOYAI_TEST_POSTGRES_URL` privately:

```sh
uv sync --frozen --extra postgres-migration
uv run --frozen pytest -q tests/test_postgres_runtime.py tests/test_postgres_migration.py
uv run --frozen pytest --postgres-backend -q tests/test_workspace.py tests/test_sessions.py \
  tests/test_attachments.py tests/test_message_queue.py tests/test_trace_outbox.py
uv run --frozen python scripts/postgres_runtime_smoke.py
```

The smoke script starts a real HTTP server process, completes a synthetic demo
chat, verifies its rows directly in Postgres, stops the process, starts a second
process, and completes a follow-up in the same chat. It verifies that no SQLite
database was created. Test schemas and server processes are cleaned up afterward.
This proves database-backed runtime persistence, not uninterrupted model streams
or cloud-provider behavior during deployment.

The optional [coordinator/worker topology](runtime-scaling.md) provides independent
execution replicas after shared file migration. Multiple API replicas and
uninterrupted coordinator deployments remain future work.
