# PostgreSQL runtime

Production uses one PostgreSQL database with distinct canonical and native
schemas. `MOYAI_DATABASE_URL` and `MOYAI_DATABASE_SCHEMA` identify the existing
product data; `MOYAI_NATIVE_SCHEMA` identifies native execution records. A failed
PostgreSQL connection never falls back to SQLite. The root installation includes
both synchronous and asynchronous drivers.

Configure existing-product roles with `MOYAI_SCHEMA_MODE=verify`,
`MOYAI_NATIVE_SCHEMA_MODE=verify` and `MOYAI_DATABASE_INITIALIZE=false`.
Provision through [guarded maintenance](pydantic-deployment.md). Canonical schema
revision 21 and its historical digest are preserved. Native application receipts
and the Harness storage receipt are verified separately. Harness still performs
its public lazy idempotent DDL, so its required schema permissions remain necessary.

## Ownership and transactions

API, coordinator, broker and worker retain their canonical process owners and
shared runtime fence. A lost ownership connection causes the role to retire;
it cannot silently reconnect beneath in-flight operations. Migration requires
exclusive ownership after all source processes retire.

Canonical writes use the schema-scoped writer lock; session-scoped writes share
that lock and hold their session's exclusive lock. Ordinary reads do not acquire
the writer lock. Read projections use a consistent snapshot. Transaction helpers
reuse their caller's connection, avoiding a second pool borrow while holding a
writer lock. Writes are never automatically replayed after an ambiguous response.

Native ORM/Harness transactions bind subordinate ownership markers to the exact
canonical parent's backend identity. Markers do not let a new process claim an
old owner's authority. The exclusive migration fence sees subordinate transactions
too; an owner disappearing does not make its in-flight child transaction safe.

Use a direct connection or session-preserving proxy. Transaction-mode pooling
cannot preserve session advisory locks. The configured pool budget covers the
native and canonical connection pools; see [runtime performance](pydantic-performance.md).
Pool timeouts indicate contention, not proof that a migration or the pool itself
caused the original blocking transaction.

## Data maintenance

Keep provider backups/PITR and preserve keys, object populations and Temporal
histories with the database. `app.storage_maintenance` uses the configured
canonical schema; it does not open a stale SQLite file when PostgreSQL is selected.
Its database backup operation is SQLite-only; use PostgreSQL backup facilities
for production.

[SQLite transfer](postgres-migration.md) remains available for historical data.
It copies rows into a new schema without switching the application or finishing
old workflows. A production transition separately needs the schema, source-work
drain and role checks in the deployment contract.

For local qualification use a disposable PostgreSQL server and run the commands
in [verification](verification.md). Tests create and drop their own schemas or
databases; never point them at production.
