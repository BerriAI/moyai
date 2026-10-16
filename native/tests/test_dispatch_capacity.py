"""Durable batching and commit/rollback notification regressions."""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select
from temporalio.service import RPCError, RPCStatusCode

from moyai.contracts import NewMessage, NewSession
from moyai.database import Database
from moyai.dispatch_wakeups import DispatchWakeups, channel
from moyai.models import Turn, User
from moyai.security import Security
from moyai.settings import Settings
from moyai.worker import DispatchState, dispatch_once, dispatcher


async def submissions(database, actor, count):
    turns = []
    for index in range(count):
        session = await database.create_session(actor, NewSession())
        turns.append(await database.submit(
            actor, session.id, NewMessage(prompt=f"Task {index}", request_key=str(uuid4())),
        ))
    return turns


async def test_burst_drains_in_bounded_parallel_batches_without_waiting_for_worker_start(runtime, actor):
    runtime.settings.temporal_dispatch_batch_size = 7
    runtime.settings.temporal_dispatch_concurrency = 3
    turns = await submissions(runtime.database, actor, 37)
    active = peak = 0
    started = []

    async def start(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        started.append(kwargs["id"])
        try:
            await asyncio.sleep(0.01)
        finally:
            active -= 1

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    state = DispatchState()
    for _ in range(10):
        await dispatch_once(runtime, state)
        if not state.backlog:
            break
    assert not state.backlog
    assert peak == 3
    assert len(started) == len(turns)
    assert set(started) == {turn.workflow_id for turn in turns}
    # No execution worker consumed these rows. Paging must not repeatedly start
    # only the oldest batch while newly claimed turns remain undispatched.
    assert len(await runtime.database.starting_turns(limit=100)) == 37


async def test_dispatch_loop_does_not_poll_sleep_between_full_batches(runtime, actor, monkeypatch):
    import moyai.dispatch_wakeups as notifications

    monkeypatch.setattr(notifications, "FALLBACK_SECONDS", 30)
    runtime.settings.temporal_dispatch_batch_size = 4
    turns = await submissions(runtime.database, actor, 13)
    started = set()
    complete = asyncio.Event()

    async def start(*args, **kwargs):
        started.add(kwargs["id"])
        if len(started) == len(turns):
            complete.set()

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    task = asyncio.create_task(dispatcher(runtime))
    try:
        await asyncio.wait_for(complete.wait(), 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(started) == 13


async def test_batch_reserves_only_one_turn_per_session_and_fills_other_sessions(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    queued = [await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Serial", request_key=str(uuid4())),
    ) for _ in range(12)]
    others = await submissions(runtime.database, actor, 5)
    claimed = await runtime.database.claim_batch(6)
    assert len(claimed) == 6
    assert {turn.session_id for turn in claimed} == {session.id, *(turn.session_id for turn in others)}
    assert claimed[0].id == queued[0].id
    assert await runtime.database.claim_batch(6) == []


async def test_continuous_arrivals_cannot_starve_failed_starts_or_scan_tail(runtime, actor):
    runtime.settings.temporal_dispatch_batch_size = 2
    runtime.settings.temporal_dispatch_concurrency = 2
    initial = await submissions(runtime.database, actor, 6)
    attempts = Counter()

    async def start(*args, **kwargs):
        identity = kwargs["id"]
        attempts[identity] += 1
        if identity == initial[0].workflow_id and attempts[identity] == 1:
            raise RPCError("Temporary outage.", RPCStatusCode.UNAVAILABLE, b"")

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    state = DispatchState()
    arrivals = []
    retried_at = None
    for iteration in range(20):
        arrivals.extend(await submissions(runtime.database, actor, 2))
        await dispatch_once(runtime, state)
        if attempts[initial[0].workflow_id] >= 2 and retried_at is None:
            retried_at = iteration
    assert retried_at is not None and retried_at <= 5
    assert all(attempts[turn.workflow_id] > 0 for turn in initial)
    assert all(attempts[turn.workflow_id] > 0 for turn in arrivals[:6])


@pytest_asyncio.fixture
async def postgres_database(tmp_path):
    url = os.getenv("MOYAI_TEST_POSTGRES")
    if not url:
        pytest.skip("Disposable local PostgreSQL required.")
    schema = f"moyai_dispatch_{uuid4().hex}"
    admin = await asyncpg.connect(url)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    settings = Settings(
        _env_file=None, database_url=url, database_schema=schema, data_dir=tmp_path,
        encryption_key="dispatch-fixture-key-at-least-32-bytes", default_model="test",
    )
    database = Database(settings, Security(settings.master_key))
    try:
        await database.start()
        async with database.sessions.begin() as db:
            actor = User(username="dispatch", display_name="Dispatch", password="unused")
            db.add(actor)
            await db.flush()
        yield database, actor
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def test_postgres_submission_notifies_only_after_outer_commit_and_never_after_rollback(postgres_database):
    database, actor = postgres_database
    session = await database.create_session(actor, NewSession())
    async with DispatchWakeups(database.settings) as listener:
        await asyncio.wait_for(listener.ready.wait(), 5)
        listener.event.clear()
        async with database.sessions.begin() as db:
            committed = await database.submit(
                actor, session.id, NewMessage(prompt="Committed", request_key="commit"), db=db,
            )
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(listener.event.wait(), .05)
        await asyncio.wait_for(listener.event.wait(), 2)
        listener.event.clear()
        async with database.sessions() as db:
            await db.begin()
            rolled_back = await database.submit(
                actor, session.id, NewMessage(prompt="Discarded", request_key="rollback"), db=db,
            )
            await db.rollback()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(listener.event.wait(), .1)
        async with database.sessions() as db:
            assert await db.get(Turn, committed.id) is not None
            assert await db.get(Turn, rolled_back.id) is None


async def test_committed_api_write_wakes_coordinator_without_waiting_for_fallback(
    postgres_database, monkeypatch,
):
    import moyai.dispatch_wakeups as notifications

    monkeypatch.setattr(notifications, "FALLBACK_SECONDS", 30)
    database, actor = postgres_database
    completed = asyncio.Event()
    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=lambda *a, **k: completed.set()))
    runtime = SimpleNamespace(
        database=database, settings=database.settings, product_automations=None,
        connect=AsyncMock(return_value=client),
    )
    listener = DispatchWakeups(database.settings)
    task = asyncio.create_task(dispatcher(runtime, wakeups=listener))
    try:
        await asyncio.wait_for(listener.ready.wait(), 5)
        await submissions(database, actor, 1)
        await asyncio.wait_for(completed.wait(), 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    client.start_workflow.assert_awaited_once()


def test_notification_channel_is_scoped_to_native_schema_and_queue():
    first = Settings(_env_file=None, database_schema="moyai_one", temporal_queue="first")
    assert channel(first) == channel(first.model_copy())
    assert channel(first) != channel(first.model_copy(update={"temporal_queue": "second"}))
    assert channel(first) != channel(first.model_copy(update={"database_schema": "moyai_two"}))


async def test_postgres_busy_session_does_not_block_other_admissions(postgres_database):
    database, actor = postgres_database
    turns = await submissions(database, actor, 3)
    async with database.sessions.begin() as held:
        from moyai.models import Session

        await held.scalar(select(Session).where(Session.id == turns[0].session_id).with_for_update())
        claimed = await asyncio.wait_for(database.claim_batch(3), 1)
        assert {turn.id for turn in claimed} == {turn.id for turn in turns[1:]}
    assert (await database.claim_next()).id == turns[0].id


async def test_listener_reconnects_after_database_disconnect(postgres_database):
    database, actor = postgres_database
    admin = await asyncpg.connect(database.settings.postgres_url)
    try:
        async with DispatchWakeups(database.settings) as listener:
            await asyncio.wait_for(listener.ready.wait(), 5)
            query = "SELECT pid FROM pg_stat_activity WHERE application_name=$1"
            previous = await admin.fetchval(query, channel(database.settings))
            assert previous is not None
            await admin.fetchval("SELECT pg_terminate_backend($1)", previous)
            async with asyncio.timeout(5):
                while True:
                    current = await admin.fetchval(query, channel(database.settings))
                    if current is not None and current != previous and listener.ready.is_set():
                        break
                    await asyncio.sleep(.01)
            listener.event.clear()
            await submissions(database, actor, 1)
            await asyncio.wait_for(listener.event.wait(), 2)
    finally:
        await admin.close()


async def test_missing_listener_still_dispatches_through_bounded_fallback(runtime, actor):
    class Polling(DispatchWakeups):
        def __init__(self, settings):
            super().__init__(settings)
            self.waiting = asyncio.Event()

        async def wait(self):
            self.waiting.set()
            await super().wait()

    completed = asyncio.Event()
    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=lambda *a, **k: completed.set()))
    runtime.connect = AsyncMock(return_value=client)
    wakeups = Polling(runtime.settings)
    task = asyncio.create_task(dispatcher(runtime, wakeups=wakeups))
    try:
        await asyncio.wait_for(wakeups.waiting.wait(), 2)
        await submissions(runtime.database, actor, 1)
        # SQLite has no LISTEN connection or local notification shortcut.
        await asyncio.wait_for(completed.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    client.start_workflow.assert_awaited_once()
