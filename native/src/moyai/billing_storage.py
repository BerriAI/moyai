"""Explicit, reversible consolidation of existing PostgreSQL billing storage.

The ledger stores the exact allowlisted source fields, not recalculated prices.
Mirrors run in the source write transaction, including late cost recovery.
Backfill is bounded and resumable; reports switch only after exact comparison.
Original tables remain current throughout the rollback window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .billing_storage_models import BillingRecord, BillingStorageState
from .product_billing import LEGACY_FIELDS, NATIVE_FIELDS, BillingReadError

VERSION = 1
LEDGER = BillingRecord.__tablename__
STATE = BillingStorageState.__tablename__
TRIGGER = "moyai_billing_mirror"
TRUNCATE_TRIGGER = "moyai_billing_truncate"
Source = Literal["legacy", "native"]
SOURCES: tuple[Source, Source] = ("legacy", "native")

# Native bookkeeping fields are preserved even when today's report does not
# consume them. Never copy unknown columns, prompts, credentials or responses.
STORAGE_FIELDS = {
    "legacy": LEGACY_FIELDS,
    "native": (*NATIVE_FIELDS, "finished_at", "response_model", "provider", "receipt_key"),
}
IDENTITY_FIELDS = {
    "legacy": ("id", "gateway_id", "cost_receipt_id"),
    "native": ("id", "request_key", "provider_request_id"),
}
REQUIRED_FIELDS = {
    "legacy": {"id", "model", "created_at", "status", "run_id", "user_id", "cost"},
    "native": {"id", "model", "created_at", "status", "session_id", "actor_id", "cost_usd", "cost_status"},
}


class BillingStorageError(ValueError):
    """Metadata-only operator diagnostic; never includes a connection URL."""


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) or value.startswith("pg_") or value in {
        "public", "information_schema",
    }:
        raise BillingStorageError("Select explicit non-system billing schemas.")
    return f'"{value}"'


@dataclass(frozen=True)
class Storage:
    source_schema: str
    native_schema: str

    def __post_init__(self) -> None:
        identifier(self.source_schema)
        identifier(self.native_schema)
        if self.source_schema == self.native_schema:
            raise BillingStorageError("Billing sources require distinct product and execution schemas.")

    @property
    def ledger(self) -> str:
        return f"{identifier(self.native_schema)}.{LEDGER}"

    @property
    def state(self) -> str:
        return f"{identifier(self.native_schema)}.{STATE}"

    def source_table(self, source: Source) -> str:
        return (
            f"{identifier(self.source_schema)}.model_requests" if source == "legacy"
            else f"{identifier(self.native_schema)}.moyai_inference_usage"
        )

    def function(self, source: Source) -> str:
        return f'{identifier(self.native_schema)}."moyai_billing_mirror_{source}"'


async def fields(connection: AsyncConnection, storage: Storage, source: Source) -> tuple[str, ...]:
    schema = storage.source_schema if source == "legacy" else storage.native_schema
    table = "model_requests" if source == "legacy" else "moyai_inference_usage"

    def read(conn: Any) -> set[str]:
        inspector = inspect(conn)
        if not inspector.has_table(table, schema=schema):
            raise BillingStorageError(f"Missing {source} billing source.")
        return {column["name"] for column in inspector.get_columns(table, schema=schema)}

    available = await connection.run_sync(read)
    if not REQUIRED_FIELDS[source] <= available:
        raise BillingStorageError(f"Incompatible {source} billing source.")
    return tuple(name for name in STORAGE_FIELDS[source] if name in available)


def record_sql(selected: tuple[str, ...], alias: str) -> str:
    return "jsonb_build_object(" + ",".join(f"'{name}',{alias}.\"{name}\"" for name in selected) + ")"


def identities_sql(selected: tuple[str, ...], source: Source, alias: str) -> str:
    values = ",".join(
        f'NULLIF({alias}."{name}"::text,\'\')' for name in IDENTITY_FIELDS[source] if name in selected
    )
    return f"array_remove(ARRAY[{values}]::text[],NULL)"


def mirror_body(storage: Storage, source: Source, selected: tuple[str, ...]) -> str:
    return f"""BEGIN
IF TG_OP = 'TRUNCATE' THEN
    DELETE FROM {storage.ledger} WHERE source='{source}';
    RETURN NULL;
