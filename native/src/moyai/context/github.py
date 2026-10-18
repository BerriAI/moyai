from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Protocol

from app.connector_errors import ConnectorError
from app.db import database as source_database

from ..contracts import RunScope
from .base import ContextSource

if TYPE_CHECKING:
    from ..product_connections import ProductConnections


class RepositoryNames(Protocol):
    async def __call__(self, repository_ids: Sequence[int]) -> list[str]: ...


def make_repository_names(connections: ProductConnections) -> RepositoryNames:
    async def resolve(repository_ids: Sequence[int]) -> list[str]:
        async def repository_name(repository_id: int) -> str | None:
            try:
                return await source_database(
                    connections.connectors.github.repository_name,
                    repository_id,
                )
            except ConnectorError:
                return None

        names = await asyncio.gather(*(repository_name(repository_id) for repository_id in repository_ids))
        return [name for name in names if name is not None]

    return resolve


class GitHubRepositories(ContextSource):
    name: ClassVar[str] = "github"

    def __init__(self, repository_names: RepositoryNames):
        self.repository_names = repository_names

    async def render(self, scope: RunScope) -> str:
        repository_ids = tuple(dict.fromkeys(
            repository_id
            for grant in scope.grants
            if grant.kind == "github"
            for repository_id in (grant.policy.get("product", {}).get("repository_id"),)
            if type(repository_id) is int and repository_id > 0
        ))
        if not repository_ids:
            return ""
        names = await self.repository_names(repository_ids)
        if not names:
            return ""
        repositories = ", ".join(sorted(names)[:30])
        return (
            "Connected GitHub repositories for this session: "
            f"{repositories}. Use them to work out what the user's team works on."
        )
