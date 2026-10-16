"""Live canonical identity/relationship checks in one PostgreSQL transaction."""

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import test_product_authority as authority_fixtures
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Insert

from moyai.models import User
from moyai.product_schema import migrate_product_schema
from moyai.product_slack import ProductSlackAuthority

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product


async def test_concurrent_first_projection_creates_one_cache_identity(product_database, monkeypatch):
    product = product_database
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    await product.database.start()
    principal = await product.adapter.current(product.user_id)
    original = AsyncSession.execute
    simultaneous = asyncio.Barrier(8)

    async def at_first_insert(db, statement, *args, **kwargs):
        if isinstance(statement, Insert) and statement.table.name == User.__tablename__:
            # Every request has validated the absent projection before any
            # INSERT reaches PostgreSQL. The real unique indexes arbitrate.
            await simultaneous.wait()
        return await original(db, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", at_first_insert)

    async def project():
        async with product.database.sessions.begin() as db:
            user = await product.adapter.project_native_user(db, principal)
            return user.id, user.auth_version, user.enabled, getattr(user, "product_grant", None)

    for _ in range(5):
        async with product.database.sessions.begin() as db:
            await db.execute(delete(User))
        results = await asyncio.wait_for(
            asyncio.gather(*(project() for _ in range(8)), return_exceptions=True), 10,
        )
        assert results == [(principal.id, 1, True, principal.grant)] * 8
        async with product.database.sessions() as db:
            user, = list(await db.scalars(select(User)))
            assert user.id == principal.id
            assert user.username == "product:" + hashlib.sha256(principal.id.encode()).hexdigest()


async def test_projection_username_collision_never_adopts_another_native_identity(product_database):
    product = product_database
    await migrate_product_schema(product.source, schema=product.schema, source_schema="moyai")
    await product.database.start()
    principal = await product.adapter.current(product.user_id)
    other_id = str(uuid4())
    username = "product:" + hashlib.sha256(principal.id.encode()).hexdigest()
    async with product.database.sessions.begin() as db:
        db.add(User(
            id=other_id, username=username, display_name="Other local identity",
            password="not-a-product-credential", role="viewer", enabled=False, auth_version=7,
        ))
    with pytest.raises(HTTPException) as conflict:
        async with product.database.sessions.begin() as db:
            await product.adapter.project_native_user(db, principal)
    assert conflict.value.status_code == 409
    async with product.database.sessions() as db:
        assert await db.get(User, principal.id) is None
        other = await db.get(User, other_id)
        assert other is not None and other.id == other_id and other.role == "viewer"
        assert not other.enabled and other.auth_version == 7


@pytest_asyncio.fixture
async def slack_admission(admitted_product):
    product = admitted_product
    identity = "slack:TLOCAL001:UACTUAL01"
    product.adapter.settings.slack_bot_enabled = True
    product.adapter.settings.slack_session_users = "UACTUAL01"
    product.adapter.settings.slack_signing_secret = "synthetic-local-signature-only"
    product.adapter.slack_authority = ProductSlackAuthority(product.adapter)
    encrypted = product.adapter.legacy_security.encrypt(json.dumps({
        "bot": {"access_token": "synthetic-bot-token", "bot_user_id": "UBOT00001", "team": {"id": "TLOCAL001"}},
    }))
    async with product.source.begin() as db:
        for name, kind in (
            ("profile_eligible", "INTEGER NOT NULL DEFAULT 0"),
            ("profile_checked_at", "TEXT NOT NULL DEFAULT ''"),
            ("profile_conflict", "INTEGER NOT NULL DEFAULT 0"),
            ("link_status", "TEXT NOT NULL DEFAULT ''"),
        ):
            await db.execute(text(f"ALTER TABLE moyai.users ADD COLUMN {name} {kind}"))
        await db.execute(text("CREATE TABLE moyai.connections(provider TEXT PRIMARY KEY,encrypted TEXT NOT NULL)"))
        await db.execute(text(
            "CREATE TABLE moyai.connection_policies(provider TEXT PRIMARY KEY,enabled INTEGER,read_only INTEGER)"
        ))
        await db.execute(text("INSERT INTO moyai.connections VALUES('slack',:encrypted)"), {"encrypted": encrypted})
        await db.execute(text(
            "INSERT INTO moyai.users(id,kind,email,name,linked_user_id,created_at,updated_at,"
            "profile_eligible,profile_checked_at,profile_conflict,link_status) "
            "VALUES (:id,'slack','admin@example.test','Signed Slack sender',:owner,'c','u',1,:stamp,0,'linked')"
        ), {"id": identity, "owner": product.user_id, "stamp": datetime.now(UTC).isoformat()})
    # The fixture adds source columns after the baseline account was read.
    # Reconnect after this explicit schema change, as the real release does.
    await product.source.dispose()
    await product.database.engine.dispose()
    principal = await product.adapter.current(identity)
    async with product.database.sessions.begin() as db:
        await product.adapter.project_native_user(db, principal)
    product.raw_principal = principal
    return product


async def test_explicit_individual_capture_preserves_raw_slack_origin_and_creates_no_source_account(slack_admission):
    product = slack_admission
    raw = product.raw_principal
    assert raw.kind == "slack" and not raw.individual and raw.role == "member"
    async with product.database.sessions.begin() as db:
        owner = await product.adapter.individual_for_admission(db, raw.grant)
        assert owner.id == product.user_id and owner.individual and owner.role == "admin"
        assert owner.grant == product.principal.grant
        assert raw.grant.user_id.startswith("slack:") and raw.role == "member"
        projected = await product.adapter.project_native_user(db, owner)
        assert projected.id == product.user_id
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.users")) == 2


@pytest.mark.parametrize("change", ["stale", "unverified", "conflict", "ambiguous", "owner_disabled", "raw_epoch"])
async def test_individual_capture_rejects_stale_or_changed_relationship_and_original_raw_authority(slack_admission, change):
    product = slack_admission
    if change in {"owner_disabled", "raw_epoch"}:
        async with product.database.sessions.begin() as db:
            if change == "owner_disabled":
                await db.execute(update(User).where(User.id == product.user_id).values(enabled=False))
            else:
                await db.execute(update(User).where(User.id == product.raw_principal.id).values(auth_version=2))
    else:
        async with product.source.begin() as db:
            if change == "stale":
                await db.execute(text("UPDATE moyai.users SET profile_checked_at=:stamp WHERE id=:id"), {
                    "stamp": (datetime.now(UTC) - timedelta(hours=2)).isoformat(), "id": product.raw_principal.id,
                })
            elif change == "unverified":
                await db.execute(text("UPDATE moyai.users SET profile_eligible=0 WHERE id=:id"), {
                    "id": product.raw_principal.id,
                })
            elif change == "conflict":
                await db.execute(text("UPDATE moyai.users SET profile_conflict=1 WHERE id=:id"), {
                    "id": product.raw_principal.id,
                })
            else:
                await db.execute(text(
                    "INSERT INTO moyai.users(id,kind,email,name,created_at,updated_at) "
                    "VALUES ('google:ambiguous','google','admin@example.test','Ambiguous','c','u')"
                ))
    async with product.database.sessions.begin() as db:
        with pytest.raises(HTTPException):
            await product.adapter.individual_for_admission(db, product.raw_principal.grant)


async def test_browser_individual_retains_its_original_grant_and_shared_password_has_no_mapping(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    shared = await product.adapter.current("shared:password:admin")
    async with product.database.sessions.begin() as db:
        assert (await product.adapter.individual_for_admission(db, product.principal.grant)).grant == product.principal.grant
        await product.adapter.project_native_user(db, shared)
        with pytest.raises(HTTPException):
            await product.adapter.individual_for_admission(db, shared.grant)
