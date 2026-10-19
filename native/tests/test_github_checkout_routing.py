"""Shared checkout discovery and authorization; provider fixtures supplement live E2E."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from test_product_connections import bridge, github_accounts, scope_for

from moyai.connections import connection_tools
from moyai.product_connections import ProductGitHubToolset
from moyai.product_github_checkout import RepositoryCheckout, checkout_toolset

__all__ = ["bridge"]


@pytest.fixture
async def selected(bridge, monkeypatch):
    credentials = github_accounts(bridge, 3)
    for identity, name in enumerate(["BerriAI/other", "BerriAI/moyai", "BerriAI/unselected"], start=1):
        bridge.connectors.github.remember_repository(
            {"id": identity, "full_name": name, "owner": {"id": 1}}, credentials,
        )
    scope = await scope_for(bridge, "github")
    scope = replace(scope, grants=[grant for grant in scope.grants if grant.policy["product"]["repository_id"] != 3])
    ctx = RunContext(deps=scope, model=TestModel(), usage=RunUsage())
    monkeypatch.setattr("moyai.runtime.current", lambda: SimpleNamespace(product_connections=bridge))
    calls = []

    async def install(publisher, context, request):
        await publisher.check_target(request)
        await publisher.service.check(publisher.scope, publisher.grant)
        calls.append((publisher.target, publisher.grant.id, context.deps.turn_id))
        return {"repository_id": publisher.target, "directory": request.directory}

    monkeypatch.setattr("moyai.product_github_checkout.checkout", install)
    return ctx, calls


async def invoke(tools, ctx, arguments):
    tool = (await tools.get_tools(ctx))["github_checkout"]
    args = tool.args_validator.validate_python(arguments)
    return await tools.call_tool("github_checkout", args, ctx, tool)


@pytest.mark.parametrize("repository", [
    {"repository": "BerriAI/moyai"},
    {"repository": "berriai/MOYAI"},
    {"repository_id": 2},
    {"repository": "BerriAI/moyai", "repository_id": 2},
])
async def test_checkout_routes_to_requested_admitted_repository(bridge, selected, repository):
    ctx, calls = selected
    tools = checkout_toolset(bridge, ctx.deps, ctx.deps.grants)
    assert await invoke(tools, ctx, {**repository, "directory": "/workspace/moyai"}) == {
        "repository_id": 2, "directory": "/workspace/moyai",
    }
    target = next(grant for grant in ctx.deps.grants if grant.policy["product"]["repository_id"] == 2)
    assert calls == [(2, target.id, ctx.deps.turn_id)]


@pytest.mark.parametrize(("repository", "error"), [
    ({"repository": "BerriAI/unselected"}, "no unique selected"),
    ({"repository_id": 3}, "no unique selected"),
    ({"repository": "BerriAI/unknown"}, "not selected"),
    ({"repository": "BerriAI/other", "repository_id": 2}, "name and ID"),
])
async def test_checkout_refuses_unselected_unknown_or_conflicting_targets(bridge, selected, repository, error):
    ctx, calls = selected
    tools = checkout_toolset(bridge, ctx.deps, ctx.deps.grants)
    with pytest.raises(ToolFailed, match=error):
        await invoke(tools, ctx, {**repository, "directory": "/workspace/moyai"})
    assert calls == []


async def test_checkout_rechecks_revocation_after_discovery(bridge, selected):
    ctx, calls = selected
    tools = checkout_toolset(bridge, ctx.deps, ctx.deps.grants)
    definitions = await tools.get_tools(ctx)
    tool = definitions["github_checkout"]
    bridge.store.execute("DELETE FROM connections WHERE provider='github'")
    args = tool.args_validator.validate_python({"repository_id": 2, "directory": "/workspace/moyai"})
    with pytest.raises(HTTPException):
        await tools.call_tool("github_checkout", args, ctx, tool)
    assert calls == []


async def test_checkout_rechecks_revocation_during_repository_lookup(bridge, selected, monkeypatch):
    ctx, calls = selected
    target = bridge.connectors.github.target

    def revoke(repository):
        identity = target(repository)
        bridge.store.execute("DELETE FROM connections WHERE provider='github'")
        return identity

    monkeypatch.setattr(bridge.connectors.github, "target", revoke)
    tools = checkout_toolset(bridge, ctx.deps, ctx.deps.grants)
    with pytest.raises(HTTPException):
        await invoke(tools, ctx, {"repository_id": 2, "directory": "/workspace/moyai"})
    assert calls == []


@pytest.mark.parametrize("arguments", [
    {"directory": "/workspace/moyai"},
    {"repository": "BerriAI/moyai", "directory": "/workspace/../private"},
    {"repository": "BerriAI/moyai", "directory": "/workspace/moyai", "ref": "main~1"},
    {"repository": "BerriAI/moyai", "directory": "/workspace/moyai", "ref": "main", "number": 1},
])
def test_shared_checkout_retains_input_validation(arguments):
    with pytest.raises(ValidationError):
        RepositoryCheckout.model_validate(arguments)


async def catalog(capability, ctx):
    tools = await capability.toolset.for_run(ctx)
    await tools.__aenter__()
    await tools.for_run_step(ctx)
    return tools


async def test_catalog_has_one_checkout_and_dispatch_never_rediscovers_provider(bridge, selected, monkeypatch):
    ctx, calls = selected
    discoveries = []

    async def native(self, *, write=False):
        discoveries.append(self.grant.id)
        return FunctionToolset(id="empty-provider")

    monkeypatch.setattr(ProductGitHubToolset, "_native", native)
    capability = connection_tools(SimpleNamespace())
    tools = await catalog(capability, ctx)
    try:
        definitions = await tools.get_tools(ctx)
        assert [name for name in definitions if name.endswith("_checkout")] == ["github_checkout"]
        assert len(discoveries) == 2
    finally:
        await tools.__aexit__(None, None, None)

    # A fresh activity reconstructs only the shared checkout schema. This is
    # also the path used to dispatch a definition returned from the warm cache.
    next_ctx = replace(ctx, deps=replace(ctx.deps, turn_id="followup"))
    tools = await catalog(capability, next_ctx)
    try:
        assert await invoke(tools, next_ctx, {"repository_id": 2, "directory": "/workspace/moyai"}) == {
            "repository_id": 2, "directory": "/workspace/moyai",
        }
        assert len(discoveries) == 2
        assert calls[0][2] == "followup"
    finally:
        await tools.__aexit__(None, None, None)


async def test_checkout_policy_excludes_disallowed_repository(bridge, selected, monkeypatch):
    ctx, calls = selected
    allowed = next(grant for grant in ctx.deps.grants if grant.policy["product"]["repository_id"] == 1)
    blocked = next(grant for grant in ctx.deps.grants if grant.policy["product"]["repository_id"] == 2)
    blocked = replace(blocked, policy={**blocked.policy, "allowed_tools": ["publication_status"]})
    scope = replace(ctx.deps, grants=[allowed, blocked])
    ctx = replace(ctx, deps=scope)

    async def check(scope, grant):
        assert grant in scope.grants

    async def native(self, *, write=False):
        return FunctionToolset(id="empty-provider")

    monkeypatch.setattr(bridge, "check", check)
    monkeypatch.setattr(ProductGitHubToolset, "_native", native)
    tools = await catalog(connection_tools(SimpleNamespace()), ctx)
    try:
        with pytest.raises(ToolFailed, match="no unique selected"):
            await invoke(tools, ctx, {"repository_id": 2, "directory": "/workspace/moyai"})
        assert calls == []
        assert (await invoke(tools, ctx, {"repository_id": 1, "directory": "/workspace/other"}))["repository_id"] == 1
    finally:
        await tools.__aexit__(None, None, None)


async def test_recorded_prefixed_checkout_retains_original_scope(bridge, selected, monkeypatch):
    ctx, calls = selected
    grant = next(grant for grant in ctx.deps.grants if grant.policy["product"]["repository_id"] == 1)

    async def native(self, *, write=False):
        return FunctionToolset(id="empty-provider")

    monkeypatch.setattr(ProductGitHubToolset, "_native", native)
    # The recorded definition is the same real scoped adapter used before the
    # shared tool existed; targeted reconstruction must keep its original scope.
    previous = bridge.toolset(ctx.deps, grant).prefixed(f"github_{grant.id[:8]}").defer_loading()
    name = f"github_{grant.id[:8]}_checkout"
    definition = (await previous.get_tools(ctx))[name].tool_def
    tools = await catalog(connection_tools(SimpleNamespace()), ctx)
    try:
        old = await tools.get_tool_for_tool_def(definition, ctx)
        args = old.args_validator.validate_python({"repository_id": 2, "directory": "/workspace/moyai"})
        with pytest.raises(ToolFailed, match="different selected repository"):
            await tools.call_tool(name, args, ctx, old)
        assert calls == []
        args = old.args_validator.validate_python({"repository_id": 1, "directory": "/workspace/other"})
        assert (await tools.call_tool(name, args, ctx, old))["repository_id"] == 1
    finally:
        await tools.__aexit__(None, None, None)
