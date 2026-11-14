"""Native GitLab boundary tests; these do not claim actual-app E2E proof."""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.github import Change
from pydantic import ValidationError
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel

from moyai import gitlab_transport as transport
from moyai.contracts import Grant
from moyai.gitlab_branch_policy import pattern_matches, require_writable
from moyai.gitlab_request_policy import REQUEST_CHANGES, REVIEW_CAPABILITY, validate_request
from moyai.gitlab_workflow_types import (
    Changes,
    CreateMR,
    CreateReview,
    MergeRequest,
    MRRead,
    OpenMR,
    Project,
    PublishBranch,
    Search,
)
from moyai.product_gitlab import ProductGitLabToolset
from moyai.product_gitlab_publication import Publications
from moyai.product_gitlab_reads import Reads
from moyai.product_gitlab_workflows import Workflows

BASE = "a" * 40
HEAD = "b" * 40
PROJECT = {"id": 123, "path_with_namespace": "group/sub/repo", "default_branch": "main",
           "http_url_to_repo": "https://gitlab.com/group/sub/repo.git",
           "web_url": "https://gitlab.com/group/sub/repo"}


def grant(**kwargs):
    return Grant("11111111-1111-1111-1111-111111111111", "gitlab", 1, "", read_only=False,
                 policy={"product": {"repository_id": 123, "base_url": "https://gitlab.com"}}, **kwargs)


def mr(**changes):
    return {"id": 1007, "iid": 7, "project_id": 123, "source_project_id": 123, "target_project_id": 123,
            "source_branch": "feature", "target_branch": "main", "sha": HEAD, "state": "opened",
            "draft": False, "title": "A feature", "web_url": "https://gitlab.com/group/sub/repo/-/merge_requests/7",
            "diff_refs": {"base_sha": BASE, "head_sha": HEAD, "start_sha": BASE}, "merged_at": None, **changes}


async def test_full_typed_catalog_and_read_only_boundary():
    service = SimpleNamespace(database=object(), check=AsyncMock())
    ctx = RunContext(deps=SimpleNamespace(), model=TestModel(), usage=RunUsage())
    selected = grant()
    catalog = await ProductGitLabToolset(service, ctx.deps, selected).get_tools(ctx)
    assert len(catalog) == 46
    writes = {name for name, tool in catalog.items() if not tool.tool_def.metadata["annotations"]["readOnlyHint"]}
    assert len(writes) == 18
    assert {"create_merge_request", "update_merge_request", "request_merge_request_write_access",
            "request_mr_lifecycle_access", "create_stack", "delete_pending_review"} <= writes
    assert not any(word in name for name in catalog for word in ("approve", "merge_merge", "delete_branch"))
    assert catalog["request_merge_request_write_access"].tool_def.kind == "unapproved"
    for name, tool in catalog.items():
        schema = tool.tool_def.parameters_json_schema
        assert schema.get("additionalProperties") is False, name
    readonly = await ProductGitLabToolset(service, ctx.deps, replace(selected, read_only=True)).get_tools(ctx)
    assert set(readonly) == set(catalog) - writes


def test_provider_origin_project_and_operation_identity_never_collide():
    p = Publications(SimpleNamespace(database=object()), SimpleNamespace(session_id="same"), grant())
    other = Publications(SimpleNamespace(database=object()), SimpleNamespace(session_id="same"),
                         replace(grant(), policy={"product": {"repository_id": 123, "base_url": "https://gitlab.example.com"}}))
    assert p.operation_id("create_branch", "same-key") != other.operation_id("create_branch", "same-key")
    import hashlib
    assert p.operation_id("create_branch", "same-key") != hashlib.sha256(b"same:create_branch:same-key").hexdigest()


@pytest.mark.parametrize("text", [
    "/merge", "hello\n /approve", "\t/close", "```text\n/merge\n```", "\r/merge",
    "description\r/merge", "description\u2028/approve",
    "/", "/123", "/_command", "hello\n /!", "hello\x00world",
])
def test_quick_action_write_channels_refused(text):
    with pytest.raises(ValidationError):
        CreateMR(repository_id=123, request_key="quick-actions", base_sha=BASE,
                 title="Safe title", body=text, files=[{"path": "x", "content": "x"}])
    with pytest.raises(ToolFailed):
        validate_request("POST", "/projects/123/merge_requests/7/draft_notes", 123, {"note": text})


@pytest.mark.parametrize("title", ["Draft: feature", "WIP: change", "[Draft] x", "(WIP) x"])
def test_regular_creation_only(title):
    with pytest.raises(ValidationError):
        OpenMR(request_key="regular-mr", base_branch="main", head_branch="feature",
               expected_head_sha=HEAD, title=title, body="Description")


