"""Real PostgreSQL writer/pool contention; no external inference requests."""

import asyncio
import os
from itertools import pairwise

import pytest
import test_product_authority as authority
from sqlalchemy import select, text

from moyai.models import Event, Turn
from moyai.performance import PREFIX, evidence, record_dispatch

admitted_product = authority.admitted_product
pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")


@pytest.mark.parametrize("contention", ["writer", "pool"])
async def test_dispatch_enqueue_keeps_loop_responsive_under_postgres_contention(admitted_product, contention):
    product = admitted_product
    database = product.database
    session, turn, _ = await authority.active_turn(product)
    held = []
    try:
        if contention == "writer":
            holder = await database.engine.connect()
            held.append(holder)
            await holder.execute(text("LOCK TABLE moyai_events IN ACCESS EXCLUSIVE MODE"))
        else:
            for _ in range(database.settings.database_pool_size):
                held.append(await database.engine.connect())
        record_dispatch(database, evidence(
            session.id, turn.id, "dispatch", "2026-10-16T10:00:04+00:00",
            broker_received_at="2026-10-16T10:00:03+00:00",
        ))
        ticks = []
        for _ in range(10):
            ticks.append(asyncio.get_running_loop().time())
            await asyncio.sleep(.02)
        assert database.dispatch_timings.pending  # Persistence is still waiting on the held resource.
        assert max(b - a for a, b in pairwise(ticks)) < .3
        if contention == "writer":
            # A timing-table writer does not consume the remaining connection pool.
            async with database.sessions() as db:
                assert await asyncio.wait_for(db.scalar(select(Turn.id).where(Turn.id == turn.id)), .5)
    finally:
        for connection in held:
            await connection.close()
    await asyncio.wait_for(asyncio.gather(*database.performance_tasks), 5)
    async with database.sessions() as db:
        row = await db.scalar(select(Event).where(Event.identity == f"{PREFIX}dispatch:{turn.id}"))
    assert row is not None and row.created_at == "2026-10-16T10:00:04+00:00"
