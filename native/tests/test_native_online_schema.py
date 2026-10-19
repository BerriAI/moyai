"""Real PostgreSQL expansion/rollback checks; actual-app proof is separate."""

from __future__ import annotations

import asyncio
import os
import time
from copy import deepcopy

import product_database_fixtures as database_fixtures
import psycopg
import pytest
import pytest_asyncio
from psycopg import sql

from moyai.native_online_schema import (
    ADDED,
    FROM_VERSION,
    LEDGER,
    TO_VERSION,
    OnlineSchemaError,
    _indexes,
    _metadata,
    apply,
    inspect,
    plan,
    validate,
)
from moyai.product_schema import (
    _manifest,
    application_metadata,
    migrate_product_schema,
    verify_product_schema,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = database_fixtures.product_database


@pytest_asyncio.fixture
async def expansion(product_database):
    product = product_database
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=FROM_VERSION,
    )
    value = plan("moyai", product.schema, "native-online-test")
    dsn = product.settings.postgres_url
    assert dsn
    with psycopg.connect(dsn, autocommit=True) as connection:
        yield product, value, connection, dsn


def assert_missing(connection, schema, *names):
    for name in names:
        assert connection.execute("SELECT to_regclass(%s)", (schema + "." + name,)).fetchone() == (None,)


def test_frozen_expansion_matches_the_real_v22_tables_and_indexes():
    # Includes PostgreSQL index-name shortening for supported long schemas.
    for schema in ("native_online", "moyai_product_authority_regression_execution_schema"):
        value = plan("moyai", schema, "native-q")
        frozen = _metadata(schema)
        actual = application_metadata(schema, version=TO_VERSION)
        assert _manifest(frozen) == _manifest(actual)
        assert _indexes(frozen) == _indexes(actual)
        assert plan("moyai", schema, "native-q") == value
        assert value["before"]["app"].keys() < value["target"]["app"].keys()
        assert set(value["target"]["app"]) - set(value["before"]["app"]) == {TO_VERSION}
        assert all(value["target"]["app"][key] == manifest for key, manifest in value["before"]["app"].items())


@pytest.mark.parametrize("change", ["sql", "digest", "prefix", "schema", "queue", "extra"])
def test_plan_rejects_unreviewed_operations_or_changed_bindings(change):
    value = plan("moyai", "native_online", "native-q")
    if change == "sql":
        value["sql"] = ["DROP TABLE moyai.users"]
    elif change == "digest":
        value["ddl_digest"] = "f" * 64
    elif change == "prefix":
        value["before"]["app"][FROM_VERSION] = "f" * 64
    elif change == "schema":
        value["source_schema"] = "somewhere_else"
    elif change == "queue":
        value["task_queue"] = "another-queue"
    else:
        value["target"]["app"]["unknown-future-migration"] = "f" * 64
    with pytest.raises(OnlineSchemaError):
        validate(value)


async def test_inspection_is_read_only_and_never_initializes_metadata(expansion):
    product, value, connection, _ = expansion
    assert inspect(connection, value)["status"] == "planned"
    assert_missing(connection, product.schema, *ADDED, LEDGER)
    await verify_product_schema(product.source, schema=product.schema, version=FROM_VERSION)


async def test_expansion_preserves_old_receipts_rows_and_a_live_owner(expansion):
    product, value, connection, dsn = expansion
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute("SELECT pg_advisory_lock_shared(726940,'moyai'::regnamespace::oid::int)")
        pid = owner.execute("SELECT pg_backend_pid()").fetchone()[0]
        before = connection.execute("SELECT * FROM moyai.users").fetchall()
        guards = []

        def guard(budget):
            assert budget.execute("SELECT 1 FROM pg_stat_activity WHERE pid=%s", (pid,)).fetchone() == (1,)
            guards.append(True)

        receipt = await asyncio.to_thread(apply, connection, value, guard=guard)
        assert receipt["status"] == "applied" and len(guards) == 2
        assert connection.execute("SELECT * FROM moyai.users").fetchall() == before
        assert owner.execute("SELECT 1").fetchone() == (1,)
        assert inspect(connection, value)["status"] == "applied"
        assert apply(connection, value, guard=guard)["status"] == "already_applied"
    await verify_product_schema(product.source, schema=product.schema, version=FROM_VERSION)
    await verify_product_schema(product.source, schema=product.schema, version=TO_VERSION)
    for table in ADDED:
        assert connection.execute(sql.SQL("SELECT count(*) FROM {}").format(
            sql.Identifier(product.schema, table),
        )).fetchone() == (0,)


@pytest.mark.parametrize("lock", ["offline-owner", "native-migrator", "busy-parent"])
async def test_contention_aborts_without_changing_receipts_or_waiting_behind_writers(expansion, lock):
    product, value, connection, dsn = expansion
    with psycopg.connect(dsn, autocommit=True) as blocker, blocker.transaction():
        if lock == "offline-owner":
            blocker.execute("SELECT pg_advisory_xact_lock(726940,'moyai'::regnamespace::oid::int)")
        elif lock == "native-migrator":
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (
                "moyai-native-migration:" + product.schema,
            ))
        else:
            blocker.execute(sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(
                sql.Identifier(product.schema, "moyai_sessions"),
            ))
        started = time.monotonic()
        with pytest.raises(OnlineSchemaError):
            await asyncio.to_thread(apply, connection, value, guard=lambda _: None)
        assert time.monotonic() - started < 1
        assert_missing(connection, product.schema, *ADDED, LEDGER)
        # Independent reads continue despite the held writer lock and rejected DDL.
        assert blocker.execute("SELECT count(*) FROM moyai.users").fetchone() == (1,)
    assert inspect(connection, value)["status"] == "planned"


