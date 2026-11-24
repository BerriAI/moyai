"""Real Temporal timers, replay and wake delivery; model fixtures are supplementary."""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import delete, func, select, update
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_responsibilities import assignment, enroll, finish, source_answer, tick, worker

from moyai.responsibility_contracts import ResponsibilityControl
from moyai.responsibility_dispatch import (
    ResponsibilityDispatch,
    dispatch_responsibilities,
    request_wake,
    seed_wakes,
)
from moyai.responsibility_models import Responsibility, ResponsibilityAssignment, ResponsibilityWake
from moyai.responsibility_workflow import ResponsibilityRun, ResponsibilityWorkflow
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity


async def eventually[T](read: Callable[[], Awaitable[T]], predicate: Callable[[T], bool] = bool, *, seconds=30) -> T:
    async with asyncio.timeout(seconds):
        while True:
            value = await read()
            if predicate(value):
                return value
            await asyncio.sleep(0.05)


async def overdue(runtime, identity):
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Responsibility).where(Responsibility.id == identity).values(
            next_check_at="2000-01-01T00:00:00+00:00",
        ))


async def pending(runtime):
    async with runtime.database.sessions() as db:
        return list(await db.scalars(select(ResponsibilityWake)))


async def stop(runtime, actor, row, state):
    current = next(item for item in await runtime.database.responsibilities.list(actor) if item.id == row.id)
    await runtime.database.responsibilities.control(actor, row.id, ResponsibilityControl(
        generation=current.generation, action="pause",
    ))
    await dispatch_responsibilities(runtime, state)


async def replay(runtime, handle):
    history = await handle.fetch_history()
    await Replayer(
        workflows=[ResponsibilityWorkflow], data_converter=encrypted_converter(runtime.security, runtime.objects),
        interceptors=[ActivityCapacity()],
    ).replay_workflow(history)
    return history


async def test_old_generation_cannot_admit_after_pause_resume(runtime, actor):
    source, row = await enroll(runtime, actor)
    await source_answer(runtime, actor, source)
    for command in [
        ResponsibilityControl(action="pause", generation=1), ResponsibilityControl(action="resume", generation=2),
    ]:
        await runtime.database.responsibilities.control(actor, row.id, command)
    assert (await runtime.database.responsibilities.tick(row.id, generation=1)).finished
    assert (await runtime.database.responsibilities.tick(row.id, generation=1, closed=True)).finished
    assert await assignment(runtime, row.id) is None
    await runtime.database.responsibilities.tick(row.id, generation=3)
    assert await assignment(runtime, row.id)


async def test_outbox_ack_preserves_control_committed_during_rpc(runtime, actor):
    _, row = await enroll(runtime, actor)

    async def start(*args, **kwargs):
        await runtime.database.responsibilities.control(actor, row.id, ResponsibilityControl(
            generation=1, action="pause",
        ))
        await runtime.database.responsibilities.control(actor, row.id, ResponsibilityControl(
            generation=2, action="resume",
        ))

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    await dispatch_responsibilities(runtime, ResponsibilityDispatch())
    events = await pending(runtime)
    assert {event.generation for event in events} == {1, 3}
    assert any(event.id.endswith(":stop") for event in events)
    assert any(event.generation == 3 and event.id.endswith(":start") for event in events)


async def test_wake_intent_rolls_back_with_its_domain_transaction(runtime, actor):
    _, row = await enroll(runtime, actor)
    before = {event.id for event in await pending(runtime)}
    with pytest.raises(RuntimeError):
        async with runtime.database.sessions.begin() as db:
            await request_wake(db, row.id, row.generation, "rolled-back")
            raise RuntimeError("Roll back the source change")
    assert {event.id for event in await pending(runtime)} == before


