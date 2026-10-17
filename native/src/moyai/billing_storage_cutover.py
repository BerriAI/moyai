"""Retire both source tables after an exact, maintenance-only storage cutover.

The old names become ordinary automatically updatable PostgreSQL views. They
contain no rows and require no forwarding or mirroring triggers. PostgreSQL
rewrites their reads, writes, ON CONFLICT clauses and row locks to the ledger.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import BigInteger, Integer, String, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .billing_storage import (
    SOURCES,
    STORAGE_FIELDS,
    TRIGGER,
    TRUNCATE_TRIGGER,
    BillingStorageError,
    Source,
    Storage,
    _compare,
    _state,
    _timeouts,
    identifier,
    verify_mirrors,
)
from .billing_storage_models import storage_column


def quoted(value: str) -> str:
    """Quote names obtained from PostgreSQL catalogs, including role names."""
    return '"' + value.replace('"', '""') + '"'


async def _definition(connection: AsyncConnection, storage: Storage, source: Source) -> dict[str, Any]:
    async with _catalog_path(connection):
        return await _catalog_definition(connection, storage, source)


@asynccontextmanager
async def _catalog_path(connection: AsyncConnection) -> AsyncIterator[None]:
    """Deparse object definitions consistently on source and native pools."""
    previous = await connection.scalar(text("SELECT current_setting('search_path')"))
    await connection.execute(text("SELECT set_config('search_path','pg_catalog',true)"))
    yield
    # On failure the caller rolls back the transaction and its LOCAL settings.
    await connection.execute(text("SELECT set_config('search_path',:value,true)"), {"value": previous})


async def _catalog_definition(connection: AsyncConnection, storage: Storage, source: Source) -> dict[str, Any]:
    table = storage.source_table(source)
    relation = (await connection.execute(text("""
        SELECT c.relkind::text AS kind,c.relrowsecurity,c.relforcerowsecurity,
               pg_get_userbyid(c.relowner) AS owner,pg_get_viewdef(c.oid,true) AS definition,
               c.reloptions
        FROM pg_class c WHERE c.oid=CAST(:table AS regclass)
    """), {"table": table})).mappings().one()
    defaults = (await connection.execute(text("""
        SELECT a.attname,pg_get_expr(d.adbin,d.adrelid) AS value
        FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid=CAST(:table AS regclass) ORDER BY a.attname
    """), {"table": table})).mappings().all()
    return {
        **dict(relation),
        "defaults": {row["attname"]: row["value"] for row in defaults},
    }


async def _ledger_shape(connection: AsyncConnection, storage: Storage) -> dict[str, Any]:
    async with _catalog_path(connection):
        return await _catalog_ledger_shape(connection, storage)


async def _catalog_ledger_shape(connection: AsyncConnection, storage: Storage) -> dict[str, Any]:
    constraints = (await connection.execute(text("""
        SELECT conname,pg_get_constraintdef(oid,true) AS definition,convalidated
        FROM pg_constraint WHERE conrelid=CAST(:table AS regclass) ORDER BY conname
    """), {"table": storage.ledger})).mappings().all()
    indexes = (await connection.execute(text("""
        SELECT pg_get_indexdef(indexrelid) AS definition,indisvalid
        FROM pg_index WHERE indrelid=CAST(:table AS regclass) ORDER BY indexrelid::regclass::text
    """), {"table": storage.ledger})).mappings().all()
    kind = await connection.scalar(text(
        "SELECT relkind::text FROM pg_class WHERE oid=CAST(:table AS regclass)"
    ), {"table": storage.ledger})
    return {"kind": kind, "constraints": [dict(row) for row in constraints],
            "indexes": [dict(row) for row in indexes]}


async def verify_consolidated(
    connection: AsyncConnection, storage: Storage, state: Mapping[str, Any] | None = None,
) -> None:
    state = state if state is not None else await _state(connection, storage)
    saved = state["view_manifest"]
    if state["phase"] != "consolidated" or not isinstance(saved, dict) or saved.get("version") != 1:
        raise BillingStorageError("The physical billing cutover receipt is missing.")
    for source in SOURCES:
        actual = await _definition(connection, storage, source)
        if actual["kind"] != "v" or actual != saved.get(source):
            raise BillingStorageError(f"The {source} billing compatibility view differs from its receipt.")
    ledger = await _ledger_shape(connection, storage)
    if ledger["kind"] != "r" or ledger != saved.get("ledger"):
        raise BillingStorageError("The single billing store's constraints or indexes differ from its receipt.")


async def _source_spec(
    connection: AsyncConnection, storage: Storage, source: Source,
) -> dict[str, Any]:
    schema = storage.source_schema if source == "legacy" else storage.native_schema
    table = "model_requests" if source == "legacy" else "moyai_inference_usage"
    relation = (await connection.execute(text("""
        SELECT c.relkind::text AS kind,c.relrowsecurity,c.relforcerowsecurity,
               pg_get_userbyid(c.relowner) AS owner
        FROM pg_class c WHERE c.oid=CAST(:table AS regclass)
    """), {"table": storage.source_table(source)})).mappings().one()
    if relation["kind"] != "r" or relation["relrowsecurity"] or relation["relforcerowsecurity"]:
        raise BillingStorageError("Physical cutover requires the reviewed source tables without custom RLS.")
    ledger_owner = await connection.scalar(text(
        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=CAST(:table AS regclass)"
    ), {"table": storage.ledger})
    if relation["owner"] != ledger_owner:
        raise BillingStorageError("Source and ledger ownership differ; review access before consolidation.")

    def read(conn: Any) -> dict[str, Any]:
        inspector = inspect(conn)
        return {
            "columns": inspector.get_columns(table, schema=schema),
            "primary": inspector.get_pk_constraint(table, schema=schema),
            "unique": inspector.get_unique_constraints(table, schema=schema),
            "foreign": inspector.get_foreign_keys(table, schema=schema),
            "checks": inspector.get_check_constraints(table, schema=schema),
            "indexes": inspector.get_indexes(table, schema=schema),
        }

    spec = await connection.run_sync(read)
    names = {column["name"] for column in spec["columns"]}
    if not names <= set(STORAGE_FIELDS[source]):
        raise BillingStorageError(f"The {source} table has unmapped columns; no data was retired.")
    if spec["primary"]["constrained_columns"] != ["id"] or spec["checks"]:
        raise BillingStorageError("Custom billing primary/check constraints require a reviewed migration.")
    for unique in spec["unique"]:
        if (
            source != "native" or unique["column_names"] not in (["request_key"], ["receipt_key"])
            or unique.get("dialect_options", {}).get("postgresql_nulls_not_distinct")
        ):
            raise BillingStorageError("Custom billing uniqueness requires a reviewed migration.")
    custom_timing = await connection.scalar(text("""
        SELECT count(*) FROM pg_constraint WHERE conrelid=CAST(:table AS regclass)
        AND (condeferrable OR NOT convalidated)
    """), {"table": storage.source_table(source)})
    if custom_timing:
        raise BillingStorageError("Deferred or unvalidated billing constraints require a reviewed migration.")
    for foreign in spec["foreign"]:
        if (
            source != "legacy" or foreign["constrained_columns"] != ["run_id"]
            or foreign["referred_table"] != "runs" or foreign["referred_columns"] != ["id"]
            or foreign.get("referred_schema", schema) not in (None, storage.source_schema)
            or foreign.get("options")
        ):
            raise BillingStorageError("Custom billing foreign keys require a reviewed migration.")
    triggers = (await connection.execute(text("""
        SELECT tgname FROM pg_trigger WHERE tgrelid=CAST(:table AS regclass) AND NOT tgisinternal
    """), {"table": storage.source_table(source)})).scalars().all()
    if set(triggers) != {TRIGGER, TRUNCATE_TRIGGER}:
        raise BillingStorageError("Custom billing triggers require a reviewed migration.")
    grants = (await connection.execute(text("""
        SELECT x.grantee=0 AS public,pg_get_userbyid(x.grantee) AS role,x.privilege_type,x.is_grantable
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
        WHERE c.oid=CAST(:table AS regclass) AND x.grantee<>c.relowner
    """), {"table": storage.source_table(source)})).mappings().all()
    column_grants = (await connection.execute(text("""
        SELECT a.attname,x.grantee=0 AS public,pg_get_userbyid(x.grantee) AS role,
               x.privilege_type,x.is_grantable
        FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
        WHERE a.attrelid=CAST(:table AS regclass) AND a.attnum>0 AND NOT a.attisdropped
    """), {"table": storage.source_table(source)})).mappings().all()
    return {**spec, "owner": relation["owner"], "grants": grants, "column_grants": column_grants}


async def _constraints(
    connection: AsyncConnection, storage: Storage, source: Source, spec: dict[str, Any],
) -> None:
    checks = []
    for column in spec["columns"]:
        name = quoted(storage_column(source, column["name"]))
        if not column["nullable"]:
            checks.append(f"{name} IS NOT NULL")
        kind = column["type"]
        if isinstance(kind, String) and kind.length is not None:
            checks.append(f"({name} IS NULL OR length({name})<={kind.length})")
        if isinstance(kind, Integer) and not isinstance(kind, BigInteger):
            checks.append(f"({name} IS NULL OR {name} BETWEEN -2147483648 AND 2147483647)")
    if checks:
        await connection.execute(text(
            f"ALTER TABLE {storage.ledger} ADD CONSTRAINT moyai_billing_{source}_shape "
            f"CHECK (source<>'{source}' OR ({' AND '.join(checks)}))"
        ))
    if spec["foreign"]:
        await connection.execute(text(
            f"ALTER TABLE {storage.ledger} ADD CONSTRAINT moyai_billing_legacy_run "
            f"FOREIGN KEY (legacy_run_id) REFERENCES {identifier(storage.source_schema)}.runs(id)"
        ))
    # Preserve source-specific access paths without copying stored rows.
    for number, index in enumerate(spec["indexes"]):
        if index.get("duplicates_constraint"):
            continue
        columns = index["column_names"]
        if not all(name in STORAGE_FIELDS[source] for name in columns):
            raise BillingStorageError("Expression billing indexes require a reviewed migration.")
        if index["unique"]:
            predicate = index.get("dialect_options", {}).get("postgresql_where") or ""
            normalized = re.sub(r"[\s()]", "", predicate).replace("::text", "")
            if (
                source == "legacy" and columns == ["gateway_scope", "key_hash", "cost_receipt_id"]
                and normalized == "cost_receipt_id<>''"
                and not index.get("dialect_options", {}).get("postgresql_nulls_not_distinct")
            ):
                continue  # Already present on the shared ledger.
            raise BillingStorageError("Custom billing unique indexes require a reviewed migration.")
        predicate = index.get("dialect_options", {}).get("postgresql_where")
        if predicate and not (
            source == "legacy" and columns == ["key_hash", "gateway_scope", "cost_next_attempt_at", "id"]
        ):
            raise BillingStorageError("Custom partial billing indexes require a reviewed migration.")
        names = ",".join(quoted(storage_column(source, name)) for name in columns)
        # The reviewed recovery index can be wider here; query semantics stay
        # unchanged and its exact original field ordering remains available.
        await connection.execute(text(
            f"CREATE INDEX moyai_billing_{source}_compat_{number} ON {storage.ledger} "
            f"({names}) WHERE source='{source}'"
        ))


async def _view(
    connection: AsyncConnection, storage: Storage, source: Source, spec: dict[str, Any],
) -> None:
    table = storage.source_table(source)
    columns = ",".join(
        f'{quoted(storage_column(source, column["name"]))} AS {quoted(column["name"])}'
        for column in spec["columns"]
    )
    # RESTRICT is deliberate: unknown incoming FKs/views prevent retirement
    # instead of silently dropping an unrelated consumer.
    await connection.execute(text(f"DROP TABLE {table} RESTRICT"))
    await connection.execute(text(
        f"CREATE VIEW {table} WITH (security_barrier=true) AS SELECT {columns},source "
        f"FROM {storage.ledger} WHERE source='{source}' WITH CASCADED CHECK OPTION"
    ))
    await connection.execute(text(f"ALTER VIEW {table} ALTER COLUMN source SET DEFAULT '{source}'"))
    for column in spec["columns"]:
        if column.get("default") is not None:
            # This expression is PostgreSQL's saved source default, not user
            # input. Restoring it preserves omitted-field INSERT behavior.
            await connection.exec_driver_sql(
                f'ALTER VIEW {table} ALTER COLUMN {quoted(column["name"])} SET DEFAULT {column["default"]}'
            )
    await connection.execute(text(f"ALTER VIEW {table} OWNER TO {quoted(spec['owner'])}"))
    default_grants = (await connection.execute(text("""
        SELECT DISTINCT x.grantee=0 AS public,pg_get_userbyid(x.grantee) AS role
        FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x
        WHERE c.oid=CAST(:table AS regclass) AND x.grantee<>c.relowner
    """), {"table": table})).mappings().all()
    for grantee in default_grants:
        role = "PUBLIC" if grantee["public"] else quoted(grantee["role"])
        await connection.execute(text(f"REVOKE ALL ON {table} FROM {role}"))
    for grant in spec["grants"]:
        privilege = grant["privilege_type"]
        if privilege not in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
            continue  # Physical-table DDL privileges have no view equivalent.
        role = "PUBLIC" if grant["public"] else quoted(grant["role"])
        suffix = " WITH GRANT OPTION" if grant["is_grantable"] else ""
        await connection.execute(text(f"GRANT {privilege} ON {table} TO {role}{suffix}"))
    for grant in spec["column_grants"]:
        privilege = grant["privilege_type"]
        if privilege not in {"SELECT", "INSERT", "UPDATE"}:
            continue
        role = "PUBLIC" if grant["public"] else quoted(grant["role"])
        suffix = " WITH GRANT OPTION" if grant["is_grantable"] else ""
        await connection.execute(text(
            f"GRANT {privilege} ({quoted(grant['attname'])}) ON {table} TO {role}{suffix}"
        ))
    await connection.execute(text(f"DROP FUNCTION {storage.function(source)}() RESTRICT"))


async def consolidate(engine: AsyncEngine, storage: Storage) -> None:
    """Atomically replace both physical sources; only one store survives commit."""
    async with engine.begin() as connection:
        await _timeouts(connection)
        exclusive = await connection.scalar(text(
            "SELECT pg_try_advisory_xact_lock(726940,CAST(CAST(:schema AS regnamespace) AS oid)::int)"
        ), {"schema": storage.source_schema})
        if not exclusive:
            raise BillingStorageError("Drain and stop all Moyai runtime owners before physical consolidation.")
        source_revision = (await connection.execute(text(
            f"SELECT revision,dirty FROM {identifier(storage.source_schema)}.schema_state WHERE id=1"
        ))).one_or_none()
        if source_revision is None or source_revision[0] < 22 or source_revision[1]:
            raise BillingStorageError("Apply the canonical schema v22 migration before physical consolidation.")
        state = await _state(connection, storage, lock=True)
        if state["phase"] == "consolidated":
            await verify_consolidated(connection, storage, state)
            return
        if not state["legacy_done"] or not state["native_done"]:
            raise BillingStorageError("Finish both source backfills before physical consolidation.")
        for source in SOURCES:
            await connection.execute(text(f"LOCK TABLE {storage.source_table(source)} IN ACCESS EXCLUSIVE MODE"))
        await connection.execute(text(f"LOCK TABLE {storage.ledger} IN ACCESS EXCLUSIVE MODE"))
        await verify_mirrors(connection, storage)
        comparison = await _compare(connection, storage)
        if any(row["mismatches"] or row["extra_rows"] for row in comparison.values()):
            raise BillingStorageError("Billing records differ; no source table was retired.")
        specs = {source: await _source_spec(connection, storage, source) for source in SOURCES}
        for source in SOURCES:
            await _constraints(connection, storage, source, specs[source])
        for source in SOURCES:
            await _view(connection, storage, source, specs[source])
        manifest = {
            "version": 1, "ledger": await _ledger_shape(connection, storage),
            **{source: await _definition(connection, storage, source) for source in SOURCES},
        }
        await connection.execute(text(
            f"UPDATE {storage.state} SET phase='consolidated',view_manifest=CAST(:manifest AS jsonb) WHERE id=1"
        ), {"manifest": json.dumps(manifest)})
        await verify_consolidated(connection, storage)
