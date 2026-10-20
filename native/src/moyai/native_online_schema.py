"""Reviewed additive expansions; never called by application startup.

Operator tooling can send this standalone module to an older application.
Frozen steps preserve historical manifests and are selected from exact receipts.
No arbitrary SQL, backfill, schema repair or general provisioning is accepted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.base import PGDDLCompiler
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable

PROTOCOL = "moyai-native-online-v1"
FROM_VERSION = "2026-10-18-message-sources-v21"
TO_VERSION = "2026-10-17-goals-v22"
AUTOMATION_CHAT_VERSION = "2026-10-19-automation-chats-v23"
LEDGER = "moyai_native_online_migrations"
ADDED = frozenset({"moyai_goals", "moyai_goal_turns", "moyai_goal_receipts"})
PARENTS = ("moyai_sessions", "moyai_turns", "moyai_users")
# Legacy receipt hashes omit defaults and most constraints/index semantics.
# Freeze the reviewed v21 root at a canonical scope without changing its
# historical plan/receipt format. Later column births must preserve this root.
BASE_SHAPE = "c5f437f3de8fb0045cd10635f2adb3af7d7764071a45f144caedd77df474d019"


class OnlineSchemaError(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise OnlineSchemaError("native_online_" + reason)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identifier(value: str) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) is not None
        and value not in {"pg_catalog", "information_schema"},
        "scope_invalid",
    )
    return value


def _goal_tables(metadata: MetaData, schema: str) -> None:
    # Keep these definitions frozen. Tests compare them with the application's
    # v22 inventory, including indexes, so later model edits cannot alter this step.
    Table(
        "moyai_goals", metadata,
        Column("session_id", String(36), ForeignKey(f"{schema}.moyai_sessions.id"), primary_key=True),
        Column("owner_id", String(512), ForeignKey(f"{schema}.moyai_users.id"), nullable=False),
        Column("auth_version", Integer, nullable=False),
        Column("source_turn_id", String(36), ForeignKey(f"{schema}.moyai_turns.id"), nullable=True),
        Column("objective", Text, nullable=False),
        Column("status", String(30), nullable=False),
        Column("generation", Integer, nullable=False),
        Column("model", String(200), nullable=False),
        Column("connections", Text, nullable=False),
        Column("grants_key", String(64), nullable=False),
        Column("token_budget", Integer, nullable=True),
        Column("tokens_used", Integer, nullable=False),
        Column("turns", Integer, nullable=False),
        Column("blocked_count", Integer, nullable=False),
        Column("blocker", Text, nullable=False),
        Column("reason", Text, nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("checked_at", String(40), nullable=False),
        Index("moyai_goal_dispatch", "status", "checked_at"), schema=schema,
    )
    Table(
        "moyai_goal_turns", metadata,
        Column("turn_id", String(36), ForeignKey(f"{schema}.moyai_turns.id"), primary_key=True),
        Column("session_id", String(36), ForeignKey(f"{schema}.moyai_goals.session_id"), nullable=False, index=True),
        Column("generation", Integer, nullable=False),
        Column("generated", Boolean, nullable=False),
        Column("settled", Boolean, nullable=False),
        Column("decision", String(20), nullable=True),
        Column("evidence", Text, nullable=False), schema=schema,
    )
    Table(
        "moyai_goal_receipts", metadata,
        Column("id", String(64), primary_key=True),
        Column("session_id", String(36), ForeignKey(f"{schema}.moyai_sessions.id"), nullable=False, index=True),
        Column("fingerprint", String(64), nullable=False),
        Column("result", Text, nullable=False), schema=schema,
    )


class Expansion(NamedTuple):
    """Code-reviewed metadata changes, never operations supplied in a plan."""

    before: str
    after: str
    tables: frozenset[str]
    columns: tuple[tuple[str, str], ...]
    define: Callable[[MetaData, str], None]


def _automation_chat_tables(metadata: MetaData, schema: str) -> None:
    # Freeze the v23 provenance table independently of later ORM changes.
    Table(
        "moyai_product_automation_turns", metadata,
        Column("turn_id", String(36), ForeignKey(f"{schema}.moyai_turns.id"), primary_key=True),
        Column("origin_turn_id", String(36), ForeignKey(f"{schema}.moyai_turns.id"), nullable=False, index=True),
        schema=schema,
    )


EXPANSIONS = (
    Expansion(FROM_VERSION, TO_VERSION, ADDED, (), _goal_tables),
    Expansion(TO_VERSION, AUTOMATION_CHAT_VERSION, frozenset({"moyai_product_automation_turns"}), (),
              _automation_chat_tables),
)


def _versions() -> list[str]:
    from .product_schema import MIGRATIONS

    versions = [version for version, _ in MIGRATIONS]
    require(len(versions) == len(set(versions)), "registry_invalid")
    require(len({step.before for step in EXPANSIONS}) == len(EXPANSIONS)
            and len({step.after for step in EXPANSIONS}) == len(EXPANSIONS), "registry_invalid")
    pending = list(EXPANSIONS)
    while pending:
        ready = [step for step in pending if step.before in versions]
        require(bool(ready), "registry_invalid")
        for step in ready:
            index = versions.index(step.before) + 1
            if step.after not in versions:
                require(index == len(versions), "registry_invalid")
                versions.append(step.after)
            require(versions.index(step.after) == index, "registry_invalid")
            pending.remove(step)
    return versions


def _steps(before: str, after: str) -> tuple[Expansion, ...]:
    versions = _versions()
    require(before in versions and after in versions, "source_version_unsupported")
    first, last = versions.index(before), versions.index(after)
    require(first <= last, "downgrade_forbidden")
    registered = {(step.before, step.after): step for step in EXPANSIONS}
    selected = []
    for start, end in zip(versions[first:last], versions[first + 1:last + 1], strict=True):
        require((start, end) in registered, "step_unregistered")
        selected.append(registered[start, end])
    return tuple(selected)


def _table_shape(table: Table, *, without: frozenset[str] = frozenset()) -> tuple:
    """Compare database semantics, independent of constraint set iteration order.

    Keep column order and complete compiled constraints/defaults/indexes. The
    historical receipt manifest deliberately does not encode all those fields.
    This is a verifier only: it must not change the original v22 DDL digest.
    """
    dialect = postgresql.dialect()
    compiler = dialect.ddl_compiler(dialect, CreateTable(table))
    return (
        tuple(str(CreateColumn(column).compile(dialect=dialect))
              for column in table.columns if column.name not in without),
        tuple(sorted(compiler.process(constraint) for constraint in table.constraints)),
        tuple(sorted(str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes)),
        tuple(table._prefixes), tuple(sorted(table.dialect_kwargs.items())),
    )


def _plain_column(column: Column) -> None:
    require(column.server_default is None and column.server_onupdate is None
            and column.computed is None and column.identity is None and not column.constraints
            and type(column.type) in {String, Text, Integer, Boolean}
            and not column.type._variant_mapping and not getattr(column.type, "collation", None),
            "column_not_online")


def _new_table(table: Table, schema: str) -> None:
    """The supported subset is deliberately narrower than PostgreSQL DDL."""
    _identifier(table.name)
    require(bool(table.primary_key.columns) and not table._prefixes and not table.dialect_kwargs,
            "table_not_online")
    for column in table.columns:
        _identifier(column.name)
        _plain_column(column)
        require(not (column.primary_key and isinstance(column.type, Integer)
                     and column.autoincrement is not False), "column_not_online")
    for constraint in table.constraints:
        require(type(constraint) in {PrimaryKeyConstraint, ForeignKeyConstraint, UniqueConstraint}
                and not constraint.deferrable and not constraint.initially and not constraint.dialect_kwargs,
                "constraint_not_online")
        if isinstance(constraint, ForeignKeyConstraint):
            require(constraint.referred_table.schema == schema and not constraint.use_alter
                    and not constraint.onupdate and not constraint.ondelete and not constraint.match,
                    "constraint_not_online")
    for index in table.indexes:
        require(not index.dialect_kwargs
                and all(isinstance(item, Column) and item.table is table for item in index.expressions),
                "index_not_online")


def _define(metadata: MetaData, schema: str, step: Expansion) -> None:
    """Reject edits disguised as additions, including expensive column defaults."""
    old = {table.name: _table_shape(table) for table in metadata.tables.values()}
    original_columns = {table.name: tuple(table.columns.keys()) for table in metadata.tables.values()}
    require(bool(step.tables or step.columns) and len(set(step.columns)) == len(step.columns), "step_invalid")
    require(not set(old).intersection(step.tables), "step_invalid")
    require(not step.tables.intersection({LEDGER, "moyai_app_schema_versions"}), "step_invalid")
    step.define(metadata, schema)
    require({table.name for table in metadata.tables.values()} == set(old) | step.tables, "step_not_additive")
    require(all(table.schema == schema for table in metadata.tables.values()), "scope_invalid")
    for name, column_name in step.columns:
        require(name in old and column_name not in original_columns[name], "step_not_additive")
        table = metadata.tables[f"{schema}.{name}"]
        require(column_name in table.columns, "step_not_additive")
        column = table.columns[column_name]
        _identifier(column_name)
        _plain_column(column)
        require(column.nullable is True and not column.primary_key and not column.foreign_keys
                and not column.unique and not column.index, "column_not_online")
    for name in step.tables:
        _new_table(metadata.tables[f"{schema}.{name}"], schema)
    for name, shape in old.items():
        table = metadata.tables[f"{schema}.{name}"]
        additions = {column for target, column in step.columns if target == name}
        require(set(table.columns.keys()) == set(original_columns[name]) | additions, "step_not_additive")
        # No metadata mutation is needed to compare an old table's shape.
        # New indexes/constraints, including textual CHECKs, still differ.
        require(_table_shape(table, without=frozenset(additions)) == shape, "step_not_additive")


def _inventory(schema: str) -> dict[str, MetaData]:
    from .product_schema import MIGRATIONS, application_metadata

    baseline = application_metadata("native_online_history", version=FROM_VERSION)
    require(digest({table.name: _table_shape(table) for table in baseline.tables.values()}) == BASE_SHAPE,
            "source_inventory_changed")
    installed = {version for version, _ in MIGRATIONS}
    frozen = {step.after: step for step in EXPANSIONS}
    inventories: dict[str, MetaData] = {}
    for version in _versions():
        if version in frozen:
            step = frozen[version]
            metadata = MetaData()
            for table in inventories[step.before].tables.values():
                table.to_metadata(metadata)
            _define(metadata, schema, step)
            if version in installed:
                actual = application_metadata(schema, version=version)
                require({table.name: _table_shape(table) for table in metadata.tables.values()}
                        == {table.name: _table_shape(table) for table in actual.tables.values()},
                        "target_manifest_changed")
        else:
            metadata = application_metadata(schema, version=version)
        inventories[version] = metadata
    return inventories


def _metadata(schema: str, version: str = TO_VERSION) -> MetaData:
    return _inventory(schema)[version]


class _OrderedDDLCompiler(PGDDLCompiler):
    def create_table_constraints(self, table, _include_foreign_key_constraints=None, **kw):
        # Cloning metadata can change SQLAlchemy's constraint creation order.
        # Keep new batch digests deterministic across processes and interpreters.
        primary = self.process(table.primary_key)
        rest = sorted(self.process(item) for item in table.constraints if item is not table.primary_key)
        return ", \n\t".join(item for item in [primary, *rest] if item)


def _ddl(metadata: MetaData, steps: tuple[Expansion, ...] | None = None) -> list[str]:
    steps = _steps(FROM_VERSION, TO_VERSION) if steps is None else steps
    added = frozenset().union(*(step.tables for step in steps))
    dialect = postgresql.dialect()
    if [(step.before, step.after) for step in steps] != [(FROM_VERSION, TO_VERSION)]:
        # The shipped v21→v22 statement order is part of its durable identity.
        dialect.ddl_compiler = _OrderedDDLCompiler
    statements = []
    names = {table.name for table in metadata.tables.values()}
    names.update(
        dialect.identifier_preparer.format_index(index).strip('"')
        for table in metadata.tables.values() if table.name not in added for index in table.indexes
    )
    for table in metadata.sorted_tables:
        if table.name not in added:
            continue
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in sorted(table.indexes, key=lambda value: str(value.name)):
            name = dialect.identifier_preparer.format_index(index).strip('"')
            require(name not in names, "index_collision")
            names.add(name)
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))
    for step in steps:
        for name, column in step.columns:
            # A multi-step upgrade may add a column to a table created earlier
            # in the same transaction. Its final CREATE TABLE already has it.
            if name not in added:
                table = next(table for table in metadata.tables.values() if table.name == name)
                qualified = dialect.identifier_preparer.format_table(table)
                declaration = str(CreateColumn(table.columns[column]).compile(dialect=dialect))
                statements.append(f"ALTER TABLE {qualified} ADD COLUMN {declaration}")
    return statements


def _indexes(metadata: MetaData) -> list:
    preparer = postgresql.dialect().identifier_preparer
    return sorted(
        (table.name, preparer.format_index(index), bool(index.unique), tuple(column.name for column in index.columns))
        for table in metadata.tables.values() for index in table.indexes
    )


def plan(
    source_schema: str, native_schema: str, task_queue: str, *,
    from_version: str = FROM_VERSION, to_version: str | None = None,
) -> dict:
    """Pure, schema-bound plan; no credentials, database access or arbitrary DDL."""
    _identifier(source_schema)
    _identifier(native_schema)
    require(source_schema != native_schema, "scope_invalid")
    require(
        isinstance(task_queue, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", task_queue) is not None,
        "scope_invalid",
    )
    # The transported engine can know more versions than its old interpreter.
    to_version = to_version or _versions()[-1]
    return _plan(source_schema, native_schema, task_queue, from_version, to_version, _inventory(native_schema))


def _plan(
    source_schema: str, native_schema: str, task_queue: str, from_version: str, to_version: str,
    inventories: dict[str, MetaData], manifests: dict[str, str] | None = None,
) -> dict:
    from app.native_contract import FRAMEWORK_VERSIONS

    from .product_schema import _manifest

    steps = _steps(from_version, to_version)
    versions = _versions()
    if manifests is None:
        manifests = {version: _manifest(metadata) for version, metadata in inventories.items()}
    before = {version: manifests[version] for version in versions[:versions.index(from_version) + 1]}
    target = {version: manifests[version] for version in versions[:versions.index(to_version) + 1]}
    harness = {"sdk_version": FRAMEWORK_VERSIONS["pydantic-ai"],
               "harness_version": FRAMEWORK_VERSIONS["pydantic-ai-harness"]}
    body = {
        "protocol": PROTOCOL, "source_schema": source_schema, "native_schema": native_schema,
        "task_queue": task_queue, "from_version": from_version, "to_version": to_version,
        "before": {"schema": native_schema, "app": before, "harness": harness},
        "target": {"schema": native_schema, "app": target, "harness": harness},
        "ddl_digest": digest(_ddl(inventories[to_version], steps)),
    }
    return {**body, "digest": digest(body)}


def plan_from_receipts(source_schema: str, native_schema: str, task_queue: str, receipts: dict) -> dict:
    versions = _versions()
    require(isinstance(receipts, dict) and bool(receipts) and set(receipts) <= set(versions),
            "source_receipts_changed")
    current = max(receipts, key=versions.index)
    value = plan(source_schema, native_schema, task_queue, from_version=current)
    require(receipts == value["before"]["app"], "source_receipts_changed")
    return value


def validate(value: dict) -> dict:
    try:
        expected = plan(
            value["source_schema"], value["native_schema"], value["task_queue"],
            from_version=value["from_version"], to_version=value["to_version"],
        )
        require(value == expected, "plan_changed")
        return expected
    except (KeyError, TypeError):
        raise OnlineSchemaError("native_online_plan_invalid") from None


def _prepared(value: dict) -> tuple[dict[str, MetaData], dict[tuple[str, str], dict], dict[str, str]]:
    """Compile possible historical batch receipts before the SQL deadline."""
    from .product_schema import _manifest

    inventories = _inventory(value["native_schema"])
    manifests = {version: _manifest(metadata) for version, metadata in inventories.items()}
    versions = _versions()
    edges = {(step.before, step.after) for step in EXPANSIONS}
    plans = {}
    for first, before in enumerate(versions):
        for after in versions[first + 1:]:
            previous = versions[versions.index(after) - 1]
            if (previous, after) not in edges:
                break
            plans[before, after] = _plan(
                value["source_schema"], value["native_schema"], value["task_queue"],
                before, after, inventories, manifests,
            )
    return inventories, plans, manifests


class Budget:
    """Every statement, including operator guards, shares one short deadline."""

    def __init__(self, connection: psycopg.Connection, seconds: float):
        self.connection, self.deadline = connection, time.monotonic() + seconds

    def execute(self, query, parameters=None):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        require(remaining > 0, "budget_exhausted")
        self.connection.execute("SELECT set_config('statement_timeout',%s,true)", (f"{remaining}ms",))
        return self.connection.execute(query, parameters)


def _table(schema: str, name: str):
    return sql.Identifier(schema, name)


def _verify_not_null_constraints(connection, relation: str, reason: str) -> None:
    # PostgreSQL 18 records NOT NULL in pg_constraint as well as attnotnull.
    # Keep validating column nullability on every supported server, and reject
    # unvalidated/unenforced catalog entries rather than ignoring all 'n' rows.
    valid = connection.execute("""
        SELECT coalesce(bool_and(convalidated AND
            coalesce((to_jsonb(c)->>'conenforced')::boolean,true)),true)
        FROM pg_constraint c WHERE conrelid=%s::regclass AND contype='n'
    """, (relation,)).fetchone()
    require(valid == (True,), reason)


def _receipt_relation(connection, schema: str, name: str) -> None:
    """A matching receipt value cannot qualify a view or unexpected write hook."""
    relation = schema + "." + name
    # Bind inspection to this relation for the rest of the transaction. This
    # lock is read-only compatible and prevents a verify/use DDL race.
    connection.execute(sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE NOWAIT").format(_table(schema, name)))
    kind = connection.execute("""
        SELECT relkind,relrowsecurity,relforcerowsecurity,relhasrules,
            EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=pg_class.oid AND NOT tgisinternal),
            EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=pg_class.oid OR inhparent=pg_class.oid),
            EXISTS(SELECT 1 FROM pg_index WHERE indrelid=pg_class.oid AND NOT indisprimary)
        FROM pg_class WHERE oid=to_regclass(%s)
    """, (relation,)).fetchone()
    require(kind == ("r", False, False, False, False, False, False), "receipt_structure_changed")
    columns = connection.execute("""
        SELECT a.attname,format_type(a.atttypid,a.atttypmod),NOT a.attnotnull,
            pg_get_expr(d.adbin,d.adrelid)
        FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
    """, (relation,)).fetchall()
    expected = (
        [("version", "text", False, None), ("protocol", "text", False, None),
         ("plan_digest", "character(64)", False, None), ("plan", "jsonb", False, None)]
        if name == LEDGER else
        [("version", "character varying(100)", False, None), ("manifest", "character varying(64)", False, None),
         ("applied_at", "timestamp with time zone", False, "clock_timestamp()")]
    )
    require(columns == expected, "receipt_structure_changed")
    _verify_not_null_constraints(connection, relation, "receipt_structure_changed")
    primary = connection.execute("""
        SELECT c.contype,ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(id,n)
            JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.id ORDER BY k.n),
            i.indisvalid,i.indisready,c.condeferrable
        FROM pg_constraint c LEFT JOIN pg_index i ON i.indexrelid=c.conindid
        WHERE c.conrelid=%s::regclass AND c.contype!='n'
    """, (relation,)).fetchall()
    require(primary == [("p", ["version"], True, True, False)], "receipt_structure_changed")


def _receipts(connection, schema: str) -> dict:
    _receipt_relation(connection, schema, "moyai_app_schema_versions")
    return dict(connection.execute(sql.SQL("SELECT version,manifest FROM {}").format(
        _table(schema, "moyai_app_schema_versions"),
    )).fetchall())


def _ledger(connection, value: dict, plans: dict[tuple[str, str], dict]) -> dict[str, dict]:
    schema = value["native_schema"]
    relation = connection.execute("SELECT to_regclass(%s)", (schema + "." + LEDGER,)).fetchone()[0]
    if relation is None:
        return {}
    _receipt_relation(connection, schema, LEDGER)
    rows = connection.execute(sql.SQL("SELECT version,protocol,plan_digest,plan FROM {}").format(
        _table(schema, LEDGER),
    )).fetchall()
    require(bool(rows) and len(rows) <= len(EXPANSIONS), "ledger_changed")
    saved = {}
    for version, protocol, plan_digest, document in rows:
        require(protocol == PROTOCOL and isinstance(document, dict)
                and {"from_version", "to_version"} <= document.keys(), "ledger_changed")
        pair = document["from_version"], document["to_version"]
        require(all(isinstance(item, str) for item in pair), "ledger_changed")
        require(pair in plans, "ledger_changed")
        expected = plans[pair]
        require(document == expected and plan_digest == expected["digest"]
                and version in set(expected["target"]["app"]) - set(expected["before"]["app"]),
                "ledger_changed")
        saved[version] = expected
    # Every version in an atomic batch needs its receipt; an incomplete batch
    # cannot be adopted as a partial success or silently completed later.
    for document in saved.values():
        require(all(saved.get(version) == document for version in
                    set(document["target"]["app"]) - set(document["before"]["app"])), "ledger_changed")
    return saved


def _verify_added(
    connection, schema: str, metadata: MetaData, tables: frozenset[str], columns: tuple[tuple[str, str], ...],
) -> None:
    dialect = postgresql.dialect()
    names = tables | {name for name, _ in columns}
    if names:
        connection.execute(sql.SQL("LOCK TABLE {} IN ACCESS SHARE MODE NOWAIT").format(
            sql.SQL(",").join(_table(schema, name) for name in sorted(names)),
        ))
    for name in sorted(tables):
        table = metadata.tables[f"{schema}.{name}"]
        relation = schema + "." + name
        kind = connection.execute("""
            SELECT relkind,relrowsecurity,relforcerowsecurity,relhasrules,relpersistence,
                EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=pg_class.oid AND NOT tgisinternal),
                EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=pg_class.oid OR inhparent=pg_class.oid)
            FROM pg_class WHERE oid=to_regclass(%s)
        """, (relation,)).fetchone()
        require(kind == ("r", False, False, False, "p", False, False), "target_structure_changed")
        actual_columns = connection.execute("""
            SELECT a.attname,format_type(a.atttypid,a.atttypmod),NOT a.attnotnull,
                pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated,a.attcollation=t.typcollation
            FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
            LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum
        """, (relation,)).fetchall()
        expected = [(column.name, str(column.type.compile(dialect=dialect)).lower().replace(
            "varchar", "character varying"), column.nullable, None, "", "", True) for column in table.columns]
        require(actual_columns == expected, "target_structure_changed")
        _verify_not_null_constraints(connection, relation, "target_structure_changed")
        constraints = connection.execute("""
            SELECT c.contype,
                ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(id,n)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.id ORDER BY k.n),
                n.nspname,r.relname,
                ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(id,n)
                    JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.id ORDER BY k.n),
                c.convalidated AND coalesce((to_jsonb(c)->>'conenforced')::boolean,true)
                    AND CASE WHEN c.contype IN ('p','u') THEN
                        i.indisvalid AND i.indisready AND NOT i.indnullsnotdistinct
                        AND i.indpred IS NULL AND i.indexprs IS NULL ELSE true END,
                c.condeferrable,
                CASE WHEN c.contype='f' THEN c.confupdtype::text||c.confdeltype::text||c.confmatchtype::text ELSE '' END
            FROM pg_constraint c LEFT JOIN pg_class r ON r.oid=c.confrelid
            LEFT JOIN pg_namespace n ON n.oid=r.relnamespace
            LEFT JOIN pg_index i ON i.indexrelid=c.conindid
            WHERE c.conrelid=%s::regclass AND c.contype!='n'
        """, (relation,)).fetchall()
        actual = {(kind, tuple(cols), ns, ref, tuple(refcols), valid, deferred, actions)
                  for kind, cols, ns, ref, refcols, valid, deferred, actions in constraints}
        wanted: set[tuple[str, tuple[str, ...], str | None, str | None, tuple[str, ...], bool, bool, str]] = {
            ("p", tuple(column.name for column in table.primary_key.columns), None, None, (), True, False, ""),
        }
        wanted.update(
            ("f", tuple(column.name for column in fk.columns), schema, fk.referred_table.name,
             tuple(element.column.name for element in fk.elements), True, False, "aas")
            for fk in table.foreign_key_constraints
        )
        wanted.update(
            ("u", tuple(column.name for column in constraint.columns), None, None, (), True, False, "")
            for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
        )
        require(actual == wanted and len(actual) == len(constraints), "target_structure_changed")
        indexes = connection.execute("""
            SELECT r.relname,i.indisunique,i.indisvalid,i.indisready,
                ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(id,n)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.id ORDER BY k.n),
                i.indpred IS NULL,i.indexprs IS NULL,am.amname,
                NOT i.indnullsnotdistinct AND r.reloptions IS NULL
                    AND NOT EXISTS(SELECT 1 FROM unnest(i.indclass) k(id)
                        JOIN pg_opclass o ON o.oid=k.id WHERE NOT o.opcdefault)
                    AND NOT EXISTS(SELECT 1 FROM unnest(i.indkey,i.indcollation,i.indoption) k(id,coll,opts)
                        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.id
                        WHERE k.coll!=a.attcollation OR k.opts!=0)
            FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid JOIN pg_am am ON am.oid=r.relam
            WHERE i.indrelid=%s::regclass AND NOT EXISTS(
                SELECT 1 FROM pg_constraint c WHERE c.conindid=i.indexrelid AND c.contype IN ('p','u'))
        """, (relation,)).fetchall()
        require(
            {(name, unique, valid, ready, tuple(cols), full, plain, method, ordinary)
             for name, unique, valid, ready, cols, full, plain, method, ordinary in indexes}
            == {(dialect.identifier_preparer.format_index(index).strip('"'), bool(index.unique),
                 True, True, tuple(column.name for column in index.columns), True, True, "btree", True)
                for index in table.indexes},
            "target_structure_changed",
        )
    for name, column_name in columns:
        if name in tables:
            continue
        kind = connection.execute("""
            SELECT relkind,relpersistence,
                EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=c.oid OR inhparent=c.oid)
            FROM pg_class c WHERE oid=to_regclass(%s)
        """, (schema + "." + name,)).fetchone()
        require(kind == ("r", "p", False), "target_structure_changed")
        column = metadata.tables[f"{schema}.{name}"].columns[column_name]
        actual = connection.execute("""
            SELECT format_type(a.atttypid,a.atttypmod),NOT a.attnotnull,
                pg_get_expr(d.adbin,d.adrelid),a.attidentity,a.attgenerated,a.attcollation=t.typcollation,
                EXISTS(SELECT 1 FROM pg_constraint c WHERE c.conrelid=a.attrelid AND a.attnum=ANY(c.conkey)),
                EXISTS(SELECT 1 FROM pg_depend d JOIN pg_class r ON r.oid=d.objid AND r.relkind='i'
                       WHERE d.refobjid=a.attrelid AND d.refobjsubid=a.attnum AND d.classid='pg_class'::regclass)
            FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
            LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid=%s::regclass AND a.attname=%s AND NOT a.attisdropped
        """, (schema + "." + name, column_name)).fetchone()
        expected = (str(column.type.compile(dialect=dialect)).lower().replace(
            "varchar", "character varying"), True, None, "", "", True, False, False)
        require(actual == expected, "target_structure_changed")


def _status(
    connection, value: dict, inventories: dict[str, MetaData], plans: dict[tuple[str, str], dict],
    manifests: dict[str, str],
) -> str:
    schema = value["native_schema"]
    current, saved = _receipts(connection, schema), _ledger(connection, value, plans)
    versions = _versions()
    require(bool(current) and set(current) <= set(manifests), "source_receipts_changed")
    observed = max(current, key=versions.index)
    require(current == {version: manifests[version] for version in versions[:versions.index(observed) + 1]},
            "source_receipts_changed")
    steps = _steps(value["from_version"], value["to_version"])
    created = frozenset().union(*(step.tables for step in steps))
    for name in {name for step in steps for name, _ in step.columns} - created:
        # ALTER TABLE must never recurse into an unknown inheritance tree.
        kind = connection.execute("""
            SELECT relkind,relpersistence,
                EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=c.oid OR inhparent=c.oid)
            FROM pg_class c WHERE oid=to_regclass(%s)
        """, (schema + "." + name,)).fetchone()
        require(kind == ("r", "p", False), "target_structure_changed")
    new_versions = {step.after for step in steps}
    if not steps:
        require(current == value["target"]["app"], "source_receipts_changed")
        status = "current"
    elif new_versions.intersection(saved):
        require(all(saved.get(version) == value for version in new_versions)
                and versions.index(observed) >= versions.index(value["to_version"]), "committed_plan_changed")
        # A historical batch remains applied after later registered additions.
        # Never adopt an unknown/unregistered suffix or change its saved plan.
        _steps(value["to_version"], observed)
        status = "applied"
    else:
        require(current == value["before"]["app"], "source_receipts_changed")
        for name in frozenset().union(*(step.tables for step in steps)):
            require(connection.execute("SELECT to_regclass(%s)", (schema + "." + name,)).fetchone() == (None,),
                    "unreceipted_target")
        for step in steps:
            for name, column in step.columns:
                require(connection.execute("""
                    SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass(%s) AND attname=%s AND NOT attisdropped
                """, (schema + "." + name, column)).fetchone() is None, "unreceipted_target")
        status = "planned"
    for document in saved.values():
        require(all(current.get(version) == manifest for version, manifest in document["target"]["app"].items()),
                "committed_plan_changed")
    # Offline provisioning legitimately has no online ledger. A no-op must
    # still verify all registered structures already covered by its receipts.
    historical = [step for step in EXPANSIONS if versions.index(step.after) <= versions.index(observed)]
    _verify_added(
        connection, schema, inventories[observed],
        frozenset().union(*(step.tables for step in historical)),
        tuple(column for step in historical for column in step.columns),
    )
    return status


def _receipt(value: dict, status: str) -> dict:
    return {"protocol": PROTOCOL, "status": status, "digest": value["digest"], "target": value["target"]}


def inspect(connection: psycopg.Connection, value: dict) -> dict:
    value = validate(value)
    inventories, plans, manifests = _prepared(value)
    require(connection.autocommit and connection.info.transaction_status == TransactionStatus.IDLE,
            "connection_not_idle")
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout='3s'")
        return _receipt(value, _status(Budget(connection, 3), value, inventories, plans, manifests))


def apply(
    connection: psycopg.Connection, value: dict, *, guard: Callable[[Budget], None],
) -> dict:
    """Atomically apply registered additions and append their durable receipts.

    The operator guard must verify live owners, contracts, Harness shape and
    the unchanged, unpaused maintenance generation using this same transaction.
    It runs before and after DDL; it may not pause or replace any application.
    """
    value = validate(value)
    # Compile immutable metadata/DDL before holding any database locks. All
    # database verification and operator guards still share the short budget.
    inventories, plans, manifests = _prepared(value)
    metadata = inventories[value["to_version"]]
    steps = _steps(value["from_version"], value["to_version"])
    statements = _ddl(metadata, steps)
    added = frozenset().union(*(step.tables for step in steps))
    altered = {name for step in steps for name, _ in step.columns} - added
    parents = {
        key.column.table.name
        for table in metadata.tables.values() if table.name in added for key in table.foreign_keys
        if key.column.table.name not in added
    }
    require(all(key.column.table.schema == value["native_schema"]
                for table in metadata.tables.values() if table.name in added for key in table.foreign_keys),
            "scope_invalid")
    require(connection.autocommit and connection.info.transaction_status == TransactionStatus.IDLE,
            "connection_not_idle")
    source, native = value["source_schema"], value["native_schema"]
    try:
        with connection.transaction():
            connection.execute("SET LOCAL statement_timeout='2s'")
            connection.execute("SET LOCAL lock_timeout='100ms'")
            bounded = Budget(connection, 2)
            require(bounded.execute(
                "SELECT pg_try_advisory_xact_lock_shared(726940,%s::regnamespace::oid::int)", (source,),
            ).fetchone() == (True,), "offline_owner_present")
            require(bounded.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s))", (f"moyai-native-migration:{native}",),
            ).fetchone() == (True,), "migration_busy")
            status = _status(bounded, value, inventories, plans, manifests)
            guard(bounded)
            if status == "applied":
                return _receipt(value, "already_applied")
            if status == "current":
                return _receipt(value, "already_current")
            # Nullable-column additions still need ACCESS EXCLUSIVE. NOWAIT
            # refuses a busy table without queuing in front of live readers.
            if altered:
                relations = sql.SQL(",").join(_table(native, name) for name in sorted(altered))
                bounded.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE NOWAIT").format(relations))
            locked = (parents - altered) | {"moyai_app_schema_versions"}
            ledger_exists = bounded.execute("SELECT to_regclass(%s)", (native + "." + LEDGER,)).fetchone() != (None,)
            if ledger_exists:
                locked.add(LEDGER)
            relations = sql.SQL(",").join(_table(native, name) for name in sorted(locked))
            bounded.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE NOWAIT").format(relations))
            require(_status(bounded, value, inventories, plans, manifests) == "planned", "source_receipts_changed")
            for statement in statements:
                bounded.execute(statement)
            if not ledger_exists:
                bounded.execute(sql.SQL("""
                    CREATE TABLE {} (
                        version text PRIMARY KEY, protocol text NOT NULL,
                        plan_digest char(64) NOT NULL, plan jsonb NOT NULL
                    )
                """).format(_table(native, LEDGER)))
            for step in steps:
                bounded.execute(sql.SQL("INSERT INTO {} VALUES(%s,%s,%s,%s)").format(_table(native, LEDGER)), (
                    step.after, PROTOCOL, value["digest"], Jsonb(value),
                ))
                bounded.execute(sql.SQL("INSERT INTO {}(version,manifest) VALUES(%s,%s)").format(
                    _table(native, "moyai_app_schema_versions"),
                ), (step.after, value["target"]["app"][step.after]))
            require(_status(bounded, value, inventories, plans, manifests) == "applied", "target_structure_changed")
            guard(bounded)
            bounded.execute("SELECT 1")  # Check the remaining budget before COMMIT.
        return _receipt(value, "applied")
    except psycopg.Error:
        # A disconnected COMMIT can have succeeded. The next action is a
        # read-only receipt inspection, never an automatic repeat of this call.
        raise OnlineSchemaError("native_online_outcome_unconfirmed") from None
