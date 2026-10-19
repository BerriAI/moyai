"""One reviewed online expansion; never called by application startup.

This module can be sent by qualified operator tooling to a v21 application.
It imports that application's v21 metadata, then adds frozen v22 definitions.
It never runs the general provisioning path or accepts caller-supplied SQL.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, MetaData, String, Table, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

PROTOCOL = "moyai-native-online-v1"
FROM_VERSION = "2026-10-18-message-sources-v21"
TO_VERSION = "2026-10-17-goals-v22"
LEDGER = "moyai_native_online_migrations"
ADDED = frozenset({"moyai_goals", "moyai_goal_turns", "moyai_goal_receipts"})
PARENTS = ("moyai_sessions", "moyai_turns", "moyai_users")


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


def _metadata(schema: str) -> MetaData:
    from .product_schema import application_metadata

    metadata = application_metadata(schema, version=FROM_VERSION)
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
    return metadata


def _ddl(metadata: MetaData) -> list[str]:
    dialect = postgresql.dialect()
    statements = []
    names = {table.name for table in metadata.tables.values()}
    names.update(
        dialect.identifier_preparer.format_index(index).strip('"')
        for table in metadata.tables.values() if table.name not in ADDED for index in table.indexes
    )
    for table in metadata.sorted_tables:
        if table.name not in ADDED:
            continue
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in sorted(table.indexes, key=lambda value: str(value.name)):
            name = dialect.identifier_preparer.format_index(index).strip('"')
            require(name not in names, "index_collision")
            names.add(name)
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))
    return statements


def _indexes(metadata: MetaData) -> list:
    preparer = postgresql.dialect().identifier_preparer
    return sorted(
        (table.name, preparer.format_index(index), bool(index.unique), tuple(column.name for column in index.columns))
        for table in metadata.tables.values() if table.name in ADDED for index in table.indexes
    )


def plan(source_schema: str, native_schema: str, task_queue: str) -> dict:
    """Pure, schema-bound plan; no credentials, database access or arbitrary DDL."""
    from app.native_contract import FRAMEWORK_VERSIONS

    from .product_schema import MIGRATIONS, _manifest, application_metadata

    _identifier(source_schema)
    _identifier(native_schema)
    require(source_schema != native_schema, "scope_invalid")
    require(
        isinstance(task_queue, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", task_queue) is not None,
        "scope_invalid",
    )
    versions = [version for version, _ in MIGRATIONS]
    require(FROM_VERSION in versions, "source_version_unsupported")
    before = {version: _manifest(application_metadata(native_schema, version=version))
              for version in versions[:versions.index(FROM_VERSION) + 1]}
    metadata = _metadata(native_schema)
    target = {**before, TO_VERSION: _manifest(metadata)}
    # A new application must still agree with the frozen expansion. On the old
    # source image v22 is absent; its v21 definitions must match the same plan.
    if TO_VERSION in versions:
        actual = application_metadata(native_schema, version=TO_VERSION)
        require(
            target[TO_VERSION] == _manifest(actual) and _indexes(metadata) == _indexes(actual),
            "target_manifest_changed",
        )
    harness = {"sdk_version": FRAMEWORK_VERSIONS["pydantic-ai"],
               "harness_version": FRAMEWORK_VERSIONS["pydantic-ai-harness"]}
    body = {
        "protocol": PROTOCOL, "source_schema": source_schema, "native_schema": native_schema,
        "task_queue": task_queue, "from_version": FROM_VERSION, "to_version": TO_VERSION,
        "before": {"schema": native_schema, "app": before, "harness": harness},
        "target": {"schema": native_schema, "app": target, "harness": harness},
        "ddl_digest": digest(_ddl(metadata)),
    }
    return {**body, "digest": digest(body)}


def validate(value: dict) -> dict:
    try:
        expected = plan(value["source_schema"], value["native_schema"], value["task_queue"])
        require(value == expected, "plan_changed")
        return expected
    except (KeyError, TypeError):
        raise OnlineSchemaError("native_online_plan_invalid") from None


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


def _receipts(connection, schema: str) -> dict:
    return dict(connection.execute(sql.SQL("SELECT version,manifest FROM {}").format(
        _table(schema, "moyai_app_schema_versions"),
    )).fetchall())


def _ledger(connection, schema: str) -> dict | None:
    relation = connection.execute("SELECT to_regclass(%s)", (schema + "." + LEDGER,)).fetchone()[0]
    if relation is None:
        return None
    kind = connection.execute("SELECT relkind FROM pg_class WHERE oid=%s::regclass", (relation,)).fetchone()
    columns = connection.execute("""
        SELECT column_name,udt_name,is_nullable FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position
    """, (schema, LEDGER)).fetchall()
    require(kind == ("r",) and columns == [
        ("version", "text", "NO"), ("protocol", "text", "NO"),
        ("plan_digest", "bpchar", "NO"), ("plan", "jsonb", "NO"),
    ], "ledger_changed")
    rows = connection.execute(sql.SQL("SELECT version,protocol,plan_digest,plan FROM {}").format(
        _table(schema, LEDGER),
    )).fetchall()
    require(len(rows) == 1 and rows[0][0] == TO_VERSION and rows[0][1] == PROTOCOL, "ledger_changed")
    require(isinstance(rows[0][3], dict), "ledger_changed")
    require(rows[0][2] == digest({key: val for key, val in rows[0][3].items() if key != "digest"})
            == rows[0][3].get("digest"), "ledger_changed")
    return rows[0][3]


def _verify_added(connection, schema: str) -> None:
    metadata = _metadata(schema)
    dialect = postgresql.dialect()
    for name in sorted(ADDED):
        table = metadata.tables[f"{schema}.{name}"]
        relation = schema + "." + name
        kind = connection.execute("""
            SELECT relkind,relrowsecurity,relforcerowsecurity,
                EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=pg_class.oid AND NOT tgisinternal)
            FROM pg_class WHERE oid=to_regclass(%s)
        """, (relation,)).fetchone()
        require(kind == ("r", False, False, False), "target_structure_changed")
        columns = connection.execute("""
            SELECT attname,format_type(atttypid,atttypmod),NOT attnotnull
            FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum
        """, (relation,)).fetchall()
        expected = [(column.name, str(column.type.compile(dialect=dialect)).lower().replace(
            "varchar", "character varying"), column.nullable) for column in table.columns]
        require(columns == expected, "target_structure_changed")
        constraints = connection.execute("""
            SELECT c.contype,
                ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(id,n)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.id ORDER BY k.n),
                n.nspname,r.relname,
                ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(id,n)
                    JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.id ORDER BY k.n),
                c.convalidated,c.condeferrable,
                CASE WHEN c.contype='f' THEN c.confupdtype::text||c.confdeltype::text||c.confmatchtype::text ELSE '' END
            FROM pg_constraint c LEFT JOIN pg_class r ON r.oid=c.confrelid
            LEFT JOIN pg_namespace n ON n.oid=r.relnamespace WHERE c.conrelid=%s::regclass
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
        require(actual == wanted, "target_structure_changed")
        indexes = connection.execute("""
            SELECT r.relname,i.indisunique,i.indisvalid,i.indisready,
                ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(id,n)
                    JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.id ORDER BY k.n),
                i.indpred IS NULL,i.indexprs IS NULL
            FROM pg_index i JOIN pg_class r ON r.oid=i.indexrelid
            WHERE i.indrelid=%s::regclass AND NOT i.indisprimary
        """, (relation,)).fetchall()
        require(
            {(name, unique, valid, ready, tuple(cols), full, plain)
             for name, unique, valid, ready, cols, full, plain in indexes}
            == {(dialect.identifier_preparer.format_index(index).strip('"'), bool(index.unique),
                 True, True, tuple(column.name for column in index.columns), True, True) for index in table.indexes},
            "target_structure_changed",
        )


