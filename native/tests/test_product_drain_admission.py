"""Real PostgreSQL admission races for the assembled native database hooks."""

import asyncio
import os
from uuid import uuid4

import pytest
import test_product_drain as drain_fixtures
from sqlalchemy import select
from test_product_authority import active_turn

from moyai.contracts import NewMessage, NewSession
from moyai.models import Turn

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.",
)
product_database = drain_fixtures.product_database
admitted_product = drain_fixtures.admitted_product
drain = drain_fixtures.drain


async def test_paused_dispatch_preserves_message_until_explicit_resume(drain):
    product, service = drain.product, drain.first.service
    database = product.database
    database.product_drain = service
    session = await database.create_session(product.actor, NewSession())
    turn = await database.submit(product.actor, session.id, NewMessage(prompt="Preserve me", request_key=str(uuid4())))
    assert await database.claim_next() is None
    async with database.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "queued"
    await service.request(paused=False, expected_generation=0, request_key="deploy-resume")
    assert (await database.claim_next()).id == turn.id


async def test_paused_new_and_saved_steering_remain_queued_with_intent(drain):
    product, service = drain.product, drain.first.service
    database = product.database
    database.product_drain = service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, active, _ = await active_turn(product)
    await service.request(paused=True, expected_generation=1, request_key="pause")
    immediate = await database.submit(product.actor, session.id, NewMessage(
        prompt="Steer me", mode="asap", request_key=str(uuid4()),
    ))
    saved = await database.submit(product.actor, session.id, NewMessage(prompt="Queued", request_key=str(uuid4())))
    promoted = await database.steer_queued(product.actor, saved.id)
    assert immediate.status == promoted.status == "queued"
    assert immediate.mode == promoted.mode == "asap"
    assert immediate.parent_turn is promoted.parent_turn is None
    await service.checkpoint(
        active.id, workflow_id=active.workflow_id, workflow_run_id=str(uuid4()), boundary="native_node:1",
    )
    await service.request(paused=False, expected_generation=2, request_key="resume")
    promoted = await database.steer_queued(product.actor, saved.id)
    assert promoted.status == "steering" and promoted.parent_turn == active.id


async def test_pause_waits_for_the_actual_steering_admission_commit(drain, monkeypatch):
    product, service = drain.product, drain.first.service
    database = product.database
    database.product_drain = service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, active, _ = await active_turn(product)
    entered, finish = asyncio.Event(), asyncio.Event()
    admit = service.admit

    async def held_admission(db):
        allowed = await admit(db)
        entered.set()
        await finish.wait()
        return allowed

    monkeypatch.setattr(service, "admit", held_admission)
    submission = asyncio.create_task(database.submit(product.actor, session.id, NewMessage(
        prompt="Before pause", mode="asap", request_key=str(uuid4()),
    )))
    await asyncio.wait_for(entered.wait(), 2)
    pause = asyncio.create_task(service.request(paused=True, expected_generation=1, request_key="pause"))
    try:
        done, _ = await asyncio.wait({pause}, timeout=.1)
        assert not done
    finally:
        finish.set()
    turn = await submission
    assert (await pause)["paused"]
    async with database.sessions() as db:
        stored = await db.scalar(select(Turn).where(Turn.id == turn.id))
        assert stored.status == "steering" and stored.parent_turn == active.id
