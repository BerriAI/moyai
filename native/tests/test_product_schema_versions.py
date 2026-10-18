"""Each new feature is additive to every already-provisioned receipt."""

import os
from uuid import uuid4

import pytest
import test_product_authority as authority_fixtures
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_product_automation_schema import receipts, source_automation

from moyai.product_schema import (
    AUTOMATION_CONTEXT_SCHEMA_VERSION,
    AUTOMATION_CONTEXT_TABLE,
    AUTOMATION_CONTEXT_TRIGGER,
    AUTOMATION_SCHEMA_VERSION,
    BILLING_STORAGE_SCHEMA_VERSION,
    CONTINUATION_CAPTURE_SCHEMA_VERSION,
    CONTINUATION_CAPTURE_TABLE,
    CONTINUATION_CAPTURE_TRIGGER,
    CREDENTIAL_REQUEST_SCHEMA_VERSION,
    DELEGATION_SCHEMA_VERSION,
    DRAIN_SCHEMA_VERSION,
    EXECUTION_COMPATIBILITY_SCHEMA_VERSION,
    GIT_OPERATION_SCHEMA_VERSION,
    GITHUB_WORKFLOW_SCHEMA_VERSION,
    INFERENCE_SCHEMA_VERSION,
    MEMORY_REVIEW_SCHEMA_VERSION,
    MESSAGE_SOURCE_SCHEMA_VERSION,
    SLACK_ACTIVITY_SCHEMA_VERSION,
    SWARM_SCHEMA_VERSION,
    ProductSchemaError,
    migrate_product_schema,
    verify_product_schema,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product


async def test_inference_version_preserves_prior_receipts_and_requires_explicit_provision(product_database):
    product = product_database
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=AUTOMATION_SCHEMA_VERSION,
    )
    old = await receipts(product)
    with pytest.raises(ProductSchemaError, match="version/manifest differs"):
        await verify_product_schema(product.source, schema=product.schema, version=INFERENCE_SCHEMA_VERSION)
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT to_regclass(:name)"), {
            "name": product.schema + ".moyai_product_inference_requests",
        }) is None
    assert await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=INFERENCE_SCHEMA_VERSION,
    ) == INFERENCE_SCHEMA_VERSION
    current = await receipts(product)
    assert set(current) == {*old, INFERENCE_SCHEMA_VERSION}
    assert all(current[key] == value for key, value in old.items())
    await verify_product_schema(product.source, schema=product.schema, version=INFERENCE_SCHEMA_VERSION)
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=INFERENCE_SCHEMA_VERSION,
    )
    assert await receipts(product) == current
    async with product.source.connect() as db:
        assert await db.scalar(text(f'SELECT count(*) FROM "{product.schema}".moyai_product_inference_requests')) == 0


async def test_automation_context_version_is_additive_and_rejects_rewriting_captured_authority(admitted_product):
    from moyai.product_automation_context_models import ProductAutomationExecutionContext

    product = admitted_product
    automation_id = await source_automation(product)
    before = await receipts(product)
    async with product.database.sessions.begin() as db:
        db.add(ProductAutomationExecutionContext(
            automation_id=automation_id, generation=2, owner_id=product.user_id,
            authority_version=product.principal.authority_version,
            connection_ids="[]", workspace_context="{}", fingerprint="a" * 64,
        ))
    with pytest.raises(IntegrityError, match="immutable"):
        async with product.database.sessions.begin() as db:
            context = await db.get(ProductAutomationExecutionContext, (automation_id, 2))
            context.connection_ids = f'["{uuid4()}"]'
    async with product.database.sessions() as db:
        context = await db.get(ProductAutomationExecutionContext, (automation_id, 2))
        assert context.connection_ids == "[]"
        assert context.authority_version == product.principal.authority_version
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=AUTOMATION_CONTEXT_SCHEMA_VERSION,
    )
    assert await receipts(product) == before
    async with product.source.begin() as db:
        await db.execute(text(
            f'ALTER TABLE "{product.schema}"."{AUTOMATION_CONTEXT_TABLE}" '
            f'DISABLE TRIGGER "{AUTOMATION_CONTEXT_TRIGGER}"'
        ))
    with pytest.raises(ProductSchemaError, match="immutable"):
        await verify_product_schema(product.source, schema=product.schema, version=AUTOMATION_CONTEXT_SCHEMA_VERSION)


@pytest.mark.parametrize("mutation", ["update", "noop", "delete"])
async def test_continuation_capture_cannot_be_rewritten_or_deleted(admitted_product, mutation):
    from moyai.contracts import NewSession
    from moyai.product_continuation_models import ProductContinuationCapture

    product = admitted_product
    session = await product.database.create_session(product.actor, NewSession())
    source_id = uuid4().hex
    async with product.database.sessions.begin() as db:
        db.add(ProductContinuationCapture(
            native_session_id=session.id, source_session_id=source_id,
            descriptor=product.database.security.seal({"files": []}), fingerprint="a" * 64,
        ))
    with pytest.raises(DBAPIError) as immutable:
        async with product.database.sessions.begin() as db:
            statement = delete(ProductContinuationCapture) if mutation == "delete" else update(
                ProductContinuationCapture,
            ).values(fingerprint=ProductContinuationCapture.fingerprint if mutation == "noop" else "b" * 64)
            await db.execute(statement.where(ProductContinuationCapture.native_session_id == session.id))
    assert getattr(immutable.value.orig, "sqlstate", None) == "55000"
    async with product.database.sessions() as db:
        saved = await db.get(ProductContinuationCapture, (session.id, source_id))
        assert saved.fingerprint == "a" * 64


