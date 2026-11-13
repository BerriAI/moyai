from types import SimpleNamespace
from typing import cast

import pytest

from moyai.context.gitlab import GitLabProjects
from moyai.contracts import Grant, RunScope
from moyai.product_connections import ProductConnections


class Metadata:
    origin = "https://gitlab.example"

    def saved_credentials(self):
        return {"base_url": self.origin}

    def repository_options(self, credentials):
        return [{"id": 1, "full_name": "selected/project"}, {"id": 2, "full_name": "other/private-project"}]


def setup(*, origin="https://gitlab.example", revoke=False):
    adapter, checks = Metadata(), []

    async def check(scope, grant):
        checks.append(grant.id)
        if revoke and len(checks) == 2:
            raise PermissionError("Connection revoked.")

    async def require_scope(scope):
        return None

    service = cast(ProductConnections, SimpleNamespace(
        connectors=SimpleNamespace(gitlab=adapter), check=check, require_scope=require_scope,
    ))
    scope = RunScope("session", "turn", "actor", "model", grants=[
        Grant("selected", "gitlab", 1, "", policy={"product": {"repository_id": 1, "base_url": origin}}),
    ])
    return GitLabProjects(service), scope, checks


async def test_gitlab_context_contains_only_current_selected_project_and_origin():
    context, scope, checks = setup()
    result = await context.render(scope)
    assert "https://gitlab.example" in result and "selected/project (project ID 1)" in result
    assert "other/private-project" not in result
    assert checks == ["selected", "selected"]


async def test_gitlab_context_rejects_equal_project_id_on_changed_origin():
    context, scope, _ = setup(origin="https://another-gitlab.example")
    with pytest.raises(PermissionError, match="origin changed"):
        await context.render(scope)


async def test_gitlab_context_rechecks_authority_after_metadata_read():
    context, scope, _ = setup(revoke=True)
    with pytest.raises(PermissionError, match="revoked"):
        await context.render(scope)
