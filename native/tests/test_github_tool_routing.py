"""Shared GitHub schemas, target policies and approval before effect reservation."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from test_github_checkout_routing import catalog, selected
from test_product_connections import bridge

from moyai.connection_catalog import ConnectionToolSearch, connection_search
from moyai.connections import connection_tools
from moyai.github_workflow_types import PublishBranch, WorkspacePublishBranch
from moyai.product_connections import ProductGitHubToolset
from moyai.product_github_workflows import Workflows

__all__ = ["bridge", "selected"]


@pytest.fixture
async def routed(bridge, selected, monkeypatch):
    ctx, _ = selected
    calls, discoveries = [], []

    async def native(adapter, *, write=False):
        discoveries.append(adapter.grant.id)
        tools = FunctionToolset(id="provider")

        @tools.tool_plain(metadata={"annotations": {"readOnlyHint": True}})
        async def get_file_contents(owner: str, repo: str, path: str) -> dict:
            calls.append(("read", adapter.grant.policy["product"]["repository_id"], owner, repo, path))
            return {"path": path}

        @tools.tool_plain(metadata={"annotations": {"readOnlyHint": True}})
        async def search_code(query: str, perPage: int = 30) -> dict:
            calls.append(("search", adapter.grant.policy["product"]["repository_id"], query, perPage))
            return {"items": []}

        return tools

    async def create_branch(workflows, context, request):
        await workflows.publisher.check_target(request)
        calls.append(("branch", workflows.publisher.target, request.branch, context.tool_name))
        return {"repository_id": workflows.publisher.target, "branch": request.branch}

    monkeypatch.setattr(ProductGitHubToolset, "_native", native)
    monkeypatch.setattr(Workflows, "create_branch", create_branch)
    return SimpleNamespace(
        ctx=ctx, calls=calls, discoveries=discoveries, capability=connection_tools(SimpleNamespace()),
    )


async def invoke(f, name, arguments, *, approved=False):
    ctx = replace(f.ctx, tool_call_approved=approved)
    tools = await catalog(f.capability, ctx)
    try:
        definitions = await tools.get_tools(ctx)
        tool = await tools.get_tool_for_tool_def(definitions[name].tool_def, ctx)
        args = tool.args_validator.validate_python(arguments)
        return await tools.call_tool(name, args, ctx, tool)
    finally:
        await tools.__aexit__(None, None, None)


async def test_one_definition_per_operation_across_selected_repositories(routed):
    f = routed
    tools = await catalog(f.capability, f.ctx)
    try:
        names = set(await tools.get_tools(f.ctx))
        assert {"github_checkout", "github_create_branch", "github_get_file_contents", "github_search_code"} <= names
        assert not any(name.startswith(f"github_{grant.id[:8]}_") for grant in f.ctx.deps.grants for name in names)
        assert len(f.discoveries) == 2
    finally:
        await tools.__aexit__(None, None, None)


async def test_reads_route_to_owner_repo_and_search_injects_only_requested_repository(routed):
    f = routed
    assert await invoke(f, "github_get_file_contents", {"owner": "BerriAI", "repo": "moyai", "path": "README.md"}) == {
        "path": "README.md",
    }
    assert f.calls[-1] == ("read", 2, "BerriAI", "moyai", "README.md")
    assert await invoke(f, "github_search_code", {"repository_id": 1, "query": "checkout", "perPage": 7}) == {"items": []}
    assert f.calls[-1] == ("search", 1, '"checkout" repo:BerriAI/other', 7)
    with pytest.raises(ToolFailed, match="plain search"):
        await invoke(f, "github_search_code", {"repository": "BerriAI/moyai", "query": "repo:BerriAI/other"})
    assert len(f.calls) == 2


@pytest.mark.parametrize("arguments", [
    {"repository_id": 3},
    {"repository": "BerriAI/other", "repository_id": 2},
])
async def test_shared_operation_refuses_unselected_or_conflicting_target(routed, arguments):
    with pytest.raises(ToolFailed):
        await invoke(routed, "github_search_code", {**arguments, "query": "checkout"})
    assert routed.calls == []


async def test_shared_native_read_requires_explicit_repository(routed):
    with pytest.raises(ToolFailed, match="explicit repository"):
        await invoke(routed, "github_publication_status", {"request_key": "missing-selector"})
    assert routed.calls == []


async def test_cached_local_dispatch_rebuilds_without_provider_discovery(routed):
    f = routed
    tools = await catalog(f.capability, f.ctx)
    try:
        definitions = await tools.get_tools(f.ctx)
        definition = definitions["github_create_branch"].tool_def
    finally:
        await tools.__aexit__(None, None, None)
    assert len(f.discoveries) == 2
    f.ctx = replace(f.ctx, deps=replace(f.ctx.deps, turn_id="next-turn"))
    tools = await catalog(f.capability, f.ctx)
    try:
        tool = await tools.get_tool_for_tool_def(definition, f.ctx)
        args = tool.args_validator.validate_python({
            "repository": "BerriAI/moyai", "branch": "feature", "commit_sha": "a" * 40,
            "request_key": "shared-branch",
        })
        assert (await tools.call_tool("github_create_branch", args, f.ctx, tool))["repository_id"] == 2
        assert f.calls == [("branch", 2, "feature", "github_create_branch")]
        assert len(f.discoveries) == 2
    finally:
        await tools.__aexit__(None, None, None)


async def test_legacy_prefixed_read_and_write_retain_their_original_repository(bridge, routed):
    f = routed
    grant = next(grant for grant in f.ctx.deps.grants if grant.policy["product"]["repository_id"] == 1)
    prefix = f"github_{grant.id[:8]}"
    previous = bridge.toolset(f.ctx.deps, grant).prefixed(prefix).defer_loading()
    definitions = await previous.get_tools(f.ctx)
    tools = await catalog(f.capability, f.ctx)
    try:
        for operation, args in [
            ("get_file_contents", {"owner": "BerriAI", "repo": "moyai", "path": "README.md"}),
            ("create_branch", {"repository_id": 2, "branch": "feature", "commit_sha": "a" * 40,
                               "request_key": "legacy-branch"}),
        ]:
            name = f"{prefix}_{operation}"
            tool = await tools.get_tool_for_tool_def(definitions[name].tool_def, f.ctx)
            with pytest.raises(ToolFailed, match="different selected repository"):
                await tools.call_tool(name, tool.args_validator.validate_python(args), f.ctx, tool)
        assert f.calls == []
    finally:
        await tools.__aexit__(None, None, None)


async def test_cold_discovery_overlaps_independent_repositories(routed, monkeypatch):
    native = ProductGitHubToolset._native
    started, both_started = [], asyncio.Event()

    async def concurrent(adapter, *, write=False):
        started.append(adapter.grant.id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=2)
        return await native(adapter, write=write)

    monkeypatch.setattr(ProductGitHubToolset, "_native", concurrent)
    await invoke(routed, "github_get_file_contents", {"owner": "BerriAI", "repo": "moyai", "path": "README.md"})
    assert len(set(started)) == 2


async def test_warm_read_never_discovers_an_unrelated_repository(routed, monkeypatch):
    f = routed
    tools = await catalog(f.capability, f.ctx)
    try:
        await tools.get_tools(f.ctx)
    finally:
        await tools.__aexit__(None, None, None)
    native = ProductGitHubToolset._native
    discoveries = []

    async def target_only(adapter, *, write=False):
        identity = adapter.grant.policy["product"]["repository_id"]
        assert identity == 2, "An unrelated provider must not participate in this cached call."
        discoveries.append(identity)
        return await native(adapter, write=write)

    monkeypatch.setattr(ProductGitHubToolset, "_native", target_only)
    await invoke(f, "github_get_file_contents", {"owner": "BerriAI", "repo": "moyai", "path": "README.md"})
    await invoke(f, "github_search_code", {"repository_id": 2, "query": "checkout"})
    assert discoveries == [2, 2]
    assert f.calls[-1] == ("search", 2, '"checkout" repo:BerriAI/moyai', 30)


@pytest.mark.parametrize("warm", [False, True])
async def test_provider_schema_drift_refreshes_same_activity_and_accepts_new_arguments(routed, monkeypatch, warm):
    f = routed
    tools = await catalog(f.capability, f.ctx)
    definitions = await tools.get_tools(f.ctx)
    if warm:
        await tools.__aexit__(None, None, None)
        tools = await catalog(f.capability, f.ctx)
    discoveries = []

    async def changed(adapter, *, write=False):
        identity = adapter.grant.policy["product"]["repository_id"]
        discoveries.append(identity)
        tools = FunctionToolset(id="changed-provider")

        @tools.tool_plain(metadata={"annotations": {"readOnlyHint": True}})
        async def get_file_contents(owner: str, repo: str, path: str, revision: str) -> dict:
            f.calls.append(("updated-schema", identity, revision))
            return {"path": path, "revision": revision}

        return tools

    monkeypatch.setattr(ProductGitHubToolset, "_native", changed)
    try:
        name = "github_get_file_contents"
        tool = await tools.get_tool_for_tool_def(definitions[name].tool_def, f.ctx)
        args = {"owner": "BerriAI", "repo": "moyai", "path": "README.md"}
        with pytest.raises(ToolFailed, match="definition changed"):
            await tools.call_tool(name, tool.args_validator.validate_python(args), f.ctx, tool)
        assert f.calls == [] and discoveries == [2]
        refreshed = (await tools.get_tools(f.ctx))[name]
        assert "revision" in refreshed.tool_def.parameters_json_schema["required"]
        assert len(discoveries) == 3 and set(discoveries[1:]) == {1, 2}
        result = await tools.call_tool(
            name, refreshed.args_validator.validate_python({**args, "revision": "new-sha"}), f.ctx, refreshed,
        )
        assert result == {"path": "README.md", "revision": "new-sha"}
        assert f.calls == [("updated-schema", 2, "new-sha")]
    finally:
        await tools.__aexit__(None, None, None)


@pytest.mark.parametrize(("extra", "model"), [
    ({"files": [{"path": "README.md", "content": "updated"}]}, PublishBranch),
    ({"directory": "/workspace/repo"}, WorkspacePublishBranch),
])
async def test_publish_union_survives_cold_and_warm_schema_reconstruction(routed, extra, model):
    f = routed
    for _ in range(2):
        tools = await catalog(f.capability, f.ctx)
        try:
            definitions = await tools.get_tools(f.ctx)
            tool = await tools.get_tool_for_tool_def(definitions["github_publish_branch"].tool_def, f.ctx)
            request = {"repository_id": 2, "base_sha": "a" * 40, "branch": "feature", "title": "Updated",
                       "request_key": "union-validation", **extra}
            assert isinstance(tool.args_validator.validate_python({"request": request})["request"], model)
            with pytest.raises(ValidationError):
                tool.args_validator.validate_python({"request": {**request, "base_sha": "invalid"}})
        finally:
            await tools.__aexit__(None, None, None)


async def live_policy(bridge, f, monkeypatch, repository, **policy):
    original = bridge.resolve_grants

    async def resolve(*args, **kwargs):
        return [
            replace(grant, policy={**grant.policy, **policy})
            if grant.policy["product"]["repository_id"] == repository else grant
            for grant in await original(*args, **kwargs)
        ]

    monkeypatch.setattr(bridge, "resolve_grants", resolve)
    grants = await resolve(f.ctx.deps.actor_id, [grant.id for grant in f.ctx.deps.grants])
    f.ctx = replace(f.ctx, deps=replace(f.ctx.deps, grants=grants))


async def test_per_target_allowlist_and_live_revocation_are_enforced(bridge, routed, monkeypatch):
    f = routed
    await live_policy(bridge, f, monkeypatch, 2, allowed_tools=["publication_status"])
    with pytest.raises(ToolFailed, match="no unique selected"):
        await invoke(f, "github_search_code", {"repository_id": 2, "query": "checkout"})
    await invoke(f, "github_search_code", {"repository_id": 1, "query": "checkout"})
    assert len(f.calls) == 1
    bridge.store.execute("DELETE FROM connections WHERE provider='github'")
    with pytest.raises(HTTPException):
        await invoke(f, "github_search_code", {"repository_id": 1, "query": "checkout"})
    assert len(f.calls) == 1


@pytest.mark.parametrize("repository", [1, 2])
async def test_only_requested_repository_requires_approval_before_execution(bridge, routed, monkeypatch, repository):
    f = routed
    await live_policy(bridge, f, monkeypatch, 2, require_approval=True)
    reserved = []

    class Receipts(AbstractCapability):
        async def before_tool_execute(self, ctx, *, call, tool_def, args):
            if call.tool_name == "github_create_branch":
                reserved.append(call.tool_name)
            return args

    def model(messages, info):
        if "github_create_branch" not in {tool.name for tool in info.function_tools}:
            return ModelResponse(parts=[ToolCallPart(
                "search_tools", {"queries": ["github create branch"]}, tool_call_id="search",
            )])
        if any(isinstance(part, ToolReturnPart) and part.tool_name == "github_create_branch"
               for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("Done")])
        return ModelResponse(parts=[ToolCallPart("github_create_branch", {
            "repository_id": repository, "branch": "feature", "commit_sha": "a" * 40,
            "request_key": "approved-branch",
        }, tool_call_id="branch")])

    agent = Agent(FunctionModel(model), capabilities=[
        f.capability, Receipts(), ConnectionToolSearch(strategy=connection_search),
    ],
                  output_type=[str, DeferredToolRequests])
    result = await agent.run("Create the selected branch.", deps=f.ctx.deps)
    if repository == 1:
        assert result.output == "Done"
        assert f.calls == [("branch", 1, "feature", "github_create_branch")]
        return
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.approvals] == ["github_create_branch"]
    assert reserved == [] and f.calls == []
    result = await agent.run(
        message_history=result.all_messages(), deps=f.ctx.deps,
        deferred_tool_results=DeferredToolResults(approvals={"branch": True}),
    )
    assert result.output == "Done"
    assert reserved == ["github_create_branch"]
    assert f.calls == [("branch", 2, "feature", "github_create_branch")]
