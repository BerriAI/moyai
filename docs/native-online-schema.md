# Reviewed online native schema expansion

`moyai.native_online_schema` provides an explicit operator path for reviewed
additive migrations. Its initial registered step is v21 → v22, adding
`moyai_goals`, `moyai_goal_turns` and `moyai_goal_receipts` while the v21
application can continue serving. No new product version is introduced by the
registry framework. Application startup never invokes it.

This is schema preparation. It does not replace processes, change HTTP or
Temporal routing, pause execution, or qualify retirement of an old worker.
The existing rolling-release compatibility and shutdown requirements still
apply to the subsequent application upgrade.

## Reviewed definitions

The module carries an append-only `EXPANSIONS` registry of frozen metadata
callbacks, declared new tables and declared nullable columns on earlier tables.
It reconstructs consecutive registered versions from the installed application's
historical metadata, including when transported to an older interpreter. A plan binds
the canonical/native schemas, queue, every existing migration manifest, the
target manifest, Harness versions, and the exact generated DDL digest.

The candidate must agree with those frozen definitions. Semantic comparison
includes column order, types, nullability, defaults, full constraints and indexes,
without depending on SQLAlchemy's unordered constraint collections.
The operator sends no arbitrary SQL. A changed earlier manifest, partial target
table, unsupported migration, changed schema/queue, or mismatched plan is
rejected. The original v21 → v22 plan format, DDL digest and receipts retain their
exact identity. Later batch DDL uses deterministic constraint ordering.

`plan_from_receipts` selects the exact observed contiguous prefix and derives
the missing registered steps up to the candidate version. It rejects unknown,
changed or missing receipts, gaps, unregistered steps and downgrades. Explicit
`from_version` / `to_version` bind a retained approval: inspection and status
must use that original plan after the database advances, rather than derive a
different operation from its new receipts.

## Transaction boundary

The qualified operator verifies all live owners and role contracts, Harness
shape, and the exact unpaused maintenance generation. These guards run through
the migration's connection before and after DDL.

The migration retains shared canonical ownership, so offline migration cannot
enter. It also takes the existing native migration lock without waiting.
Policy and maintenance rows remain share-locked by the operator's guard.
Existing foreign-key parent tables are locked with `NOWAIT`; an active writer
can therefore reject the expansion promptly. Plain nullable-column additions
take `ACCESS EXCLUSIVE NOWAIT` on their existing tables. Even a live reader can
refuse that attempt. They do not queue behind readers and block incoming traffic.

All guard and DDL statements share a two-second budget, with a 100ms lock
timeout. Creating foreign keys still needs brief locks on their parent tables.
This is not lock-free DDL or a universal latency guarantee. A busy installation
can refuse the attempt while its existing application remains available.

All selected tables/columns, application receipts and online-plan receipts commit
together. Each intermediate version in a multi-step batch carries the same
approved plan. Separate sequential batches preserve every earlier receipt and
its original plan. An incomplete batch, changed binding or modified previously
added structure is rejected. A failed guard rolls the entire batch back. Prior
application receipts and rows are not rewritten, and no execution pause is issued.
Metadata and historical plan compilation happen before acquiring database locks;
database checks and both runtime guards remain inside the SQL budget.

## Reconcile an uncertain result

The operator persists intent before invoking `apply` once. A lost connection
or reply is not proof that the transaction rolled back.

`inspect` uses a bounded read-only transaction. `planned` means the exact source
prefix is present and all selected additions/online receipts are absent. `applied` means
the exact approved batch, application receipts, and new structures are present. Unknown
or modified state is rejected instead of repaired. An explicit repeat of the
same committed plan returns `already_applied`.

An exact current-version plan returns `current`; its `apply` returns
`already_current`. Both verify receipts and all previously registered additions.
They do not create a ledger, add rows or repair drift. This also supports a
current schema installed by explicit offline provisioning, which has no online
ledger. Operators still verify unchanged ready owners and runtime guards.

The qualified operator separately verifies that the original services remain
ready with the same ownership, policies and maintenance state. A migration
receipt alone does not establish that a whole deployment succeeded.

## Future migrations

Every future step requires code review and compatibility/lock tests:

1. Add the product version to `MIGRATIONS`, and add the model definitions.
2. For a new nullable column on an existing table, register its birth in
   `COLUMN_MIGRATIONS`. Historical metadata omits it before that version on an
   independent copy; earlier manifest hashes must stay unchanged. Explicit
   offline provisioning installs these columns at their registered versions.
3. Append an `Expansion(before, after, tables, columns, define)` with a frozen
   callback. Never edit a prior callback to reflect a new model. The callback
   must declare exactly its additions; old columns, constraints and indexes
   must remain semantically identical.
4. Prove old-app compatibility, sequential and multi-step upgrades, atomic
   rollback, no-op integrity, reader/writer contention, and transport on the
   actual older interpreter on supported PostgreSQL versions. Test that all
   historical receipts and approved batch identities remain stable.

Supported new tables use ordinary persistent tables, plain String/Text/Integer/
Boolean columns, primary/unique keys, same-schema ordinary foreign keys, and
plain indexes. Existing-table additions are strictly nullable plain columns
with no defaults, generated values, keys, checks or indexes. Sequences,
inheritance/partitioning, custom types/collations, expression/partial indexes,
arbitrary SQL, backfills and changes to existing definitions are not automatically
admitted. Some otherwise additive operations need a separately reviewed
implementation. Data changes or workflow contracts may need an expand/backfill/
switch/contract sequence across releases; unsupported operations retain the
maintenance requirement.

Regression checks cover actual PostgreSQL rollback, contention, live ownership,
old/new schema verification and receipt reconciliation. Actual application
before/after qualification must also cover continued authenticated traffic and
the relevant running-task behavior; provider deployment remains a separate
qualification.