async def test_unacknowledged_outbox_page_does_not_starve_later_wakes(runtime, actor):
    rows = [(await enroll(runtime, actor))[1] for _ in range(3)]
    delivered = []

    async def unavailable(*args, **kwargs):
        delivered.append(kwargs["id"])
        raise TimeoutError("Temporal is unavailable")

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=unavailable))
    state = ResponsibilityDispatch()
    for _ in range(2):
        await dispatch_responsibilities(runtime, state, limit=2)
    assert set(delivered) == {ResponsibilityRun(row.id, row.generation).workflow_id for row in rows}
    assert len(await pending(runtime)) == 3
    assert state.after is None and state.through is None


async def test_cancelled_dispatch_does_not_skip_groups_waiting_for_rpc_slot(runtime, actor):
    rows = [(await enroll(runtime, actor))[1] for _ in range(3)]
    runtime.settings.temporal_dispatch_concurrency = 1
    state = ResponsibilityDispatch()
    attempted = []
    entered = asyncio.Event()
    stalled = True

    async def start(*args, **kwargs):
        attempted.append(kwargs["id"])
        if stalled:
            entered.set()
            await asyncio.Future()

    runtime.connect = AsyncMock(return_value=SimpleNamespace(start_workflow=start))
    dispatch = asyncio.create_task(dispatch_responsibilities(runtime, state))
    await asyncio.wait_for(entered.wait(), 3)
    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch
    assert len(attempted) == 1 and len(await pending(runtime)) == 3
    stalled = False
    await dispatch_responsibilities(runtime, state)
    assert set(attempted) == {ResponsibilityRun(row.id, row.generation).workflow_id for row in rows}
    assert len(await pending(runtime)) == 1
    await dispatch_responsibilities(runtime, state)
    assert not await pending(runtime)


async def test_closed_current_workflow_blocks_until_explicit_resume(runtime, actor):
    _, row = await enroll(runtime, actor)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        state = ResponsibilityDispatch()
        async with make_worker(runtime):
            await dispatch_responsibilities(runtime, state)
            handle = environment.client.get_workflow_handle(ResponsibilityRun(row.id, row.generation).workflow_id)
            await handle.terminate("Exercise explicit recovery for a closed controller")
            async with runtime.database.sessions.begin() as db:
                await request_wake(db, row.id, row.generation, "source:after-termination")
            await dispatch_responsibilities(runtime, state)
            blocked = (await runtime.database.responsibilities.list(actor))[0]
            assert blocked.status == "blocked" and "closed unexpectedly" in blocked.reason
            assert blocked.generation == row.generation and blocked.evaluations == 0
            assert not await pending(runtime)
            resumed = await runtime.database.responsibilities.control(actor, row.id, ResponsibilityControl(
                generation=row.generation, action="resume",
            ))
            await dispatch_responsibilities(runtime, state)
            replacement = environment.client.get_workflow_handle(ResponsibilityRun(row.id, resumed.generation).workflow_id)
            assert (await replacement.describe()).run_id != (await handle.describe()).run_id
            await stop(runtime, actor, resumed, state)
            await asyncio.wait_for(replacement.result(), 15)


@pytest.mark.parametrize("budget", [1, 2])
async def test_closed_controller_reconciles_accepted_save_without_new_admission(runtime, actor, budget):
    source, row = await enroll(runtime, actor, max_evaluations=budget)
    await source_answer(runtime, actor, source)
    await tick(runtime, row.id)
    _, prepared, decision = await worker(runtime, row.id)
    assert await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output=decision, workspace=None, usage={},
    )
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        state = ResponsibilityDispatch()
        async with make_worker(runtime):
            await dispatch_responsibilities(runtime, state)
            handle = environment.client.get_workflow_handle(ResponsibilityRun(row.id, row.generation).workflow_id)
            await handle.terminate("Exercise closure after result acceptance")
            async with runtime.database.sessions.begin() as db:
                await request_wake(db, row.id, row.generation, "source:after-termination")
            await dispatch_responsibilities(runtime, state)
            blocked = (await runtime.database.responsibilities.list(actor))[0]
            assert blocked.status == "blocked" and blocked.active_turn_id == prepared.scope.turn_id
            if budget > 1:
                with pytest.raises(HTTPException, match="review is still finishing"):
                    await runtime.database.responsibilities.control(actor, row.id, ResponsibilityControl(
                        generation=row.generation, action="resume",
                    ))
            # More evidence cannot turn this recovery path into new admission.
            await source_answer(runtime, actor, source, "Another status update")
            await runtime.database.settle(prepared.scope.turn_id)
            if budget > 1:
                with pytest.raises(HTTPException, match="review is still finishing"):
                    await runtime.database.responsibilities.control(actor, row.id, ResponsibilityControl(
                        generation=row.generation, action="resume",
                    ))
            await dispatch_responsibilities(runtime, state)
            final = (await runtime.database.responsibilities.list(actor))[0]
            assert final.status == ("budget_limited" if budget == 1 else "blocked")
            assert final.evaluations == 1 and final.active_turn_id is None and final.watermark == 1
            assert len(await runtime.database.responsibilities.inbox(actor)) == 1
            assert not await pending(runtime)
            async with runtime.database.sessions() as db:
                assert await db.scalar(select(func.count()).select_from(ResponsibilityAssignment)) == 1


