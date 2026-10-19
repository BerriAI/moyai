"""Registered future versions are fixtures, not a new product schema version."""

from __future__ import annotations

import os
from copy import deepcopy
from types import ModuleType

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, String, Table, Text, text
from test_native_online_schema import expansion as expansion_fixture
from test_native_online_schema import product_database as product_database_fixture

from moyai import native_online_schema as online
from moyai import product_schema as product

expansion = expansion_fixture
product_database = product_database_fixture

V23 = "2026-10-19-test-online-v23"
V24 = "2026-10-19-test-online-v24"
NOTES = "moyai_online_test_notes"
PG = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")


def notes(metadata, schema):
    Table(
        NOTES, metadata, Column("id", String(36), primary_key=True),
        Column("session_id", String(36), ForeignKey(f"{schema}.moyai_sessions.id"), nullable=False),
        Column("owner_id", String(512), ForeignKey(f"{schema}.moyai_users.id"), nullable=False),
        Column("body", Text, nullable=False), Index("moyai_online_test_notes_session", "session_id"), schema=schema,
    )
    metadata.tables[f"{schema}.moyai_sessions"].append_column(Column("online_note", Text, nullable=True))


def later(metadata, schema):
    metadata.tables[f"{schema}.{NOTES}"].append_column(Column("reviewed", Boolean, nullable=True))


@pytest.fixture
def registered(monkeypatch):
    steps = (
        online.Expansion(online.TO_VERSION, V23, frozenset({NOTES}), (("moyai_sessions", "online_note"),), notes),
        online.Expansion(V23, V24, frozenset(), ((NOTES, "reviewed"),), later),
    )
    monkeypatch.setattr(online, "EXPANSIONS", (*online.EXPANSIONS, *steps))
    return steps


@pytest.fixture
def installed(registered, monkeypatch):
    # Exercise actual application_metadata and historical filtering, including
    # fresh offline provisioning. Restore the ORM inventory after each test.
    product.application_metadata("native")
    base = product.Base.metadata
    sessions = base.tables["moyai_sessions"]
    column = Column("online_note", Text, nullable=True)
    sessions.append_column(column)
    table = Table(
        NOTES, base, Column("id", String(36), primary_key=True),
        Column("session_id", String(36), ForeignKey("moyai_sessions.id"), nullable=False),
        Column("owner_id", String(512), ForeignKey("moyai_users.id"), nullable=False),
        Column("body", Text, nullable=False), Column("reviewed", Boolean, nullable=True),
        Index("moyai_online_test_notes_session", "session_id"),
    )
    monkeypatch.setattr(product, "MIGRATIONS", (*product.MIGRATIONS, (V23, frozenset({NOTES})), (V24, frozenset())))
    monkeypatch.setattr(product, "COLUMN_MIGRATIONS", {
        V23: frozenset({("moyai_sessions", "online_note")}), V24: frozenset({(NOTES, "reviewed")}),
    })
    try:
        yield
    finally:
        base.remove(table)
        sessions._columns.remove(column)


def test_original_v22_plan_has_exact_historical_identity():
    value = online.plan("moyai", "native_online", "native-q")
    assert value["digest"] == "2481c80e0f82eea6b84c88dcbcc032137f38f3d79b6cd5238a7fbebcbb227cbf"
    assert online.plan("moyai", "moyai_product_authority_regression_execution_schema", "native-q")["digest"] == (
        "84cd528a35a78db3c1b71e270871ef127a7c71f5fccfa443c3cb3914e8e4607d"
    )


def test_frozen_prefixes_and_compiled_batches_are_stable(installed):
    inventories = online._inventory("native_online")
    for version in (online.FROM_VERSION, online.TO_VERSION, V23, V24):
        assert product._manifest(inventories[version]) == product._manifest(
            product.application_metadata("native_online", version=version),
        )
    legacy = online.plan("moyai", "native_online", "native-q", to_version=online.TO_VERSION)
    assert legacy["digest"] == "2481c80e0f82eea6b84c88dcbcc032137f38f3d79b6cd5238a7fbebcbb227cbf"
    value = online.plan("moyai", "native_online", "native-q")
    assert value["to_version"] == V24
    for _ in range(3):
        assert online.plan("moyai", "native_online", "native-q") == value
    ddl = online._ddl(inventories[V24], online._steps(online.FROM_VERSION, V24))
    assert sum("ADD COLUMN online_note" in item for item in ddl) == 1
    assert not any("ADD COLUMN reviewed" in item for item in ddl)  # Included in the batch's CREATE.
    assert "online_note" not in inventories[online.TO_VERSION].tables["native_online.moyai_sessions"].columns
    assert "reviewed" not in inventories[V23].tables[f"native_online.{NOTES}"].columns