END IF;
IF TG_OP = 'DELETE' OR (TG_OP = 'UPDATE' AND OLD.id IS DISTINCT FROM NEW.id) THEN
    DELETE FROM {storage.ledger} WHERE source='{source}' AND source_id=OLD.id::text;
END IF;
IF TG_OP <> 'DELETE' THEN
    INSERT INTO {storage.ledger} (source,source_id,created_at,identities,record)
    VALUES ('{source}',NEW.id::text,NEW.created_at::text,
        {identities_sql(selected, source, "NEW")},{record_sql(selected, "NEW")})
    ON CONFLICT (source,source_id) DO UPDATE SET
        created_at=excluded.created_at,identities=excluded.identities,record=excluded.record;
END IF;
RETURN NULL;
END"""


async def _timeouts(connection: AsyncConnection) -> None:
    await connection.execute(text("SET LOCAL lock_timeout = '5s'"))
    await connection.execute(text("SET LOCAL statement_timeout = '60s'"))


async def _state(
    connection: AsyncConnection, storage: Storage, *, lock: bool = False,
) -> Mapping[str, Any]:
    row = (await connection.execute(text(
        f"SELECT * FROM {storage.state} WHERE id=1" + (" FOR UPDATE" if lock else ""),
    ))).mappings().one_or_none()
    if row is None:
        raise BillingStorageError("Billing storage has not been prepared.")
    if (
        row["version"] != VERSION or row["source_schema"] != storage.source_schema
        or row["native_schema"] != storage.native_schema or row["phase"] not in {"shadow", "active"}
        or (row["phase"] == "active" and (not row["legacy_done"] or not row["native_done"]))
    ):
        raise BillingStorageError("Billing storage configuration differs from its saved receipt.")
    return dict(row)


async def verify_mirrors(connection: AsyncConnection, storage: Storage) -> None:
    for source in SOURCES:
        selected = await fields(connection, storage, source)
        rows = (await connection.execute(text("""
            SELECT t.tgname,t.tgtype,t.tgenabled::text AS enabled,p.prosrc,
                   n.nspname AS function_schema,p.proname,p.prosecdef,p.proconfig
            FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE t.tgrelid=CAST(:table AS regclass) AND NOT t.tgisinternal
              AND t.tgname IN (:row_trigger,:truncate_trigger)
        """), {
            "table": storage.source_table(source), "row_trigger": TRIGGER, "truncate_trigger": TRUNCATE_TRIGGER,
        })).mappings().all()
        saved = {row["tgname"]: row for row in rows}
        for name, events in ((TRIGGER, 29), (TRUNCATE_TRIGGER, 32)):
            row = saved.get(name)
            if row is None or (
                row["tgtype"] != events or row["enabled"] != "O" or row["prosecdef"]
                or row["proconfig"] != ["search_path=pg_catalog"]
                or row["function_schema"] != storage.native_schema
                or row["proname"] != f"moyai_billing_mirror_{source}"
                or row["prosrc"].strip() != mirror_body(storage, source, selected).strip()
            ):
                raise BillingStorageError(f"The {source} billing mirror is missing or incompatible.")


async def prepare(engine: AsyncEngine, storage: Storage) -> None:
    """Install capture first. DDL and the shadow receipt commit atomically."""
    if engine.dialect.name != "postgresql":
        raise BillingStorageError("Billing storage consolidation requires PostgreSQL.")
    async with engine.begin() as connection:
        await _timeouts(connection)
        await connection.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext(:name))"
        ), {"name": f"moyai-billing-storage:{storage.native_schema}"})
        # Explicit native provisioning owns the ledger DDL and version receipt.
        for table in (LEDGER, STATE):
            exists = await connection.run_sync(
                lambda conn, name=table: inspect(conn).has_table(name, schema=storage.native_schema)
            )
            if not exists:
                raise BillingStorageError("Provision the reviewed native billing schema before preparing storage.")
        current = await connection.scalar(text(f"SELECT id FROM {storage.state} WHERE id=1"))
        if current is not None:
            await _state(connection, storage)
            await verify_mirrors(connection, storage)
            return
        for source in SOURCES:
            await connection.execute(text(f"LOCK TABLE {storage.source_table(source)} IN SHARE ROW EXCLUSIVE MODE"))
            selected = await fields(connection, storage, source)
            await connection.execute(text(
                f"CREATE FUNCTION {storage.function(source)}() RETURNS trigger LANGUAGE plpgsql "
                f"SET search_path=pg_catalog AS $mirror${mirror_body(storage, source, selected)}$mirror$"
            ))
            await connection.execute(text(
                f"CREATE TRIGGER {TRIGGER} AFTER INSERT OR UPDATE OR DELETE ON {storage.source_table(source)} "
                f"FOR EACH ROW EXECUTE FUNCTION {storage.function(source)}()"
            ))
            await connection.execute(text(
                f"CREATE TRIGGER {TRUNCATE_TRIGGER} AFTER TRUNCATE ON {storage.source_table(source)} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {storage.function(source)}()"
            ))
        await connection.execute(text(
            f"INSERT INTO {storage.state} "
            "(id,version,source_schema,native_schema,phase,legacy_done,native_done) "
            "VALUES (1,:version,:source,:native,'shadow',false,false)"
        ), {"version": VERSION, "source": storage.source_schema, "native": storage.native_schema})
        await verify_mirrors(connection, storage)


async def backfill_batch(
    engine: AsyncEngine, storage: Storage, source: Source, *, batch_size: int = 1000,
) -> bool:
    """Return true when this source is complete. No stale overwrite on retries."""
    if not 1 <= batch_size <= 10_000:
        raise BillingStorageError("Choose a batch size between 1 and 10,000.")
    async with engine.begin() as connection:
        await _timeouts(connection)
        state = await _state(connection, storage, lock=True)
        await verify_mirrors(connection, storage)
        if state[f"{source}_done"]:
            return True
        selected = await fields(connection, storage, source)
        cursor = state[f"{source}_cursor"]
        # Lock each source row until its copy commits: concurrent deletion
        # cannot remove a row before the backfill inserts a stale copy of it.
        result = (await connection.execute(text(f"""
            WITH batch AS MATERIALIZED (
                SELECT {",".join(f's."{name}"' for name in selected)} FROM {storage.source_table(source)} s
                WHERE (CAST(:cursor AS text) IS NULL OR s.id > CAST(:cursor AS text))
                ORDER BY s.id LIMIT :size FOR SHARE
            ), copied AS (
                INSERT INTO {storage.ledger} (source,source_id,created_at,identities,record)
                SELECT '{source}',b.id::text,b.created_at::text,
                    {identities_sql(selected, source, "b")},{record_sql(selected, "b")}
                FROM batch b ON CONFLICT (source,source_id) DO NOTHING
                RETURNING source_id
            )
            SELECT max(id) AS cursor,count(*) AS scanned,(SELECT count(*) FROM copied) AS inserted FROM batch
        """), {"cursor": cursor, "size": batch_size})).mappings().one()
        complete = result["scanned"] < batch_size
        await connection.execute(text(
            f"UPDATE {storage.state} SET {source}_cursor=COALESCE(:cursor,{source}_cursor),"
            f"{source}_done=:done WHERE id=1"
        ), {"cursor": result["cursor"], "done": complete})
        return complete


async def _compare(connection: AsyncConnection, storage: Storage) -> dict[str, dict[str, int]]:
    result = {}
    for source in SOURCES:
        selected = await fields(connection, storage, source)
        row = (await connection.execute(text(f"""
            SELECT count(*) AS source_rows,
                count(*) FILTER (WHERE b.source_id IS NULL OR b.record IS DISTINCT FROM {record_sql(selected, "s")}
                    OR b.created_at IS DISTINCT FROM s.created_at::text
                    OR b.identities IS DISTINCT FROM {identities_sql(selected, source, "s")}) AS mismatches
            FROM {storage.source_table(source)} s LEFT JOIN {storage.ledger} b
                ON b.source='{source}' AND b.source_id=s.id::text
        """))).mappings().one()
        extra = await connection.scalar(text(f"""
            SELECT count(*) FROM {storage.ledger} b WHERE b.source='{source}'
            AND NOT EXISTS (SELECT 1 FROM {storage.source_table(source)} s WHERE s.id::text=b.source_id)
        """))
        result[source] = {
            "source_rows": row["source_rows"], "mismatches": row["mismatches"], "extra_rows": int(extra or 0),
        }
    return result


async def activate(engine: AsyncEngine, storage: Storage) -> dict[str, dict[str, int]]:
    async with engine.begin() as connection:
        await _timeouts(connection)
        state = await _state(connection, storage, lock=True)
        if not state["legacy_done"] or not state["native_done"]:
            raise BillingStorageError("Finish both source backfills before switching billing reads.")
        # Briefly freeze source changes so comparison and the read switch are
        # one atomic operation; mirrored writers resume after the commit.
        for source in SOURCES:
            await connection.execute(text(f"LOCK TABLE {storage.source_table(source)} IN SHARE MODE"))
        await verify_mirrors(connection, storage)
        comparison = await _compare(connection, storage)
        if any(row["mismatches"] or row["extra_rows"] for row in comparison.values()):
            raise BillingStorageError("Billing records differ; reads were not switched.")
        await connection.execute(text(f"UPDATE {storage.state} SET phase='active' WHERE id=1"))
        return comparison


async def rollback(engine: AsyncEngine, storage: Storage) -> None:
    async with engine.begin() as connection:
        await _timeouts(connection)
        await _state(connection, storage, lock=True)
        await connection.execute(text(f"UPDATE {storage.state} SET phase='shadow' WHERE id=1"))


async def active_storage(
    connection: AsyncConnection, *, source_schema: str | None, native_schema: str | None,
) -> Storage | None:
    if connection.dialect.name != "postgresql" or not source_schema or not native_schema:
        return None
    storage = Storage(source_schema, native_schema)
    exists = await connection.run_sync(lambda conn: inspect(conn).has_table(STATE, schema=native_schema))
    if not exists:
        return None
    present = await connection.scalar(text(f"SELECT id FROM {storage.state} WHERE id=1"))
    if present is None:
        return None
    try:
        state = await _state(connection, storage)
        if state["phase"] != "active":
            return None
        await verify_mirrors(connection, storage)
    except BillingStorageError:
        raise BillingReadError("consolidated_accounting_storage_unavailable") from None
    return storage


async def status(engine: AsyncEngine, storage: Storage) -> dict[str, Any]:
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        await _timeouts(connection)
        state = dict(await _state(connection, storage))
        await verify_mirrors(connection, storage)
        counts = (await connection.execute(text(
            f"SELECT source,count(*) AS records FROM {storage.ledger} GROUP BY source ORDER BY source"
        ))).mappings().all()
        return {"version": VERSION, "phase": state["phase"],
                "backfill": {source: bool(state[f"{source}_done"]) for source in SOURCES},
                "records": {row["source"]: row["records"] for row in counts}}


async def run_command(args: argparse.Namespace) -> dict[str, Any]:
    from .postgres import create_engine

    storage = Storage(args.source_schema, args.native_schema)
    url = os.environ.get("MOYAI_DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        raise BillingStorageError("Set the existing MOYAI_DATABASE_URL PostgreSQL connection.")
    engine = create_engine(url)
    try:
        if args.action == "prepare":
            await prepare(engine, storage)
        elif args.action == "backfill":
            for source in SOURCES:
                for _ in range(args.max_batches):
                    if await backfill_batch(engine, storage, source, batch_size=args.batch_size):
                        break
        elif args.action == "activate":
            comparison = await activate(engine, storage)
            return {**await status(engine, storage), "comparison": comparison}
        elif args.action == "rollback":
            await rollback(engine, storage)
        return await status(engine, storage)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "backfill", "activate", "rollback", "status"))
    parser.add_argument("--source-schema", default=os.environ.get("MOYAI_DATABASE_SCHEMA", ""))
    parser.add_argument("--native-schema", default=os.environ.get("MOYAI_NATIVE_SCHEMA", ""))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000, choices=range(1, 10_001), metavar="1..10000")
    parser.add_argument("--max-batches", type=int, default=100, choices=range(1, 10_001), metavar="1..10000")
    args = parser.parse_args()
    if args.action != "status" and not args.apply:
        parser.error("Mutations require --apply; status is read-only.")
    try:
        print(json.dumps(asyncio.run(run_command(args)), sort_keys=True))
    except BillingStorageError as error:
        parser.exit(1, f"{error}\n")
    except (SQLAlchemyError, OSError):
        parser.exit(1, "Billing storage operation failed; check database access and the reviewed schema.\n")


if __name__ == "__main__":
    main()