@pytest.mark.parametrize("branch", ["", "HEAD", "a..b", "a b", "a.lock", "/main", "feature~1", None])
def test_retarget_rejects_invalid_branch(branch):
    with pytest.raises(ToolFailed):
        validate_request("PUT", "/projects/123/merge_requests/7", 123, {"target_branch": branch})


def test_retarget_accepts_branch():
    validate_request("PUT", "/projects/123/merge_requests/7", 123, {"target_branch": "release/next"})


@pytest.mark.parametrize("changed", [{"id": 124}, {"path_with_namespace": "group/renamed"}, {}, None])
@pytest.mark.parametrize("operation", ["push", "request-changes"])
async def test_path_write_rechecks_identity_at_transport_boundary(monkeypatch, changed, operation):
    value = {} if changed == {} else {**PROJECT, **(changed or {})}
    provider = SimpleNamespace(request=AsyncMock(return_value=value))

    async def call(callback, **kwargs):
        return await callback(SimpleNamespace(gitlab=provider))

    service = SimpleNamespace(database=object(), execution=SimpleNamespace(call=call), check=AsyncMock(),
                              connectors=SimpleNamespace(gitlab=provider))
    p = Publications(service, SimpleNamespace(), grant())
    monkeypatch.setattr(p, "credentials", AsyncMock(return_value={"access_token": "synthetic-token"}))
    if operation == "request-changes":
        action = p.request("POST", "/graphql", json={
            "query": REQUEST_CHANGES, "variables": {"projectPath": PROJECT["path_with_namespace"], "iid": "7"},
        })
    else:
        document = {"intent": {"branch": "feature", "project": PROJECT["path_with_namespace"]}, "commit": HEAD}
        monkeypatch.setattr(p, "document", lambda row: document)

        async def dispatch(row, key, callback):
            return await callback()

        monkeypatch.setattr(p, "dispatch", dispatch)
        workflows = Workflows(p)
        monkeypatch.setattr(workflows, "writable", AsyncMock(return_value=None))
        monkeypatch.setattr(workflows, "push_receipt", lambda row, response: response)
        push = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(transport, "push", push)
        action = workflows.push(SimpleNamespace())
    if changed is not None:
        with pytest.raises(ToolFailed):
            await action
    else:
        await action
    assert provider.request.call_args_list[0].args == ("GET", "/projects/123")
    if operation == "push":
        assert push.await_count == (1 if changed is None else 0)
        assert provider.request.await_count == 1
    else:
        assert provider.request.await_count == (2 if changed is None else 1)
        if changed is None:
            assert provider.request.call_args_list[1].args == ("POST", "/graphql")


@pytest.mark.parametrize("method,path,body", [
    ("PUT", "/projects/123/merge_requests/7/merge", {}),
    ("POST", "/projects/123/merge_requests/7/approve", {}),
    ("DELETE", "/projects/123/repository/branches/feature", {}),
    ("POST", "/projects/123/merge_requests/7/draft_notes/bulk_publish", {}),
    ("POST", "/projects/123/repository/commits", {"actions": []}),
    ("GET", "/projects/124/repository/tree", None),
    ("POST", "/graphql", {"query": "mutation { merge }", "variables": {}}),
    ("GET", "/projects/123/repository/files/%2e%2e%2fsecret", None),
])
def test_closed_request_vocabulary(method, path, body):
    with pytest.raises(ToolFailed):
        validate_request(method, path, 123, body)


def test_fixed_graphql_queries_and_staged_input_validation():
    validate_request("POST", "/graphql", 123, {"query": REVIEW_CAPABILITY})
    validate_request("POST", "/graphql", 123, {"query": REQUEST_CHANGES,
                                            "variables": {"projectPath": "group/sub/repo", "iid": "7"}})
    with pytest.raises(ValidationError):
        PublishBranch(request_key="branch-head", base_sha=BASE, expected_head_sha=HEAD,
                      branch="feature", title="Change files", files=[{"path": "x", "content": "x"}])
    with pytest.raises(ValidationError):
        Changes(request_key="both-inputs", base_sha=BASE, title="Change files", directory="/workspace/repo",
                files=[{"path": "x", "content": "x"}])
    with pytest.raises(ValidationError):
        Changes(request_key="case-input", base_sha=BASE, title="Change files",
                files=[{"path": "A", "content": "x"}, {"path": "a", "content": "y"}])
    with pytest.raises(ValidationError):
        CreateReview(request_key="wrong-head", number=7, head_sha=HEAD, base_branch="main", comments=[{
            "body": "note", "position": {"base_sha": BASE, "start_sha": BASE, "head_sha": BASE,
                                       "old_path": "x", "new_path": "x", "new_line": 1},
        }])


