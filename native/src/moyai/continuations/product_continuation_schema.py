"""Explicit source admission fence for continued sessions.

Run only from the reviewed migration/provision command, after the native
continuation tables exist. Startup calls verify_source_continuation_fence.
Neither function constructs Store, starts a runtime, or changes session data.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

VERSION = "2026-10-15-product-continuation-source-v2"
FUNCTION = "moyai_native_continuation_guard"
TRIGGER = "moyai_native_continuation_fence"
IMMUTABLE_FUNCTION = "moyai_continuation_immutable"
SOURCE_TABLES = (
    "runs", "messages", "durable_sessions", "runtime_leases", "computer_operations",
    "native_sessions", "published_artifacts", "artifact_parts", "artifact_uploads",
    "artifact_objects", "attachments", "attachment_parts", "events", "session_goals", "swarm_missions",
    "run_roots", "run_ancestry",
)
REQUIRED_SOURCE_TABLES = {"runs", "messages", "durable_sessions", "runtime_leases"}
IMMUTABLE_TABLES = (
    "moyai_product_continuations", "moyai_product_execution_contexts",
    "moyai_product_workspace_restorations",
    "moyai_product_continuation_captures",
)
IMMUTABLE_BODY = """BEGIN
            RAISE EXCEPTION 'Product continuation provenance is immutable.' USING ERRCODE='55000';
        END"""


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) or value in {"pg_catalog", "information_schema"}:
        raise ValueError("Select an explicit product schema.")
    return '"' + value + '"'


def session_lease_key(schema: str, run_id: str) -> int:
    """Exactly app.database.PostgresDatabase.acquire_lease's advisory key."""
    return int.from_bytes(hashlib.sha256((schema + ":lease:session:" + run_id).encode()).digest()[:8],
                          "big", signed=True)


def guard_body(source_schema: str, native_schema: str) -> str:
    source, native = identifier(source_schema), identifier(native_schema)
    return f"""DECLARE
        records jsonb[];
        row_data jsonb;
        run_identity text;
        run_identities text[] := ARRAY[]::text[];
        moved boolean;
    BEGIN
        IF TG_OP = 'DELETE' THEN records := ARRAY[to_jsonb(OLD)];
        ELSIF TG_OP = 'UPDATE' THEN records := ARRAY[to_jsonb(OLD),to_jsonb(NEW)];
        ELSE records := ARRAY[to_jsonb(NEW)]; END IF;
        FOREACH row_data IN ARRAY records LOOP
            run_identity := NULL;
            IF TG_TABLE_NAME = 'runtime_leases' THEN
                IF COALESCE(row_data->>'name','') ~ '^session:[0-9a-f]{{32}}$' THEN
                    run_identity := substr(row_data->>'name',9);
                END IF;
            ELSIF TG_TABLE_NAME = 'runs' THEN
                run_identity := row_data->>'id';
                run_identities := array_append(run_identities,NULLIF(row_data->>'parent_run_id',''));
            ELSIF TG_TABLE_NAME IN ('run_roots','run_ancestry') THEN
                run_identity := row_data->>'run_id';
                run_identities := array_append(run_identities,row_data->>'root_id');
                run_identities := array_append(run_identities,row_data->>'ancestor_id');
            ELSIF TG_TABLE_NAME = 'artifact_objects' THEN
                IF COALESCE(row_data->>'name','') ~ '^[0-9a-f]{{32}}(\\.zip$|-captures/)' THEN
                    run_identity := substr(row_data->>'name',1,32);
                END IF;
            ELSIF TG_TABLE_NAME = 'attachments' THEN
                SELECT m.run_id INTO run_identity FROM {source}.messages m
                    WHERE m.id = NULLIF(row_data->>'message_id','')::bigint;
            ELSIF TG_TABLE_NAME = 'attachment_parts' THEN
                SELECT m.run_id INTO run_identity FROM {source}.messages m
                    JOIN {source}.attachments a ON a.message_id=m.id
                    WHERE a.id = row_data->>'attachment_id';
            ELSE
                run_identity := row_data->>'run_id';
            END IF;
            run_identities := array_append(run_identities,run_identity);
        END LOOP;
        SELECT EXISTS(
            SELECT 1 FROM {native}.moyai_product_continuations moved
            WHERE moved.source_schema='{source_schema}'
              AND (moved.source_session_id=ANY(run_identities)
                OR moved.source_session_id IN (
                    SELECT root_id FROM {source}.run_roots
                    WHERE run_id=ANY(run_identities)
                ))
        ) INTO moved;
        IF moved THEN
            RAISE EXCEPTION 'This session now belongs to native execution; use its preserved session link.'
                USING ERRCODE='55000';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END"""


