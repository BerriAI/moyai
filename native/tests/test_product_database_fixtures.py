"""Guard the isolation assumptions of the shared schema template."""

import os

import asyncpg
import product_database_fixtures as database_fixtures
import pytest
from product_database_fixtures import product_database_state
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = database_fixtures.product_database


async def test_raw_database_stays_unprovisioned_for_migration_tests(product_database):
    product = product_database
    async with product.source.connect() as db:
        assert await db.scalar(text(
            "SELECT to_regnamespace(:schema)"
        ), {"schema": product.schema}) is None
        assert await db.scalar(text("SELECT count(*) FROM moyai.users")) == 1


async def test_template_clones_isolate_rows_schema_and_cleanup(tmp_path, product_database_template):
    admin = await asyncpg.connect(os.environ["MOYAI_TEST_POSTGRES"])
    names = []
    try:
        assert await admin.fetchval(
            "SELECT datallowconn FROM pg_database WHERE datname=$1", product_database_template,
        ) is False
        async with product_database_state(tmp_path / "peer", template=product_database_template) as peer:
            names.append(peer.database_name)
            await peer.database.start()
            async with product_database_state(tmp_path / "changed", template=product_database_template) as changed:
                names.append(changed.database_name)
                assert changed.database_name != peer.database_name
                await changed.database.start()
                async with changed.database.sessions.begin() as db:
                    await db.execute(text("UPDATE moyai_product_drain_state SET generation=99"))
                    await db.execute(text("CREATE TABLE only_this_case (value integer)"))
                    await db.execute(text("UPDATE moyai.users SET name='Changed by one case'"))
                async with peer.database.sessions() as db:
                    assert await db.scalar(text("SELECT generation FROM moyai_product_drain_state")) == 0
                    assert await db.scalar(text("SELECT to_regclass('only_this_case')")) is None
                    assert await db.scalar(text("SELECT name FROM moyai.users")) == "Product admin"
            assert not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", changed.database_name)
            # Dropping one clone cannot interrupt the other clone's connection pool.
            await peer.database.start()
        async with product_database_state(tmp_path / "later", template=product_database_template) as later:
            names.append(later.database_name)
            await later.database.start()
            async with later.database.sessions() as db:
                assert await db.scalar(text("SELECT generation FROM moyai_product_drain_state")) == 0
                assert await db.scalar(text("SELECT to_regclass('only_this_case')")) is None
        assert not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname=ANY($1::text[])", names)
    finally:
        await admin.close()
