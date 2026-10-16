"""Reviewed native application migration; never invoked implicitly by startup.

Explicit release command integration:
    await migrate_product_schema(engine, schema=config.native_schema,
                                 source_schema=config.product_schema)
Normal API/worker startup:
    await verify_product_schema(engine, schema=settings.database_schema)

v1 creates missing native application tables, explicitly widens canonical user
IDs/FKs/scope columns to VARCHAR(512), and installs immutable turn-authority
bindings. create_all is used ONLY for missing tables in this explicit migration;
ALTER TABLE performs upgrades of existing columns. A version+manifest receipt
prevents an unchanged version name from silently applying a later model change.
v2 adds automation authority/occurrence/operation bindings. The v1 inventory and
receipt remain unchanged; neither migration enables or copies any automation.

This covers Moyai's application tables, not the SDK's independent lazy DDL.
Legacy source schemas are never modified.
The caller must use the existing product maintenance/drain boundary for a
production cutover. These transaction locks do not stop in-flight external work.
"""

from __future__ import annotations

import hashlib
import json
import re
from importlib import import_module

from sqlalchemy import MetaData, String, Text, UniqueConstraint, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from .models import Base, now

AUTHORITY_SCHEMA_VERSION = "2026-10-15-product-authority-v1"
AUTOMATION_SCHEMA_VERSION = "2026-10-15-product-automations-v2"
INFERENCE_SCHEMA_VERSION = "2026-10-15-product-inference-v3"
AUTOMATION_CONTEXT_SCHEMA_VERSION = "2026-10-15-product-automation-context-v4"
CONTINUATION_SCHEMA_VERSION = "2026-10-15-product-continuation-v5"
SLACK_SCHEMA_VERSION = "2026-10-15-product-slack-v6"
MEMORY_SCHEMA_VERSION = "2026-10-15-product-memory-v7"
AUTOMATION_ACCESS_SCHEMA_VERSION = "2026-10-15-product-automation-access-v8"
CONTINUATION_CAPTURE_SCHEMA_VERSION = "2026-10-15-product-continuation-capture-v9"
CREDENTIAL_REQUEST_SCHEMA_VERSION = "2026-10-15-product-credential-requests-v10"
MEMORY_REVIEW_SCHEMA_VERSION = "2026-10-15-product-memory-review-v11"
DRAIN_SCHEMA_VERSION = "2026-10-15-product-drain-v12"
GIT_OPERATION_SCHEMA_VERSION = "2026-10-15-product-git-operations-v13"
EXECUTION_COMPATIBILITY_SCHEMA_VERSION = "2026-10-16-product-execution-v14"
PRODUCT_SCHEMA_VERSION = EXECUTION_COMPATIBILITY_SCHEMA_VERSION
RECEIPTS = "moyai_app_schema_versions"
GRANT_TABLE = "moyai_product_turn_authority"
GRANT_TRIGGER = "moyai_product_grant_immutable"
AUTOMATION_CONTEXT_TABLE = "moyai_product_automation_execution_contexts"
AUTOMATION_CONTEXT_TRIGGER = "moyai_product_automation_context_immutable"
CONTINUATION_CAPTURE_TABLE = "moyai_product_continuation_captures"
CONTINUATION_CAPTURE_TRIGGER = "moyai_product_continuation_capture_immutable"
GRANT_FUNCTION_BODY = (
    "BEGIN IF NEW IS DISTINCT FROM OLD THEN "
    "RAISE EXCEPTION 'Admitted product authority is immutable' USING ERRCODE='23514'; "
    "END IF; RETURN NEW; END"
)
CAPTURE_FUNCTION_BODY = (
    "BEGIN RAISE EXCEPTION 'Captured product workspace evidence is immutable' USING ERRCODE='55000'; END"
)
IDENTITY_COLUMNS = {"actor_id", "owner_id", "author_id", "user_id", "last_actor_id", "scope_key"}
AUTHORITY_TABLES = frozenset({
    "moyai_inference_usage", "moyai_login_attempts", "moyai_pr_publications",
    "moyai_slack_inbox", "moyai_users", "moyai_connections", "moyai_session_folders",
    "moyai_sessions", "moyai_setting_secrets", "moyai_setting_skills",
    "moyai_computer_desktops", "moyai_computer_leases", "moyai_computer_operations",
    "moyai_session_links", "moyai_session_preferences", "moyai_session_reads",
    "moyai_setting_automations", "moyai_slack_threads", "moyai_subagent_labels",
    "moyai_turns", "moyai_uploads", "moyai_artifacts", "moyai_deliveries",
    "moyai_effects", "moyai_events", "moyai_messages", "moyai_product_turn_authority",
    "moyai_setting_automation_runs", "moyai_turn_images",
})
AUTOMATION_TABLES = frozenset({
    "moyai_product_automation_bindings", "moyai_product_automation_occurrences",
    "moyai_product_automation_operations",
})
INFERENCE_TABLES = frozenset({"moyai_product_inference_requests"})
AUTOMATION_CONTEXT_TABLES = frozenset({
    AUTOMATION_CONTEXT_TABLE, "moyai_product_automation_event_cursors",
})
CONTINUATION_TABLES = frozenset({
    "moyai_product_continuations", "moyai_product_workspace_restorations", "moyai_product_execution_contexts",
})
SLACK_TABLES = frozenset({"moyai_product_slack_bindings"})
MEMORY_TABLES = frozenset({"moyai_product_memory_operations", "moyai_product_memory_barriers"})
AUTOMATION_ACCESS_TABLES = frozenset({"moyai_product_automation_chat_grants"})
CONTINUATION_CAPTURE_TABLES = frozenset({CONTINUATION_CAPTURE_TABLE})
CREDENTIAL_REQUEST_TABLES = frozenset({"moyai_product_credential_requests"})
MEMORY_REVIEW_TABLES = frozenset({"moyai_product_memory_reviews"})
DRAIN_STATE_TABLE = "moyai_product_drain_state"
DRAIN_TABLES = frozenset({DRAIN_STATE_TABLE, "moyai_product_drain_workers", "moyai_product_drain_receipts"})
GIT_OPERATION_TABLES = frozenset({"moyai_product_git_operations"})
MIGRATIONS = (
    (AUTHORITY_SCHEMA_VERSION, AUTHORITY_TABLES),
    (AUTOMATION_SCHEMA_VERSION, AUTOMATION_TABLES),
    (INFERENCE_SCHEMA_VERSION, INFERENCE_TABLES),
    (AUTOMATION_CONTEXT_SCHEMA_VERSION, AUTOMATION_CONTEXT_TABLES),
    (CONTINUATION_SCHEMA_VERSION, CONTINUATION_TABLES),
    (SLACK_SCHEMA_VERSION, SLACK_TABLES),
    (MEMORY_SCHEMA_VERSION, MEMORY_TABLES),
    (AUTOMATION_ACCESS_SCHEMA_VERSION, AUTOMATION_ACCESS_TABLES),
    (CONTINUATION_CAPTURE_SCHEMA_VERSION, CONTINUATION_CAPTURE_TABLES),
    (CREDENTIAL_REQUEST_SCHEMA_VERSION, CREDENTIAL_REQUEST_TABLES),
    (MEMORY_REVIEW_SCHEMA_VERSION, MEMORY_REVIEW_TABLES),
    (DRAIN_SCHEMA_VERSION, DRAIN_TABLES),
    (GIT_OPERATION_SCHEMA_VERSION, GIT_OPERATION_TABLES),
    # A durable release barrier: v13 candidates cannot resume the new workflow commands.
    (EXECUTION_COMPATIBILITY_SCHEMA_VERSION, frozenset()),
)


