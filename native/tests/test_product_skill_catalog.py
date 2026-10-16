"""Read-only source skill compatibility with real ciphertext and revocation boundaries."""

import asyncio
import hashlib
import json
from uuid import uuid4

import pytest
import pytest_asyncio
import test_product_identity as identity_fixtures
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import text

from moyai.product_skill_catalog import ProductSkillCatalog
from moyai.security import Security

product = identity_fixtures.product


@pytest_asyncio.fixture
async def skill_product(product):
    async with product.writer.begin() as db:
        await db.execute(text(
            "CREATE TABLE skills (id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,"
            "encrypted TEXT NOT NULL,scope TEXT NOT NULL,owner_id TEXT NOT NULL,namespace TEXT NOT NULL,"
            "revision INTEGER NOT NULL,archived INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "client_id TEXT NOT NULL,icon TEXT NOT NULL)"
        ))
        await db.execute(text(
            "CREATE TABLE skill_bundles (skill_id TEXT NOT NULL,revision INTEGER NOT NULL,"
            "encrypted TEXT NOT NULL,manifest TEXT NOT NULL,PRIMARY KEY(skill_id,revision))"
        ))
        personal, organization, other = (uuid4().hex for _ in range(3))
        for skill_id, scope, owner, name in (
            (personal, "personal", "google:admin-sub", "owner-skill"),
            (organization, "organization", "google:member-sub", "team-skill"),
            (other, "personal", "google:member-sub", "other-private"),
        ):
            await db.execute(text(
                "INSERT INTO skills VALUES (:id,:name,'Original description',:encrypted,:scope,:owner,"
                ":namespace,3,0,'created','updated',:id,'code')"
            ), {
                "id": skill_id, "name": name, "encrypted": product.adapter.legacy_security.encrypt("Original " + name),
                "scope": scope, "owner": owner, "namespace": "organization" if scope == "organization" else owner,
            })
        content = "Original supporting file.\n漢🙂\n"
        manifest = [{
            "path": "references/runbook.md", "size": len(content.encode()), "characters": len(content),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }]
        await db.execute(text(
            "INSERT INTO skill_bundles VALUES (:id,2,:encrypted,:manifest)"
        ), {
            "id": personal, "encrypted": product.adapter.legacy_security.encrypt(json.dumps({"references/runbook.md": content})),
            "manifest": json.dumps(manifest),
        })
    product.catalog = ProductSkillCatalog(product.adapter)
    product.principal = await product.adapter.current("google:admin-sub")
    product.personal, product.organization, product.other = personal, organization, other
    product.supporting_content = content
    return product


async def test_source_library_files_and_ciphertext_are_preserved_without_conversion(skill_product):
    product = skill_product
    async with product.writer.connect() as db:
        before = (await db.execute(text("SELECT * FROM skills ORDER BY id"))).all()
    listing = await product.catalog.listing(product.principal.grant)
    assert {row["id"] for row in listing["skills"]} == {product.personal, product.organization}
    assert not listing["can_publish"] and not listing["capabilities"]["update"]
    assert all(not row["can_manage"] for row in listing["skills"])
    detail = await product.catalog.detail(product.principal.grant, product.personal, revision=3)
    assert detail["instructions"] == "Original owner-skill"
    assert detail["files"][0]["path"] == "references/runbook.md"
    assert await product.catalog.file(
        product.principal.grant, product.personal, "references/runbook.md", revision=3,
    ) == product.supporting_content
    async with product.writer.connect() as db:
        assert (await db.execute(text("SELECT * FROM skills ORDER BY id"))).all() == before
        assert await db.scalar(text("SELECT count(*) FROM skill_bundles")) == 1


async def test_admin_has_no_private_skill_bypass_and_archived_history_is_explicit(skill_product):
    product = skill_product
    with pytest.raises(HTTPException) as hidden:
        await product.catalog.detail(product.principal.grant, product.other)
    assert hidden.value.status_code == 404
    async with product.writer.begin() as db:
        await db.execute(text("UPDATE skills SET archived=1,revision=4 WHERE id=:id"), {"id": product.personal})
    assert [row["id"] for row in (await product.catalog.listing(product.principal.grant))["skills"]] == [
        product.organization,
    ]
    archived = await product.catalog.listing(product.principal.grant, archived=True)
    assert next(row for row in archived["skills"] if row["id"] == product.personal)["archived"]
    with pytest.raises(HTTPException) as stale:
        await product.catalog.detail(product.principal.grant, product.personal, revision=3)
    assert stale.value.status_code == 409


@pytest.mark.parametrize("mutation", ["scope", "revision", "role"])
async def test_detail_rechecks_authority_and_source_after_bundle_await(skill_product, monkeypatch, mutation):
    product = skill_product
    read, release = asyncio.Event(), asyncio.Event()
    original = product.catalog._bundle

    async def delayed(row):
        result = await original(row)
        read.set()
        await release.wait()
        return result

    monkeypatch.setattr(product.catalog, "_bundle", delayed)
    pending = asyncio.create_task(product.catalog.detail(product.principal.grant, product.personal))
    try:
        await asyncio.wait_for(read.wait(), 5)
        async with product.writer.begin() as db:
            if mutation == "scope":
                await db.execute(text("UPDATE skills SET owner_id='google:member-sub',revision=4 WHERE id=:id"), {
                    "id": product.personal,
                })
            elif mutation == "revision":
                await db.execute(text("UPDATE skills SET revision=4 WHERE id=:id"), {"id": product.personal})
            else:
                await db.execute(text(
                    "INSERT INTO user_roles VALUES ('admin@example.test','member',1,'other@example.test','now')"
                ))
        release.set()
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(pending, 5)
        assert denied.value.status_code == {"scope": 404, "revision": 409, "role": 401}[mutation]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_supporting_file_manifest_and_source_cipher_fail_closed(skill_product):
    product = skill_product
    async with product.writer.begin() as db:
        await db.execute(text("UPDATE skill_bundles SET manifest='[]' WHERE skill_id=:id"), {"id": product.personal})
    with pytest.raises(HTTPException) as corrupt:
        await product.catalog.file(product.principal.grant, product.personal, "references/runbook.md")
    assert corrupt.value.status_code == 409
    async with product.writer.begin() as db:
        await db.execute(text("UPDATE skills SET encrypted=:cipher WHERE id=:id"), {
            "id": product.organization, "cipher": Security(Fernet.generate_key()).seal("Not a legacy ciphertext"),
        })
    with pytest.raises(HTTPException) as wrong_format:
        await product.catalog.detail(product.principal.grant, product.organization)
    assert wrong_format.value.status_code == 503
    assert "Not a legacy ciphertext" not in str(wrong_format.value.detail)


@pytest.mark.parametrize("path", ["../secret", "/absolute", ".hidden", "references/../../secret", "references\\secret"])
async def test_supporting_files_reject_unknown_or_escaping_paths(skill_product, path):
    with pytest.raises(HTTPException) as hidden:
        await skill_product.catalog.file(skill_product.principal.grant, skill_product.personal, path)
    assert hidden.value.status_code == 404