@pytest.mark.parametrize("dependency", ["index", "check"])
def test_historical_column_filter_rejects_textual_dependencies(installed, dependency):
    table = product.Base.metadata.tables["moyai_sessions"]
    if dependency == "index":
        item = Index("unregistered_textual_index", text("lower(online_note)"), _table=table)
        collection = table.indexes
    else:
        item = CheckConstraint("online_note IS NULL")
        table.append_constraint(item)
        collection = table.constraints
    try:
        with pytest.raises(product.ProductSchemaError, match="plain nullable"):
            product.application_metadata("native_online", version=online.TO_VERSION)
    finally:
        collection.remove(item)


@pytest.mark.parametrize("change", ["default", "check", "fk", "type", "nullable", "index", "drop", "column-default"])
def test_semantic_verifier_rejects_edits_disguised_as_additions(monkeypatch, change):
    def unsafe(metadata, schema):
        sessions = metadata.tables[f"{schema}.moyai_sessions"]
        target = sessions.columns["title"]
        sessions.append_column(Column("online_note", Text, nullable=True))
        if change == "default":
            from sqlalchemy import DefaultClause
            target.server_default = DefaultClause(text("'changed'"))
        elif change == "check":
            sessions.append_constraint(CheckConstraint("online_note IS NULL"))
        elif change == "fk":
            next(iter(sessions.foreign_key_constraints)).ondelete = "CASCADE"
        elif change == "type":
            target.type = Integer()
        elif change == "nullable":
            target.nullable = not target.nullable
        elif change == "index":
            Index("unreviewed", sessions.columns["online_note"])
        elif change == "column-default":
            from sqlalchemy import DefaultClause
            sessions.columns["online_note"].server_default = DefaultClause(text("'backfill'"))
        else:
            sessions._columns.remove(target)

    step = online.Expansion(online.TO_VERSION, V23, frozenset(), (("moyai_sessions", "online_note"),), unsafe)
    monkeypatch.setattr(online, "EXPANSIONS", (*online.EXPANSIONS, step))
    with pytest.raises(online.OnlineSchemaError):
        online.plan("moyai", "native_online", "native-q")


@pytest.mark.parametrize("change", ["check", "default", "expression-index", "generated", "serial"])
def test_new_tables_do_not_admit_unknown_operations(monkeypatch, change):
    def unsafe(metadata, schema):
        from sqlalchemy import Computed
        table = Table(
            NOTES, metadata, Column("id", String(36), primary_key=True), Column("body", Text), schema=schema,
        )
        if change == "check":
            table.append_constraint(CheckConstraint("length(body)>0"))
        elif change == "default":
            table.append_column(Column("unsafe", Text, server_default=text("current_user")))
        elif change == "generated":
            table.append_column(Column("unsafe", Integer, Computed("length(body)")))
        elif change == "serial":
            table.append_column(Column("unsafe", Integer, primary_key=True, autoincrement=True))
        else:
            Index("unsafe", text("lower(body)"), _table=table)
    monkeypatch.setattr(online, "EXPANSIONS", (*online.EXPANSIONS, online.Expansion(
        online.TO_VERSION, V23, frozenset({NOTES}), (), unsafe,
    )))
    with pytest.raises(online.OnlineSchemaError):
        online.plan("moyai", "native_online", "native-q")


def test_unregistered_gap_and_downgrade_are_never_planned(registered, monkeypatch):
    for before, after in ((V24, V23), ("unknown", V24), (product.AUTHORITY_SCHEMA_VERSION, V24)):
        with pytest.raises(online.OnlineSchemaError):
            online.plan("moyai", "native_online", "native-q", from_version=before, to_version=after)
    monkeypatch.setattr(online, "EXPANSIONS", (online.EXPANSIONS[0], online.EXPANSIONS[2]))
    with pytest.raises(online.OnlineSchemaError):
        online.plan("moyai", "native_online", "native-q")


