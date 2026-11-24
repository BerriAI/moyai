"""Real PostgreSQL contention at responsibility admission and inference fences."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update
from test_dispatch_capacity import postgres_database
from test_product_inference import admitted_product, inference, perform
from test_product_role_authority import role
from test_responsibilities import assignment, enroll, source_answer, tick, worker

from moyai.contracts import NewMessage
from moyai.models import Session, Turn
from moyai.product_role_authority import ProductOwnershipLost
from moyai.responsibility_contracts import ResponsibilityControl
from moyai.responsibility_models import Responsibility, ResponsibilityAssignment

__all__ = ["admitted_product", "inference", "postgres_database", "role"]


def harness(database):
    return SimpleNamespace(database=database, admission=database.admission, security=database.security)


async def test_source_submission_and_review_share_user_before_source_lock(postgres_database, monkeypatch):
    database, actor = postgres_database
    runtime = harness(database)
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    async with database.sessions.begin() as db:
        await db.execute(update(Responsibility).values(next_check_at="2000"))
    submission_locked, release_submission, wake_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_session = database.allowed_session
    original_authority = database.responsibilities.owner_authority
    submitter = waker = None

    async def session_barrier(db, identity, owner, **kwargs):
        if asyncio.current_task() is submitter:
            # Admission already holds User, but has not acquired Session.
            submission_locked.set()
            await release_submission.wait()
        return await original_session(db, identity, owner, **kwargs)

    async def authority_barrier(*args, **kwargs):
        if asyncio.current_task() is waker:
            wake_started.set()
        return await original_authority(*args, **kwargs)

    monkeypatch.setattr(database, "allowed_session", session_barrier)
    monkeypatch.setattr(database.responsibilities, "owner_authority", authority_barrier)
    submitter = asyncio.create_task(database.admission.submit(actor, source.id, NewMessage(
        prompt="Another owner message", request_key="concurrent",
    )))
    try:
        await asyncio.wait_for(submission_locked.wait(), 3)
        waker = asyncio.create_task(database.responsibilities.tick(row.id))
        await asyncio.wait_for(wake_started.wait(), 3)
        # A wake waiting for this same User cannot own the source lock. NOWAIT
        # proves the absence of the reverse lock edge using PostgreSQL itself.
        async with database.sessions.begin() as db:
            await db.execute(select(Session).where(Session.id == source.id).with_for_update(nowait=True))
    finally:
        release_submission.set()
        await asyncio.wait_for(asyncio.gather(*(task for task in [submitter, waker] if task)), 5)
    assert await assignment(runtime, row.id)


async def test_locked_due_page_cannot_starve_later_responsibility(postgres_database):
    database, actor = postgres_database
    runtime = harness(database)
    source, first = await enroll(runtime, actor)
    rows = [first]
    for _ in range(10):
        _, row = await enroll(runtime, actor, source)
        rows.append(row)
    await source_answer(runtime, actor, source)
    async with database.sessions.begin() as db:
        for index, row in enumerate(rows):
            await db.execute(update(Responsibility).where(Responsibility.id == row.id).values(next_check_at=f"2000-{index:02}"))
    async with database.sessions.begin() as holder:
        await holder.execute(select(Responsibility).where(
            Responsibility.id.in_([row.id for row in rows[:10]]),
        ).with_for_update())
        for _ in range(3):
            await asyncio.wait_for(database.responsibilities.reconcile(), 3)
        assert await assignment(runtime, rows[-1].id)
    assert all([await assignment(runtime, row.id) is None for row in rows[:10]])


async def test_failed_page_backs_off_so_later_page_progresses(postgres_database, monkeypatch):
    database, actor = postgres_database
    runtime = harness(database)
    bad_source, first = await enroll(runtime, actor)
    rows = [first]
    for _ in range(9):
        _, row = await enroll(runtime, actor, bad_source)
        rows.append(row)
    good_source, good = await enroll(runtime, actor)
    await source_answer(runtime, actor, good_source)
    async with database.sessions.begin() as db:
        for index, row in enumerate([*rows, good]):
            await db.execute(update(Responsibility).where(Responsibility.id == row.id).values(next_check_at=f"2000-{index:02}"))
    original = database.responsibilities.source

    async def unavailable(db, owner, identity, **kwargs):
        if identity == bad_source.id:
            raise RuntimeError("Temporary source read failure")
        return await original(db, owner, identity, **kwargs)

    monkeypatch.setattr(database.responsibilities, "source", unavailable)
    await database.responsibilities.reconcile()
    await database.responsibilities.reconcile()
    assert await assignment(runtime, good.id)
    async with database.sessions() as db:
        assert all(row.next_check_at > "2000" for row in await db.scalars(
            select(Responsibility).where(Responsibility.id.in_([row.id for row in rows])),
        ))


@pytest.mark.parametrize("newer", ["successful_tick", "pause_resume"])
async def test_delayed_failure_cannot_overwrite_newer_schedule(postgres_database, monkeypatch, newer):
    database, actor = postgres_database
    runtime = harness(database)
    _, row = await enroll(runtime, actor)
    async with database.sessions.begin() as db:
        await db.execute(update(Responsibility).values(next_check_at="2000"))
    failing, release = asyncio.Event(), asyncio.Event()
    original = database.responsibilities.tick

    async def delayed_failure(*args, **kwargs):
        failing.set()
        await release.wait()
        raise RuntimeError("Late transient failure")

    monkeypatch.setattr(database.responsibilities, "tick", delayed_failure)
    scanning = asyncio.create_task(database.responsibilities.reconcile())
    try:
        await asyncio.wait_for(failing.wait(), 3)
        if newer == "successful_tick":
            await original(row.id)
        else:
            for action, generation in [("pause", 1), ("resume", 2)]:
                await database.responsibilities.control(actor, row.id, ResponsibilityControl(
                    action=action, generation=generation,
                ))
        current = (await database.responsibilities.list(actor))[0]
    finally:
        release.set()
        await asyncio.wait_for(scanning, 3)
    after = (await database.responsibilities.list(actor))[0]
    assert after.next_check_at == current.next_check_at and after.generation == current.generation


@pytest.mark.parametrize("role_kind", ["api", "worker", "coordinator", "broker"])
async def test_stale_production_role_cannot_admit_claim_or_prepare_review(postgres_database, role, role_kind):
    database, actor = postgres_database
    runtime = harness(database)
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding, prepared, _ = await worker(runtime, row.id)
    original_sessions = database.sessions
    if role_kind != "api":
        await role.admin.execute(
            f'CREATE TABLE "{role.schema}".runtime_{role_kind}_policy AS '
            f'SELECT * FROM "{role.schema}".runtime_api_policy',
        )
    database.sessions = replace(role.authority, role=role_kind).sessions(database.engine)
    assert await database.admission.prepare(binding.turn_id) == prepared
    await role.admin.execute(f'UPDATE "{role.schema}".runtime_{role_kind}_policy SET fingerprint=$1', "c" * 64)
    try:
        for operation in [
            database.responsibilities.tick(row.id), database.claim_batch(10),
            database.admission.prepare(binding.turn_id),
        ]:
            with pytest.raises(ProductOwnershipLost):
                await operation
        async with original_sessions() as db:
            assert (await db.get(Turn, binding.turn_id)).status == "running"
    finally:
        database.sessions = original_sessions


async def test_source_deletion_waits_for_broker_receipt_and_then_denies_new_inference(inference, monkeypatch):
    ctx = inference
    database, actor = ctx.product.database, ctx.product.actor
    runtime = harness(database)
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    _, prepared, _ = await worker(runtime, row.id)
    ctx.scope = prepared.scope
    source_locked, release_broker = asyncio.Event(), asyncio.Event()
    delete_pid = asyncio.get_running_loop().create_future()
    original_source = database.responsibilities.source
    original_admission = database._admission_session
    broker = deleter = None

    async def source_barrier(*args, **kwargs):
        result = await original_source(*args, **kwargs)
        if asyncio.current_task() is broker and kwargs.get("lock"):
            source_locked.set()
            await release_broker.wait()
        return result

    @asynccontextmanager
    async def admission_barrier(db):
        async with original_admission(db) as transaction:
            if asyncio.current_task() is deleter:
                delete_pid.set_result(await transaction.scalar(text("SELECT pg_backend_pid()")))
            yield transaction

    monkeypatch.setattr(database.responsibilities, "source", source_barrier)
    monkeypatch.setattr(database, "_admission_session", admission_barrier)
    broker = asyncio.create_task(perform(ctx, logical="responsibility-one"))
    try:
        await asyncio.wait_for(source_locked.wait(), 5)
        deleter = asyncio.create_task(database.delete_session(actor, source.id))
        pid = await asyncio.wait_for(delete_pid, 3)
        async with database.sessions() as db:
            for _ in range(100):
                if await db.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("Deletion did not wait on the actual source fence.")
        assert not deleter.done()
    finally:
        release_broker.set()
        await asyncio.wait_for(asyncio.gather(*(task for task in [broker, deleter] if task)), 10)
    calls_before = len(ctx.calls)
    with pytest.raises(Exception, match="Conversation not found|active private conversation"):
        await perform(ctx, logical="responsibility-after-delete")
    assert len(ctx.calls) == calls_before


async def test_settlement_winning_expiry_race_publishes_once(postgres_database, monkeypatch):
    database, actor = postgres_database
    runtime = harness(database)
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    binding, prepared, decision = await worker(runtime, row.id)
    assert await database.save_result(
        prepared.scope, conversation='{"messages":[]}', output=decision, workspace=None, usage={},
    )
    async with database.sessions.begin() as db:
        await db.execute(update(Responsibility).values(next_check_at="2000"))
        await db.execute(update(ResponsibilityAssignment).where(
            ResponsibilityAssignment.turn_id == binding.turn_id,
        ).values(expires_at="2000"))
    pids = asyncio.Queue()
    original_admission = database._admission_session

    @asynccontextmanager
    async def admission_barrier(db):
        async with original_admission(db) as transaction:
            await pids.put(await transaction.scalar(text("SELECT pg_backend_pid()")))
            yield transaction

    monkeypatch.setattr(database, "_admission_session", admission_barrier)

    async def wait_for_lock():
        pid = await asyncio.wait_for(pids.get(), 3)
        async with database.sessions() as db:
            for _ in range(100):
                if await db.scalar(text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}):
                    return
                await asyncio.sleep(0.01)
        pytest.fail("The contender did not reach the actual session lock.")

    settling = waking = None
    try:
        async with database.sessions.begin() as holder:
            await holder.execute(select(Session).where(
                Session.id == prepared.scope.session_id,
            ).with_for_update())
            settling = asyncio.create_task(database.settle(binding.turn_id))
            await wait_for_lock()
            waking = asyncio.create_task(database.responsibilities.tick(row.id))
            await wait_for_lock()
    finally:
        await asyncio.wait_for(asyncio.gather(*(task for task in [settling, waking] if task)), 5)
    async with database.sessions() as db:
        assert (await db.get(Turn, binding.turn_id)).status == "completed"
    assert len(await database.responsibilities.inbox(actor)) == 1
    await tick(runtime, row.id)
    assert len(await database.responsibilities.inbox(actor)) == 1
