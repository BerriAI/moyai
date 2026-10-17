"""Independent PostgreSQL databases with a reusable, immutable initial schema."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_product_identity import SCHEMA, settings_for

from moyai.database import Database
from moyai.product_identity import ProductIdentityAdapter
from moyai.product_schema import migrate_product_schema
from moyai.security import Security
from moyai.settings import Settings


async def seed_runtime_store_schema(source):
    async with source.begin() as connection:
        await connection.execute(text(
            "CREATE TABLE IF NOT EXISTS moyai.runs ("
            "id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'completed', "
            "private_owner_id TEXT NOT NULL DEFAULT '', deleted_at TEXT NOT NULL DEFAULT '', "
            "deletion_requested_at TEXT NOT NULL DEFAULT '')"
        ))


@asynccontextmanager
async def product_database_state(tmp_path, *, template="template0"):
    base = urlsplit(os.environ["MOYAI_TEST_POSTGRES"])
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    name = "moyai_authority_" + uuid4().hex
    admin = await asyncpg.connect(urlunsplit(base))
    source = database = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}" ENCODING \'UTF8\' TEMPLATE "{template}"')
        created = True
        url = urlunsplit(base._replace(path="/" + name))
        source = create_async_engine("postgresql+asyncpg://" + url.split("://", 1)[1])
        # Keep the existing long-identifier and canonical-identity regressions.
        schema = "moyai_product_authority_regression_execution_schema"
        subject = "long-" + "x" * 240 + "用户" * 10
        user_id = "google:" + subject
        if template == "template0":
            async with source.begin() as connection:
                await connection.execute(text("CREATE SCHEMA moyai"))
                for statement in SCHEMA[:2]:
                    await connection.execute(text(statement.replace("CREATE TABLE ", "CREATE TABLE moyai.", 1)))
                await connection.execute(text(
                    "INSERT INTO moyai.users VALUES (:id,'google','admin@example.test','Product admin',NULL,'c','u')"
                ), {"id": user_id})
        settings = Settings(
            _env_file=None, data_dir=tmp_path / "never-bootstrap", database_url=url,
            database_schema=schema, product_mode="existing", native_schema_mode="verify",
            encryption_key="synthetic-native-authority-key-no-production", default_model="test",
            workspace_password="must-not-bootstrap-an-admin",
        )
        database = Database(settings, Security(settings.master_key))
        adapter = ProductIdentityAdapter(
            settings_for(tmp_path), source, schema="moyai", native_engine=database.engine,
            allow_shared_identities=True,
        )
        database.product_identity = adapter
        yield SimpleNamespace(
            database=database, adapter=adapter, source=source, schema=schema,
            settings=settings, user_id=user_id, subject=subject, database_name=name,
        )
    finally:
        if database:
            await database.close()
        if source:
            await source.dispose()
        if created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


@pytest_asyncio.fixture
async def product_database(tmp_path):
    """Migration and bootstrap tests still start without native schema tables."""
    async with product_database_state(tmp_path) as product:
        yield product


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def product_database_template(tmp_path_factory):
    # Each pytest process/shard owns its template. No connections or mutable
    # Python objects from this session loop are passed into a test's event loop.
    async with product_database_state(tmp_path_factory.mktemp("product-template")) as product:
        await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
        await product.database.close()
        await product.source.dispose()
        admin = await asyncpg.connect(os.environ["MOYAI_TEST_POSTGRES"])
        try:
            await admin.execute(f'ALTER DATABASE "{product.database_name}" ALLOW_CONNECTIONS false')
        finally:
            await admin.close()
        yield product.database_name


@pytest_asyncio.fixture
async def prepared_product_database(tmp_path, product_database_template):
    """Copy only initial state; every case owns fresh pools and a separate DB."""
    async with product_database_state(tmp_path, template=product_database_template) as product:
        yield product