def test_receipt_planner_requires_the_exact_contiguous_prefix(registered):
    value = online.plan("moyai", "native_online", "native-q", from_version=V23)
    assert online.plan_from_receipts("moyai", "native_online", "native-q", value["before"]["app"]) == value
    for receipts in ({}, {V23: value["before"]["app"][V23]}, {**value["before"]["app"], "unknown": "f" * 64}):
        with pytest.raises(online.OnlineSchemaError):
            online.plan_from_receipts("moyai", "native_online", "native-q", receipts)


def rows(connection, schema, table):
    return connection.execute(sql.SQL("SELECT * FROM {} ORDER BY version").format(
        sql.Identifier(schema, table),
    )).fetchall()


@PG
async def test_sequential_batches_preserve_original_native_and_online_receipts(expansion, registered):
    database, legacy, connection, _ = expansion
    original = rows(connection, database.schema, product.RECEIPTS)
    online.apply(connection, legacy, guard=lambda _: None)
    legacy_ledger = rows(connection, database.schema, online.LEDGER)
    batches = [legacy]
    for before, after in ((online.TO_VERSION, V23), (V23, V24)):
        value = online.plan("moyai", database.schema, "native-online-test", from_version=before, to_version=after)
        assert online.inspect(connection, value)["status"] == "planned"
        assert online.apply(connection, value, guard=lambda _: None)["status"] == "applied"
        assert online.inspect(connection, value)["status"] == "applied"
        batches.append(value)
    for batch in batches:
        assert online.inspect(connection, batch) == {
            "protocol": online.PROTOCOL, "status": "applied", "digest": batch["digest"], "target": batch["target"],
        }
        assert online.apply(connection, batch, guard=lambda _: None)["status"] == "already_applied"
    assert set(original) <= set(rows(connection, database.schema, product.RECEIPTS))
    assert rows(connection, database.schema, online.LEDGER)[0] == legacy_ledger[0]
    assert len(rows(connection, database.schema, online.LEDGER)) == 3
    current = online.plan_from_receipts("moyai", database.schema, "native-online-test", value["target"]["app"])
    assert current["from_version"] == current["to_version"] == V24
    assert online.inspect(connection, current)["status"] == "current"
    before = rows(connection, database.schema, online.LEDGER)
    assert online.apply(connection, current, guard=lambda _: None)["status"] == "already_current"
    assert rows(connection, database.schema, online.LEDGER) == before
    await product.verify_product_schema(database.source, schema=database.schema, version=online.FROM_VERSION)
    await product.verify_product_schema(database.source, schema=database.schema, version=online.TO_VERSION)

    # Historical inspection still validates the actual latest structure.
    connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN reviewed SET DEFAULT false").format(
        sql.Identifier(database.schema, NOTES),
    ))
    with pytest.raises(online.OnlineSchemaError, match="target_structure_changed"):
        online.inspect(connection, legacy)


@PG
@pytest.mark.parametrize("target", [V23, V24])
async def test_atomic_multi_step_receipts_and_rollback(expansion, registered, target):
    database, _, connection, _ = expansion
    value = online.plan("moyai", database.schema, "native-online-test", to_version=target)
    calls = []

    def fail_after_ddl(_):
        calls.append(True)
        if len(calls) == 2:
            raise online.OnlineSchemaError("test_guard_changed")

    with pytest.raises(online.OnlineSchemaError, match="test_guard_changed"):
        online.apply(connection, value, guard=fail_after_ddl)
    assert online.inspect(connection, value)["status"] == "planned"
    assert connection.execute("SELECT to_regclass(%s)", (database.schema + "." + online.LEDGER,)).fetchone() == (None,)
    assert online.apply(connection, value, guard=lambda _: None)["status"] == "applied"
    saved = rows(connection, database.schema, online.LEDGER)
    assert {row[0] for row in saved} == set(value["target"]["app"]) - set(value["before"]["app"])
    assert all(row[2:] == (value["digest"], value) for row in saved)
    assert online.apply(connection, deepcopy(value), guard=lambda _: None)["status"] == "already_applied"