def _status(connection, value: dict) -> str:
    schema = value["native_schema"]
    current, saved = _receipts(connection, schema), _ledger(connection, schema)
    if saved is not None:
        require(saved == value and current == value["target"]["app"], "committed_plan_changed")
        _verify_added(connection, schema)
        return "applied"
    require(current == value["before"]["app"], "source_receipts_changed")
    for name in ADDED:
        require(connection.execute("SELECT to_regclass(%s)", (schema + "." + name,)).fetchone() == (None,),
                "unreceipted_target")
    return "planned"


def _receipt(value: dict, status: str) -> dict:
    return {"protocol": PROTOCOL, "status": status, "digest": value["digest"], "target": value["target"]}


def inspect(connection: psycopg.Connection, value: dict) -> dict:
    value = validate(value)
    require(connection.autocommit and connection.info.transaction_status == TransactionStatus.IDLE,
            "connection_not_idle")
    with connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout='3s'")
        return _receipt(value, _status(Budget(connection, 3), value))


def apply(
    connection: psycopg.Connection, value: dict, *, guard: Callable[[Budget], None],
) -> dict:
    """Atomically add only the three approved tables and both durable receipts.

    The operator guard must verify live owners, contracts, Harness shape and
    the unchanged, unpaused maintenance generation using this same transaction.
    It runs before and after DDL; it may not pause or replace any application.
    """
    value = validate(value)
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
            guard(bounded)
            status = _status(bounded, value)
            if status == "applied":
                return _receipt(value, "already_applied")
            # These are the existing FK parents. NOWAIT avoids placing a DDL
            # waiter in front of live writes while an unrelated transaction runs.
            relations = sql.SQL(",").join(_table(native, name) for name in (*PARENTS, "moyai_app_schema_versions"))
            bounded.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE NOWAIT").format(relations))
            require(_status(bounded, value) == "planned", "source_receipts_changed")
            for statement in _ddl(_metadata(native)):
                bounded.execute(statement)
            bounded.execute(sql.SQL("""
                CREATE TABLE {} (
                    version text PRIMARY KEY, protocol text NOT NULL,
                    plan_digest char(64) NOT NULL, plan jsonb NOT NULL
                )
            """).format(_table(native, LEDGER)))
            bounded.execute(sql.SQL("INSERT INTO {} VALUES(%s,%s,%s,%s)").format(_table(native, LEDGER)), (
                TO_VERSION, PROTOCOL, value["digest"], Jsonb(value),
            ))
            bounded.execute(sql.SQL("INSERT INTO {}(version,manifest) VALUES(%s,%s)").format(
                _table(native, "moyai_app_schema_versions"),
            ), (TO_VERSION, value["target"]["app"][TO_VERSION]))
            _verify_added(bounded, native)
            guard(bounded)
            bounded.execute("SELECT 1")  # Check the remaining budget before COMMIT.
        return _receipt(value, "applied")
    except psycopg.Error:
        # A disconnected COMMIT can have succeeded. The next action is a
        # read-only receipt inspection, never an automatic repeat of this call.
        raise OnlineSchemaError("native_online_outcome_unconfirmed") from None
