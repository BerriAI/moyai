"""Additive schema and real PostgreSQL transaction boundaries, not scheduler E2E."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import test_product_authority as authority_fixtures
from app.database import PostgresConnection
from fastapi import HTTPException
from psycopg import connect
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from test_product_authority import demote

from moyai.contracts import NewMessage, NewSession
from moyai.models import Message, ProductTurnAuthority, Session, Turn, User
from moyai.product_automation_models import (
    ProductAutomationBinding,
    ProductAutomationOccurrence,
    ProductAutomationOperation,
)
from moyai.product_identity import ProductGrant
from moyai.product_schema import (
    AUTHORITY_SCHEMA_VERSION,
    AUTOMATION_SCHEMA_VERSION,
    AUTOMATION_TABLES,
    RECEIPTS,
    ProductSchemaError,
    _manifest,
    application_metadata,
    migrate_product_schema,
    verify_product_schema,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product


async def receipts(product):
    async with product.source.connect() as db:
        return dict((await db.execute(text(
            f'SELECT version, row_to_json(r)::text FROM "{product.schema}"."{RECEIPTS}" r'
        ))).all())


async def test_v2_preserves_exact_v1_manifest_receipt_and_existing_data(product_database):
    product = product_database
    # Captured before automation models existed; a changed v1 inventory fails this.
    assert _manifest(application_metadata(
        "moyai_product_automation_schema_test", version=AUTHORITY_SCHEMA_VERSION,
    )) == "f0745b9745c5c68bd3cd564325e7eedd50336c727dc36f068cd241b195d531b4"
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=AUTHORITY_SCHEMA_VERSION,
    )
    before = await receipts(product)
    assert set(before) == {AUTHORITY_SCHEMA_VERSION}
    async with product.database.sessions.begin() as db:
        db.add(User(id="native-existing", username="existing", display_name="Existing", password="unusable"))
    with pytest.raises(ProductSchemaError, match="version/manifest differs"):
        await product.database.start()
    async with product.source.connect() as db:
        for table in AUTOMATION_TABLES:
            assert await db.scalar(text("SELECT to_regclass(:name)"), {
                "name": f"{product.schema}.{table}",
            }) is None
    assert await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=AUTOMATION_SCHEMA_VERSION,
    ) == AUTOMATION_SCHEMA_VERSION
    after = await receipts(product)
    assert set(after) == {AUTHORITY_SCHEMA_VERSION, AUTOMATION_SCHEMA_VERSION}
    assert after[AUTHORITY_SCHEMA_VERSION] == before[AUTHORITY_SCHEMA_VERSION]
    await verify_product_schema(product.database.engine, schema=product.schema, version=AUTOMATION_SCHEMA_VERSION)
    async with product.database.sessions() as db:
        assert (await db.get(User, "native-existing")).display_name == "Existing"
        for model in (ProductAutomationBinding, ProductAutomationOccurrence, ProductAutomationOperation):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        assert await db.scalar(text("SELECT count(*) FROM moyai.users")) == 1
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=AUTOMATION_SCHEMA_VERSION,
    )
    assert await receipts(product) == after


@pytest.mark.parametrize("tamper", ["v1_receipt", "v2_table", "v2_foreign_key"])
async def test_verify_fails_closed_and_never_recreates_or_restamps(admitted_product, tamper):
    product = admitted_product
    async with product.source.begin() as db:
        if tamper == "v1_receipt":
            await db.execute(text(
                f'UPDATE "{product.schema}"."{RECEIPTS}" SET manifest=:bad WHERE version=:version'
            ), {"bad": "0" * 64, "version": AUTHORITY_SCHEMA_VERSION})
        elif tamper == "v2_table":
            await db.execute(text(f'DROP TABLE "{product.schema}".moyai_product_automation_operations'))
        else:
            from sqlalchemy import inspect
            constraints = await db.run_sync(lambda conn: inspect(conn).get_foreign_keys(
                "moyai_product_automation_occurrences", schema=product.schema,
            ))
            target = next(item["name"] for item in constraints if item["constrained_columns"] == ["turn_id"])
            await db.execute(text(
                f'ALTER TABLE "{product.schema}".moyai_product_automation_occurrences DROP CONSTRAINT "{target}"'
            ))
    before = await receipts(product)
    with pytest.raises(ProductSchemaError):
        await verify_product_schema(product.database.engine, schema=product.schema)
    with pytest.raises(ProductSchemaError):
        await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    assert await receipts(product) == before
    if tamper == "v2_table":
        async with product.source.connect() as db:
            assert await db.scalar(text("SELECT to_regclass(:name)"), {
                "name": f"{product.schema}.moyai_product_automation_operations",
            }) is None


async def test_concurrent_v2_provision_is_single_additive_migration(product_database):
    product = product_database
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=AUTHORITY_SCHEMA_VERSION,
    )
    old = await receipts(product)
    assert await asyncio.gather(*[
        migrate_product_schema(
            product.source, schema=product.schema, source_schema="moyai", version=AUTOMATION_SCHEMA_VERSION,
        )
        for _ in range(2)
    ]) == [AUTOMATION_SCHEMA_VERSION] * 2
    current = await receipts(product)
    assert len(current) == 2 and current[AUTHORITY_SCHEMA_VERSION] == old[AUTHORITY_SCHEMA_VERSION]
    await verify_product_schema(product.database.engine, schema=product.schema, version=AUTOMATION_SCHEMA_VERSION)


async def source_automation(product):
    """Minimal canonical receipt/FK schema; no legacy runtime import or copied executor."""
    automation_id = uuid4().hex
    async with product.source.begin() as db:
        for statement in (
            ("CREATE TABLE moyai.automations (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES moyai.users(id), "
             "definition TEXT NOT NULL, revision INTEGER NOT NULL, paused INTEGER NOT NULL)"),
            "CREATE TABLE moyai.runs (id TEXT PRIMARY KEY)",
            ("CREATE TABLE moyai.automation_runs (occurrence TEXT PRIMARY KEY, "
            "automation_id TEXT NOT NULL REFERENCES moyai.automations(id), revision INTEGER NOT NULL, "
            "run_id TEXT UNIQUE REFERENCES moyai.runs(id), outcome TEXT NOT NULL, detail TEXT NOT NULL, "
             "created_at TEXT NOT NULL)"),
        ):
            await db.execute(text(statement))
        await db.execute(text(
            "INSERT INTO moyai.automations VALUES (:id,:owner,:definition,4,1)"
        ), {"id": automation_id, "owner": product.user_id, "definition": '{"harness":"pydantic-v1","mode":"modal"}'})
    async with product.database.sessions.begin() as db:
        db.add(ProductAutomationBinding(
            id=automation_id, owner_id=product.user_id,
            authority_version=product.principal.authority_version, source_revision=4,
            generation=2, state="active", sync_state="ready", definition_digest="d" * 64, synced_generation=2,
        ))
    return automation_id


async def fence_source(db):
    await db.execute(text("SELECT pg_advisory_xact_lock(726941, 'moyai'::regnamespace::oid::int)"))


async def admit_with_receipt(product, db, automation_id, occurrence):
    await fence_source(db)
    session, turn = await product.database.admit_automation(
        db, grant=product.principal.grant, title="Scheduled task", prompt="Work",
        model="test", request_key=occurrence,
    )
    await db.execute(text(
        "INSERT INTO moyai.automation_runs VALUES (:occurrence,:id,4,NULL,'started','','now')"
    ), {"id": automation_id, "occurrence": occurrence})
    db.add(ProductAutomationOccurrence(
        occurrence=occurrence, automation_id=automation_id, generation=2, source_revision=4,
        trigger_id="default", owner_id=product.user_id, session_id=session.id, turn_id=turn.id, status="admitted",
    ))
    await db.flush()
    return session, turn


@pytest.mark.parametrize("commit", [False, True])
async def test_receipt_session_turn_and_original_grant_share_one_transaction(admitted_product, commit):
    product = admitted_product
    automation_id = await source_automation(product)
    async with product.database.sessions() as db:
        transaction = await db.begin()
        session, turn = await admit_with_receipt(product, db, automation_id, "occurrence-one")
        # The helper must not commit or make work visible to the dispatcher.
        async with product.database.sessions() as observer:
            assert await observer.get(Turn, turn.id) is None
            assert await observer.scalar(text("SELECT count(*) FROM moyai.automation_runs")) == 0
        assert await product.database.claim_next() is None
        if commit:
            await transaction.commit()
        else:
            await transaction.rollback()
    async with product.database.sessions() as db:
        for model in (Session, Turn, Message, ProductTurnAuthority, ProductAutomationOccurrence):
            assert await db.scalar(select(func.count()).select_from(model)) == int(commit)
        assert await db.scalar(text("SELECT count(*) FROM moyai.automation_runs")) == int(commit)
        assert await db.scalar(text("SELECT count(*) FROM moyai.runs")) == 0
        assert await db.scalar(text("SELECT paused FROM moyai.automations")) == 1
        if commit:
            saved = await db.get(ProductTurnAuthority, turn.id)
            assert saved.actor_id == product.user_id
            assert saved.authority_version == product.principal.authority_version
            assert (await db.get(Session, session.id)).surface == "automation"
            assert (await db.get(Session, session.id)).visibility == "shared"
    if commit:
        assert (await product.database.claim_next()).id == turn.id
        prepared = await product.database.prepare(turn.id)
        assert prepared.scope.product_grant == product.principal.grant
        assert prepared.scope.surface == "automation"


async def test_duplicate_occurrence_rolls_back_candidate_work_instead_of_committing_partial_session(admitted_product):
    product = admitted_product
    automation_id = await source_automation(product)
    async with product.database.sessions.begin() as db:
        _, first = await admit_with_receipt(product, db, automation_id, "same-occurrence")
    with pytest.raises(IntegrityError):
        async with product.database.sessions.begin() as db:
            await admit_with_receipt(product, db, automation_id, "same-occurrence")
    async with product.database.sessions() as db:
        for model in (Session, Turn, Message, ProductTurnAuthority, ProductAutomationOccurrence):
            assert await db.scalar(select(func.count()).select_from(model)) == 1
        assert await db.scalar(text("SELECT count(*) FROM moyai.automation_runs")) == 1
        assert await db.get(Turn, first.id) is not None


async def test_native_occurrence_holds_real_legacy_writer_fence_until_atomic_commit(admitted_product):
    product = admitted_product
    automation_id = await source_automation(product)
    started = asyncio.Event()
    loop = asyncio.get_running_loop()

    def legacy_writer():
        with connect(product.settings.postgres_url) as raw:
            raw.execute("SET LOCAL search_path=moyai,pg_catalog")
            raw.execute("SET LOCAL lock_timeout='5s'")
            # Use the original product write-lock implementation, without a
            # production process-owner lease or a second execution runtime.
            legacy = PostgresConnection(raw, "moyai", None)
            loop.call_soon_threadsafe(started.set)
            legacy.begin_write()
            paused = raw.execute("SELECT paused FROM automations WHERE id=%s", (automation_id,)).fetchone()[0]
            receipt = raw.execute(
                "SELECT occurrence,run_id FROM automation_runs WHERE occurrence='cross-backend'",
            ).fetchone()
            return paused, receipt

    pending = None
    try:
        async with product.database.sessions.begin() as db:
            await admit_with_receipt(product, db, automation_id, "cross-backend")
            pending = asyncio.create_task(asyncio.to_thread(legacy_writer))
            await asyncio.wait_for(started.wait(), 5)
            done, _ = await asyncio.wait({pending}, timeout=0.1)
            assert not done
        assert await asyncio.wait_for(pending, 5) == (1, ("cross-backend", None))
    finally:
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("revocation", ["role", "native_epoch", "missing_grant"])
async def test_occurrence_cannot_mint_authority_after_revocation(admitted_product, revocation):
    product = admitted_product
    grant = product.principal.grant
    if revocation == "role":
        await demote(product)
    elif revocation == "native_epoch":
        async with product.database.sessions.begin() as db:
            await db.execute(update(User).where(User.id == product.user_id).values(auth_version=2))
    else:
        grant = ProductGrant(product.user_id, "")
    with pytest.raises(HTTPException) as denied:
        async with product.database.sessions.begin() as db:
            await fence_source(db)
            await product.database.admit_automation(
                db, grant=grant, title="Old saved task", prompt="Work", model="test", request_key="old",
            )
    assert denied.value.status_code == 401
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0


async def test_admission_rejects_missing_or_wrong_native_transaction(admitted_product):
    product = admitted_product
    arguments = {
        "grant": product.principal.grant, "title": "Task", "prompt": "Work", "model": "test", "request_key": "bad-tx",
    }
    async with product.database.sessions() as db:
        with pytest.raises(ValueError, match="transaction"):
            await product.database.admit_automation(db, **arguments)
    async with AsyncSession(product.source) as db, db.begin():
        with pytest.raises(ValueError, match="transaction"):
            await product.database.admit_automation(db, **arguments)


@pytest.mark.parametrize("shared_first", [False, True])
async def test_automation_admission_requires_exclusive_source_fence_before_shared_authority_check(
    admitted_product, shared_first,
):
    product = admitted_product
    async with product.database.sessions.begin() as db:
        if shared_first:
            await product.adapter.authorize_admission(db, product.principal.grant)
        with pytest.raises(ValueError, match="canonical product writer lock first"):
            await product.database.admit_automation(
                db, grant=product.principal.grant, title="Unfenced", prompt="Work", model="test", request_key="unfenced",
            )
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0


async def test_operation_receipt_supports_unmigrated_source_without_creating_binding(admitted_product):
    product = admitted_product
    operation = ProductAutomationOperation(
        owner_id=product.user_id, operation_scope="http:create", request_key="owner-request",
        fingerprint="f" * 64, automation_id=uuid4().hex, source_revision=1, result='{"paused":true}',
    )
    async with product.database.sessions.begin() as db:
        db.add(operation)
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(ProductAutomationOperation)) == 1
        assert await db.scalar(select(func.count()).select_from(ProductAutomationBinding)) == 0


async def test_shared_automation_owner_stays_shared_and_keeps_saved_authority(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    principal = await product.adapter.current("shared:password:admin")
    async with product.database.sessions.begin() as db:
        await product.adapter.project_native_user(db, principal)
    async with product.database.sessions.begin() as db:
        await fence_source(db)
        session, turn = await product.database.admit_automation(
            db, grant=principal.grant, title="Shared", prompt="Work", model="test", request_key="shared",
        )
    assert session.surface == "automation" and session.visibility == "shared"
    async with product.database.sessions() as db:
        assert (await db.get(ProductTurnAuthority, turn.id)).authority_version == principal.authority_version


async def test_public_transaction_seams_reuse_native_policy_without_commit(admitted_product):
    product = admitted_product
    async with product.database.sessions() as db:
        transaction = await db.begin()
        await fence_source(db)
        session = await product.database.create_session(product.actor, NewSession(), db=db)
        session.surface = "automation"
        turn = await product.database.submit(
            product.actor, session.id, NewMessage(prompt="Work", request_key="seam"), db=db,
        )
        assert await product.database.submit(
            product.actor, session.id, NewMessage(prompt="Work", request_key="seam"), db=db,
        ) is turn
        with pytest.raises(HTTPException) as conflicting:
            await product.database.submit(
                product.actor, session.id, NewMessage(prompt="Different", request_key="seam"), db=db,
            )
        assert conflicting.value.status_code == 409
        await transaction.rollback()
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
