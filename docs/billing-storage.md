# Consolidate the two spend tables into one store

The completed migration stores legacy and native billing records in one physical
table: `MOYAI_NATIVE_SCHEMA.moyai_billing_records`.

`model_requests` and `moyai_inference_usage` become ordinary PostgreSQL views over
that table. They contain no copies. Existing reads, inserts, updates, deletes,
native upserts and row locks go directly to the ledger through those names.
There are no mirroring triggers or old physical spend tables after the final
transaction commits. The small `moyai_billing_storage` table holds the migration
receipt and cursors, not a second copy of the spend records.

The migration preserves original IDs, exact recorded cost strings, nulls, statuses,
token counts and request metadata. Source IDs remain namespaced; equal IDs from
different engines do not prove duplicate charges. Existing calculations, charge
reconciliation, identity mappings and cost recovery remain unchanged. Operational
admission/turn tables retain their current purpose. This work does not recover
lost costs or change which history the native usage UI displays.

## Provision

Use the existing PostgreSQL connection and explicit distinct schemas:
`MOYAI_DATABASE_URL`, `MOYAI_DATABASE_SCHEMA`, and `MOYAI_NATIVE_SCHEMA`.
Standalone SQLite is unchanged.

Follow the existing maintenance and backup procedure. Canonical schema v22
recognizes the completed compatibility view during future migrations. Native
schema v15 adds the shared ledger and receipt tables while preserving earlier
native migration receipts.

```sh
python -m app.schema_migrations --apply
python -m moyai.product_start provision --apply
```

Normal application startup never copies history or retires tables.

## Copy while existing writers continue

```sh
python -m moyai.billing_storage prepare --apply
python -m moyai.billing_storage backfill --apply --batch-size 1000 --max-batches 100
python -m moyai.billing_storage status
```

During this temporary stage, PostgreSQL triggers mirror source changes into the
new table in the original write transaction. Both old tables remain authoritative
until physical cutover. Backfill commits a bounded batch and its cursor together.
Repeat it until both completion flags are true. Source-row locks prevent
delete/copy races, and a copied historical row cannot overwrite a newer mirrored
cost update. Inserts behind the cursor are captured by the temporary mirrors.

Commands use a five-second lock timeout and sixty-second statement timeout.
Timed-out batches do not advance their committed cursor and can be retried.

Optionally compare the new reporting path before physical cutover:

```sh
python -m moyai.billing_storage activate --apply
```

This validates every stored accounting field and switches the product billing
APIs and CSV exports. `activate` alone is not the completed consolidation.
Before physical cutover, `rollback --apply` can switch reads back while both
original tables remain current.

## Complete physical consolidation

For the managed Render installation, use **Deploy production** with
**release_type: maintenance**, **execution_core: pydantic**, and
**consolidate_billing: true** after both backfills finish. A preflight-only run
checks the prepared storage without pausing work. The actual run invokes physical
consolidation at its verified zero-owner boundary before activating any role.
See the [release procedure](production-deploy.md#finish-physical-billing-consolidation).

For an independently coordinated installation, drain and stop all Moyai runtime
owners, then run the same installed migration command:

```sh
python -m moyai.billing_storage consolidate --apply
python -m moyai.billing_storage status
```

The command requires exclusive canonical runtime ownership and locks both source
tables. In one transaction it:

1. Verifies both backfills and the temporary mirrors, with no missing, extra or
   different records.
2. Preserves source nullability/length constraints, native unique request and
   receipt keys, the legacy run foreign key, supported indexes and source defaults.
3. Drops the two original tables with `RESTRICT` and creates automatically
   updatable, source-filtered views under their original names. The views use
   `CHECK OPTION` and a security barrier; existing data-access grants are retained
   without granting callers direct access to the whole ledger.
4. Removes the mirror functions and records the completed physical layout.

Unknown source columns, custom RLS, unsupported constraints/triggers, changed
ownership or dependent objects stop the transaction. Nothing is retired on a
failed comparison or failed DDL statement. The command never uses `DROP CASCADE`.
Custom database extensions must be reviewed and mapped before retirement.

Successful status is `phase: consolidated` and `physical_billing_stores: 1`.
Restart the updated application through the normal deployment workflow. Native
startup verifies the compatibility views and ledger constraints against the
cutover receipt. Re-running the physical cutover only verifies that receipt.

The original names continue to support the application's data operations; they
are no longer targets for physical-table DDL or `TRUNCATE`. Recovery updates
through either existing write interface modify the same ledger row. The billing
adapter reads the ledger directly, while user/session/privacy/admission lookups
continue using their owning tables.

## Rollback boundary

Before `consolidate`, use `rollback --apply` to select the original reporting path.
After `consolidate`, the two old stores no longer exist, so that read-only rollback
command refuses to imply that it can restore them. Restore the reviewed backup
or roll forward if a pre-cutover application/schema is required. Keep the backup
outside the active billing schema; the completed application maintains one store.

## One reporting API

`GET /api/settings/billing` is the canonical product billing and usage API.
Spend & usage calls it with `include_activity=true`; the response contains the
accounting report and an `activity` view for the dashboard. Both use the same
financial projection, including historical and native records. The activity
view reuses that projection rather than reading and summing billing again.
Its session, turn and PR measurements retain their native activity scope.

The existing `/api/settings/usage` URL remains a compatibility adapter for
clients expecting the older dashboard shape. `/api/admin/spend` and personal
`/api/spend` retain their existing scopes. They do not own separate spend
calculations. Standalone installations, which have no product history, keep
their native usage API.

Date and canonical user filters apply to the financial report. `model` selects
the recorded inference alias; `root_model` and `turn_status` select the saved
historical session or native turn cohort. The dashboard uses those latter two
filters. Daily, user and model CSV exports use the same canonical endpoint and
exact decimal amounts. `include_activity=true&format=csv&export=turns` exports
visible native turn details; it does not invent historical turn records.

Unknown amounts and unresolved identities remain explicit. Accounting totals
retain private usage, while request/session details and native turn exports
respect visibility. A failed financial read returns an error instead of falling
back to a native-only subtotal. Reporting is read-only and needs no additional
backfill or schema migration.
