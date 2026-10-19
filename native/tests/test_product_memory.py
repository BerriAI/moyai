"""Canonical memory policy over isolated PostgreSQL; no production reads or imports."""

import json
import os

import pytest
import pytest_asyncio
import test_product_authority as authority_fixtures
from app.memory import Note, Preferences
from fastapi import HTTPException
from sqlalchemy import text, update

from moyai.models import User
from moyai.product_memory import ProductMemoryLibrary, ProductMemoryService

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product

MEMORY_SCHEMA = (
    ("CREATE TABLE moyai.memory_preferences (owner_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, "
     "auto_save INTEGER NOT NULL, revision INTEGER NOT NULL)"),
    ("CREATE TABLE moyai.personal_memories (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, key_hash TEXT NOT NULL, "
     "encrypted TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
     "expires_at TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(owner_id,key_hash))"),
    ("CREATE TABLE moyai.memory_operations (owner_id TEXT NOT NULL, request_id TEXT NOT NULL, "
     "fingerprint TEXT NOT NULL, memory_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(owner_id,request_id))"),
    ("CREATE TABLE moyai.memory_selections (run_id TEXT NOT NULL, turn_id INTEGER NOT NULL, actor_id TEXT NOT NULL, "
     "owner_id TEXT NOT NULL, memory_id TEXT NOT NULL, PRIMARY KEY(run_id,turn_id,memory_id))"),
    "CREATE TABLE moyai.memory_capture_barriers (owner_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL)",
    "CREATE TABLE moyai.messages (id INTEGER PRIMARY KEY)",
    ("CREATE TABLE moyai.memory_reviews (message_id INTEGER PRIMARY KEY, owner_id TEXT NOT NULL, "
     "status TEXT NOT NULL, updated_at TEXT NOT NULL, saved_count INTEGER NOT NULL DEFAULT 0)"),
)


