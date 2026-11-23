"""Active checkpoint authority and publication tests; real-app proof is separate."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from pydantic import TypeAdapter
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.usage import RunUsage
from sqlalchemy import select
from temporalio.exceptions import ApplicationError
from test_maintenance_workspace import runtime  # noqa: F401

from moyai.active_workspace import RECOVERY_NOTICE, active_workspace, preserve_or_recover
from moyai.history_handoff import AgentCheckpoint
from moyai.models import ComputerLease, ComputerOperation, Effect, Event, Session, User

OLD = {"provider": "modal", "id": "active-old"}
NEW = {"provider": "modal", "id": "active-new"}


@pytest_asyncio.fixture
async def case(runtime, active, monkeypatch):  # noqa: F811 - imported parametrized fixture
    session, turn, prepared = active
    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    capture = AsyncMock(return_value="active-image")
    live = AsyncMock(return_value=True)
    restore = AsyncMock(return_value=SimpleNamespace(
        object_id=NEW["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
    ))
    monkeypatch.setattr(runtime.database.answers, "snapshots", SimpleNamespace(capture=capture))
    monkeypatch.setattr("moyai.active_workspace.live_sandbox", live)
    monkeypatch.setattr("moyai.active_workspace.restored_sandbox", restore)
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        current.workspace, current.workspace_scope = json.dumps(OLD), prepared.scope.workspace_key
    conversation = Conversation(
        messages=[
            ModelRequest(parts=[UserPromptPart("Write once, then inspect.")]),
            ModelResponse(parts=[ToolCallPart("shell", {"command": "write once"}, "write-once")]),
            ModelRequest(parts=[ToolReturnPart("shell", "written", "write-once")]),
        ],
        conversation_id=prepared.scope.conversation_id,
        usage=RunUsage(requests=3, input_tokens=71),
    )
    graph = AgentCheckpoint(
        prepared=replace(prepared, prompt=""), workspace=OLD,
        conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        run_step=4, output_retries_used=1, tool_retries={"shell": 1},
    )
    return SimpleNamespace(
        runtime=runtime, session=session, turn=turn, graph=graph,
        capture=capture, live=live, restore=restore,
    )


async def boundary(case, graph=None):
    return await preserve_or_recover(
        case.runtime, graph or case.graph,
        workflow_id=case.turn.workflow_id, workflow_run_id="active-run",
    )


def next_graph(case):
    conversation = ConversationTypeAdapter.validate_json(case.graph.conversation)
    conversation.messages.extend([
        ModelResponse(parts=[ToolCallPart("shell", {"command": "cat saved.txt"}, "refused-read")]),
        ModelRequest(parts=[ToolReturnPart("shell", "The command was refused before dispatch.", "refused-read")]),
    ])
    conversation.usage.requests += 1
    return replace(
        case.graph, conversation=ConversationTypeAdapter.dump_json(conversation).decode(), run_step=6,
    )


async def test_restoration_keeps_current_results_budgets_and_immutable_source(case):
    assert (await boundary(case)).reference is None
    async with case.runtime.database.sessions() as db:
        saved = active_workspace((await db.get(Session, case.session.id)).snapshot)
    original = await case.runtime.objects.get(saved.graph)
    case.live.return_value = False
    graph = next_graph(case)
    result = await boundary(case, graph)
    continued = TypeAdapter(AgentCheckpoint).validate_json(await case.runtime.objects.get(result.reference))
    assert continued.workspace == continued.prepared.workspace == NEW
    assert continued.prepared.scope == graph.prepared.scope
    assert continued.run_step == 6 and continued.output_retries_used == 1
    assert continued.tool_retries == {"shell": 1}
    conversation = ConversationTypeAdapter.validate_json(continued.conversation)
    assert conversation.usage.requests == 4
    assert [
        part.tool_call_id for message in conversation.messages for part in message.parts
        if isinstance(part, ToolReturnPart)
    ] == ["write-once", "refused-read"]
    assert conversation.messages[-1].parts[-1].content == RECOVERY_NOTICE
    assert await case.runtime.objects.get(saved.graph) == original
    # A lost activity reply observes the same publication without another launch.
    assert (await boundary(case, graph)).reference == result.reference
    case.restore.assert_awaited_once()
    async with case.runtime.database.sessions() as db:
        current = await db.get(Session, case.session.id)
        assert json.loads(current.workspace) == NEW
        assert active_workspace(current.snapshot).image_id == saved.image_id
        assert not await db.get(ComputerOperation, current.id)


async def test_pending_effect_never_authorizes_recovery_or_repetition(case):
    await boundary(case)
    case.live.return_value = False
    async with case.runtime.database.sessions.begin() as db:
        db.add(Effect(
            id="unknown", session_id=case.session.id, turn_id=case.turn.id,
            actor_id=case.graph.prepared.scope.actor_id, tool_name="shell",
            arguments=case.runtime.security.seal({"command": "external write"}),
            fingerprint="unknown",
        ))
    with pytest.raises(ApplicationError, match="uncertain outcome"):
        await boundary(case, next_graph(case))
    async with case.runtime.database.sessions() as db:
        assert (await db.get(Effect, "unknown")).state == "pending"
        assert json.loads((await db.get(Session, case.session.id)).workspace) == OLD
    case.restore.assert_not_awaited()


@pytest.mark.parametrize("change", ["stop", "revoke", "control", "snapshot"])
async def test_late_restore_cannot_publish_after_task_or_authority_changes(case, change):
    await boundary(case)
    case.live.return_value = False

    async def changed(*args):
        async with case.runtime.database.sessions.begin() as db:
            session = await db.get(Session, case.session.id)
            if change == "stop":
                session.status, session.active_turn = "stopped", None
            elif change == "revoke":
                (await db.get(User, case.graph.prepared.scope.actor_id)).enabled = False
            elif change == "control":
                session.snapshot = None
                session.revision += 1
            else:
                session.snapshot = "different"
        return SimpleNamespace(object_id=NEW["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)))

    case.restore.side_effect = changed
    with pytest.raises((PermissionError, ApplicationError)):
        await boundary(case, next_graph(case))
    async with case.runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, case.session.id)).workspace) == OLD
        assert not await db.get(ComputerOperation, case.session.id)
        assert len(list(await db.scalars(select(Event).where(Event.kind == "active_workspace_boundary")))) == 1


@pytest.mark.parametrize("missing", ["none", "human_control", "wrong_turn", "wrong_scope", "wrong_workspace"])
async def test_only_the_current_bound_checkpoint_can_restore(case, missing):
    await boundary(case)
    case.live.return_value = False
    async with case.runtime.database.sessions.begin() as db:
        session = await db.get(Session, case.session.id)
        point = active_workspace(session.snapshot)
        if missing in {"none", "human_control"}:
            session.snapshot = None
        else:
            changes = {
                "wrong_turn": {"turn_id": "another-turn"},
                "wrong_scope": {"scope": "another-scope"},
                "wrong_workspace": {"workspace": {"provider": "modal", "id": "other"}},
            }
            session.snapshot = point.model_copy(update=changes[missing]).model_dump_json()
    with pytest.raises(ApplicationError, match="verified checkpoint"):
        await boundary(case, next_graph(case))
    case.restore.assert_not_awaited()


async def test_transport_error_never_proves_computer_loss(case):
    await boundary(case)
    case.live.side_effect = TimeoutError("Provider unavailable")
    with pytest.raises(TimeoutError):
        await boundary(case, next_graph(case))
    case.restore.assert_not_awaited()
    assert case.capture.await_count == 1


async def test_an_unrelated_step_failure_does_not_snapshot_or_replace_a_live_computer(case):
    result = await preserve_or_recover(
        case.runtime, case.graph, workflow_id=case.turn.workflow_id, workflow_run_id="active-run",
        recover_only=True,
    )
    assert result.reference is None
    case.capture.assert_not_awaited()
    case.restore.assert_not_awaited()


async def test_loss_during_capture_restores_the_previous_image_with_current_graph(case):
    await boundary(case)
    case.capture.side_effect = TimeoutError("The computer expired during capture")
    case.live.side_effect = [True, False]
    result = await boundary(case, next_graph(case))
    assert result.reference
    case.restore.assert_awaited_once()
    continued = TypeAdapter(AgentCheckpoint).validate_json(await case.runtime.objects.get(result.reference))
    assert "refused-read" in continued.conversation
    assert continued.workspace == NEW


async def test_failed_capture_keeps_prior_backup_and_live_execution(case):
    await boundary(case)
    case.capture.side_effect = TimeoutError("Capture reply lost")
    assert (await boundary(case, next_graph(case))).reference is None
    async with case.runtime.database.sessions() as db:
        session = await db.get(Session, case.session.id)
        assert active_workspace(session.snapshot).image_id == "active-image"
        assert "could not be backed up" in session.save_warning
        assert not await db.get(ComputerOperation, session.id)
    case.restore.assert_not_awaited()


async def test_human_control_skips_live_capture_and_blocks_dead_recovery(case):
    await boundary(case)
    async with case.runtime.database.sessions.begin() as db:
        db.add(ComputerLease(
            session_id=case.session.id, actor_id=case.graph.prepared.scope.actor_id,
            expires_at=int(time.time()) + 600,
        ))
        (await db.get(Session, case.session.id)).snapshot = None
    assert (await boundary(case, next_graph(case))).reference is None
    assert case.capture.await_count == 1
    case.live.return_value = False
    with pytest.raises(ApplicationError, match="controlled by a person"):
        await boundary(case, next_graph(case))
    case.restore.assert_not_awaited()


async def test_competing_recovery_cannot_publish_two_computers(case):
    await boundary(case)
    case.live.return_value = False
    started, release = asyncio.Event(), asyncio.Event()

    async def restore(*args):
        started.set()
        await release.wait()
        return SimpleNamespace(object_id=NEW["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)))

    case.restore.side_effect = restore
    first = asyncio.create_task(boundary(case, next_graph(case)))
    try:
        await asyncio.wait_for(started.wait(), 5)
        with pytest.raises(ApplicationError, match="unsettled desktop action"):
            await boundary(case, next_graph(case))
    finally:
        release.set()
    assert (await first).reference
    case.restore.assert_awaited_once()