async def test_wildcard_inherited_protection_and_incomplete_pages_fail_closed():
    repo = Project.model_validate(PROJECT)
    async def request(_method, path, **kwargs):
        if "/repository/branches/" in path:
            return None
        return [{"name": "release/*", "inherited": True}]
    p = SimpleNamespace(prefix="/projects/123", request=request)
    with pytest.raises(ToolFailed, match="inherited"):
        await require_writable(p, repo, "release/sub/path", existing=False)
    with pytest.raises(ToolFailed, match="default"):
        await require_writable(p, repo, "main", existing=True)
    assert pattern_matches("feature/*", "feature/a/b")
    with pytest.raises(ToolFailed):
        pattern_matches("feature/[abc]", "feature/a")
    async def many(_method, path, **kwargs):
        return None if "/repository/branches/" in path else [{"name": "unrelated"}] * 100
    p.request = many
    with pytest.raises(ToolFailed, match="limit"):
        await require_writable(p, repo, "feature", existing=False)


async def test_mr_diff_pages_reject_changed_revision_and_search_is_project_scoped():
    p = SimpleNamespace(target=123, prefix="/projects/123", check_target=AsyncMock())
    p.request = AsyncMock(return_value=[{"diff": "a", "too_large": True}])
    reader = Reads(p)
    reader.w.read_mr = AsyncMock(side_effect=[MergeRequest.model_validate(mr()), MergeRequest.model_validate(mr(sha="c" * 40))])
    with pytest.raises(ToolFailed, match="changed while"):
        await reader.call("merge_request_diffs", MRRead(number=7))
    result = await reader.call("search", Search(scope="blobs", query="needle"))
    assert result["coverage"] == "page_only"
    assert p.request.call_args.args[1] == "/projects/123/search"
    assert p.request.call_args.kwargs["params"]["search"] == "needle"


async def test_following_diff_pages_requires_both_head_and_base():
    p = SimpleNamespace(target=123, prefix="/projects/123", check_target=AsyncMock())
    p.request = AsyncMock(return_value=[])
    reader = Reads(p)
    reader.w.read_mr = AsyncMock(return_value=MergeRequest.model_validate(mr()))
    with pytest.raises(ToolFailed, match="base_sha"):
        await reader.call("merge_request_diffs", MRRead(number=7, page=2, head_sha=HEAD))
    p.request.assert_not_awaited()
    result = await reader.call("merge_request_diffs", MRRead(number=7, page=2, head_sha=HEAD, base_sha=BASE))
    assert result["head_sha"] == HEAD and result["base_sha"] == BASE
    reader.w.read_mr = AsyncMock(return_value=MergeRequest.model_validate(mr(
        diff_refs={"base_sha": "c" * 40, "start_sha": BASE, "head_sha": HEAD},
    )))
    p.request.reset_mock()
    with pytest.raises(ToolFailed, match="base changed"):
        await reader.call("merge_request_diffs", MRRead(number=7, page=2, head_sha=HEAD, base_sha=BASE))
    p.request.assert_not_awaited()


async def test_transport_environment_and_ref_lease_never_use_token_argv_or_config(monkeypatch):
    calls = []
    async def fake_git(args, root, env, **kwargs):
        calls.append((args, root, dict(env)))
        assert "sensitive-test-token" not in json.dumps(args)
        if args[0] == "push":
            ref = "refs/heads/feature"
            return f"To https://gitlab.com/group/repo.git\n*\t{HEAD}:{ref}\t[new branch]\nDone\n".encode()
        return b""
    monkeypatch.setattr(transport, "git", fake_git)
    monkeypatch.setattr(transport, "trusted_environment", AsyncMock(return_value=transport.environment("synthetic")))
    result = await transport.push(base_url="https://gitlab.com", project_path="group/repo",
                                  token="sensitive-test-token", branch="feature", commit=HEAD,
                                  expected_head=None, bundle=b"test-bundle")
    assert result["acknowledged"] is True
    push = next(call for call in calls if call[0][0] == "push")
    assert "--force-with-lease=refs/heads/feature:" in push[0]
    assert "--force" not in push[0]
    assert "http.followRedirects" in push[2].values()
    assert "false" in push[2].values()
    assert not any("TOKEN" in key and key != "GIT_CONFIG_VALUE_10" for key in push[2])
    with pytest.raises(ValueError, match="explicit"):
        await transport.fetch_bundle(base_url="http://127.0.0.1:58990", project_path="group/repo",
                                     commit=HEAD, branch="feature", token="x", destination=Path("/unused"))


