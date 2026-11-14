"""Make selected GitLab project identities available without exposing credentials."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from app.db import database as source_database
from app.repository_identity import canonical_origin

from ..contracts import RunScope
from .base import ContextSource

if TYPE_CHECKING:
    from ..product_connections import ProductConnections


class GitLabProjects(ContextSource):
    name: ClassVar[str] = "gitlab"

    def __init__(self, connections: ProductConnections):
        self.connections = connections

    async def render(self, scope: RunScope) -> str:
        grants = [grant for grant in scope.grants if grant.kind == "gitlab"]
        projects = {
            value
            for grant in grants
            for value in (grant.policy.get("product", {}).get("repository_id"),)
            if type(value) is int and value > 0
        }
        if not projects:
            return ""
        await self.connections.require_scope(scope)
        for grant in grants:
            await self.connections.check(scope, grant)

        def selected_metadata():
            adapter = self.connections.connectors.gitlab
            credentials = adapter.saved_credentials()
            origin = canonical_origin(credentials["base_url"])
            if any(canonical_origin(grant.policy["product"]["base_url"]) != origin for grant in grants):
                raise PermissionError("The selected GitLab origin changed.")
            return origin, adapter.repository_options(credentials)

        origin, available = await source_database(selected_metadata)
        for grant in grants:
            await self.connections.check(scope, grant)
        names = sorted(
            f"{project['full_name']} (project ID {project['id']})"
            for project in available if project["id"] in projects
        )
        if not names:
            return ""
        return (
            f"Connected GitLab projects on {origin} for this session: " + ", ".join(names[:30]) + ". "
            "Discover the selected project's GitLab tools to read, check out, publish changes, "
            "and work with merge requests. Project IDs and merge request numbers are distinct; "
            "a merge request number is local to its project. Credentials remain on the server."
        )
