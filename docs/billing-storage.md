# Consolidate billing storage

This migration gives existing-product billing reports one persisted source,
`MOYAI_NATIVE_SCHEMA.moyai_billing_records`. It copies recorded accounting
observations from `model_requests` and `moyai_inference_usage` without changing
prices, recovery policies, identity mappings, duplicate-charge rules or missing
cost coverage. It does not recover lost costs or change the native analytics UI.

Each record retains its source and original ID. The same ID in the two engines
remains two observations. Existing billing reconciliation still decides whether
observations describe one charge. An explicit field allowlist preserves accounting
data, including exact cost strings and null values, without copying unknown
columns, prompts, provider responses or credentials.

The current release keeps the original tables as write-compatible sources during
the rollback window. PostgreSQL triggers mirror inserts, updates, deletes and
truncates in the original transaction. This includes existing cost-recovery
updates; the storage command never calls a model or provider. This is the
consolidated-read stage, not deletion of the source tables. Retiring the
compatibility writers and tables is a later release.

## Prerequisites and provisioning

Use the existing product database connection and distinct configured schemas:
`MOYAI_DATABASE_URL`, `MOYAI_DATABASE_SCHEMA`, and `MOYAI_NATIVE_SCHEMA`. These
commands support PostgreSQL existing-product deployments. Standalone SQLite
continues using its current storage.

Follow the existing reviewed deployment and backup procedure. Native schema v15
adds the ledger and migration-state tables without modifying earlier migration
receipts. Normal application startup never installs mirrors or copies history.

```sh
python -m moyai.product_start provision --apply
```

The operator/runtime database role must be able to use both configured schemas,
execute the mirror functions and write the ledger. Mirrors use the caller's
privileges, not `SECURITY DEFINER`. Keep the same schema ownership/access policy
as the existing product. No role or permission changes are made by these commands.

## Prepare, copy and switch

```sh
python -m moyai.billing_storage prepare --apply
python -m moyai.billing_storage backfill --apply --batch-size 1000 --max-batches 100
python -m moyai.billing_storage status
```

Preparation installs both mirrors and a shadow-mode receipt atomically. Repeating
preparation verifies the existing configuration; it does not replace modified
triggers or silently reset progress.

Backfill commits one batch at a time and stores its cursor in the same transaction.
Repeat the command until both backfill flags are true. Interrupted batches can be
retried. Source-row locks prevent delete/copy races, and existing mirrored records
win conflicts so a historical copy cannot overwrite a more recent cost update.
New records with IDs behind the cursor are captured by the mirrors.

Each operation has a five-second lock timeout and a sixty-second statement
timeout. A timeout fails that operation without advancing its committed cursor.
Use smaller batches and retry rather than disabling the mirrors.

```sh
python -m moyai.billing_storage activate --apply
```

Activation briefly blocks writes to the two source tables. It compares every
allowlisted field, lookup identity and timestamp, checks for missing and extra
records, verifies the mirrors, and switches the receipt in one transaction. Any
mismatch keeps the original read path selected. No calculated subtotal is used
as a substitute for comparing the underlying records.

After activation, `/api/admin/spend`, `/api/settings/billing`, `/api/spend` and
their CSV exports obtain both engines' accounting observations from the ledger.
Identity, session-privacy and admission-proof lookups still use their owning
product tables. Amount calculations and permissions are unchanged.

An active report refuses unavailable or incompatible mirrors rather than
silently returning a stale consolidated total. Status output contains only the
phase, completion flags and record counts, not credentials or request contents.

## Roll back reads

```sh
python -m moyai.billing_storage rollback --apply
```

This selects the original billing read path. It leaves both mirrors running and
does not delete any records. Because original writers remain authoritative and
current, rollback does not lose requests or late cost updates. Activate again
only after exact comparison succeeds.

Do not remove or disable the source tables, mirrors or ledger while any runtime
depends on this migration. A later cleanup must switch every writer and recovery
path before retiring the original tables; retaining a table name as a stale
archive is not sufficient for rollback.