async def test_git_prepared_commit_preserves_binary_modes_deletion_and_rejects_symlink(tmp_path, monkeypatch):
    """Actual local Git objects; no refs in the shared working checkout are touched."""
    source = tmp_path / "source"
    source.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=source, check=True, capture_output=True).stdout.decode().strip()
    git("init", "-b", "main")
    (source / "remove.txt").write_text("remove")
    (source / "keep.txt").write_text("keep")
    (source / "link").symlink_to("keep.txt")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Base")
    base = git("rev-parse", "HEAD")
    original_fetch = transport.fetch
    async def local_fetch(root, env, _url, commit):
        # Test replaces only network boundary; all preparation uses production Git code.
        env = dict(env)
        count = int(env["GIT_CONFIG_COUNT"])
        env.update({f"GIT_CONFIG_KEY_{count}": "protocol.file.allow", f"GIT_CONFIG_VALUE_{count}": "always",
                    "GIT_CONFIG_COUNT": str(count + 1)})
        await original_fetch(root, env, str(source), commit)
    monkeypatch.setattr(transport, "fetch", local_fetch)
    monkeypatch.setattr(transport, "trusted_environment", AsyncMock(side_effect=lambda *_a, **_kw: transport.environment("synthetic")))
    files = [
        Change(path="remove.txt", content=None),
        Change(path="run.sh", content="#!/bin/sh\ntrue\n", executable=True),
        Change(path="binary.bin", content=base64.b64encode(b"\x00\xff\x01").decode(), encoding="base64"),
    ]
    prepared = await transport.prepare_commit(base_url="https://gitlab.com", project_path="group/repo",
                                              base_sha=base, token="synthetic", files=files,
                                              title="Prepared change", timestamp=1800000000)
    bundle = tmp_path / "prepared.bundle"
    bundle.write_bytes(prepared.bundle)
    git("fetch", str(bundle), prepared.sha)
    assert git("show", prepared.sha + ":keep.txt") == "keep"
    assert git("ls-tree", prepared.sha, "run.sh").startswith("100755 ")
    assert git("ls-tree", prepared.sha, "remove.txt") == ""
    raw = (await asyncio.to_thread(subprocess.run, ["git", "show", prepared.sha + ":binary.bin"],
                                   cwd=source, check=True, capture_output=True)).stdout
    assert raw == b"\x00\xff\x01"
    assert git("rev-parse", prepared.sha + "^") == base
    with pytest.raises(ToolFailed, match="symlinks"):
        await transport.prepare_commit(base_url="https://gitlab.com", project_path="group/repo",
                                       base_sha=base, token="synthetic", files=[Change(path="link", content="replace")],
                                       title="Invalid change", timestamp=1800000000)


async def test_actual_git_empty_and_exact_leases_refuse_racing_updates(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    def run(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True).stdout.decode().strip()
    run("init", "--bare", str(remote))
    run("init", "-b", "main", str(checkout))
    def local(*args):
        return run("-C", str(checkout), *args)
    (checkout / "a").write_text("one")
    local("add", ".")
    local("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "base")
    first = local("rev-parse", "HEAD")
    local("push", str(remote), "HEAD:refs/heads/main")
    (checkout / "a").write_text("two")
    local("add", ".")
    local("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "next")
    second = local("rev-parse", "HEAD")
    local("push", str(remote), "HEAD:refs/heads/main")
    monkeypatch.setattr(transport, "permitted_remote", lambda *_args: str(remote))
    async def local_environment(*_args, **_kwargs):
        env = transport.environment("synthetic")
        index = int(env["GIT_CONFIG_COUNT"])
        env.update({f"GIT_CONFIG_KEY_{index}": "protocol.file.allow", f"GIT_CONFIG_VALUE_{index}": "always",
                    "GIT_CONFIG_COUNT": str(index + 1)})
        return env
    monkeypatch.setattr(transport, "trusted_environment", local_environment)
    args = {"base_url": "https://gitlab.com", "project_path": "group/repo", "token": "synthetic", "branch": "feature"}
    created = await transport.push(**args, commit=first, expected_head=None)
    assert created["commit"] == first
    updated = await transport.push(**args, commit=second, expected_head=first)
    assert updated["before"] == first and updated["commit"] == second
    with pytest.raises(ToolFailed):
        await transport.push(**args, commit=first, expected_head=None)
    with pytest.raises(ToolFailed):
        await transport.push(**args, commit=first, expected_head=first, allow_rebase=True)
    assert run("--git-dir", str(remote), "rev-parse", "refs/heads/feature") == second
