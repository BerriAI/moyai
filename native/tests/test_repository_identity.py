"""URL normalization never establishes provider authority by itself."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.memory import Memory, Note
from app.repository_identity import (
    canonical_origin,
    canonical_repository_url,
    repository_origin,
    repository_provider,
)
from fastapi import HTTPException
from pydantic_ai.exceptions import ToolFailed

from moyai.contracts import Grant, RunScope
from moyai.product_continuation_workspace import ProductWorkspaceCompatibility
from moyai.product_identity import ProductGrant
from moyai.product_memory_store import ProductMemoryStore


@pytest.mark.parametrize(("url", "expected", "provider"), [
    ("", "", "github"),
    ("https://github.com/Owner/Repo.git/", "https://github.com/Owner/Repo", "github"),
    ("https://gitlab.com/group/subgroup/repo.git", "https://gitlab.com/group/subgroup/repo", "gitlab"),
    ("https://GitLab.Example.test:443/Group/Sub/Repo/", "https://gitlab.example.test/Group/Sub/Repo", "gitlab"),
    ("https://gitlab.example.test:8443/group/sub/repo", "https://gitlab.example.test:8443/group/sub/repo", "gitlab"),
    ("http://127.0.0.1:8929/group/sub/repo.git", "http://127.0.0.1:8929/group/sub/repo", "gitlab"),
    ("http://[::1]:8929/group/sub/repo", "http://[::1]:8929/group/sub/repo", "gitlab"),
])
def test_canonical_repository_identity_keeps_origin_and_nested_project(url, expected, provider):
    assert canonical_repository_url(url) == expected
    assert repository_provider(url) == provider


@pytest.mark.parametrize("url", [
    "https://user:secret@gitlab.com/a/b", "https://@gitlab.com/a/b",
    "https://gitlab.com/a/b?", "https://gitlab.com/a/b#", "https://gitlab.com/a/b?token=secret",
    "https://gitlab.com/a/../b", "https://gitlab.com/a/%2e%2e/b", "https://gitlab.com/a/%252e%252e/b",
    "https://gitlab.com/a//b", "https://gitlab.com//a/b", "https://gitlab.com/a/b//",
    "https://gitlab.com/a/b\n", "https://gitlab.com/a/b\\c", "https://gitlab.com/a/.",
    "file:///a/b", "http://gitlab.example.test/a/b", "http://127.0.0.1.evil.test/a/b",
    "https://github.com:443/a/b", "https://github.com/a/b/c",
    "https://127.0.0.1/a/b", "https://gitlab.example.test:0/a/b", "https://gitlab.example.test./a/b",
    "https://gitlab.example.test:/a/b",
])
def test_ambiguous_and_credential_bearing_repository_urls_fail(url):
    with pytest.raises(ValueError):
        canonical_repository_url(url)
    with pytest.raises(ValueError):
        repository_provider(url)


def test_origin_normalization_and_memory_preserve_legacy_github_names():
    assert canonical_origin("https://GitLab.Example.test:443/") == "https://gitlab.example.test"
    assert repository_origin("http://localhost:8929/group/sub/repo.git") == "http://localhost:8929"
    with pytest.raises(ValueError):
        canonical_origin("https://gitlab.com/api/v4")
    assert Note.repository("https://github.com/Owner/Project.git") == "https://github.com/owner/project"
    assert Note.repository("https://gitlab.example.test/Group/Sub/Project.git") == "https://gitlab.example.test/Group/Sub/Project"


def gitlab_grant(origin="https://gitlab.example.test", target=42, provider="gitlab"):
    return Grant("connection", provider, 1, "", policy={"product": {"base_url": origin, "repository_id": target}})


def selection(**changes):
    return {
        "repo_url": "https://gitlab.example.test/group/sub/project",
        "gitlab_project_id": 42, "environment_id": "none", **changes,
    }


def workspace():
    return ProductWorkspaceCompatibility(SimpleNamespace(
        product_identity=SimpleNamespace(schema="source"), product_connections=SimpleNamespace(prepare_repository=AsyncMock()),
    ))


async def test_bare_gitlab_selection_freezes_provider_origin_id_without_querying_builds():
    db = SimpleNamespace(execute=AsyncMock(side_effect=AssertionError("GitHub-only catalog must not be queried")))
    context = await workspace()._selection(db, selection(), require_ready=True)
    assert context["repository_provider"] == "gitlab"
    assert context["repository_origin"] == "https://gitlab.example.test"
    assert context["gitlab_project_id"] == context["repository_id"] == 42
    assert context["selection"]["gitlab_project_id"] == 42
    assert "github_repository_id" not in context["selection"]
    assert context["environment_id"] == "none" and context["environment_build_id"] is None
    ProductWorkspaceCompatibility.authorize_project_grants(context, [gitlab_grant()])
    for grants in ([], [gitlab_grant(target=43)], [gitlab_grant(provider="github")], [gitlab_grant(origin="https://gitlab.com")]):
        with pytest.raises(HTTPException):
            ProductWorkspaceCompatibility.authorize_project_grants(context, grants)


@pytest.mark.parametrize("changes", [
    {"github_repository_id": 42}, {"gitlab_project_id": None}, {"gitlab_project_id": True},
    {"repository_provider": "github"}, {"repository_origin": "https://other.example.test"},
    {"environment_id": "invalid"}, {"environment_build_id": "invalid"},
])
async def test_gitlab_selection_refuses_identity_confusion_and_invalid_environment_references(changes):
    with pytest.raises(HTTPException):
        await workspace()._selection(None, selection(**changes), require_ready=True)


async def test_legacy_github_context_shape_and_fingerprint_remain_unchanged():
    context = await workspace()._selection(
        None, {"repo_url": "https://github.com/Owner/Project", "github_repository_id": 42, "environment_id": "none"},
        require_ready=True,
    )
    assert context["selection"] == {
        "repo_url": "https://github.com/Owner/Project", "github_repository_id": 42, "environment_id": "none",
    }
    assert "repository_provider" not in context and "gitlab_project_id" not in context


@pytest.mark.parametrize("changes", [
    {"repository_origin": "https://other.example.test"}, {"gitlab_project_id": 43},
    {"repository_provider": "github"},
])
async def test_saved_gitlab_context_cannot_change_provider_origin_or_project(changes):
    instance = workspace()
    original = await instance._selection(None, selection(), require_ready=True)
    instance.capture_workspace_context = AsyncMock(return_value={**original, **changes})
    with pytest.raises(HTTPException, match="409"):
        await instance.validate_workspace_context(None, None, selection(), original)


@pytest.mark.parametrize(("grants", "changes", "accepted"), [
    ([gitlab_grant()], {}, True),
    ([], {}, False),
    ([gitlab_grant(provider="github")], {}, False),
    ([gitlab_grant(origin="https://other.example.test")], {}, False),
    ([gitlab_grant(target=43)], {}, False),
    ([gitlab_grant()], {"repository_provider": "github"}, False),
    ([gitlab_grant()], {"gitlab_project_id": 43}, False),
    ([gitlab_grant()], {"repository_origin": "https://other.example.test"}, False),
    ([gitlab_grant()], {"repository_id": True}, False),
    ([gitlab_grant()], {"repo_url": ""}, False),
])
async def test_gitlab_memory_requires_provider_origin_and_selected_project(grants, changes, accepted):
    descriptor = {
        "repository_provider": "gitlab", "repository_origin": "https://gitlab.example.test",
        "gitlab_project_id": 42, "repository_id": 42, "repo_url": selection()["repo_url"],
        "source_schema": "source", "context_fingerprint": "f" * 64, **changes,
    }
    source = AsyncMock(return_value=descriptor)
    service = SimpleNamespace(identity=SimpleNamespace(schema="source"))
    scope = RunScope("session", "turn", "actor", "model", grants, product_grant=ProductGrant("actor", "authority"))
    store = ProductMemoryStore(service, scope, "turn", repository_scope=source)
    if accepted:
        assert await store._repository() == selection()["repo_url"]
    else:
        with pytest.raises(ToolFailed):
            await store._repository()
    source.assert_awaited_once_with("session", scope.product_grant, turn_id="turn")


def test_source_memory_does_not_trust_a_mentioned_gitlab_url_or_numeric_github_id():
    memory = Memory.__new__(Memory)
    memory.github = None
    with pytest.raises(HTTPException):
        memory.selected_repository({"repo_url": selection()["repo_url"], "github_repository_id": 42, "plugins": ["github"]})
    helper = SimpleNamespace(
        saved_credentials=Mock(return_value={"base_url": "https://gitlab.example.test", "repository_ids": [42]}),
        target=Mock(return_value=42),
    )
    memory.github = SimpleNamespace(connectors=SimpleNamespace(
        gitlab=helper, policy=Mock(return_value={"enabled": True}),
    ))
    run = {**selection(), "plugins": ["gitlab"]}
    assert memory.selected_repository(run) == selection()["repo_url"]
    helper.saved_credentials.return_value["base_url"] = "https://other.example.test"
    helper.target.reset_mock()
    with pytest.raises(HTTPException):
        memory.selected_repository(run)
    helper.target.assert_not_called()