@pytest.mark.parametrize("mode", ["waiting", "running", "saving"])
async def test_cutover_seeds_existing_generation_without_resetting_work(runtime, actor, mode):
    source, row = await enroll(runtime, actor)
    prepared = None
    if mode != "waiting":
        await source_answer(runtime, actor, source)
        await tick(runtime, row.id)
    if mode == "saving":
        _, prepared, decision = await worker(runtime, row.id)
        assert await runtime.database.save_result(
            prepared.scope, conversation='{"messages":[]}', output=decision, workspace=None, usage={},
        )
    before = (await runtime.database.responsibilities.list(actor))[0].model_dump()
    async with runtime.database.sessions.begin() as db:
        await db.execute(delete(ResponsibilityWake))
    await seed_wakes(runtime.database)
    await seed_wakes(runtime.database)
    assert (await runtime.database.responsibilities.list(actor))[0].model_dump() == before
    assert len(await pending(runtime)) == 1
    if mode == "saving":
        assert prepared is not None
        step = await runtime.database.responsibilities.tick(row.id, generation=row.generation)
        assert not step.finished and step.wake_at is None
        await runtime.database.settle(prepared.scope.turn_id)
        await runtime.database.responsibilities.tick(row.id, generation=row.generation)
        assert len(await runtime.database.responsibilities.inbox(actor)) == 1
        assert (await runtime.database.responsibilities.list(actor))[0].evaluations == 1


async def test_real_timer_survives_restart_and_lost_start_ack(runtime, actor, monkeypatch):
    source, row = await enroll(runtime, actor, interval_seconds=10)
    await source_answer(runtime, actor, source)
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        request = ResponsibilityRun(row.id, row.generation)
        state = ResponsibilityDispatch()
        lost = True

        async def start(*args, **kwargs):
            nonlocal lost
            handle = await environment.client.start_workflow(*args, **kwargs)
            if lost:
                lost = False
                raise TimeoutError("Accepted start, lost reply")
            return handle

        monkeypatch.setattr(runtime, "connect", AsyncMock(return_value=SimpleNamespace(start_workflow=start)))
        async with make_worker(runtime):
            await dispatch_responsibilities(runtime, state)
            assert await pending(runtime)
            handle = environment.client.get_workflow_handle(request.workflow_id)
            first = await handle.describe()
            await dispatch_responsibilities(runtime, state)
            assert not await pending(runtime)
            assert (await handle.describe()).run_id == first.run_id
            await eventually(handle.fetch_history, lambda history: any(
                event.HasField("timer_started_event_attributes") for event in history.events
            ))
        # A replacement worker reconstructs the timer from Temporal history.
        async with make_worker(runtime):
            await eventually(lambda: assignment(runtime, row.id))
            _, prepared, decision = await worker(runtime, row.id)
            assert await finish(runtime, prepared, decision)
            # Settlement's committed event is delivered after this worker restart.
            await dispatch_responsibilities(runtime, state)
            await eventually(lambda: runtime.database.responsibilities.inbox(actor))
            for _ in range(3):
                await handle.signal("changed")
            await stop(runtime, actor, row, state)
            await asyncio.wait_for(handle.result(), 15)
        history = await replay(runtime, handle)
        assert any(event.HasField("timer_fired_event_attributes") for event in history.events)
        assert len(await runtime.database.responsibilities.inbox(actor)) == 1
        async with runtime.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(ResponsibilityAssignment)) == 1