@pytest.mark.parametrize("tamper", ["replica_only", "missing_delete", "function_body"])
async def test_capture_verifier_checks_effective_trigger_events_and_function(admitted_product, tamper):
    product = admitted_product
    async with product.source.begin() as db:
        if tamper == "replica_only":
            await db.execute(text(
                f'ALTER TABLE "{product.schema}"."{CONTINUATION_CAPTURE_TABLE}" '
                f'ENABLE REPLICA TRIGGER "{CONTINUATION_CAPTURE_TRIGGER}"'
            ))
        elif tamper == "missing_delete":
            await db.execute(text(
                f'DROP TRIGGER "{CONTINUATION_CAPTURE_TRIGGER}" ON "{product.schema}"."{CONTINUATION_CAPTURE_TABLE}"'
            ))
            await db.execute(text(
                f'CREATE TRIGGER "{CONTINUATION_CAPTURE_TRIGGER}" BEFORE UPDATE '
                f'ON "{product.schema}"."{CONTINUATION_CAPTURE_TABLE}" FOR EACH ROW '
                f'EXECUTE FUNCTION "{product.schema}"."{CONTINUATION_CAPTURE_TRIGGER}"()'
            ))
        else:
            await db.execute(text(
                f'CREATE OR REPLACE FUNCTION "{product.schema}"."{CONTINUATION_CAPTURE_TRIGGER}"() '
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END $$"
            ))
    with pytest.raises(ProductSchemaError, match="immutable"):
        await verify_product_schema(
            product.source, schema=product.schema, version=CONTINUATION_CAPTURE_SCHEMA_VERSION,
        )


async def test_additive_versions_preserve_every_earlier_receipt(product_database):
    product = product_database
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=CONTINUATION_CAPTURE_SCHEMA_VERSION,
    )
    earlier = await receipts(product)
    for version in (
        CREDENTIAL_REQUEST_SCHEMA_VERSION, MEMORY_REVIEW_SCHEMA_VERSION, DRAIN_SCHEMA_VERSION,
        GIT_OPERATION_SCHEMA_VERSION, EXECUTION_COMPATIBILITY_SCHEMA_VERSION,
        BILLING_STORAGE_SCHEMA_VERSION, SWARM_SCHEMA_VERSION, GITHUB_WORKFLOW_SCHEMA_VERSION,
        DELEGATION_SCHEMA_VERSION, SLACK_ACTIVITY_SCHEMA_VERSION, MESSAGE_SOURCE_SCHEMA_VERSION,
    ):
        await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai", version=version)
        current = await receipts(product)
        assert set(current) == {*earlier, version}
        assert all(current[key] == value for key, value in earlier.items())
        await verify_product_schema(product.source, schema=product.schema, version=version)
        earlier = current
    async with product.source.connect() as db:
        state = (await db.execute(text(
            f'SELECT * FROM "{product.schema}".moyai_product_drain_state'
        ))).mappings().one()
        assert state["id"] == 1 and state["generation"] == 0 and state["paused"]
        assert state["request_key"] == "bootstrap"
        assert state["owner_pid"] is None and state["owner_started"] is None and state["source_policy"] is None
    async with product.source.begin() as db:
        await db.execute(text(
            f'UPDATE "{product.schema}".moyai_product_drain_state SET generation=8,paused=false'
        ))
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    assert await receipts(product) == earlier
    async with product.source.connect() as db:
        state = (await db.execute(text(
            f'SELECT generation,paused FROM "{product.schema}".moyai_product_drain_state'
        ))).one()
        assert tuple(state) == (8, False)


async def test_drain_verifier_rejects_missing_singleton_constraint_without_repair(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text(
            f'ALTER TABLE "{product.schema}".moyai_product_drain_state '
            "DROP CONSTRAINT moyai_product_drain_singleton"
        ))
    with pytest.raises(ProductSchemaError, match="singleton"):
        await verify_product_schema(product.source, schema=product.schema)


async def test_message_sources_are_additive_and_do_not_rewrite_old_receipts(product_database):
    product = product_database
    await migrate_product_schema(product.source, schema=product.schema, source_schema='moyai',
                                 version=SLACK_ACTIVITY_SCHEMA_VERSION)
    old = await receipts(product)
    await migrate_product_schema(product.source, schema=product.schema, source_schema='moyai')
    current = await receipts(product)
    assert set(current) == {*old, MESSAGE_SOURCE_SCHEMA_VERSION}
    assert all(current[key] == value for key, value in old.items())
    await verify_product_schema(product.source, schema=product.schema)
    async with product.source.connect() as db:
        for table in ('moyai_message_sources', 'moyai_delivery_schedule'):
            assert await db.scalar(text('SELECT to_regclass(:name)'), {'name': product.schema + '.' + table})
