"""Product authority and cross-process admission on a disposable PostgreSQL DB."""

import asyncio
import os

import pytest
import test_product_authority as authority
from sqlalchemy import func, select
from test_goals import report_complete

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


async def test_pause_racing_dispatch_never_starts_work_after_pause_commits(admitted_product):
    product = admitted_product
    database = product.database
    for index in range(5):
        session, turn_id = await start(product)
        await asyncio.gather(
            database.claim_next(),
            database.goals.command(product.actor, session.id, NewMessage(
                prompt="/goal pause", request_key=f"pause-{index}",
            )),
        )
        async with database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            assert turn.status in {"queued", "stopped"}
            assert (await db.get(Goal, session.id)).status == "paused"


@pytest.mark.parametrize("mode", ["asap", "send_now"])
async def test_concurrent_ticks_complete_consumed_steering_without_duplicate_work(admitted_product, mode):
    product = admitted_product
    database = product.database
    session, initial = await start(product)
    await database.claim_next()
    prepared = await database.prepare(initial)
    correction = await database.submit(product.actor, session.id, NewMessage(
        prompt="Verify the checksum too.", request_key="correction",
        mode="asap" if mode == "asap" else "when_idle",
    ))
    if mode == "send_now":
        await database.steer_queued(product.actor, correction.id)
    await database.steering_receipt(correction.id, "accepted-correction")
    await report_complete(product, prepared, input_id=correction.id)
    await database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="Verified all requested outcomes.",
        workspace=None, usage={"input_tokens": 8, "output_tokens": 3},
    )
    await database.settle(initial)
    await asyncio.gather(*(database.goals.tick(session.id) for _ in range(3)))
    async with database.sessions() as db:
        saved = await db.get(Goal, session.id)
        assert saved.status == "complete" and saved.turns == 1 and saved.tokens_used == 11
        assert (await db.get(Turn, correction.id)).status == "included"
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2
    assert await database.claim_next() is None
