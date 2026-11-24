"""Cached orientation must never widen the current repository authority."""

import json
from dataclasses import replace

import pytest
import test_product_connections as connection_fixtures
from app.repository_context import GUIDE_BYTES
from fastapi import HTTPException
from test_product_connections import github_accounts

from moyai.context.github import GitHubRepositories, make_repository_names
from moyai.context.repository_guides import RepositoryGuides
from moyai.contracts import Grant, RunScope

bridge = connection_fixtures.bridge


def publish(service, target, *, guide=None):
    github = service.connectors.github
    credentials = github.saved_credentials()
    github.remember_repository({
        "id": target, "full_name": f"org/repository-{target}", "owner": {"id": 1},
    }, credentials)
    content = guide or {
        "coverage": {"indexed_paths": 2, "paths_incomplete": False, "failed_documents": 0},
        "available": [{"path": "README.md", "text": f"Run pytest for repository {target}"}],
    }
    github.context_index.publish(
        github.connection_version(), target,
        {"base_sha": "a" * 40, "repository": f"org/repository-{target}", "default_branch": "main"},
        content, [], {},
    )


async def selected(service, *ids):
    available = await service.resolve_grants("google:owner", ["github"])
    selections = [g.id for g in available if g.policy["product"]["repository_id"] in ids]
    grants = await service.resolve_grants("google:owner", selections)
    return RunScope("session", "turn", "google:owner", "test", grants)


async def test_only_selected_repository_guide_is_in_native_context(bridge):
    github_accounts(bridge, 2)
    publish(bridge, 1)
    publish(bridge, 2)
    scope = await selected(bridge, 1)
    rendered = await GitHubRepositories(
        make_repository_names(bridge), RepositoryGuides(bridge),
    ).render(scope)
    assert "org/repository-1" in rendered and "org/repository-2" not in rendered
    assert "a" * 40 in rendered and "indexed_at" in rendered and "coverage" in rendered
    assert "Run pytest" in rendered and "untrusted reference" in rendered
    assert "verify build/test commands" in rendered


@pytest.mark.parametrize("condition", ["disabled", "cold", "rotated", "renamed"])
async def test_unavailable_cache_keeps_repository_names_and_tools(bridge, condition):
    github_accounts(bridge, 1)
    publish(bridge, 1)
    github = bridge.connectors.github
    if condition == "disabled":
        bridge.settings.repository_context_enabled = False
    elif condition == "cold":
        bridge.store.execute("DELETE FROM repository_context_heads")
    elif condition == "rotated":
        github_accounts(bridge, 1)
        github.remember_repository(
            {"id": 1, "full_name": "org/repository-1", "owner": {"id": 1}},
            github.saved_credentials(),
        )
    else:
        github.remember_repository(
            {"id": 1, "full_name": "org/renamed", "owner": {"id": 1}},
            github.saved_credentials(),
        )
    scope = await selected(bridge, 1)
    rendered = await GitHubRepositories(
        make_repository_names(bridge), RepositoryGuides(bridge),
    ).render(scope)
    assert "Connected GitHub repositories" in rendered
    assert "Run pytest" not in rendered


@pytest.mark.parametrize(
    "value", ["not-json", "[]", '{"too_large":"' + "x" * GUIDE_BYTES + '"}', "invalid-ciphertext"],
    ids=["invalid-json", "array", "oversized", "invalid-ciphertext"],
)
async def test_corrupt_or_oversized_cache_is_omitted(bridge, value):
    github_accounts(bridge, 1)
    publish(bridge, 1)
    ciphertext = value if value == "invalid-ciphertext" else bridge.security.encrypt(value)
    bridge.store.execute("UPDATE repository_context_snapshots SET guide=?", (ciphertext,))
    assert await RepositoryGuides(bridge)(await selected(bridge, 1), [1]) == ""


async def test_personal_oauth_and_invalid_ids_do_not_inherit_shared_cache(bridge):
    github_accounts(bridge, 1)
    publish(bridge, 1)
    scope = await selected(bridge, 1)
    original = scope.grants[0]
    grants = [
        replace(original, policy={**original.policy, "oauth": {"owner_id": "google:owner"}}),
        Grant(id="invalid", kind="github", revision=1, credential="", policy={"product": {"repository_id": True}}),
    ]
    assert await RepositoryGuides(bridge)(replace(scope, grants=grants), [1]) == ""


async def test_guides_are_bounded_and_duplicate_ids_do_not_expand_context(bridge):
    github_accounts(bridge, 4)
    for identity in range(1, 5):
        publish(bridge, identity)
    scope = await selected(bridge, 1, 2, 3, 4)
    rendered = await RepositoryGuides(bridge)(scope, [1, 2, 3, 4, 1])
    guides = json.loads(rendered.split("\n", 1)[1])
    assert len(guides) == 3
    assert len({guide["repository_id"] for guide in guides}) == 3


@pytest.mark.parametrize("when", ["before", "during"])
async def test_revocation_cannot_disclose_cached_source(bridge, monkeypatch, when):
    github_accounts(bridge, 1)
    publish(bridge, 1)
    scope = await selected(bridge, 1)
    reader = RepositoryGuides(bridge)

    def revoke():
        bridge.store.execute("DELETE FROM connections WHERE provider='github'")

    if when == "before":
        revoke()
    else:
        original = reader._cached

        def changed(target):
            result = original(target)
            revoke()
            return result

        monkeypatch.setattr(reader, "_cached", changed)
    with pytest.raises((HTTPException, PermissionError)):
        await reader(scope, [1])
