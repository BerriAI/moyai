# Reviewed online native schema expansion

`moyai.native_online_schema` provides one explicit operator migration:
native application v21 → v22. It adds `moyai_goals`, `moyai_goal_turns` and
`moyai_goal_receipts` while the v21 application can continue serving. Application
startup never invokes it.

This is schema preparation. It does not replace processes, change HTTP or
Temporal routing, pause execution, or qualify retirement of an old worker.
The existing rolling-release compatibility and shutdown requirements still
apply to the subsequent application upgrade.

## Reviewed definitions

The module carries frozen definitions for the three new tables and indexes.
It combines them with the installed application's v21 metadata. A plan binds
the canonical/native schemas, queue, every existing migration manifest, the
target manifest, Harness versions, and the exact generated DDL digest.

The candidate must agree with those frozen table and index definitions.
The operator sends no arbitrary SQL. A changed earlier manifest, partial target
table, unsupported migration, changed schema/queue, or mismatched plan is
rejected. The existing general provisioning procedure remains unchanged.

## Transaction boundary

The qualified operator verifies all live owners and role contracts, Harness
shape, and the exact unpaused maintenance generation. These guards run through
the migration's connection before and after DDL.

The migration retains shared canonical ownership, so offline migration cannot
enter. It also takes the existing native migration lock without waiting.
Policy and maintenance rows remain share-locked by the operator's guard.
Existing foreign-key parent tables are locked with `NOWAIT`; an active writer
can therefore reject the expansion promptly.

All guard and DDL statements share a two-second budget, with a 100ms lock
timeout. Creating foreign keys still needs brief locks on their parent tables.
This is not lock-free DDL or a universal latency guarantee. A busy installation
can refuse the attempt while its existing application remains available.

The three tables, v22 application receipt, and immutable online-plan receipt
commit together. A failed guard rolls the transaction back. Prior application
receipts and rows are not rewritten, and no execution pause is issued.

## Reconcile an uncertain result

The operator persists intent before invoking `apply` once. A lost connection
or reply is not proof that the transaction rolled back.

`inspect` uses a bounded read-only transaction. `planned` means the exact v21
prefix is present and target tables/online receipt are absent. `applied` means
the exact approved plan, v22 receipt, and new structures are present. Unknown
or modified state is rejected instead of repaired. An explicit repeat of the
same committed plan returns `already_applied`.

The qualified operator separately verifies that the original services remain
ready with the same ownership, policies and maintenance state. A migration
receipt alone does not establish that a whole deployment succeeded.

## Future migrations

This implementation authorizes only v21 → v22. Future additive migrations need
reviewed definitions and compatibility/lock tests. Changes to existing data,
columns, constraints or workflow contracts may need an expand/backfill/switch/
contract sequence across releases. Unsupported changes retain the maintenance
requirement.

Regression checks cover actual PostgreSQL rollback, contention, live ownership,
old/new schema verification and receipt reconciliation. Actual application
before/after qualification must also cover continued authenticated traffic and
the relevant running-task behavior; provider deployment remains a separate
qualification.
