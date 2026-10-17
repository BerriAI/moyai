"""Execution compatibility receipts on isolated PostgreSQL; no provider calls."""

from __future__ import annotations

import os
from typing import Protocol

import pytest
import test_product_authority as authority_fixtures
from app import native_contract
from app.config import Settings as ProductSettings
from app.database import runtime_fingerprint
from app.runtime_compatibility import api_fingerprint, background_fingerprint
from scripts.release.native_probe import migration_plan
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.ext.asyncio import AsyncEngine
from test_product_deployment import environment as environment_fixture

from moyai.database import Database
from moyai.models import Session, User
from moyai.product_config import load_product_config
from moyai.product_schema import (
    EXECUTION_COMPATIBILITY_SCHEMA_VERSION,
    GIT_OPERATION_SCHEMA_VERSION,
    MIGRATIONS,
    ProductSchemaError,
    migrate_product_schema,
    verify_product_schema,
)

environment = environment_fixture
product_database = authority_fixtures.product_database


class ProductDatabase(Protocol):
    source: AsyncEngine
    database: Database
    schema: str


def fingerprints(settings: ProductSettings) -> tuple[str, ...]:
    return (
        runtime_fingerprint(settings), api_fingerprint(settings),
        *(background_fingerprint(settings, role) for role in ("worker", "broker", "coordinator")),
    )


def test_execution_protocol_changes_all_native_contracts_but_preserves_legacy(
    environment: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = load_product_config(environment)
    assert config is not None
    settings = config.legacy_settings()
    legacy = settings.model_copy(update={"moyai_core": "legacy"})
    assert native_contract.NATIVE_PROTOCOL_REVISION == 4
    current, unchanged = fingerprints(settings), fingerprints(legacy)
    monkeypatch.setattr(native_contract, "NATIVE_PROTOCOL_REVISION", 3)
    assert all(new != old for new, old in zip(current, fingerprints(settings), strict=True))
    assert fingerprints(legacy) == unchanged


async def receipts(engine: AsyncEngine, schema: str) -> dict[str, tuple[str, str]]:
    async with engine.connect() as connection:
        rows = (await connection.execute(text(
            f'SELECT version,manifest,applied_at::text FROM "{schema}".moyai_app_schema_versions'
        ))).all()
        return {str(row[0]): (str(row[1]), str(row[2])) for row in rows}


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
async def test_v13_to_v14_adds_only_receipt_and_preserves_data_and_drain(
    product_database: ProductDatabase,
) -> None:
    product = product_database
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=GIT_OPERATION_SCHEMA_VERSION,
    )
    previous = await receipts(product.source, product.schema)
    assert len(previous) == 13
    assert dict(MIGRATIONS)[EXECUTION_COMPATIBILITY_SCHEMA_VERSION] == frozenset()
    async with product.database.sessions.begin() as db:
        db.add(User(
            id="retained", username="retained", display_name="Retained", password="unused",
            auth_version=7, preferences='{"preserve":true}',
        ))
        await db.flush()
        db.add(Session(id="retained-session", owner_id="retained", title="Retained", model="test"))
        await db.execute(text(
            "UPDATE moyai_product_drain_state SET generation=8,paused=false"
        ))
    with pytest.raises(ProductSchemaError, match="version/manifest differs"):
        await verify_product_schema(product.source, schema=product.schema)
    assert await receipts(product.source, product.schema) == previous

    writes: list[str] = []

    def observe(
        connection: Connection, cursor: object, statement: str, parameters: object,
        context: ExecutionContext, executemany: bool,
    ) -> None:
        operation = statement.lstrip().split(maxsplit=1)[0].upper()
        assert operation not in {"CREATE", "ALTER", "DROP", "TRUNCATE"}, statement
        if operation in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(product.source.sync_engine, "before_cursor_execute", observe)
    try:
        assert await migrate_product_schema(
            product.source, schema=product.schema, source_schema="moyai",
            version=EXECUTION_COMPATIBILITY_SCHEMA_VERSION,
        ) == EXECUTION_COMPATIBILITY_SCHEMA_VERSION
        assert len(writes) == 1 and "moyai_app_schema_versions" in writes[0]
        writes.clear()
        await migrate_product_schema(
            product.source, schema=product.schema, source_schema="moyai",
            version=EXECUTION_COMPATIBILITY_SCHEMA_VERSION,
        )
        assert writes == []
    finally:
        event.remove(product.source.sync_engine, "before_cursor_execute", observe)

    current = await receipts(product.source, product.schema)
    assert set(current) == {*previous, EXECUTION_COMPATIBILITY_SCHEMA_VERSION}
    assert all(current[version] == receipt for version, receipt in previous.items())
    assert current[EXECUTION_COMPATIBILITY_SCHEMA_VERSION][0] == previous[GIT_OPERATION_SCHEMA_VERSION][0]
    manifests = {version: receipt[0] for version, receipt in current.items()}
    target = {
        "schema": product.schema,
        "app": manifests,
        "harness": {"sdk_version": "2.55.0", "harness_version": "0.55.0"},
    }
    old_target = {**target, "app": {
        version: manifest for version, manifest in manifests.items() if version != EXECUTION_COMPATIBILITY_SCHEMA_VERSION
    }}
    assert migration_plan({**old_target, "state": "receipted"}, target)["apply"]
    with pytest.raises(ValueError, match="incomplete or incompatible"):
        migration_plan({**target, "state": "receipted"}, old_target)
    await verify_product_schema(
        product.source, schema=product.schema, version=EXECUTION_COMPATIBILITY_SCHEMA_VERSION,
    )
    async with product.database.sessions() as db:
        user = await db.get(User, "retained")
        session = await db.get(Session, "retained-session")
        assert user is not None and user.auth_version == 7 and user.preferences == '{"preserve":true}'
        assert session is not None and session.owner_id == user.id and session.title == "Retained"
        state = (await db.execute(text(
            "SELECT generation,paused,request_key FROM moyai_product_drain_state"
        ))).one()
        assert tuple(state) == (8, False, "bootstrap")
