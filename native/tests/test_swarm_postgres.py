"""Swarm schema and concurrent admission against disposable real PostgreSQL."""

import asyncio
import os

import pytest
import test_product_authority as fixtures
from fastapi import HTTPException
from sqlalchemy import func, select, text
from test_product_authority import demote

from moyai.models import ProductTurnAuthority, Session, Turn
from moyai.product_schema import (
    MIGRATIONS,
    SWARM_SCHEMA_VERSION,
    SWARM_TABLES,
    migrate_product_schema,
    verify_product_schema,
)
from moyai.swarm_contracts import NewSwarm
from moyai.swarm_models import SwarmMember, SwarmMission
from moyai.swarm_tools import operate

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product


async def receipt_rows(product):
    async with product.source.connect() as db:
        return dict((await db.execute(text(
            f'SELECT version,row_to_json(r)::text FROM "{product.schema}".moyai_app_schema_versions r',
        ))).all())


async def test_swarm_schema_preserves_prior_receipts_and_requires_explicit_provisioning(product_database):
    product = product_database
    versions = [version for version, _ in MIGRATIONS]
    target_index = versions.index(SWARM_SCHEMA_VERSION)
    # Other additive migrations may precede this historical step. Include their
    # receipts before checking that the swarm migration adds only its own.
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai",
        version=versions[target_index - 1],
    )
    before = await receipt_rows(product)
    assert len(before) == target_index
    async with product.source.connect() as db:
        for name in SWARM_TABLES:
            assert await db.scalar(text("SELECT to_regclass(:name)"), {"name": f"{product.schema}.{name}"}) is None
    assert await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai",
        version=SWARM_SCHEMA_VERSION,
    ) == SWARM_SCHEMA_VERSION
    after = await receipt_rows(product)
    assert set(after) == {*before, SWARM_SCHEMA_VERSION}
    assert all(after[key] == value for key, value in before.items())
    await verify_product_schema(product.source, schema=product.schema, version=SWARM_SCHEMA_VERSION)
    await migrate_product_schema(
        product.source, schema=product.schema, source_schema="moyai", version=SWARM_SCHEMA_VERSION,
    )
    assert await receipt_rows(product) == after


async def test_concurrent_product_start_lead_claim_and_original_authority(admitted_product):
    product = admitted_product
    service = product.database.swarms
    body = NewSwarm(prompt="Investigate one shared goal.", workers=2, request_key="same-admission")
    one, two = await asyncio.gather(service.start(product.actor, body), service.start(product.actor, body))
    assert one[0].id == two[0].id
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 1
        assert await db.scalar(select(func.count()).select_from(Session)) == 3
        assert await db.scalar(select(func.count()).select_from(SwarmMember)) == 3
        grants = list(await db.scalars(select(ProductTurnAuthority)))
        assert len(grants) == 3
        assert all(grant.authority_version == product.principal.authority_version for grant in grants)
    turns = await product.database.claim_batch(3)
    prepared = [await product.database.prepare(turn.id) for turn in turns if turn.session_id != one[0].id]
    claims = await asyncio.gather(*(
        operate(service, value.scope, value.scope.turn_id, "claim", "claim", {"plan": f"Plan {index}"})
        for index, value in enumerate(prepared)
    ))
    assert sum(claim["claimed"] for claim in claims) == 1
    await demote(product)
    with pytest.raises((HTTPException, PermissionError)):
        await product.database.authorize_scope(prepared[0].scope)
    async with product.database.sessions() as db:
        mission = await db.get(SwarmMission, one[0].id)
        with pytest.raises((HTTPException, PermissionError)):
            await service.saved_actor(db, mission)
        assert await db.scalar(select(func.count()).select_from(Turn)) == 3