class ProductSchemaError(RuntimeError):
    pass


def _schema(value: str | None) -> str:
    if not value or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        raise ProductSchemaError("Select an explicit native execution schema.")
    if value in {"pg_catalog", "information_schema"}:
        raise ProductSchemaError("System schemas cannot contain the native application.")
    return value


def _is_identity(table: str, column: str) -> bool:
    return (table == "moyai_users" and column == "id") or column in IDENTITY_COLUMNS


def application_metadata(schema: str, *, version: str = PRODUCT_SCHEMA_VERSION) -> MetaData:
    """A schema-qualified, versioned application inventory; no runtime startup."""
    schema = _schema(schema)
    versions = [item[0] for item in MIGRATIONS]
    if version not in versions:
        raise ProductSchemaError("Unknown native application migration version.")
    for module in (
        "settings_models", "inference_usage", "pr_publications", "product_automation_models", "product_inference",
        "product_automation_context_models",
        "product_continuation_models",
        "product_slack_models",
        "product_memory_models",
        "product_automation_access_models",
        "product_credential_models",
        "product_memory_review_models",
        "product_drain_models",
        "product_github_models",
    ):
        import_module(f"{__package__}.{module}")
    included = frozenset().union(*(item[1] for item in MIGRATIONS[:versions.index(version) + 1]))
    reviewed = frozenset().union(*(item[1] for item in MIGRATIONS))
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        if not table.name.startswith("moyai_"):
            raise ProductSchemaError("Unexpected table in the native application inventory.")
        if table.name not in reviewed:
            raise ProductSchemaError(f"Native table has no reviewed migration version: {table.name}.")
        if table.name not in included:
            continue
        copy = table.to_metadata(metadata, schema=schema)
        for column in copy.columns:
            if _is_identity(table.name, column.name):
                column.type = String(512)
    return metadata


