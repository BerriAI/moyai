"""Product authority and cross-process admission on a disposable PostgreSQL DB."""

import asyncio
import os

import pytest
import test_product_authority as authority
from sqlalchemy import func, select

from moyai.contracts import NewMessage, NewSession
from moyai.dispatch_wakeups import DispatchWakeups
from moyai.goal_models import Goal
from moyai.models import ProductTurnAuthority, Turn

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
admitted_product = authority.admitted_product


async def start(product):
    database = product.database
    session = await database.create_session(product.actor, NewSession())
    result = await database.goals.command(product.actor, session.id, NewMessage(
        prompt="/goal Verify all requested outcomes.", request_key="goal",
    ))
    return session, result["id"]


async def test_concurrent_product_ticks_preserve_original_authority(admitted_product):
    product = admitted_product
    database = product.database
    session, initial = await start(product)
    assert (await database.claim_next()).id == initial
    prepared = await database.prepare(initial)
    await database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="First part complete.",
        workspace=None, usage={"input_tokens": 5, "output_tokens": 4},
    )
    await database.settle(initial)
    await asyncio.gather(*(database.goals.tick(session.id) for _ in range(3)))
    async with database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2
        grants = list(await db.scalars(select(ProductTurnAuthority)))
        assert len(grants) == 2
        assert {grant.authority_version for grant in grants} == {product.principal.authority_version}
        assert (await db.get(Goal, session.id)).tokens_used == 9
    await authority.demote(product)
    await database.goals.tick(session.id)
    async with database.sessions() as db:
        assert (await db.get(Goal, session.id)).status == "blocked"
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2


async def test_idle_goal_inspection_does_not_wake_dispatcher_again(admitted_product):
    database = admitted_product.database
    session, _ = await start(admitted_product)
    async with DispatchWakeups(database.settings) as listener:
        await asyncio.wait_for(listener.ready.wait(), 5)
        listener.event.clear()
        await database.goals.tick(session.id)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(listener.event.wait(), .1)