async def test_temporal_retries_one_responsibility_without_blocking_another(runtime, actor, monkeypatch):
    first_source, first = await enroll(runtime, actor)
    second_source, second = await enroll(runtime, actor)
    for source, row in [(first_source, first), (second_source, second)]:
        await source_answer(runtime, actor, source)
        await overdue(runtime, row.id)
    attempts = Counter()
    actual_attempts = []
    original = runtime.database.responsibilities.tick

    async def flaky(identity, **kwargs):
        attempts[identity] += 1
        if identity == first.id:
            actual_attempts.append(activity.info().attempt)
            if attempts[identity] <= 2:
                raise RuntimeError("Transient database read failure")
        return await original(identity, **kwargs)

    monkeypatch.setattr(runtime.database.responsibilities, "tick", flaky)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        state = ResponsibilityDispatch()
        async with make_worker(runtime):
            await dispatch_responsibilities(runtime, state)
            await eventually(lambda: assignment(runtime, second.id), seconds=10)
            await eventually(lambda: assignment(runtime, first.id), seconds=15)
            assert actual_attempts[:3] == [1, 2, 3]
            for row in [first, second]:
                await stop(runtime, actor, row, state)
                handle = environment.client.get_workflow_handle(ResponsibilityRun(row.id, 1).workflow_id)
                await asyncio.wait_for(handle.result(), 15)
                await replay(runtime, handle)


async def test_signal_during_step_and_continue_as_new_keeps_committed_evidence(runtime, actor, monkeypatch):
    source, row = await enroll(runtime, actor)
    original = runtime.database.responsibilities.tick
    calls = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(*args, **kwargs):
        nonlocal calls
        calls += 1
        state = await original(*args, **kwargs)
        if calls == 2:
            entered.set()
            await release.wait()
        return state

    monkeypatch.setattr(runtime.database.responsibilities, "tick", gated)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        request = ResponsibilityRun(row.id, 1, max_wakes=2)
        state = ResponsibilityDispatch()
        async with make_worker(runtime):
            handle = await environment.client.start_workflow(
                ResponsibilityWorkflow.run, request, id=request.workflow_id,
                task_queue=runtime.settings.temporal_queue,
            )
            first = await handle.describe()
            await eventually(lambda: asyncio.sleep(0, result=calls), lambda value: value >= 1)
            await handle.signal("changed")
            await asyncio.wait_for(entered.wait(), 10)
            await source_answer(runtime, actor, source)
            await overdue(runtime, row.id)
            # The old run has already read the database. Commit evidence and
            # deliver its hint before allowing that stale result to return.
            await dispatch_responsibilities(runtime, state)
            release.set()
            await eventually(handle.describe, lambda description: description.run_id != first.run_id)
            await eventually(lambda: assignment(runtime, row.id))
            _, prepared, decision = await worker(runtime, row.id)
            assert await finish(runtime, prepared, decision)
            await dispatch_responsibilities(runtime, state)
            await eventually(lambda: runtime.database.responsibilities.inbox(actor))
            await stop(runtime, actor, row, state)
            await asyncio.wait_for(handle.result(), 15)
        run_id = first.run_id
        histories = []
        while run_id:
            history = await replay(runtime, environment.client.get_workflow_handle(request.workflow_id, run_id=run_id))
            histories.append(history)
            last = history.events[-1]
            run_id = (
                last.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                if last.HasField("workflow_execution_continued_as_new_event_attributes") else None
            )
        assert len(histories) >= 2
        assert len(await runtime.database.responsibilities.inbox(actor)) == 1
        async with runtime.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(ResponsibilityAssignment)) == 1