@PG
@pytest.mark.parametrize("change", ["unknown", "gap", "manifest"])
async def test_historical_inspection_requires_an_exact_known_receipt_prefix(expansion, registered, change):
    database, legacy, connection, _ = expansion
    online.apply(connection, legacy, guard=lambda _: None)
    later_batch = online.plan("moyai", database.schema, "native-online-test", from_version=online.TO_VERSION)
    online.apply(connection, later_batch, guard=lambda _: None)
    receipt_table = sql.Identifier(database.schema, product.RECEIPTS)
    if change == "unknown":
        connection.execute(sql.SQL("INSERT INTO {}(version,manifest) VALUES('unknown',%s)").format(receipt_table),
                           ("f" * 64,))
    elif change == "gap":
        connection.execute(sql.SQL("DELETE FROM {} WHERE version=%s").format(receipt_table), (V23,))
    else:
        connection.execute(sql.SQL("UPDATE {} SET manifest=%s WHERE version=%s").format(receipt_table),
                           ("f" * 64, V23))
    saved = rows(connection, database.schema, online.LEDGER)
    current = rows(connection, database.schema, product.RECEIPTS)
    with pytest.raises(online.OnlineSchemaError, match="source_receipts_changed"):
        online.inspect(connection, legacy)
    with pytest.raises(online.OnlineSchemaError, match="source_receipts_changed"):
        online.apply(connection, legacy, guard=lambda _: None)
    assert rows(connection, database.schema, online.LEDGER) == saved
    assert rows(connection, database.schema, product.RECEIPTS) == current


@PG
async def test_offline_provisioned_current_noop_does_not_create_a_ledger(product_database, installed):
    database = product_database
    await product.migrate_product_schema(database.source, schema=database.schema, source_schema="moyai", version=V24)
    await product.verify_product_schema(database.source, schema=database.schema, version=V24)
    value = online.plan("moyai", database.schema, "native-q", from_version=V24)
    with psycopg.connect(database.settings.postgres_url, autocommit=True) as connection:
        # A read-only session proves there is no durable write, including a
        # hidden ledger/table initializer, even through the apply entrypoint.
        connection.execute("SET default_transaction_read_only=on")
        assert online.inspect(connection, value)["status"] == "current"
        assert online.apply(connection, value, guard=lambda _: None)["status"] == "already_current"
        assert connection.execute("SELECT to_regclass(%s)", (database.schema + "." + online.LEDGER,)).fetchone() == (None,)


@PG
async def test_nullable_addition_refuses_a_reader_without_queueing(expansion, registered):
    database, legacy, connection, dsn = expansion
    online.apply(connection, legacy, guard=lambda _: None)
    value = online.plan("moyai", database.schema, "native-online-test", from_version=online.TO_VERSION, to_version=V23)
    with psycopg.connect(dsn, autocommit=True) as reader, reader.transaction():
        reader.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(database.schema, "moyai_sessions")))
        with pytest.raises(online.OnlineSchemaError) as caught:
            online.apply(connection, value, guard=lambda _: None)
        assert isinstance(caught.value.__context__, psycopg.errors.LockNotAvailable)
        assert reader.execute("SELECT 1").fetchone() == (1,)
    assert online.inspect(connection, value)["status"] == "planned"


@PG
@pytest.mark.parametrize("change", [
    "batch-row", "digest", "scope", "queue", "default", "check", "column-index",
    "nullability", "table-default", "index-order", "index-method", "inheritance",
])
async def test_current_noop_rejects_drift_in_batches_or_added_structure(expansion, registered, change):
    database, _, connection, _ = expansion
    value = online.plan("moyai", database.schema, "native-online-test")
    online.apply(connection, value, guard=lambda _: None)
    ledger = sql.Identifier(database.schema, online.LEDGER)
    if change == "batch-row":
        connection.execute(sql.SQL("DELETE FROM {} WHERE version=%s").format(ledger), (V23,))
    elif change == "digest":
        connection.execute(sql.SQL("UPDATE {} SET plan_digest=%s").format(ledger), ("f" * 64,))
    elif change in {"scope", "queue"}:
        key = "source_schema" if change == "scope" else "task_queue"
        connection.execute(sql.SQL("UPDATE {} SET plan=jsonb_set(plan,%s,'\"changed\"')").format(ledger), ([key],))
    elif change in {"table-default", "index-order", "index-method", "inheritance"}:
        notes_table = sql.Identifier(database.schema, NOTES)
        if change == "table-default":
            connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN body SET DEFAULT 'changed'").format(notes_table))
        elif change == "inheritance":
            connection.execute(sql.SQL("CREATE TABLE {} () INHERITS ({})").format(
                sql.Identifier(database.schema, "inherited_sessions"),
                sql.Identifier(database.schema, "moyai_sessions"),
            ))
        else:
            name = sql.Identifier(database.schema, "moyai_online_test_notes_session")
            connection.execute(sql.SQL("DROP INDEX {}").format(name))
            definition = "USING hash (session_id)" if change == "index-method" else "(session_id DESC)"
            connection.execute(sql.SQL("CREATE INDEX moyai_online_test_notes_session ON {} " + definition)
                               .format(notes_table))
    else:
        sessions = sql.Identifier(database.schema, "moyai_sessions")
        if change == "default":
            connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN online_note SET DEFAULT 'changed'").format(sessions))
        elif change == "check":
            connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT changed CHECK(online_note IS NULL)").format(sessions))
        elif change == "column-index":
            connection.execute(sql.SQL("CREATE INDEX changed ON {}(lower(online_note))").format(sessions))
        else:
            connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN online_note SET NOT NULL").format(sessions))
    current = online.plan("moyai", database.schema, "native-online-test", from_version=V24)
    with pytest.raises(online.OnlineSchemaError):
        online.inspect(connection, current)
    with pytest.raises(online.OnlineSchemaError):
        online.apply(connection, current, guard=lambda _: None)


