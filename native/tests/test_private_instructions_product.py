"""Canonical individual/shared identity and live PostgreSQL scope fences."""

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest
import test_product_authority as authority_fixtures
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from test_product_authority import active_turn, demote

from moyai.models import Session, User
from moyai.preferences.settings_api import routes
from moyai.private_instructions import PrivateInstructions, preferences_for_user

admitted_product = authority_fixtures.admitted_product

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")


async def store(product, actor):
    async with product.database.sessions.begin() as db:
        (await db.get(User, actor.id)).preferences = json.dumps({
            "private_instructions": {"revision": 1, "sealed": product.database.security.seal({
                "owner_id": actor.id, "text": "PRIVATE_CANONICAL_TEST",
            })},
        })


async def test_preference_read_does_not_take_canonical_admission_gate(admitted_product):
    product = admitted_product
    await store(product, product.actor)
    async with product.source.begin() as writer:
        await writer.execute(text(
            "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
        ))
        result = await asyncio.wait_for(preferences_for_user(product.database, product.actor), timeout=3)
        assert result.personal_instructions == "PRIVATE_CANONICAL_TEST"


async def test_canonical_shared_principal_cannot_read_or_write_instructions(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    principal = await product.adapter.current("shared:password:admin")
    async with product.database.sessions.begin() as db:
        actor = await product.adapter.project_native_user(db, principal)
    await store(product, actor)
    assert (await preferences_for_user(product.database, actor)).personal_instructions == ""
    app = FastAPI()

    async def requester():
        return actor

    app.include_router(routes(SimpleNamespace(
        database=product.database, security=product.database.security,
    ), requester))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/settings/general")).json()["personal_instructions"] == ""
        response = await client.patch("/api/settings/general", json={
            "personal_instructions": "Unapproved shared preference", "personal_instructions_revision": 1,
        })
        assert response.status_code == 403


@pytest.mark.parametrize("change", ["disabled", "revoked"])
async def test_canonical_disabled_or_revoked_individual_cannot_load(admitted_product, change):
    product = admitted_product
    await store(product, product.actor)
    _, _, prepared = await active_turn(product, private=True)
    capability = PrivateInstructions(product.database)
    assert await capability.load(prepared.scope) == "PRIVATE_CANONICAL_TEST"
    if change == "revoked":
        await demote(product)
    else:
        async with product.database.sessions.begin() as db:
            (await db.get(User, product.actor.id)).enabled = False
    with pytest.raises((PermissionError, HTTPException)):
        await capability.load(prepared.scope)
    with pytest.raises((PermissionError, HTTPException)):
        await preferences_for_user(product.database, product.actor)


@pytest.mark.parametrize("change", ["shared", "clear"])
async def test_final_postgres_read_does_not_release_stale_private_text(admitted_product, monkeypatch, change):
    product = admitted_product
    await store(product, product.actor)
    session, _, prepared = await active_turn(product, private=True)
    original = product.database.human_origin

    async def changed(db, scope):
        origin = await original(db, scope)
        async with product.database.sessions.begin() as writer:
            if change == "shared":
                (await writer.get(Session, session.id)).visibility = "shared"
            else:
                row = await writer.get(User, product.actor.id)
                document = json.loads(row.preferences)
                document["private_instructions"] = {"revision": 2, "sealed": ""}
                row.preferences = json.dumps(document)
        return origin

    monkeypatch.setattr(product.database, "human_origin", changed)
    assert await PrivateInstructions(product.database).load(prepared.scope) == ""


async def test_canonical_verified_approval_resume_retains_private_preferences(admitted_product):
    product = admitted_product
    await store(product, product.actor)
    session, turn, prepared = await active_turn(product, private=True)
    assert await product.database.save_result(
        prepared.scope, conversation="{}", output="Approval required", workspace=None, usage={}, deferred=True,
    )
    resumed = await product.database.resume(product.actor, session.id, turn.id, "approval", {})
    assert (await product.database.claim_next()).id == resumed.id
    scope = (await product.database.admission.prepare(resumed.id)).scope
    assert await PrivateInstructions(product.database).load(scope) == "PRIVATE_CANONICAL_TEST"
