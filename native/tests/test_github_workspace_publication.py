"""Workspace preflight recovery uses real SQL receipts; GitHub I/O is simulated."""

import json
from types import SimpleNamespace

import pytest
import test_product_github_publication as support
from app.connector_errors import ConnectorError
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy import select

from moyai.effects import EffectReceipts, PendingEffectError
from moyai.github_workflow_types import PublicationStatus, WorkspacePublishBranch
from moyai.models import Effect
from moyai.product_github_models import ProductGitOperation
from moyai.product_github_publication import toolset
from moyai.product_github_workflows import Workflows
from moyai.workspace_recovery import require_settled

pytestmark = support.pytestmark
admitted_product = support.admitted_product
product_database = support.product_database
publication = support.publication


def request(**changes):
    return WorkspacePublishBranch(
        repository_id=123, directory="/workspace/repo", base_sha="a" * 40,
        branch="work/workspace", title="Publish staged workspace", request_key="workspace-recovery",
        **changes,
    )


async def invoke(f, change):
    tools = toolset(f.service, f.scope, f.grant)
    selected = (await tools.get_tools(f.ctx))["publish_branch"]
    args = selected.args_validator.validate_python({"request": change.model_dump(mode="json")})
    return await tools.call_tool("publish_branch", args, f.ctx, selected)


async def status(f, change):
    return await f.publisher.status(PublicationStatus(
        repository_id=123, request_key=change.request_key, operation="publish_branch",
    ))


def exported(content="Frozen staged content"):
    return SimpleNamespace(exit_code=0, stdout=json.dumps({
        "files": [{"path": "hello.txt", "content": content}],
    }))


@pytest.mark.parametrize("failure", ["export", "invalid_paths", "head_mismatch"])
async def test_export_refusal_settles_before_dispatch_and_allows_same_turn_repair(publication, failure):
    f = publication
    repaired = False

    async def run(*args, **kwargs):
        if repaired:
            return exported()
        if failure == "invalid_paths":
            return SimpleNamespace(exit_code=0, stdout=json.dumps({
                "files": [{"path": "../outside", "content": "invalid"}],
            }))
        return SimpleNamespace(exit_code=1, stdout='{"error":"No staged changes"}')

    f.ctx.workspace = SimpleNamespace(run=run)
    change = request(expected_head_sha="b" * 40) if failure == "head_mismatch" else request()
    original_effect = await support.reserve(f, "publish_branch", change)
    with pytest.raises(ToolFailed, match="No repository write was attempted"):
        await invoke(f, change)
    refused = await status(f, change)
    assert refused["status"] == "not_dispatched"
    assert refused["attempted_steps"] == refused["confirmed_steps"] == []
    assert f.provider.calls == []
    async with f.product.database.sessions() as db:
        await require_settled(db, f.scope.session_id)
        assert (await db.get(Effect, original_effect)).state == "not_dispatched"
    # This is the reservation the original session's corrective shell command
    # could not obtain. No new turn or manual effect reconciliation is needed.
    receipts = EffectReceipts(f.product.database)
    await receipts.reserve(f.scope, "repair-shell", "shell", {"command": "git add hello.txt"})
    await receipts.confirm("repair-shell")
    if failure == "head_mismatch":
        return  # Corrected arguments require a new request key.
    repaired = True
    f.ctx.tool_call_id = "retry-after-local-repair"
    new_effect = await support.reserve(f, "publish_branch", change)
    result = await invoke(f, change)
    assert result["branch"] == change.branch and f.provider.commits == 1
    assert (await status(f, change))["status"] == "confirmed"
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, original_effect)).state == "not_dispatched"
        assert (await db.get(Effect, new_effect)).state == "confirmed"


async def test_retry_uses_frozen_files_and_confirmed_replay_does_not_need_workspace(publication):
    f = publication
    exports = []

    async def run(*args, **kwargs):
        exports.append(True)
        return exported("original staged files" if len(exports) == 1 else "later unrelated edit")

    f.ctx.workspace = SimpleNamespace(run=run)
    change = request()
    await support.reserve(f, "publish_branch", change)
    f.current["token_unavailable"] = True
    with pytest.raises(ToolFailed, match="No repository write was attempted"):
        await invoke(f, change)
    assert (await status(f, change))["status"] == "not_dispatched"
    f.current["token_unavailable"] = False
    f.ctx.tool_call_id = "retry-with-frozen-export"
    await support.reserve(f, "publish_branch", change)
    result = await invoke(f, change)
    blobs = [kwargs["json"]["content"] for _, path, kwargs in f.provider.calls if path.endswith("/git/blobs")]
    assert blobs == ["original staged files"]
    assert len(exports) == 1
    del f.ctx.workspace
    assert await Workflows(f.publisher).publish_branch(f.ctx, change) == result
    assert f.provider.commits == 1


async def test_ambiguous_github_write_keeps_its_barrier_and_frozen_export(publication):
    f = publication

    async def run(*args, **kwargs):
        return exported()

    original_request = f.provider.request

    async def lost_response(method, path, **kwargs):
        if path.endswith("/git/blobs"):
            raise ConnectorError("Lost GitHub acknowledgement", dispatched=True)
        return await original_request(method, path, **kwargs)

    f.provider.request = lost_response
    f.ctx.workspace = SimpleNamespace(run=run)
    change = request()
    effect = await support.reserve(f, "publish_branch", change)
    with pytest.raises(ToolFailed, match="may already have attempted writes"):
        await invoke(f, change)
    saved = await status(f, change)
    assert saved["status"] == "incomplete" and saved["attempted_steps"] == ["blob-0"]
    async with f.product.database.sessions() as db:
        assert (await db.get(Effect, effect)).state == "pending"
        row = await db.scalar(select(ProductGitOperation).where(ProductGitOperation.request_key == change.request_key))
        assert f.publisher.document(row)["workspace_files"][0]["content"] == "Frozen staged content"
    with pytest.raises(PendingEffectError):
        await EffectReceipts(f.product.database).reserve(f.scope, "unsafe-retry", "shell", {})


@pytest.mark.parametrize("import_fails", [False, True])
async def test_missing_base_is_fetched_on_worker_without_sandbox_credentials(publication, monkeypatch, import_fails):
    f = publication
    commands, writes, fetches = [], [], []

    async def fetch(url, branch, commit, token, destination):
        fetches.append((url, branch, commit, token))
        return b"credential-free-git-bundle"

    async def write(path, value):
        writes.append((path, value))

    async def run(command, **kwargs):
        commands.append(command)
        assert "synthetic-token" not in json.dumps(command)
        if len(commands) == 1:
            return SimpleNamespace(exit_code=1, stdout='{"code":"missing_base"}')
        if len(commands) == 2:
            return SimpleNamespace(exit_code=int(import_fails), stdout='{"imported":true}')
        return exported()

    monkeypatch.setattr("moyai.product_github_workspace.fetch_bundle", fetch)
    f.ctx.workspace = SimpleNamespace(run=run, write_bytes=write)
    change = request()
    await support.reserve(f, "publish_branch", change)
    if import_fails:
        with pytest.raises(ToolFailed, match="No repository write was attempted"):
            await invoke(f, change)
        assert (await status(f, change))["status"] == "not_dispatched"
        assert not any(method != "GET" for method, _, _ in f.provider.calls)
    else:
        assert (await invoke(f, change))["branch"] == change.branch
        assert len(commands) == 3
    assert fetches == [("https://github.com/owner/repo.git", "moyai-base/" + change.base_sha,
                        change.base_sha, "synthetic-token")]
    assert len(writes) == 1 and writes[0][1] == b"credential-free-git-bundle"