def _manifest(metadata: MetaData) -> str:
    dialect = postgresql.dialect()
    document = [{
        "table": table.name,
        "columns": [{
            "name": column.name, "type": str(column.type.compile(dialect=dialect)),
            "nullable": column.nullable, "primary_key": column.primary_key,
            "foreign_keys": sorted(key.target_fullname for key in column.foreign_keys),
        } for column in table.columns],
        "unique": sorted(
            sorted(column.name for column in constraint.columns)
            for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
        ),
        # New v12 policy; do not change the earlier receipt documents.
        **({"checks": [["moyai_product_drain_singleton", "id = 1"]]} if table.name == DRAIN_STATE_TABLE else {}),
    } for table in metadata.sorted_tables]
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


async def _has_table(connection: AsyncConnection, schema: str, table: str) -> bool:
    return await connection.run_sync(lambda conn: inspect(conn).has_table(table, schema=schema))


async def _verify(
    connection: AsyncConnection, schema: str, metadata: MetaData, *, version: str, structure: bool = True,
) -> None:
    if not await _has_table(connection, schema, RECEIPTS):
        raise ProductSchemaError("Native application migration receipt is missing; run explicit provision first.")
    receipt = (await connection.execute(text(
        f'SELECT manifest FROM "{schema}"."{RECEIPTS}" WHERE version=:version'
    ), {"version": version})).scalar_one_or_none()
    if receipt != _manifest(metadata):
        raise ProductSchemaError("Native application migration version/manifest differs; review a versioned migration.")
    if not structure:
        return
    dialect = postgresql.dialect()
    for table in metadata.sorted_tables:
        if not await _has_table(connection, schema, table.name):
            raise ProductSchemaError(f"Required native table is missing: {table.name}.")
        columns = await connection.run_sync(
            lambda conn, name=table.name: inspect(conn).get_columns(name, schema=schema)
        )
        actual = {column["name"]: column for column in columns}
        for column in table.columns:
            saved = actual.get(column.name)
            if saved is None or bool(saved["nullable"]) != column.nullable:
                raise ProductSchemaError(f"Native column is incompatible: {table.name}.{column.name}.")
            if _is_identity(table.name, column.name):
                kind = saved["type"]
                length = kind.length if isinstance(kind, String) else None
                if not isinstance(kind, (String, Text)) or (
                    length is not None and length < 512
                ):
                    raise ProductSchemaError(f"Canonical identity column is too narrow: {table.name}.{column.name}.")
            elif str(saved["type"].compile(dialect=dialect)) != str(column.type.compile(dialect=dialect)):
                raise ProductSchemaError(f"Native column type differs: {table.name}.{column.name}.")
        primary = await connection.run_sync(
            lambda conn, name=table.name: inspect(conn).get_pk_constraint(name, schema=schema)
        )
        if set(primary["constrained_columns"]) != {column.name for column in table.primary_key.columns}:
            raise ProductSchemaError(f"Native primary key differs: {table.name}.")
        unique = await connection.run_sync(
            lambda conn, name=table.name: inspect(conn).get_unique_constraints(name, schema=schema)
        )
        unique_columns = {tuple(sorted(item["column_names"])) for item in unique}
        for constraint in table.constraints:
            if (
                isinstance(constraint, UniqueConstraint)
                and tuple(sorted(column.name for column in constraint.columns)) not in unique_columns
            ):
                raise ProductSchemaError(f"Native uniqueness constraint differs: {table.name}.")
        if table.name == DRAIN_STATE_TABLE:
            checks = await connection.run_sync(
                lambda conn: inspect(conn).get_check_constraints(DRAIN_STATE_TABLE, schema=schema)
            )
            singleton = next((item for item in checks if item["name"] == "moyai_product_drain_singleton"), None)
            if singleton is None or re.sub(r"[\s()]", "", singleton["sqltext"]) != "id=1":
                raise ProductSchemaError("The native maintenance singleton constraint differs.")
        indexes = await connection.run_sync(
            lambda conn, name=table.name: inspect(conn).get_indexes(name, schema=schema)
        )
        by_name = {index["name"]: index for index in indexes}
        for index in table.indexes:
            # SQLAlchemy shortens generated names to PostgreSQL's identifier
            # limit. Verify the identifier it actually emits in CREATE INDEX.
            name = dialect.identifier_preparer.format_index(index).strip('"')
            saved_index = by_name.get(name)
            if (
                saved_index is None or bool(saved_index["unique"]) != index.unique
                or saved_index["column_names"] != [column.name for column in index.columns]
            ):
                raise ProductSchemaError(f"Required native index differs: {table.name}.{name}.")
        foreign = await connection.run_sync(
            lambda conn, name=table.name: inspect(conn).get_foreign_keys(name, schema=schema)
        )
        actual_keys = {
            (tuple(key["constrained_columns"]), key["referred_schema"] or schema,
             key["referred_table"], tuple(key["referred_columns"]))
            for key in foreign
        }
        for constraint in table.foreign_key_constraints:
            target = constraint.referred_table
            expected = (
                tuple(column.name for column in constraint.columns), target.schema or schema,
                target.name, tuple(element.column.name for element in constraint.elements),
            )
            if expected not in actual_keys:
                raise ProductSchemaError(f"Native foreign key differs: {table.name}.")
    required = [(GRANT_TABLE, GRANT_TRIGGER, 19, GRANT_TRIGGER, GRANT_FUNCTION_BODY)]
    if f"{schema}.{AUTOMATION_CONTEXT_TABLE}" in metadata.tables:
        required.append((AUTOMATION_CONTEXT_TABLE, AUTOMATION_CONTEXT_TRIGGER, 19, GRANT_TRIGGER, GRANT_FUNCTION_BODY))
    if f"{schema}.{CONTINUATION_CAPTURE_TABLE}" in metadata.tables:
        required.append((
            CONTINUATION_CAPTURE_TABLE, CONTINUATION_CAPTURE_TRIGGER, 27,
            CONTINUATION_CAPTURE_TRIGGER, CAPTURE_FUNCTION_BODY,
        ))
    for table_name, trigger_name, events, function_name, body in required:
        trigger = (await connection.execute(text(
            "SELECT t.tgtype,t.tgenabled::text AS tgenabled,p.proname,p.prosrc,pn.nspname AS function_schema "
            "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_proc p ON p.oid=t.tgfoid JOIN pg_namespace pn ON pn.oid=p.pronamespace "
            "WHERE n.nspname=:schema AND c.relname=:table AND t.tgname=:trigger "
            "AND NOT t.tgisinternal"
        ), {"schema": schema, "table": table_name, "trigger": trigger_name})).mappings().first()
        if (
            trigger is None or trigger["tgenabled"] not in {"O", "A"} or trigger["tgtype"] != events
            or trigger["function_schema"] != schema or trigger["proname"] != function_name
            or trigger["prosrc"].strip() != body
        ):
            raise ProductSchemaError(f"The immutable product-authority trigger is missing: {trigger_name}.")