@pytest_asyncio.fixture
async def memory_product(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        for statement in MEMORY_SCHEMA:
            await db.execute(text(statement))
        await db.execute(text("INSERT INTO moyai.messages VALUES (12)"))
    product.memory = ProductMemoryService(product.database, product.adapter)
    return product


def note(**overrides):
    return Note(**{
        "key": "editor-preference", "title": "Editor preference", "content": "Use the existing formatter.",
        "kind": "preference", "request_id": "owner-note-one", **overrides,
    })


async def test_existing_encrypted_rows_remain_authoritative_and_preferences_do_not_claim_runtime(memory_product):
    product = memory_product
    saved = await product.memory.save(product.principal.grant, note())
    async with product.source.connect() as db:
        original = dict((await db.execute(text("SELECT * FROM moyai.personal_memories"))).mappings().one())
    response = await product.memory.listing(product.principal.grant)
    assert ProductMemoryLibrary.model_validate(response).memories[0].source.type == "manual"
    assert response["memories"][0]["id"] == saved["id"]
    assert response["memories"][0]["content"] == note().content
    assert response["scope"] == {"kind": "personal", "owner_id": product.user_id}
    assert response["preferences"] == {"enabled": True, "auto_save": True, "revision": 0}
    assert not any(response["runtime"][key] for key in ("recall", "auto_save", "background_review"))
    async with product.source.connect() as db:
        unchanged = dict((await db.execute(text("SELECT * FROM moyai.personal_memories"))).mappings().one())
        assert original == unchanged
        assert note().content not in original["encrypted"]
        assert json.loads(product.adapter.legacy_security.decrypt(original["encrypted"]))["source"] == {"type": "manual"}
        assert await db.scalar(text("SELECT message_id FROM moyai.memory_capture_barriers")) == 12


async def test_versioned_save_retry_conflict_and_tombstone_never_resurrect(memory_product):
    product = memory_product
    grant = product.principal.grant
    first = await product.memory.save(grant, note())
    assert await product.memory.save(grant, note()) == first
    with pytest.raises(HTTPException) as conflict:
        await product.memory.save(grant, note(content="Different content"))
    assert conflict.value.status_code == 409
    with pytest.raises(HTTPException) as stale:
        await product.memory.save(grant, note(request_id="new-owner-request", content="New text"))
    assert stale.value.status_code == 409
    second = await product.memory.save(
        grant, note(revision=1, request_id="updated-owner-note", content="Preserve project formatting."), note_id=first["id"],
    )
    assert second["revision"] == 2
    assert await product.memory.forget(grant, first["id"], 2) == {"forgotten": True}
    assert (await product.memory.listing(grant))["memories"] == []
    with pytest.raises(HTTPException):
        await product.memory.save(grant, note(revision=3, request_id="cannot-resurrect"))
    async with product.source.connect() as db:
        row = (await db.execute(text("SELECT * FROM moyai.personal_memories"))).mappings().one()
        assert row["encrypted"] == "" and row["deleted"] == 1 and row["revision"] == 3
        assert await db.scalar(text("SELECT count(*) FROM moyai.memory_operations")) == 2


async def test_manual_note_cannot_be_replaced_by_observation_and_content_secret_guard_is_reused(memory_product):
    product = memory_product
    first = await product.memory.save(product.principal.grant, note())
    with pytest.raises(HTTPException) as denied:
        async with product.memory.writer(product.principal.grant) as (db, owner):
            await product.memory.save_in(
                db, owner, note(kind="project", revision=1, request_id="agent-observation"),
                source={"type": "observation", "scope": "local formatter", "evidence": "Checked existing config."},
                note_id=first["id"],
            )
    assert denied.value.status_code == 409
    with pytest.raises(HTTPException) as secret:
        await product.memory.save(product.principal.grant, note(
            key="invalid-secret", content="api_key=synthetic_secret_value", request_id="invalid-secret-note",
        ))
    assert secret.value.status_code == 422 and "synthetic_secret_value" not in str(secret.value.detail)
    assert (await product.memory.listing(product.principal.grant))["memories"][0]["content"] == note().content


async def test_preferences_revision_and_disabled_memory_remain_manually_manageable(memory_product):
    product = memory_product
    result = await product.memory.preferences(
        product.principal.grant, Preferences(enabled=False, auto_save=False, revision=0),
    )
    assert result == {"enabled": False, "auto_save": False, "revision": 1}
    with pytest.raises(HTTPException) as stale:
        await product.memory.preferences(product.principal.grant, Preferences(revision=0))
    assert stale.value.status_code == 409
    await product.memory.save(product.principal.grant, note())
    response = await product.memory.listing(product.principal.grant)
    assert response["capabilities"]["create"] and len(response["memories"]) == 1
    assert not response["runtime"]["recall"] and not response["runtime"]["auto_save"]


async def test_other_owner_admin_and_shared_login_cannot_access_personal_library(memory_product):
    product = memory_product
    saved = await product.memory.save(product.principal.grant, note())
    async with product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('google:other','google','other@example.test','Other',NULL,'c','u'),"
            "('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    other = await product.adapter.current("google:other")
    shared = await product.adapter.current("shared:password:admin")
    async with product.database.sessions.begin() as db:
        await product.adapter.project_native_user(db, other)
        await product.adapter.project_native_user(db, shared)
    assert other.role == "admin"
    assert (await product.memory.listing(other.grant))["memories"] == []
    with pytest.raises(HTTPException) as hidden:
        await product.memory.forget(other.grant, saved["id"], 1)
    assert hidden.value.status_code == 404
    unavailable = await product.memory.listing(shared.grant)
    assert ProductMemoryLibrary.model_validate(unavailable).scope is None
    assert unavailable["memories"] == [] and not unavailable["capabilities"]["read"]
    assert "shared sign-in" in unavailable["note"]
    with pytest.raises(HTTPException) as denied:
        await product.memory.save(shared.grant, note(request_id="shared-write"))
    assert denied.value.status_code == 403


async def test_revocation_between_request_and_writer_is_rechecked_in_same_transaction(memory_product):
    product = memory_product
    async with product.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == product.user_id).values(auth_version=2))
    with pytest.raises(HTTPException) as denied:
        await product.memory.save(product.principal.grant, note())
    assert denied.value.status_code == 401
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 0


async def test_manual_repository_ids_are_not_silently_mapped_and_project_expiry_preserved(memory_product):
    product = memory_product
    with pytest.raises(HTTPException) as unknown:
        await product.memory.save(product.principal.grant, note(repository_id=999))
    assert unknown.value.status_code == 422
    saved = await product.memory.save(product.principal.grant, note(
        kind="project", repo_url="https://github.com/Owner/Repo.git",
    ))
    row = (await product.memory.listing(product.principal.grant))["memories"][0]
    assert row["id"] == saved["id"] and row["repo_url"] == "https://github.com/owner/repo"
    assert row["expires_at"] and row["source"]["type"] == "manual"