async def test_guard_failure_after_ddl_rolls_back_tables_and_both_receipts(expansion):
    product, value, connection, _ = expansion
    calls = 0

    def guard(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OnlineSchemaError("native_online_owner_changed")

    with pytest.raises(OnlineSchemaError, match="owner_changed"):
        apply(connection, value, guard=guard)
    assert_missing(connection, product.schema, *ADDED, LEDGER)
    assert inspect(connection, value)["status"] == "planned"
    await verify_product_schema(product.source, schema=product.schema, version=FROM_VERSION)


async def test_uncertain_reply_reconciles_without_repeating_any_create(expansion):
    _, value, connection, _ = expansion
    # The actual commit succeeds; its caller discards the reply.
    apply(connection, value, guard=lambda _: None)
    assert inspect(connection, value)["status"] == "applied"
    assert apply(connection, deepcopy(value), guard=lambda _: None)["status"] == "already_applied"


@pytest.mark.parametrize("tamper", ["partial-table", "old-receipt", "target-without-ledger", "changed-index"])
async def test_unknown_or_modified_schema_is_never_adopted_or_repaired(expansion, tamper):
    product, value, connection, _ = expansion
    if tamper == "partial-table":
        connection.execute(sql.SQL("CREATE TABLE {}(id text)").format(
            sql.Identifier(product.schema, "moyai_goals"),
        ))
    elif tamper == "old-receipt":
        connection.execute(sql.SQL("UPDATE {} SET manifest=%s WHERE version=%s").format(
            sql.Identifier(product.schema, "moyai_app_schema_versions"),
        ), ("e" * 64, FROM_VERSION))
    else:
        apply(connection, value, guard=lambda _: None)
        if tamper == "target-without-ledger":
            connection.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(product.schema, LEDGER)))
        else:
            connection.execute(sql.SQL("DROP INDEX {}").format(
                sql.Identifier(product.schema, "moyai_goal_dispatch"),
            ))
    with pytest.raises(OnlineSchemaError):
        inspect(connection, value)
    with pytest.raises(OnlineSchemaError):
        apply(connection, value, guard=lambda _: None)


async def test_guard_queries_share_the_migration_deadline(expansion):
    product, value, connection, _ = expansion
    started = time.monotonic()
    with pytest.raises(OnlineSchemaError, match="unconfirmed"):
        await asyncio.to_thread(apply, connection, value, guard=lambda budget: budget.execute("SELECT pg_sleep(4)"))
    assert time.monotonic() - started < 3
    assert_missing(connection, product.schema, *ADDED, LEDGER)


@pytest.mark.parametrize("change", ["view", "trigger", "rule", "check", "default", "index", "rls", "primary"])
async def test_matching_receipt_values_do_not_authorize_unknown_write_behavior(expansion, change):
    product, value, connection, _ = expansion
    table = sql.Identifier(product.schema, "moyai_app_schema_versions")
    if change == "view":
        connection.execute(sql.SQL("ALTER TABLE {} RENAME TO saved_receipts").format(table))
        connection.execute(sql.SQL("CREATE VIEW {} AS SELECT * FROM {}").format(
            table, sql.Identifier(product.schema, "saved_receipts"),
        ))
    elif change == "trigger":
        function = sql.Identifier(product.schema, "unreviewed_receipt")
        connection.execute(sql.SQL("""
            CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS
            $$ BEGIN DELETE FROM moyai.users; RETURN NEW; END $$
        """).format(function))
        connection.execute(sql.SQL("CREATE TRIGGER unreviewed BEFORE INSERT ON {} FOR EACH ROW EXECUTE FUNCTION {}()")
                           .format(table, function))
    elif change == "rule":
        connection.execute(sql.SQL("CREATE RULE unreviewed AS ON INSERT TO {} DO ALSO DELETE FROM moyai.users")
                           .format(table))
    elif change == "check":
        function = sql.Identifier(product.schema, "unreviewed_check")
        connection.execute(sql.SQL("""
            CREATE FUNCTION {}() RETURNS boolean LANGUAGE plpgsql AS
            $$ BEGIN DELETE FROM moyai.users; RETURN true; END $$
        """).format(function))
        connection.execute(sql.SQL("ALTER TABLE {} ADD CONSTRAINT unreviewed CHECK ({}()) NOT VALID")
                           .format(table, function))
    elif change == "default":
        connection.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN applied_at SET DEFAULT now()").format(table))
    elif change == "index":
        connection.execute(sql.SQL("CREATE INDEX unreviewed ON {} (lower(version))").format(table))
    elif change == "rls":
        connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
    else:
        connection.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT moyai_app_schema_versions_pkey").format(table))
    # None of these DDL changes altered the existing manifest rows. The old
    # INSERT would execute the unreviewed write hooks despite matching values.
    for action in (lambda: inspect(connection, value), lambda: apply(connection, value, guard=lambda _: None)):
        with pytest.raises(OnlineSchemaError, match="receipt_structure_changed"):
            action()
    assert connection.execute("SELECT count(*) FROM moyai.users").fetchone() == (1,)
    assert_missing(connection, product.schema, *ADDED, LEDGER)


async def test_committed_ledger_requires_its_original_primary_key(expansion):
    product, value, connection, _ = expansion
    apply(connection, value, guard=lambda _: None)
    connection.execute(sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
        sql.Identifier(product.schema, LEDGER), sql.Identifier(LEDGER + "_pkey"),
    ))
    with pytest.raises(OnlineSchemaError, match="receipt_structure_changed"):
        inspect(connection, value)
