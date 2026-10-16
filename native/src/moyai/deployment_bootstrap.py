"""Explicit fresh-install schema preparation; no application or worker startup."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from contextlib import asynccontextmanager

import psycopg
from psycopg import sql

from .deployment_checks import settings
from .deployment_config import InstallationError
from .postgres import create_engine


@asynccontextmanager
async def installation_lock(values: Mapping[str, str]):
    async with await psycopg.AsyncConnection.connect(
        values["MOYAI_DATABASE_URL"], connect_timeout=5, autocommit=True,
    ) as connection:
        cursor = await connection.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
            ("moyai-install:" + values["MOYAI_NATIVE_DATABASE_SCHEMA"],),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            raise InstallationError("Another installer owns this database namespace; wait for it to finish.")
        yield


def require_empty(values: Mapping[str, str]) -> None:
    schemas = [values["MOYAI_NATIVE_DATABASE_SCHEMA"]]
    if values["MOYAI_PRODUCT_MODE"] == "existing":
        schemas.append(values["MOYAI_DATABASE_SCHEMA"])
    with psycopg.connect(values["MOYAI_DATABASE_URL"], connect_timeout=5) as connection:
        for schema in schemas:
            populated = connection.execute(
                "SELECT 1 FROM pg_class WHERE relnamespace=to_regnamespace(%s) LIMIT 1", (schema,),
            ).fetchone()
            if populated:
                raise InstallationError(
                    "The target schema already contains data. Restore installation.json or use the reviewed migration procedure."
                )


async def provision(values: Mapping[str, str], *, fresh: bool) -> dict:
    from .product_config import load_product_config
    from .product_start import prepare_native_schema

    if values["MOYAI_PRODUCT_MODE"] == "existing":
        schema = values["MOYAI_DATABASE_SCHEMA"]
        if fresh:
            def create_schema() -> None:
                with psycopg.connect(values["MOYAI_DATABASE_URL"], connect_timeout=5) as connection:
                    connection.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
            await asyncio.to_thread(create_schema)
        # Canonical migration retains historical settings semantics but runs
        # in this installation; no retired executor or second interpreter loads.
        migration = {
            **values, "MOYAI_CORE": "legacy", "MOYAI_RUNTIME_ROLE": "standalone",
            "MOYAI_DATABASE_INITIALIZE": str(fresh).lower(), "MOYAI_SCHEMA_MODE": "auto",
            "MOYAI_SEPARATE_BROKER": "false", "MOYAI_COORDINATOR_HANDOFF": "false",
        }
        completed = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "app.schema_migrations", "--apply"],
            env=migration, capture_output=True, text=True, timeout=300, check=False,
        )
        if completed.returncode:
            raise InstallationError(
                "Product schema preparation failed. Keep services stopped and inspect the migration before retrying."
            )
        try:
            receipt = json.loads(completed.stdout)
            if receipt["status"] != "ready":
                raise ValueError
        except (ValueError, KeyError):
            raise InstallationError("Product schema preparation returned no verified receipt.") from None
        config = load_product_config(values)
        assert config is not None
        native = await prepare_native_schema(config, apply=True)
        return {"product": receipt, "native": native}

    from .database import Database
    from .product_schema import migrate_product_schema, verify_product_schema
    from .security import Security
    from .stores import NativeStores

    configured = settings(values)
    engine = create_engine(configured.sqlalchemy_url)
    stores = NativeStores(configured)
    try:
        revision = await migrate_product_schema(engine, schema=configured.database_schema)
        await stores.start(provision=True)
        if fresh:
            bootstrap = configured.model_copy(update={"native_schema_mode": "bootstrap"})
            database = Database(bootstrap, Security(bootstrap.master_key))
            try:
                await database.start()
            finally:
                await database.close()
        await verify_product_schema(engine, schema=configured.database_schema)
        return {"native": {"app_schema": revision, "harness_schema": "provisioned"}}
    finally:
        await stores.close()
        await engine.dispose()


async def verify(values: Mapping[str, str]) -> dict:
    from .product_config import load_product_config
    from .product_start import prepare_native_schema

    if values["MOYAI_PRODUCT_MODE"] == "existing":
        config = load_product_config(values)
        assert config is not None
        return await prepare_native_schema(config, apply=False)
    from .product_schema import verify_product_schema
    from .stores import NativeStores

    configured = settings(values)
    engine = create_engine(configured.sqlalchemy_url)
    stores = NativeStores(configured)
    try:
        await verify_product_schema(engine, schema=configured.database_schema)
        await stores.start()
        return {"app_schema": "verified", "harness_schema": "verified"}
    finally:
        await stores.close()
        await engine.dispose()
