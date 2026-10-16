"""Shared retained-input tool, with standalone authorization and the real local backend."""

from pathlib import Path

import pytest
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import Workspace
from pydantic_ai.workspaces.local import LocalWorkspaceBackend

from moyai.input_attachments import attachment_capability
from moyai.models import Upload


async def test_standalone_generic_file_copy_is_idempotent_and_preserves_human_edits(runtime, active, tmp_path):
    scope = active[2].scope
    raw = b"column,value\nsample,42\n"
    digest = await runtime.objects.put(raw)
    async with runtime.database.sessions.begin() as db:
        row = Upload(owner_id=scope.actor_id, session_id=scope.session_id,
                     name="report.csv", digest=digest, media_type="text/csv", size=len(raw))
        db.add(row)
        await db.flush()
        identity = row.id
    workspace = Workspace(LocalWorkspaceBackend(tmp_path))
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), workspace=workspace)
    copy = attachment_capability(runtime.database, runtime.objects).tools[0]
    first = await copy(ctx, identity)
    assert await copy(ctx, identity) == first
    assert Path(first["path"]).read_bytes() == raw
    Path(first["path"]).write_text("User edits\n")
    with pytest.raises(ToolFailed, match="not overwritten"):
        await copy(ctx, identity)
    assert Path(first["path"]).read_text() == "User edits\n"


async def test_standalone_unbound_and_cross_session_inputs_are_not_readable(runtime, active, tmp_path):
    scope = active[2].scope
    digest = await runtime.objects.put(b"private draft")
    async with runtime.database.sessions.begin() as db:
        row = Upload(owner_id=scope.actor_id, name="draft.txt", digest=digest, media_type="text/plain", size=13)
        db.add(row)
        await db.flush()
        identity = row.id
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), workspace=Workspace(LocalWorkspaceBackend(tmp_path)))
    copy = attachment_capability(runtime.database, runtime.objects).tools[0]
    with pytest.raises(ToolFailed, match="unavailable"):
        await copy(ctx, identity)
    assert not (tmp_path / "attachments").exists()


async def test_shared_tool_refuses_redirected_attachment_directory(runtime, active, tmp_path):
    scope = active[2].scope
    digest = await runtime.objects.put(b"input")
    async with runtime.database.sessions.begin() as db:
        row = Upload(owner_id=scope.actor_id, session_id=scope.session_id,
                     name="input.txt", digest=digest, media_type="text/plain", size=5)
        db.add(row)
        await db.flush()
        identity = row.id
    outside = tmp_path.parent / (tmp_path.name + "-other")
    outside.mkdir()
    (tmp_path / "attachments").symlink_to(outside, target_is_directory=True)
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), workspace=Workspace(LocalWorkspaceBackend(tmp_path)))
    copy = attachment_capability(runtime.database, runtime.objects).tools[0]
    with pytest.raises(ToolFailed, match="redirected"):
        await copy(ctx, identity)
    assert not list(outside.iterdir())