async def install_source_continuation_fence(
    connection: AsyncConnection, *, source_schema: str, native_schema: str,
) -> None:
    if connection.dialect.name != "postgresql" or not connection.in_transaction():
        raise ValueError("Install continuation fencing in an explicit PostgreSQL migration transaction.")
    source, native = identifier(source_schema), identifier(native_schema)
    if source_schema == native_schema:
        raise ValueError("Source and native schemas must be distinct.")
    await connection.execute(text(
        "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
    ), {"schema": source_schema})
    tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names(schema=source_schema)))
    if not REQUIRED_SOURCE_TABLES <= tables:
        raise ValueError("The existing product's durable session schema must be installed before continuation.")
    native_tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names(schema=native_schema)))
    if not set(IMMUTABLE_TABLES) <= native_tables:
        raise ValueError("Install the native continuation migration before its source fence.")
    body = guard_body(source_schema, native_schema)
    await connection.execute(text(f"""CREATE OR REPLACE FUNCTION {source}.{FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $moyai$ {body} $moyai$"""))
    for table in SOURCE_TABLES:
        if table not in tables:
            continue
        await connection.execute(text(f'DROP TRIGGER IF EXISTS {TRIGGER} ON {source}."{table}"'))
        await connection.execute(text(f"""CREATE TRIGGER {TRIGGER}
            BEFORE INSERT OR UPDATE OR DELETE ON {source}."{table}"
            FOR EACH ROW EXECUTE FUNCTION {source}.{FUNCTION}()"""))
    await connection.execute(text(f"""CREATE OR REPLACE FUNCTION {native}.{IMMUTABLE_FUNCTION}()
        RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $moyai$
        {IMMUTABLE_BODY} $moyai$"""))
    for table in IMMUTABLE_TABLES:
        await connection.execute(text(f'DROP TRIGGER IF EXISTS {TRIGGER} ON {native}."{table}"'))
        await connection.execute(text(f"""CREATE TRIGGER {TRIGGER} BEFORE UPDATE OR DELETE ON {native}."{table}"
            FOR EACH ROW EXECUTE FUNCTION {native}.{IMMUTABLE_FUNCTION}()"""))


async def verify_source_continuation_fence(
    engine: AsyncEngine, *, source_schema: str, native_schema: str,
) -> None:
    identifier(source_schema)
    identifier(native_schema)
    if engine.dialect.name != "postgresql" or source_schema == native_schema:
        raise ValueError("Product continuation requires distinct source/native schemas in PostgreSQL.")
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        tables = set(await connection.run_sync(lambda conn: inspect(conn).get_table_names(schema=source_schema)))
        if not REQUIRED_SOURCE_TABLES <= tables:
            raise ValueError("The durable source session schema is unavailable.")
        body = await connection.scalar(text(
            "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname=:schema AND p.proname=:name"
        ), {"schema": source_schema, "name": FUNCTION})
        if not isinstance(body, str) or body.strip() != guard_body(source_schema, native_schema).strip():
            raise ValueError("The source continuation fence is missing or differs from the reviewed release.")
        immutable = await connection.scalar(text(
            "SELECT p.prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname=:schema AND p.proname=:name"
        ), {"schema": native_schema, "name": IMMUTABLE_FUNCTION})
        if not isinstance(immutable, str) or immutable.strip() != IMMUTABLE_BODY.strip():
            raise ValueError("Continuation provenance protection differs from the reviewed release.")
        for schema, names in (
            (source_schema, [table for table in SOURCE_TABLES if table in tables]),
            (native_schema, IMMUTABLE_TABLES),
        ):
            for table in names:
                present = await connection.scalar(text(
                    "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema "
                    "AND c.relname=:table AND t.tgname=:trigger AND NOT t.tgisinternal AND t.tgenabled='O' "
                    "AND t.tgfoid=to_regprocedure(:function) AND t.tgtype=:kind"
                ), {"schema": schema, "table": table, "trigger": TRIGGER,
                    "function": f'"{schema}".{FUNCTION if schema == source_schema else IMMUTABLE_FUNCTION}()',
                    "kind": 31 if schema == source_schema else 27})
                if present is None:
                    raise ValueError("A required continuation ownership trigger is missing.")
