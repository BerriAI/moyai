from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from moyai import workspace_recovery
from moyai.contracts import NewMessage, NewSession
from moyai.database import Database
from moyai.models import Session, User
from moyai.security import Security
from moyai.settings import Settings
from moyai_harbor.workspace import HarborWorkspace
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy import select


@pytest.mark.asyncio
async def test_harbor_lifecycle_routes_only_its_exact_reference(monkeypatch):
    environment = SimpleNamespace(session_id="local-1", exec=AsyncMock(return_value=SimpleNamespace(return_code=0)))
    workspace = HarborWorkspace(environment)
    original_live = AsyncMock(return_value=False)
    original_snapshots = SimpleNamespace(capture=AsyncMock(return_value="real-image"))
    database = SimpleNamespace(answers=SimpleNamespace(snapshots=original_snapshots))
    monkeypatch.setattr(workspace_recovery, "live_sandbox", original_live)
    reference = {"provider": "modal", "id": workspace.reference.id}
    foreign = {"provider": "modal", "id": "sb-foreign"}

    with workspace.runtime_context(database):
        assert await workspace_recovery.live_sandbox(reference)
        environment.exec.assert_awaited_once_with("true", cwd="/workspace", timeout_sec=5)
        environment.exec.return_value.return_code = 1
        assert not await workspace_recovery.live_sandbox(reference)
        assert not await workspace_recovery.live_sandbox(foreign)
        original_live.assert_awaited_once_with(foreign)
        with pytest.raises(RuntimeError, match="durable snapshots are unavailable"):
            await database.answers.snapshots.capture(workspace.reference, require_live=True)
        assert await database.answers.snapshots.capture(WorkspaceRef(**foreign), require_live=True) == "real-image"
        original_snapshots.capture.assert_awaited_once_with(WorkspaceRef(**foreign), require_live=True)

    assert workspace_recovery.live_sandbox is original_live
    assert database.answers.snapshots is original_snapshots


@pytest.mark.asyncio
async def test_second_turn_reuses_live_harbor_workspace_without_cloud_or_fake_checkpoint(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None, data_dir=tmp_path, workspace_password="test-password-12345",
        default_model="test", database_url="", encryption_key="",
    )
    database = Database(settings, Security(settings.master_key))
    await database.start()
    runtime = SimpleNamespace(database=database, security=database.security, product=None)
    environment = SimpleNamespace(session_id="local-1", exec=AsyncMock(return_value=SimpleNamespace(return_code=0)))
    workspace = HarborWorkspace(environment)
    reference = {"provider": "modal", "id": workspace.reference.id}
    cloud = AsyncMock(side_effect=AssertionError("A Harbor workspace must not contact Modal."))
    monkeypatch.setattr(workspace_recovery, "live_sandbox", cloud)
    try:
        async with database.sessions() as db:
            actor = await db.scalar(select(User).where(User.username == "admin"))
        session = await database.create_session(actor, NewSession(visibility="private"))
        first = await database.submit(actor, session.id, NewMessage(prompt="Write a file.", request_key="first"))
        await database.claim_next()
        prepared = await database.prepare(first.id)
        conversation = ConversationTypeAdapter.dump_json(Conversation(
            conversation_id=prepared.scope.conversation_id,
        )).decode()
        with workspace.runtime_context(database):
            assert await database.save_result(
                prepared.scope, conversation=conversation, output="Saved.", workspace=reference,
                usage={}, checkpoint_workspace=True,
            )
            await database.answers.save_computer(first.id, reference, product=False)
            async with database.sessions() as db:
                saved = await db.get(Session, session.id)
                assert saved.save_warning
                assert workspace_recovery.checkpoint(saved.snapshot).image_id is None
            second = await database.submit(
                actor, session.id, NewMessage(prompt="Read that file.", request_key="second"),
            )
            await database.claim_next()
            followup = await database.prepare(second.id)
            assert followup.workspace == reference
            assert (await workspace_recovery.prepare_workspace(runtime, followup)).workspace == reference
            environment.exec.assert_awaited_once_with("true", cwd="/workspace", timeout_sec=5)
            cloud.assert_not_awaited()
    finally:
        await database.close()
