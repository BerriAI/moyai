"""Real PostgreSQL release-store guards; actual app proof is separate."""
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_authority as authority_fixtures
from sqlalchemy import text
from test_product_automation_schema import receipts

from moyai import maintenance_activation as activation
from moyai.product_schema import migrate_product_schema, verify_product_schema

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = authority_fixtures.product_database


@pytest_asyncio.fixture
async def store(product_database, monkeypatch):
    product = product_database
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    config = SimpleNamespace(
        product_mode="existing", product_schema="moyai", native_schema=product.schema,
        database_url=product.settings.postgres_url,
    )
    monkeypatch.setattr("moyai.product_config.load_product_config", lambda: config)
    # Canonical schema verification is exercised by the actual-app rehearsal.
    monkeypatch.setattr("moyai.product_deployment.verify_product_schema", lambda _: {})
    monkeypatch.setenv("MOYAI_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MOYAI_RUNTIME_ROLE", "coordinator")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-local")
    monkeypatch.setenv("MOYAI_MAINTENANCE_TOKEN", "c" * 32)
    from moyai.product_drain_models import BOOTSTRAP_FINGERPRINT
    receipt = activation.ActivationReceipt(
        identity=activation.StageIdentity(
            build="a" * 40, role="coordinator", service="srv-local", instance="d" * 32, token="c" * 32,
        ), request_id="test:activation",
        guard=activation.ActivationGuard(
            product_schema="moyai", native_schema=product.schema,
            generation=0, request_fingerprint=BOOTSTRAP_FINGERPRINT,
        ),
    )
    return product, receipt


async def test_explicit_release_store_preserves_application_migration_receipts(store):
    product, receipt = store
    before = await receipts(product)
    assert activation.durable_receipt() is None
    activation.provision(receipt.guard)
    activation.provision(receipt.guard)
    assert await receipts(product) == before
    await verify_product_schema(product.source, schema=product.schema)
    async with product.source.connect() as connection:
        assert await connection.scalar(text(
            f'SELECT count(*) FROM "{product.schema}"."{activation.RECEIPT_TABLE}"',
        )) == 0


async def test_live_canonical_owner_prevents_receipt_provisioning(store):
    import psycopg
    product, receipt = store
    with psycopg.connect(product.settings.postgres_url) as owner:
        owner.execute("SELECT pg_advisory_lock_shared(726940,'moyai'::regnamespace::oid::int)")
        with pytest.raises(ValueError, match="runtime still owns"):
            activation.provision(receipt.guard)
    assert activation.durable_receipt() is None


async def test_unexpected_release_store_shape_is_never_adopted_or_repaired(store):
    product, receipt = store
    activation.provision(receipt.guard)
    async with product.source.begin() as connection:
        await connection.execute(text(
            f'ALTER TABLE "{product.schema}"."{activation.RECEIPT_TABLE}" ADD COLUMN unexpected text',
        ))
    for operation in (
        lambda: activation.provision(receipt.guard),
        lambda: activation.accept_durable(receipt), activation.durable_receipt,
    ):
        with pytest.raises(ValueError, match="storage differs"):
            operation()


async def test_durable_activation_requires_exact_pause_and_survives_restart(store, monkeypatch):
    product, receipt = store
    assert activation.durable_receipt() is None
    activation.provision(receipt.guard)
    activation.accept_durable(receipt)
    activation.accept_durable(receipt)
    with pytest.raises(ValueError, match="Another activation"):
        activation.accept_durable(receipt.model_copy(update={"request_id": "other"}))
    async with product.source.begin() as connection:
        await connection.execute(text(
            f'UPDATE "{product.schema}".moyai_product_drain_state SET generation=1,paused=false WHERE id=1',
        ))
    assert activation.durable_receipt() == receipt
    with pytest.raises(ValueError, match="pause changed"):
        activation.accept_durable(receipt)
    monkeypatch.setenv("MOYAI_MAINTENANCE_TOKEN", "e" * 32)
    assert activation.durable_receipt() is None
    monkeypatch.setenv("MOYAI_MAINTENANCE_TOKEN", "c" * 32)
    monkeypatch.setenv("MOYAI_BUILD_SHA", "f" * 40)
    with pytest.raises(ValueError, match="another staging attempt"):
        activation.durable_receipt()
