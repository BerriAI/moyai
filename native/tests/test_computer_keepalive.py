"""Native activity and Temporal checks; live Modal proof is recorded separately."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import modal
import pytest
import pytest_asyncio
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai import worker
from moyai.activities import save_computer
from moyai.agent import make_agent
from moyai.computer_keepalive import VERSION, Keepalive, keep_computer_alive
from moyai.contracts import RunScope
from moyai.models import Session, Turn, User
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

REFERENCE = {"provider": "modal", "id": "sb-active"}


@pytest_asyncio.fixture
async def computer(runtime, active, monkeypatch):
    session, _, prepared = active
    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    await runtime.database.save_workspace_reference(prepared.scope, REFERENCE)
    process = SimpleNamespace(wait=SimpleNamespace(aio=AsyncMock(return_value=0)))
    sandbox = SimpleNamespace(
        poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
        exec=SimpleNamespace(aio=AsyncMock(return_value=process)),
    )
    attach = AsyncMock(return_value=sandbox)
    monkeypatch.setattr(modal.Sandbox.from_id, "aio", attach)
    create = AsyncMock(side_effect=AssertionError("Maintenance must never create a computer"))
    monkeypatch.setattr(modal.Sandbox.create, "aio", create)
    return session, prepared.scope, sandbox, attach


@pytest.mark.parametrize("status", ["running", "saving"])
async def test_active_and_saving_computer_remains_live_without_changing_checkpoint(runtime, computer, status):
    session, scope, sandbox, attach = computer
    runtime.settings.computer_idle_timeout = 60
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        current.status = status
        current.snapshot = "existing-checkpoint-evidence"
        (await db.get(Turn, scope.turn_id)).status = status
    assert await keep_computer_alive(scope) == Keepalive(True, 20)
    attach.assert_awaited_once_with(REFERENCE["id"])
    sandbox.exec.aio.assert_awaited_once_with("/bin/true", timeout=10)
    async with runtime.database.sessions() as db:
        assert (await db.get(Session, session.id)).snapshot == "existing-checkpoint-evidence"


async def test_lazy_acquisition_is_observed_without_starting_a_computer(runtime, active, computer):
    session, scope, sandbox, attach = computer
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).workspace = None
    assert (await keep_computer_alive(scope)).active
    attach.assert_not_awaited()
    await runtime.database.save_workspace_reference(scope, REFERENCE)
    assert (await keep_computer_alive(scope)).active
    sandbox.exec.aio.assert_awaited_once()


@pytest.mark.parametrize("change", [
    "stopped", "failed", "completed", "waiting", "deleted", "revoked", "superseded", "scope", "actor", "unsupported",
])
async def test_inactive_or_unauthorized_workspace_is_not_touched(runtime, computer, change):
    session, scope, sandbox, attach = computer
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        if change in {"stopped", "failed", "completed", "waiting"}:
            current.status = change
        elif change == "deleted":
            current.deleted = True
        elif change == "revoked":
            (await db.get(User, scope.actor_id)).enabled = False
        elif change == "superseded":
            current.active_turn = None
        elif change == "scope":
            current.workspace_scope = "different-scope"
        elif change == "unsupported":
            current.workspace = '{"provider":"local","id":"other-provider"}'
    if change == "actor":
        scope = replace(scope, actor_id="different-actor")
    assert not (await keep_computer_alive(scope)).active
    attach.assert_not_awaited()
    sandbox.exec.aio.assert_not_awaited()


@pytest.mark.parametrize("status", ["running", "saving", "idle"])
async def test_lambda_keepalive_uses_endpoint_only_for_admitted_active_turn(runtime, computer, monkeypatch, status):
    session, scope, _, modal_attach = computer
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        current.workspace = '{"provider":"lambda","id":"lambda:us-east-1:microvm-test"}'
        current.status = status
        (await db.get(Turn, scope.turn_id)).status = status
    vm = SimpleNamespace(alive=AsyncMock(return_value=True), request=AsyncMock(return_value={}))
    control = SimpleNamespace(get=AsyncMock(return_value=vm), create=AsyncMock())
    monkeypatch.setattr("moyai.lambda_control.LambdaControl", lambda settings: control)
    result = await keep_computer_alive(scope)
    assert result.active is (status != "idle")
    if status == "idle":
        control.get.assert_not_awaited()
    else:
        control.get.assert_awaited_once_with("lambda:us-east-1:microvm-test")
        vm.request.assert_awaited_once_with("/health", {})
    control.create.assert_not_awaited()
    modal_attach.assert_not_awaited()


@pytest.mark.parametrize("failure", [None, modal.exception.NotFoundError("gone")])
async def test_terminated_computer_is_not_replaced(computer, failure):
    _, scope, sandbox, attach = computer
    if failure is None:
        sandbox.poll.aio.return_value = 137
    else:
        attach.side_effect = failure
    assert not (await keep_computer_alive(scope)).active
    sandbox.exec.aio.assert_not_awaited()


async def test_transport_failure_allows_next_tick_without_replacing_computer(computer):
    _, scope, sandbox, attach = computer
    attach.side_effect = TimeoutError("temporary transport failure")
    assert (await keep_computer_alive(scope)).active
    attach.side_effect = None
    assert (await keep_computer_alive(scope)).active
    sandbox.exec.aio.assert_awaited_once()


@pytest.mark.parametrize("legacy", [False, True])
async def test_temporal_timer_covers_checkpoint_save_and_replays_old_histories(runtime, active, monkeypatch, legacy):
    _session, turn, _ = active
    saving, release = asyncio.Event(), asyncio.Event()
    ticks = []

    @activity.defn(name="moyai_keep_computer_alive_v1")
    async def observed(scope: RunScope) -> Keepalive:
        async with runtime.database.sessions() as db:
            ticks.append(await db.scalar(select(Session.status).where(Session.id == scope.session_id)))
        if saving.is_set():
            release.set()
        return Keepalive(True, 1)

    @activity.defn(name="moyai_save_computer_v1")
    async def held_save(turn_id: str, reference: dict | None) -> None:
        saving.set()
        if not legacy:
            await asyncio.wait_for(release.wait(), 10)
        await save_computer(turn_id, reference)

    monkeypatch.setattr(worker, "ACTIVITIES", [
        observed if item is keep_computer_alive else held_save if item is save_computer else item
        for item in worker.ACTIVITIES
    ])
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        original = workflow.patched
        with monkeypatch.context() as version:
            if legacy:
                version.setattr(workflow, "patched", lambda name: False if name == VERSION else original(name))
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 40))["status"] == "completed"
                history = await handle.fetch_history()
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            interceptors=[ActivityCapacity()],
        ).replay_workflow(history)
    if legacy:
        assert ticks == []
    else:
        assert "saving" in ticks
        assert all(status in {"running", "saving"} for status in ticks)


async def test_stop_does_not_retry_inflight_maintenance_during_a_model_wait(runtime, active, actor, monkeypatch):
    session, turn, _ = active
    model_entered, maintenance_entered, maintenance_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def stream(_messages, _info):
        model_entered.set()
        await asyncio.Event().wait()
        yield "unreachable"

    runtime.models = {"test": FunctionModel(
        lambda _messages, _info: ModelResponse(parts=[TextPart("unreachable")]), stream_function=stream,
    )}
    runtime.agent, runtime.durability = make_agent(
        runtime.settings, runtime.database, runtime.stores, runtime.objects,
        models=runtime.models,
        workspace=LocalWorkspace(runtime.settings.data_dir.parent / "computer"), skills_directory="skills",
    )

    @activity.defn(name="moyai_keep_computer_alive_v1")
    async def held_tick(scope: RunScope) -> Keepalive:
        calls.append(scope.turn_id)
        maintenance_entered.set()
        try:
            await release.wait()
        finally:
            maintenance_cancelled.set()
        return Keepalive(False)

    monkeypatch.setattr(worker, "ACTIVITIES", [
        held_tick if item is keep_computer_alive else item for item in worker.ACTIVITIES
    ])
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            await asyncio.wait_for(asyncio.gather(model_entered.wait(), maintenance_entered.wait()), 15)
            await runtime.database.stop(actor, session.id)
            await handle.cancel()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(handle.result(), 15)
            # Temporal cancels the timer immediately. A short provider request
            # already dispatched by an activity may settle after that request.
            release.set()
            await asyncio.wait_for(maintenance_cancelled.wait(), 5)
            assert calls == [turn.id]
    async with runtime.database.sessions() as db:
        assert (await db.get(Session, session.id)).status == "stopped"