async def verify_product_schema(
    engine: AsyncEngine, *, schema: str, version: str = PRODUCT_SCHEMA_VERSION,
) -> None:
    schema = _schema(schema)
    if engine.dialect.name != "postgresql":
        raise ProductSchemaError("Existing-product application schema requires PostgreSQL.")
    metadata = application_metadata(schema, version=version)
    async with engine.connect() as connection, connection.begin():
        await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        await connection.exec_driver_sql("SET LOCAL statement_timeout = '10s'")
        for earlier, _ in MIGRATIONS:
            if earlier == version:
                break
            await _verify(
                connection, schema, application_metadata(schema, version=earlier),
                version=earlier, structure=False,
            )
        await _verify(connection, schema, metadata, version=version)


async def migrate_product_schema(
    engine: AsyncEngine, *, schema: str, source_schema: str | None = None,
    version: str = PRODUCT_SCHEMA_VERSION,
) -> str:
    schema = _schema(schema)
    if engine.dialect.name != "postgresql" or schema == source_schema:
        raise ProductSchemaError("Provision a distinct PostgreSQL native schema; never modify the product source.")
    application_metadata(schema, version=version)  # Validate before acquiring any database locks.
    authority = application_metadata(schema, version=AUTHORITY_SCHEMA_VERSION)
    async with engine.begin() as connection:
        if source_schema is not None:
            source_schema = _schema(source_schema)
            await connection.execute(text(
                "SELECT pg_advisory_xact_lock(726941, CAST(CAST(:source AS regnamespace) AS oid)::int)"
            ), {"source": source_schema})
        await connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:migration))"), {
            "migration": f"moyai-native-migration:{schema}",
        })
        await _migrate_authority(connection, schema, authority)
        for revision, tables in MIGRATIONS[1:]:
            if version == AUTHORITY_SCHEMA_VERSION:
                break
            metadata = application_metadata(schema, version=revision)
            existing = (await connection.execute(text(
                f'SELECT version FROM "{schema}"."{RECEIPTS}" WHERE version=:version'
            ), {"version": revision})).scalar_one_or_none()
            if not existing:
                # Add only this version's tables. Never re-stamp an earlier receipt.
                if tables:
                    await connection.run_sync(lambda conn, inventory=metadata, selected=tables: inventory.create_all(
                        conn, tables=[table for table in inventory.sorted_tables if table.name in selected],
                    ))
                if revision == AUTOMATION_CONTEXT_SCHEMA_VERSION:
                    await connection.execute(text(
                        f'CREATE TRIGGER "{AUTOMATION_CONTEXT_TRIGGER}" BEFORE UPDATE '
                        f'ON "{schema}"."{AUTOMATION_CONTEXT_TABLE}" FOR EACH ROW '
                        f'EXECUTE FUNCTION "{schema}"."{GRANT_TRIGGER}"()'
                    ))
                if revision == CONTINUATION_CAPTURE_SCHEMA_VERSION:
                    await connection.execute(text(
                        f'CREATE FUNCTION "{schema}"."{CONTINUATION_CAPTURE_TRIGGER}"() RETURNS trigger '
                        "LANGUAGE plpgsql AS $$ BEGIN "
                        "RAISE EXCEPTION 'Captured product workspace evidence is immutable' USING ERRCODE='55000'; "
                        "END $$"
                    ))
                    await connection.execute(text(
                        f'CREATE TRIGGER "{CONTINUATION_CAPTURE_TRIGGER}" BEFORE UPDATE OR DELETE '
                        f'ON "{schema}"."{CONTINUATION_CAPTURE_TABLE}" FOR EACH ROW '
                        f'EXECUTE FUNCTION "{schema}"."{CONTINUATION_CAPTURE_TRIGGER}"()'
                    ))
                if revision == DRAIN_SCHEMA_VERSION:
                    from .product_drain_models import BOOTSTRAP_FINGERPRINT
                    # A fresh cutover starts drained. Never seed/repair/resume
                    # from normal startup or overwrite an existing generation.
                    await connection.execute(text(
                        f'INSERT INTO "{schema}"."{DRAIN_STATE_TABLE}" '
                        "(id,generation,paused,request_key,request_fingerprint,requested_at,"
                        "owner_pid,owner_started,source_policy) "
                        "VALUES (1,0,true,'bootstrap',:fingerprint,:stamp,NULL,NULL,NULL)"
                    ), {"fingerprint": BOOTSTRAP_FINGERPRINT, "stamp": now()})
                await connection.execute(text(
                    f'INSERT INTO "{schema}"."{RECEIPTS}" (version,manifest) VALUES (:version,:manifest)'
                ), {"version": revision, "manifest": _manifest(metadata)})
            await _verify(connection, schema, metadata, version=revision, structure=revision == version)
            if revision == version:
                break
    return version


