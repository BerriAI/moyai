"""Cross-process admission fences and real canonical connector selection."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_product_automations as fixtures
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from sqlalchemy import select, text, update
from test_delegated_sessions import finish
from test_product_authority import active_turn

from moyai.agent_messages import (
    DELIVERED,
    PENDING,
    SIGNALLED,
    AgentMessages,
    MailDispatch,
    dispatch_mail,
    model_message,
)
from moyai.contracts import GuidanceChanged, NewMessage
from moyai.delegation import DelegationService
from moyai.delegation_models import DelegatedSession
from moyai.models import Event, Session, Turn, User

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
admitted_product = fixtures.admitted_product
automations = fixtures.automations
canonical_connections = fixtures.canonical_connections


async def test_message_retry_serializes_and_completion_follows_progress(admitted_product):
    product = admitted_product
    database = product.database
    _, parent_turn, parent = await active_turn(product)
    parent.scope.agent_messages = True
    child = await DelegationService(database).start(parent.scope, "child", "Work")
    child_turn = await database.claim_next()
    child_context = await database.prepare(child_turn.id)
    mail = AgentMessages(database)
    receipts = await asyncio.gather(*(
        mail.send(parent.scope, "same-call", child["session_id"], "Direction", parent_turn.id) for _ in range(2)
    ))
    assert receipts[0] == receipts[1]
    progress = await mail.send(child_context.scope, "progress", "parent", "Finding", child_turn.id)
    await finish(product, child_context, "Final evidence")
    signal = AsyncMock()
    dispatch_runtime = SimpleNamespace(database=database, connect=AsyncMock(return_value=SimpleNamespace(
        get_workflow_handle=lambda _: SimpleNamespace(signal=signal),
    )))
    await dispatch_mail(dispatch_runtime, MailDispatch())
    await dispatch_mail(dispatch_runtime, MailDispatch())
    assert signal.call_count == 2
    assert signal.call_args_list[0].args == ("agent_message", progress["message_id"])
    completion = await mail.read(parent.scope, signal.call_args_list[1].args[1])
    assert completion.kind == "completion" and completion.content == "Final evidence"


async def test_resume_mail_reconciliation_rolls_back_and_serializes_with_admission(admitted_product):
    product = admitted_product
    database = product.database
    session, turn, prepared = await active_turn(product)
    mail = AgentMessages(database)
    await DelegationService(database).start(prepared.scope, "child", "Work")
    child_turn = await database.claim_next()
    child = await database.prepare(child_turn.id)
    consumed, unread = [
        await mail.send(child.scope, label, "parent", label, child_turn.id)
        for label in ("consumed", "unread")
    ]
    conversation = ConversationTypeAdapter.dump_json(Conversation(
        conversation_id=prepared.scope.conversation_id,
        messages=[model_message(await mail.read(prepared.scope, consumed["message_id"]))],
    )).decode()
    assert await database.save_result(
        prepared.scope, conversation=conversation, output="", workspace=None, usage={}, deferred=True,
    )
    async with database.sessions.begin() as db:
        for receipt in (consumed, unread):
            (await db.get(Event, receipt["message_id"])).kind = SIGNALLED
    with pytest.raises(RuntimeError, match="Lost admission"):
        async with database.sessions.begin() as db:
            await database._resume(
                product.actor, session.id, turn.id, "rolled-back", {"calls": {}}, "rollback",
                transaction=db,
            )
            raise RuntimeError("Lost admission transaction")
    async with database.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "waiting"
        for receipt in (consumed, unread):
            row = await db.get(Event, receipt["message_id"])
            assert row.kind == SIGNALLED and row.turn_id == turn.id
    answers = await asyncio.gather(*(
        database.resume(product.actor, session.id, turn.id, "answer", {"calls": {}})
        for _ in range(2)
    ))
    assert answers[0].id == answers[1].id
    async with database.sessions() as db:
        seen = await db.get(Event, consumed["message_id"])
        pending = await db.get(Event, unread["message_id"])
        assert seen.kind == DELIVERED and seen.turn_id == turn.id
        assert pending.kind == PENDING and pending.turn_id == answers[0].id


async def test_correction_committing_before_admission_fence_rejects_old_model_work(admitted_product, monkeypatch):
    product = admitted_product
    database = product.database
    session, _, prepared = await active_turn(product)
    entered, release = asyncio.Event(), asyncio.Event()
    original = database._actor_product_grant

    async def pause(db, actor):
        value = await original(db, actor)
        if asyncio.current_task().get_name() == "delegation":
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(database, "_actor_product_grant", pause)
    task = asyncio.create_task(DelegationService(database).start(prepared.scope, "stale", "Obsolete work"), name="delegation")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        correction = await database.submit(product.actor, session.id, NewMessage(
            prompt="Cancel the delegation.", request_key="correction", mode="asap",
        ))
        await database.steering_receipt(correction.id, "accepted")
    finally:
        release.set()
    with pytest.raises(GuidanceChanged):
        await asyncio.wait_for(task, 5)
    async with database.sessions() as db:
        assert not list(await db.scalars(select(DelegatedSession)))
    child = await DelegationService(database).start(
        prepared.scope, "current", "Corrected work", input_id=correction.id,
    )
    assert child["session_id"]


@pytest.mark.parametrize("change", ["guidance", "stop", "disable"])
async def test_concurrent_retry_recovers_receipt_after_initial_lookup_misses(
    admitted_product, monkeypatch, change,
):
    product = admitted_product
    database = product.database
    session, _, prepared = await active_turn(product)
    entered, release = asyncio.Event(), asyncio.Event()
    original = database._actor_product_grant

    async def pause(db, actor):
        value = await original(db, actor)
        if asyncio.current_task().get_name() == "retry":
            entered.set()
            await release.wait()
        return value

    monkeypatch.setattr(database, "_actor_product_grant", pause)
    service = DelegationService(database)
    retry = asyncio.create_task(service.start(prepared.scope, "same", "Original work"), name="retry")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        first = await service.start(prepared.scope, "same", "Original work")
        if change == "guidance":
            correction = await database.submit(product.actor, session.id, NewMessage(
                prompt="Changed guidance", request_key="correction", mode="asap",
            ))
            await database.steering_receipt(correction.id, "accepted")
        elif change == "stop":
            await database.stop(product.actor, session.id)
        else:
            async with database.sessions.begin() as db:
                (await db.get(User, product.actor.id)).enabled = False
    finally:
        release.set()
    assert await asyncio.wait_for(retry, 5) == first
    async with database.sessions() as db:
        assert len(list(await db.scalars(select(DelegatedSession)))) == 1


@pytest.mark.parametrize("transition", ["receipt", "signal", "uncertain"])
async def test_steering_transitions_wait_for_session_fence(admitted_product, transition):
    product = admitted_product
    database = product.database
    session, _, _ = await active_turn(product)
    correction = await database.submit(product.actor, session.id, NewMessage(
        prompt="Correction", request_key="correction", mode="asap",
    ))
    if transition == "uncertain":
        await database.signal_attempted(correction.id)
    tasks = {
        "receipt": lambda: database.steering_receipt(correction.id, "accepted"),
        "signal": lambda: database.signal_attempted(correction.id),
        "uncertain": lambda: database.steering_uncertain(correction.id),
    }
    task = None
    try:
        async with database.sessions.begin() as holder:
            await holder.execute(update(Session).where(Session.id == session.id).values(revision=Session.revision))
            pid = await holder.scalar(text("SELECT pg_backend_pid()"))
            task = asyncio.create_task(tasks[transition]())
            async with asyncio.timeout(5):
                while not task.done():
                    await holder.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await holder.scalar(text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid=ANY(pg_blocking_pids(pid)))"
                    ), {"pid": pid})
                    if waiting:
                        break
                    await asyncio.sleep(.01)
                else:
                    pytest.fail("Steering changed without acquiring the session fence.")
            assert not task.done()
        await asyncio.wait_for(task, 5)
    finally:
        if task:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("surface", ["web", "slack"])
async def test_canonical_grants_survive_generated_resume_and_private_followup(
    automations, canonical_connections, surface,
):
    ctx = automations
    database = ctx.product.database
    await fixtures.save_connection(ctx, "slack", bot={"access_token": "synthetic-bot"})
    session, turn, _ = await active_turn(ctx.product)
    # Establish a fresh parent with real product connection resolution on the
    # transaction's canonical tables (no resolver stub or source pool fallback).
    await database.stop(ctx.product.actor, session.id)
    await database.stopped(turn.id)
    async with database.sessions.begin() as db:
        (await db.get(Session, session.id)).surface = surface
    if surface == "slack":
        await fixtures.bind_slack_test_session(database, session.id, ctx.product.actor)
    parent = await database.submit(ctx.product.actor, session.id, NewMessage(
        prompt="Delegate", request_key="connected", connections=["slack"],
    ))
    await database.claim_next()
    original = await database.prepare(parent.id)
    child = await DelegationService(database).start(original.scope, "child", "Bounded work")
    first = await database.claim_next()
    prepared = await database.prepare(first.id)
    assert prepared.scope.grants == original.scope.grants
    await database.authorize_scope(prepared.scope)
    assert await database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="", workspace=None, usage={}, deferred=True,
    )
    resumed = await database.resume(ctx.product.actor, child["session_id"], first.id, "answer", {"calls": {}})
    await database.claim_next()
    continuation = await database.prepare(resumed.id)
    assert continuation.scope.grants == original.scope.grants and continuation.scope.delegation_generated
    await finish(type("Runtime", (), {"database": database})(), continuation, "Result")
    follow = await database.submit(ctx.product.actor, child["session_id"], NewMessage(
        prompt="Explain", request_key="human", connections=[g.id for g in original.scope.grants],
    ))
    await database.claim_next()
    human = await database.prepare(follow.id)
    assert not human.scope.delegation_generated and human.scope.grants == original.scope.grants
    async with database.sessions.begin() as db:
        await db.execute(text("DELETE FROM moyai.connections WHERE provider='slack'"))
    with pytest.raises(HTTPException):
        await database.authorize_scope(human.scope)
