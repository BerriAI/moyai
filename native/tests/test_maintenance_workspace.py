"""Maintenance snapshot/continuation fences; actual-app proof is separate."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.usage import RunUsage
from sqlalchemy import select
from temporalio.exceptions import ApplicationError

from moyai.computer import lease
from moyai.history_handoff import AgentCheckpoint
from moyai.maintenance_workspace import (
    CHECKPOINT_KIND,
    RESUME_KIND,
    MaintenanceWorkspace,
    maintenance_workspace,
    preserve,
    resume,
)
from moyai.models import ComputerOperation, Effect, Event, Session, Turn, User
from moyai.product_drain import ProductDrainService
from moyai.product_drain_models import ProductDrainReceipt, ProductDrainState

OLD = {"provider": "modal", "id": "sb-maintenance-old"}
NEW = {"provider": "modal", "id": "sb-maintenance-restored"}
RUN = "maintenance-source-workflow-run"
NEXT = "maintenance-successor-workflow-run"
BOUNDARY = "native_node:2"


@pytest_asyncio.fixture
async def maintenance(runtime, active, monkeypatch):
    session, turn, prepared = active
    service = ProductDrainService(
        sessions=lambda: runtime.database.sessions, authority=lambda: None,
        native_schema="", task_queue="test",
    )
    runtime.product_drain = service
    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    capture = AsyncMock(return_value="im-settled-files")
    monkeypatch.setattr(runtime.database.answers, "snapshots", SimpleNamespace(capture=capture))
    monkeypatch.setattr("moyai.maintenance_workspace.live_sandbox", AsyncMock(return_value=False))
    restore = AsyncMock(return_value=SimpleNamespace(
        object_id=NEW["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
    ))
    monkeypatch.setattr("moyai.maintenance_workspace.restored_sandbox", restore)
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        current.workspace, current.workspace_scope = json.dumps(OLD), prepared.scope.workspace_key
        db.add(ProductDrainState(id=1, generation=22, paused=True, request_fingerprint="a" * 64))
    conversation = Conversation(
        messages=[
            ModelRequest(parts=[UserPromptPart("Write one file, then verify it.")]),
            ModelResponse(parts=[ToolCallPart("shell", {"command": "write once"}, "call-once")]),
            ModelRequest(parts=[ToolReturnPart("shell", "written", "call-once")]),
        ], conversation_id=prepared.scope.conversation_id, usage=RunUsage(requests=3, input_tokens=71),
    )
    graph = AgentCheckpoint(
        prepared=replace(prepared, prompt="", workspace=OLD),
        workspace=OLD, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        run_step=4, output_retries_used=1, tool_retries={"other_tool": 1},
    )
    return SimpleNamespace(
        runtime=runtime, session=session, turn=turn, prepared=prepared, graph=graph,
        service=service, capture=capture, restore=restore,
    )


async def save(case):
    result = await preserve(
        case.runtime, case.graph, generation=22,
        workflow_id=case.turn.workflow_id, workflow_run_id=RUN, boundary=BOUNDARY,
    )
    assert result.reference
    return result.reference


async def load(case, reference, **changes):
    args = {"workflow_id": case.turn.workflow_id, "workflow_run_id": NEXT, "previous_run_id": RUN}
    return await resume(case.runtime, case.turn.id, reference, **(args | changes))


async def unpause(case):
    async with case.runtime.database.sessions.begin() as db:
        state = await db.get(ProductDrainState, 1)
        state.paused, state.generation = False, state.generation + 1


async def test_expiry_enters_a_new_bound_continuation_without_rewriting_old_inputs(maintenance):
    case = maintenance
    original_graph = TypeAdapter(AgentCheckpoint).dump_json(case.graph)
    async with case.runtime.database.sessions.begin() as db:
        original_preparation = (await db.get(Turn, case.turn.id)).preparation
        db.add(Effect(
            id="confirmed-once", session_id=case.session.id, turn_id=case.turn.id,
            actor_id=case.prepared.scope.actor_id, tool_name="publish",
            arguments=case.runtime.security.seal("{}"), fingerprint="original", state="confirmed",
        ))
    reference = await save(case)
    # Lost capture reply reuses the same immutable envelope without a second image.
    assert await save(case) == reference
    case.capture.assert_awaited_once()
    parked = await load(case, reference)
    assert parked.paused and parked.checkpoint is None
    case.restore.assert_not_awaited()
    async with case.runtime.database.sessions() as db:
        assert (await db.get(ProductDrainReceipt, (case.turn.id, 22))).workflow_run_id == NEXT
    await unpause(case)
    result = await load(case, reference)
    assert not result.paused and result.checkpoint
    continued = result.checkpoint
    assert continued.workspace == continued.prepared.workspace == NEW
    assert continued.conversation == case.graph.conversation
    assert continued.run_step == 4 and continued.output_retries_used == 1
    assert continued.tool_retries == {"other_tool": 1}
    assert continued.prepared.scope.history_segment == 1
    assert continued.prepared.scope.actor_id == case.prepared.scope.actor_id
    assert TypeAdapter(AgentCheckpoint).dump_json(case.graph) == original_graph
    # Lost resume reply returns the committed new continuation without re-restoring.
    retry = await load(case, reference)
    assert retry == result
    case.restore.assert_awaited_once()
    async with case.runtime.database.sessions() as db:
        session, turn = await db.get(Session, case.session.id), await db.get(Turn, case.turn.id)
        assert json.loads(session.workspace) == NEW and session.snapshot is None
        assert turn.preparation == original_preparation
        assert (await db.get(Effect, "confirmed-once")).state == "confirmed"
        rows = list(await db.scalars(select(Event).where(
            Event.turn_id == turn.id, Event.kind.in_([CHECKPOINT_KIND, RESUME_KIND]),
        ).order_by(Event.id)))
        assert [row.kind for row in rows] == [CHECKPOINT_KIND, RESUME_KIND]
        assert (await db.get(ProductDrainReceipt, (turn.id, 22))).resumed_generation == 23


async def test_capture_finishes_before_park_becomes_safe(maintenance):
    case = maintenance
    entered, release = asyncio.Event(), asyncio.Event()

    async def capture(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return "im-files"

    case.capture.side_effect = capture
    task = asyncio.create_task(save(case))
    await entered.wait()
    try:
        async with case.runtime.database.sessions() as db:
            assert (await db.get(ProductDrainReceipt, (case.turn.id, 22))).phase == "blocked"
            assert (await db.get(Session, case.session.id)).snapshot is None
    finally:
        release.set()
    await task
    async with case.runtime.database.sessions() as db:
        assert (await db.get(ProductDrainReceipt, (case.turn.id, 22))).phase == "parked"


@pytest.mark.parametrize("outcome", ["timeout", "stop", "revoke", "generation"])
async def test_late_or_failed_capture_cannot_certify_a_park(maintenance, actor, outcome):
    case = maintenance

    async def capture(*_args, **_kwargs):
        if outcome == "timeout":
            raise TimeoutError("provider unavailable")
        if outcome == "stop":
            await case.runtime.database.stop(actor, case.session.id)
        elif outcome == "revoke":
            async with case.runtime.database.sessions.begin() as db:
                (await db.get(User, actor.id)).enabled = False
        else:
            await unpause(case)
        return "im-late"

    case.capture.side_effect = capture
    with pytest.raises((TimeoutError, PermissionError, HTTPException)):
        await save(case)
    async with case.runtime.database.sessions() as db:
        assert (await db.get(Session, case.session.id)).snapshot is None
        assert (await db.get(ProductDrainReceipt, (case.turn.id, 22))).phase == "blocked"
        assert await db.get(ComputerOperation, case.session.id) is None


async def test_uncertain_external_action_refuses_capture(maintenance):
    case = maintenance
    async with case.runtime.database.sessions.begin() as db:
        db.add(Effect(
            id="pending", session_id=case.session.id, turn_id=case.turn.id,
            actor_id=case.prepared.scope.actor_id, tool_name="publish",
            arguments=case.runtime.security.seal("{}"), fingerprint="original", state="pending",
        ))
    with pytest.raises(ApplicationError):
        await save(case)
    case.capture.assert_not_awaited()


async def test_live_original_workspace_is_bound_without_replacement(maintenance, monkeypatch):
    case = maintenance
    monkeypatch.setattr("moyai.maintenance_workspace.live_sandbox", AsyncMock(return_value=True))
    reference = await save(case)
    await unpause(case)
    result = await load(case, reference)
    case.restore.assert_not_awaited()
    assert result.checkpoint.workspace == result.checkpoint.prepared.workspace == OLD


@pytest.mark.parametrize("change", [
    {"workflow_run_id": RUN}, {"previous_run_id": "another-source"}, {"workflow_id": "another-workflow"},
])
async def test_only_the_exact_direct_successor_can_resume(maintenance, change):
    case = maintenance
    reference = await save(case)
    await unpause(case)
    with pytest.raises(PermissionError):
        await load(case, reference, **change)
    case.restore.assert_not_awaited()


async def test_human_input_cannot_invalidate_a_held_maintenance_snapshot(maintenance, actor):
    case = maintenance
    await save(case)
    with pytest.raises(HTTPException) as error:
        await lease(case.runtime, case.session.id, actor, "claim", "browser")
    assert error.value.status_code == 409
    async with case.runtime.database.sessions() as db:
        assert maintenance_workspace((await db.get(Session, case.session.id)).snapshot) is not None


async def test_stop_during_restore_cannot_publish_new_workspace_or_resume(maintenance, actor):
    case = maintenance
    reference = await save(case)
    await unpause(case)

    async def stopped(*_args):
        await case.runtime.database.stop(actor, case.session.id)
        return SimpleNamespace(object_id=NEW["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)))

    case.restore.side_effect = stopped
    with pytest.raises(PermissionError):
        await load(case, reference)
    async with case.runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, case.session.id)).workspace) == OLD
        assert not list(await db.scalars(select(Event).where(Event.kind == RESUME_KIND)))


async def test_missing_snapshot_never_creates_an_empty_computer(maintenance):
    case = maintenance
    reference = await save(case)
    point = MaintenanceWorkspace.model_validate_json(await case.runtime.objects.get(reference))
    broken = point.model_copy(update={"image_id": None})
    reference = await case.runtime.objects.put(broken.model_dump_json().encode())
    async with case.runtime.database.sessions.begin() as db:
        (await db.get(Session, case.session.id)).snapshot = broken.model_dump_json()
    await unpause(case)
    with pytest.raises(ApplicationError):
        await load(case, reference)
    case.restore.assert_not_awaited()