async def _migrate_authority(connection: AsyncConnection, schema: str, metadata: MetaData) -> None:
    """The original v1 migration, with its original model inventory and receipt."""
    if await _has_table(connection, schema, RECEIPTS):
        existing = (await connection.execute(text(
            f'SELECT version FROM "{schema}"."{RECEIPTS}" WHERE version=:version'
        ), {"version": AUTHORITY_SCHEMA_VERSION})).scalar_one_or_none()
        if existing:
            await _verify(connection, schema, metadata, version=AUTHORITY_SCHEMA_VERSION)
            return
    await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    # Creates missing tables only. Width changes below are explicit ALTERs.
    await connection.run_sync(metadata.create_all)
    for table in metadata.sorted_tables:
        columns = await connection.run_sync(
            lambda conn, name=table.name: inspect(conn).get_columns(name, schema=schema)
        )
        for column in columns:
            if not _is_identity(table.name, column["name"]):
                continue
            kind = column["type"]
            if not isinstance(kind, (String, Text)):
                raise ProductSchemaError(f"Cannot automatically convert identity type in {table.name}.")
            length = kind.length
            if length is not None and length < 512:
                await connection.execute(text(
                    f'ALTER TABLE "{schema}"."{table.name}" '
                    f'ALTER COLUMN "{column["name"]}" TYPE VARCHAR(512)'
                ))
    await connection.execute(text(
        f'CREATE OR REPLACE FUNCTION "{schema}"."{GRANT_TRIGGER}"() RETURNS trigger '
        "LANGUAGE plpgsql AS $$ BEGIN "
        "IF NEW IS DISTINCT FROM OLD THEN "
        "RAISE EXCEPTION 'Admitted product authority is immutable' USING ERRCODE='23514'; "
        "END IF; RETURN NEW; END $$"
    ))
    await connection.execute(text(
        f'CREATE TRIGGER "{GRANT_TRIGGER}" BEFORE UPDATE ON "{schema}"."{GRANT_TABLE}" '
        f'FOR EACH ROW EXECUTE FUNCTION "{schema}"."{GRANT_TRIGGER}"()'
    ))
    await connection.execute(text(
        f'CREATE TABLE IF NOT EXISTS "{schema}"."{RECEIPTS}" '
        "(version VARCHAR(100) PRIMARY KEY, manifest VARCHAR(64) NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())"
    ))
    await connection.execute(text(
        f'INSERT INTO "{schema}"."{RECEIPTS}" (version,manifest) VALUES (:version,:manifest)'
    ), {"version": AUTHORITY_SCHEMA_VERSION, "manifest": _manifest(metadata)})
    await _verify(connection, schema, metadata, version=AUTHORITY_SCHEMA_VERSION)
