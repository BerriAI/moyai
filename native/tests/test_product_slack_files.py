"""Local attachment protocol checks; never contact Slack or send messages."""

import io
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_product_slack as slack_tests
from app.slack_files import SlackFileError
from fastapi import HTTPException
from PIL import Image
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.workspaces import Workspace
from pydantic_ai.workspaces.local import LocalWorkspaceBackend
from sqlalchemy import select

from moyai.models import Upload
from moyai.product_slack_files import attachment_capability

bridge = slack_tests.bridge
slack = slack_tests.slack


def provenance():
    return {"team_id": "TTEST0001", "channel_id": "CTEST0001", "message_ts": "1792100000.000"}


async def test_original_bytes_and_preview_retained_once_without_retranscribing(slack, actor, active, monkeypatch):
    original, preview = io.BytesIO(), io.BytesIO()
    Image.new("RGB", (12, 12), "navy").save(original, "PNG")
    Image.new("RGB", (12, 12), "navy").save(preview, "JPEG")
    reads = []

    async def read(identity, team):
        reads.append((identity, team))
        return "diagram.png", io.BytesIO(original.getvalue()), ("image/png", preview.getvalue(), "")

    monkeypatch.setattr(slack.service.files.reader, "read", read)
    principal = await slack.service.product.identity.require_current(slack.grant)
    event = {"files": [{"id": "F12345678"}, {"id": "F12345678"}]}
    first = await slack.service.files.prepare(actor, principal, event, provenance())
    second = await slack.service.files.prepare(actor, principal, event, provenance())
    assert first == second and len(reads) == 1
    assert len(first["uploads"]) == 2 and len(first["images"]) == 1
    assert "untrusted reference data" in first["context"]
    async with slack.runtime.database.sessions.begin() as db:
        rows = list(await db.scalars(select(Upload)))
        assert all(row.session_id is None for row in rows)
        await slack.service.files.bind(db, actor.id, active[0].id, first["uploads"])
    async with slack.runtime.database.sessions() as db:
        row = await db.get(Upload, first["uploads"][0])
        assert await slack.runtime.objects.get(row.digest) == original.getvalue()


async def test_read_failure_is_explicit_and_revocation_does_not_bind_drafts(slack, actor, monkeypatch):
    async def failed(*_args):
        raise SlackFileError("missing_scope")

    monkeypatch.setattr(slack.service.files.reader, "read", failed)
    principal = await slack.service.product.identity.require_current(slack.grant)
    result = await slack.service.files.prepare(actor, principal, {"files": [{"id": "F12345678"}]}, provenance())
    assert not result["uploads"] and "missing_scope" in result["context"]

    async def revoked(*_args):
        slack.authority["current"] = False
        return "private.txt", io.BytesIO(b"private text"), ("text/plain", b"", "private text")

    monkeypatch.setattr(slack.service.files.reader, "read", revoked)
    with pytest.raises(HTTPException):
        await slack.service.files.prepare(actor, principal, {"files": [{"id": "F12345678"}]}, provenance())
    async with slack.runtime.database.sessions() as db:
        assert not list(await db.scalars(select(Upload)))


async def test_original_file_copy_is_scoped_and_never_overwrites_edited_copy(slack, actor, active, tmp_path):
    runtime, scope = slack.runtime, active[2].scope
    row = await slack.service.files.retain(
        actor.id, "11111111-1111-1111-1111-111111111111", "notes.txt", b"Original input", "text/plain",
    )
    async with runtime.database.sessions.begin() as db:
        await slack.service.files.bind(db, actor.id, scope.session_id, [row.id])
    workspace = Workspace(LocalWorkspaceBackend(tmp_path))
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage(), workspace=workspace)
    function = next(tool for tool in attachment_capability(runtime.database, runtime.objects).tools if tool.__name__ == "attachment_copy")
    result = await function(ctx, row.id)
    assert await workspace.read_bytes(result["path"]) == b"Original input"
    await workspace.write_bytes(result["path"], b"User edits")
    with pytest.raises(ToolFailed, match="not overwritten"):
        await function(ctx, row.id)
    with pytest.raises((ToolFailed, PermissionError, HTTPException)):
        await function(replace(ctx, deps=replace(scope, session_id="different-session")), row.id)
    assert await workspace.read_bytes(result["path"]) == b"User edits"


async def test_real_file_reader_downloads_inspects_and_retains_before_reuse(slack, actor, monkeypatch):
    content = b"Slack attachment bytes.\n"
    file_id = "F12345678"
    url = "https://files.slack.com/files-pri/TTEST0001-F12345678/notes.txt"
    calls = []

    async def request(method, endpoint, *, headers, params, allowed_errors):
        assert method == "GET" and endpoint == "https://slack.com/api/files.info"
        assert headers == {"Authorization": "Bearer synthetic-bot-token", "Accept-Encoding": "identity"}
        assert params == {"file": file_id} and allowed_errors == ("missing_scope",)
        calls.append("metadata")
        return {"ok": True, "file": {
            "id": file_id, "name": "notes.txt", "size": len(content),
            "mimetype": "text/plain", "url_private_download": url,
        }}

    async def chunks(size):
        assert size == 64 * 1024
        yield content[:10]
        yield content[10:]

    @asynccontextmanager
    async def response(target, *, headers, allow_redirects):
        assert target == url and not allow_redirects
        assert headers == {"Authorization": "Bearer synthetic-bot-token", "Accept-Encoding": "identity"}
        calls.append("download")
        yield SimpleNamespace(
            status=200, headers={"Content-Length": str(len(content))},
            content=SimpleNamespace(iter_chunked=chunks),
        )

    @asynccontextmanager
    async def client(*, timeout, auto_decompress):
        assert timeout.total == 45 and not auto_decompress
        yield SimpleNamespace(get=response)

    monkeypatch.setattr(slack.service.connectors, "request", request)
    monkeypatch.setattr("app.slack_files.aiohttp.ClientSession", client)
    principal = await slack.service.product.identity.require_current(slack.grant)
    event = {"files": [{"id": file_id}]}
    first = await slack.service.files.prepare(actor, principal, event, provenance())
    second = await slack.service.files.prepare(actor, principal, event, provenance())
    assert first == second and calls == ["metadata", "download"]
    assert len(first["uploads"]) == 1 and not first["images"]
    assert "Slack attachment bytes." in first["context"]
    async with slack.runtime.database.sessions() as db:
        original = await db.get(Upload, first["uploads"][0])
        assert original.name == "notes.txt" and original.media_type == "text/plain"
        assert await slack.runtime.objects.get(original.digest) == content