def test_engine_executes_as_the_unregistered_transport_module():
    from pathlib import Path
    module = ModuleType("moyai._qualified_online_schema")
    module.__package__ = "moyai"
    exec(Path(online.__file__).read_text(), module.__dict__)  # noqa: S102 - exact transport mechanism
    assert module.plan("moyai", "native_online", "native-q") == online.plan("moyai", "native_online", "native-q")


@PG
@pytest.mark.skipif(not os.getenv("MOYAI_TEST_OLD_SOURCE"), reason="Actual pre-expansion source checkout required.")
async def test_future_batches_execute_on_the_actual_old_app_interpreter(expansion, registered):
    import asyncio
    import inspect
    import json
    import subprocess
    from pathlib import Path

    database, _, connection, _ = expansion
    source = Path(os.environ["MOYAI_TEST_OLD_SOURCE"])
    value = online.plan("moyai", database.schema, "native-online-test")
    # Test-only future definitions supplement the shipped module. The old
    # interpreter, installed product schema, PostgreSQL and engine are real.
    definitions = inspect.getsource(notes) + "\n" + inspect.getsource(later)
    payload = {
        "engine": Path(online.__file__).read_text(), "definitions": definitions, "dsn": database.settings.postgres_url,
        "plan": value, "source": str(source),
    }
    child = '''
import json,sys,types
from pathlib import Path
import psycopg
from moyai import product_schema
body=json.load(sys.stdin)
assert Path(product_schema.__file__).is_relative_to(Path(body["source"])/"native/src")
assert product_schema.MIGRATIONS[-1][0]=="2026-10-18-message-sources-v21"
engine=types.ModuleType("moyai._qualified_online_schema")
engine.__package__="moyai"
exec(body["engine"],engine.__dict__)
scope=engine.__dict__
scope.update(NOTES="moyai_online_test_notes")
exec(body["definitions"],scope)
v23,v24="2026-10-19-test-online-v23","2026-10-19-test-online-v24"
engine.EXPANSIONS=(*engine.EXPANSIONS,
    engine.Expansion(engine.TO_VERSION,v23,frozenset({scope["NOTES"]}),
                     (("moyai_sessions","online_note"),),scope["notes"]),
    engine.Expansion(v23,v24,frozenset(),((scope["NOTES"],"reviewed"),),scope["later"]))
value=body["plan"]
assert engine.plan(value["source_schema"],value["native_schema"],value["task_queue"])==value
with psycopg.connect(body["dsn"],autocommit=True) as connection:
    before=engine.inspect(connection,value)
    applied=engine.apply(connection,value,guard=lambda _:None)
    observed=engine.inspect(connection,value)
    current=engine.plan_from_receipts(value["source_schema"],value["native_schema"],value["task_queue"],
                                     value["target"]["app"])
    noop=engine.apply(connection,current,guard=lambda _:None)
print(json.dumps([item["status"] for item in (before,applied,observed,noop)]))
'''
    result = await asyncio.to_thread(
        subprocess.run, [str(source / ".venv/bin/python"), "-c", child], cwd=source, check=False,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(map(str, [source / "native/src", source]))},
        input=json.dumps(payload), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["planned", "applied", "applied", "already_current"]
    assert online.inspect(connection, value)["status"] == "applied"
