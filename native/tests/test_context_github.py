from collections.abc import Sequence
from types import SimpleNamespace
from typing import cast

import pytest
from app.connector_errors import ConnectorError

from moyai.context.github import GitHubRepositories, make_repository_names
from moyai.contracts import Grant, RunScope
from moyai.product_connections import ProductConnections


class Names:
    def __init__(self, names: list[str]):
        self.names = names
        self.calls: list[tuple[int, ...]] = []

    async def __call__(self, repository_ids: Sequence[int]) -> list[str]:
        self.calls.append(tuple(repository_ids))
        return self.names


def github_grant(repository_id: object, grant_id: str) -> Grant:
    return Grant(
        id=grant_id,
        kind="github",
        revision=1,
        credential="",
        policy={"product": {"repository_id": repository_id}},
    )


def scope(*grants: Grant) -> RunScope:
    return RunScope(
        session_id="session",
        turn_id="turn",
        actor_id="actor",
        model="test",
        grants=list(grants),
    )


@pytest.mark.asyncio
async def test_github_context_resolves_only_github_grants_and_sorts_names() -> None:
    names = Names(["BerriAI/moyai", "BerriAI/litellm"])
    context = GitHubRepositories(names)

    rendered = await context.render(scope(
        github_grant(101, "first"),
        Grant(id="other", kind="slack", revision=1, credential=""),
        github_grant(102, "second"),
    ))

    assert rendered == (
        "Connected GitHub repositories for this session: BerriAI/litellm, BerriAI/moyai. "
        "Use them to work out what the user's team works on."
    )
    assert names.calls == [(101, 102)]


@pytest.mark.asyncio
async def test_duplicate_repository_ids_are_deduplicated_before_resolution() -> None:
    names = Names(["BerriAI/moyai"])

    await GitHubRepositories(names).render(scope(
        github_grant(101, "first"),
        github_grant(101, "duplicate"),
        github_grant(102, "second"),
    ))

    assert names.calls == [(101, 102)]


@pytest.mark.asyncio
async def test_no_github_grants_does_not_call_resolver() -> None:
    names = Names(["unexpected"])

    rendered = await GitHubRepositories(names).render(scope(
        Grant(id="slack", kind="slack", revision=1, credential=""),
    ))

    assert rendered == ""
    assert names.calls == []


@pytest.mark.asyncio
async def test_invalid_github_repository_ids_do_not_call_resolver() -> None:
    names = Names(["unexpected"])

    rendered = await GitHubRepositories(names).render(scope(
        github_grant(True, "boolean"),
        github_grant(0, "zero"),
    ))

    assert rendered == ""
    assert names.calls == []


@pytest.mark.asyncio
async def test_empty_resolution_returns_no_context() -> None:
    names = Names([])

    rendered = await GitHubRepositories(names).render(scope(github_grant(101, "first")))

    assert rendered == ""
    assert names.calls == [(101,)]


@pytest.mark.asyncio
async def test_github_context_caps_sorted_repository_names_at_thirty() -> None:
    names = Names([f"org/repo-{index:02}" for index in range(31)])

    rendered = await GitHubRepositories(names).render(scope(github_grant(101, "first")))

    listed = rendered.split(": ", maxsplit=1)[1].split(". Use them", maxsplit=1)[0].split(", ")
    assert listed == sorted(names.names)[:30]


@pytest.mark.asyncio
async def test_production_repository_resolver_skips_connector_errors() -> None:
    class GitHub:
        def repository_name(self, repository_id: int) -> str:
            if repository_id == 102:
                raise ConnectorError("repository metadata unavailable")
            return f"org/repo-{repository_id}"

    connections = cast(
        ProductConnections,
        SimpleNamespace(connectors=SimpleNamespace(github=GitHub())),
    )

    resolved = await make_repository_names(connections)([101, 102, 103])

    assert resolved == ["org/repo-101", "org/repo-103"]
