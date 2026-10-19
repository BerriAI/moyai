"""Catalog reuse must not reuse live providers, validators, or authority."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, DeferredToolRequests, RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_core import ValidationError

from moyai.connection_catalog import CatalogToolset, ConnectionCatalog, connection_search
from moyai.contracts import ExecutionContext, Grant, RunScope
from moyai.product_connections import ProductCatalogToolset
from moyai.product_identity import ProductGrant


def context(**changes):
    scope = RunScope(
        "session", "turn", "actor", "model",
        [Grant("connection", "github", 1, "sealed-token")],
    )
    return RunContext(deps=replace(scope, **changes), model=TestModel(), usage=RunUsage())


def test_repository_and_distinct_requested_actions_survive_a_crowded_catalog():
    tools = [ToolDefinition(
        name=f"github_{identity}_{operation}",
        description=f"Connected GitHub repository: {repository}. {description}",
    ) for identity, repository in [("11111111", "BerriAI/litellm"), ("22222222", "BerriAI/moyai"), ("33333333", "BerriAI/other")]
        for operation, description in [
            ("checkout", "Check out a branch or PR in the workspace."),
            ("publish_branch", "Publish staged files to a branch without opening a PR."),
            ("open_pull_request", "Open a PR from an existing branch."),
            ("request_pull_request_write_access", "Ask for access to edit an external PR."),
            ("create_review", "Create a pending review on a PR."),
            ("publication_status", "Inspect the receipt of a previous publication."),
        ]]
    matches = connection_search(context(), ["moyai checkout", "moyai publish branch", "moyai publication status"], tools)[:6]
    assert matches[:3] == ["github_22222222_checkout", "github_22222222_publish_branch", "github_22222222_publication_status"]
    assert len({name.split("_", 2)[2] for name in matches}) == len(matches)
    assert connection_search(context(), ["nonexistentxyz"], tools) == []


async def test_repeated_listing_reuses_only_definitions_and_validates_live_calls():
    catalog, ctx, builds, calls = ConnectionCatalog(), context(), [], []
    allowed = True

    def factory(name):
        builds.append(1)
        tools = FunctionToolset(id="live")

        @tools.tool_plain
        async def read_issue(number: int) -> str:
            """Read a numbered issue."""
            if not allowed:
                raise PermissionError("Access revoked")
            calls.append(number)
            return str(number)

        return tools

    async with CatalogToolset(catalog, ctx, factory) as cold:
        definitions = await cold.get_tools(ctx)
    for _ in range(3):
        async with CatalogToolset(catalog, ctx, factory) as hit:
            definitions = await hit.get_tools(ctx)
            definitions["read_issue"].tool_def.description = "caller-local mutation"
    assert len(builds) == 1
    async with CatalogToolset(catalog, ctx, factory) as hit:
        tool = (await hit.get_tools(ctx))["read_issue"]
        assert tool.tool_def.description == "Read a numbered issue."
        with pytest.raises(ValidationError):
            await hit.call_tool("read_issue", {"number": "invalid"}, ctx, tool)
        assert not calls
        assert await hit.call_tool("read_issue", {"number": 7}, ctx, tool) == "7"
    allowed = False
    async with CatalogToolset(catalog, ctx, factory) as hit:
        tool = (await hit.get_tools(ctx))["read_issue"]
        with pytest.raises(PermissionError, match="revoked"):
            await hit.call_tool("read_issue", {"number": 8}, ctx, tool)
    assert calls == [7]
    assert len(builds) == 3


async def test_scope_changes_expiry_and_size_eviction_refresh_catalog(monkeypatch):
    catalog, builds = ConnectionCatalog(max_bytes=1000), []

    def factory(name):
        builds.append(1)
        tools = FunctionToolset(id="live")
        tools.add_function(lambda: "ok", name="read")
        return tools

    ctx = context()
    for selected in [
        ctx, ctx, context(actor_id="another"),
        context(grants=[Grant("connection", "github", 2, "changed-token")]),
        context(grants=[Grant("connection", "github", 1, "sealed-token", read_only=False)]),
    ]:
        async with CatalogToolset(catalog, selected, factory) as tools:
            await tools.get_tools(selected)
    assert len(builds) == 4
    assert catalog.size <= catalog.max_bytes
    monkeypatch.setattr("moyai.connection_catalog.time.monotonic", lambda: float("inf"))
    async with CatalogToolset(catalog, ctx, factory) as tools:
        await tools.get_tools(ctx)
    assert len(builds) == 5


async def test_workflow_version_resolution_reuses_catalog_without_erasing_admission_boundaries():
    catalog, builds = ConnectionCatalog(), []

    def factory(name):
        builds.append(1)
        tools = FunctionToolset(id="live")
        tools.add_function(lambda: "ok", name="read")
        return tools

    # SDK discovery can precede the first versioned instruction hook. Resolving
    # execution mode must not rebuild otherwise identical connector schemas.
    for selected in [
        context(), context(interactive_delegation=True),
        context(interactive_delegation=True, workflow_public_events=True),
        context(interactive_delegation=True, workflow_public_events=True, history_segment=1),
    ]:
        async with CatalogToolset(catalog, selected, factory) as tools:
            await tools.get_tools(selected)
    assert len(builds) == 1
    for changes in [
        {"turn_id": "another-turn"}, {"actor_id": "another-actor"},
        {"swarm_id": "mission"}, {"delegation_generated": True},
        {"grants": [Grant("connection", "github", 2, "changed-token")]},
        {"grants": [Grant("connection", "github", 1, "sealed-token", read_only=False)]},
    ]:
        selected = context(interactive_delegation=True, **changes)
        async with CatalogToolset(catalog, selected, factory) as tools:
            await tools.get_tools(selected)
    assert len(builds) == 7


async def test_catalog_change_fails_closed_and_invalidates_discovery():
    catalog, ctx = ConnectionCatalog(), context()
    read_only = True

    def factory(name):
        tools = FunctionToolset(id="live")
        tools.add_function(lambda: "ok", name="operation",
                           metadata={"annotations": {"readOnlyHint": read_only}})
        return tools

    async with CatalogToolset(catalog, ctx, factory) as cold:
        original = (await cold.get_tools(ctx))["operation"].tool_def
    read_only = False
    async with CatalogToolset(catalog, ctx, factory) as live:
        with pytest.raises(ToolFailed, match="definition changed"):
            await live.get_tool_for_tool_def(original, ctx)
    assert catalog.get(catalog.key(ctx)) is None
    async with CatalogToolset(catalog, ctx, factory) as refreshed:
        tool = (await refreshed.get_tools(ctx))["operation"]
        assert not tool.tool_def.metadata["annotations"]["readOnlyHint"]


async def test_concurrent_discovery_and_cancelled_owner_do_not_poison_cache():
    catalog, ctx = ConnectionCatalog(), context()
    entered, release = asyncio.Event(), asyncio.Event()
    builds = []

    class SlowTools(FunctionToolset):
        async def get_tools(self, ctx):
            entered.set()
            await release.wait()
            return await super().get_tools(ctx)

    def factory(name):
        builds.append(1)
        tools = SlowTools(id="slow")
        tools.add_function(lambda: "ok", name="read")
        return tools

    async def discover():
        async with CatalogToolset(catalog, ctx, factory) as tools:
            return await tools.get_tools(ctx)

    first = asyncio.create_task(discover())
    await entered.wait()
    second = asyncio.create_task(discover())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert "read" in await second
    assert len(builds) == 2
    assert "read" in await discover()
    assert len(builds) == 2
    assert not catalog.loading


async def test_custom_argument_validator_is_never_erased_by_cache():
    catalog, ctx, builds = ConnectionCatalog(), context(), []

    def factory(name):
        builds.append(1)
        tools = FunctionToolset(id="live")

        def validate(ctx, number: int) -> None:
            if number != 7:
                raise ToolFailed("Only issue 7 is allowed.")

        @tools.tool_plain(args_validator=validate)
        def read(number: int) -> int:
            return number

        return tools

    for _ in range(2):
        async with CatalogToolset(catalog, ctx, factory) as tools:
            live = (await tools.get_tools(ctx))["read"]
            assert live.args_validator_func is not None
    assert len(builds) == 2
    assert not catalog.entries


async def test_cached_write_still_requires_native_approval():
    catalog, ctx, writes = ConnectionCatalog(), context(), []

    def factory(name):
        tools = FunctionToolset(id="live")

        @tools.tool_plain(requires_approval=True)
        def publish() -> str:
            writes.append("published")
            return "published"

        return tools.approval_required()

    async with CatalogToolset(catalog, ctx, factory) as cold:
        assert (await cold.get_tools(ctx))["publish"].tool_def.kind == "unapproved"
    agent = Agent(
        TestModel(call_tools=["publish"]), deps_type=RunScope,
        output_type=[str, DeferredToolRequests],
        toolsets=[CatalogToolset(catalog, ctx, factory)],
    )
    result = await agent.run("Publish.", deps=ctx.deps)
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_name for call in result.output.approvals] == ["publish"]
    assert not writes


def product_context(**changes):
    return context(
        **{"grants": [Grant(
            "connection", "github", 1, "",
            policy={"product": {"repository_id": 7, "authority": "epoch"}},
        )], **changes},
    )


async def test_product_turns_share_definitions_but_rebuild_current_validators_credentials_and_scope(monkeypatch):
    catalog, builds, checks, calls = ConnectionCatalog(), [], [], []
    allowed, credential = True, "original"

    async def check(scope, grant):
        checks.append(scope.turn_id)
        assert grant in scope.grants
        if not allowed:
            raise PermissionError("Access revoked")

    monkeypatch.setattr("moyai.runtime.current", lambda: SimpleNamespace(
        product_connections=SimpleNamespace(check=check),
    ))

    def factory(ctx):
        def build(name):
            builds.append((ctx.deps.turn_id, name))
            tools = FunctionToolset(id="canonical")

            @tools.tool_plain
            async def read(number: int) -> str:
                await check(ctx.deps, ctx.deps.grants[0])
                calls.append((ctx.deps.turn_id, credential, number))
                return ctx.deps.turn_id

            return tools
        return build

    for turn in ("first", "second", "third"):
        ctx = product_context(turn_id=turn)
        async with ProductCatalogToolset(catalog, ctx, factory(ctx)) as tools:
            definitions = await tools.get_tools(ctx)
            definitions["read"].tool_def.description = "caller-local mutation"
    assert builds == [("first", None)]
    assert checks == ["first", "second", "third"]
    ctx = product_context(turn_id="fourth")
    async with ProductCatalogToolset(catalog, ctx, factory(ctx)) as tools:
        tool = (await tools.get_tools(ctx))["read"]
        assert tool.tool_def.description != "caller-local mutation"
        credential = "rotated"
        with pytest.raises(ValidationError):
            await tools.call_tool("read", {"number": "invalid"}, ctx, tool)
        assert await tools.call_tool("read", {"number": 7}, ctx, tool) == "fourth"
        allowed = False
        with pytest.raises(PermissionError, match="revoked"):
            await tools.call_tool("read", {"number": 8}, ctx, tool)
    assert builds == [("first", None), ("fourth", "read")]
    assert calls == [("fourth", "rotated", 7)]
    ctx = product_context(turn_id="stopped-or-revoked")
    async with ProductCatalogToolset(catalog, ctx, factory(ctx)) as tools:
        with pytest.raises(PermissionError, match="revoked"):
            await tools.get_tools(ctx)
    assert not catalog.entries


@pytest.mark.parametrize("changes", [
    {"session_id": "other-session"},
    {"actor_id": "other-actor"},
    {"model": "other-model"},
    {"visibility": "shared"},
    {"surface": "slack"},
    {"execution_context": ExecutionContext("shared", "automation")},
    {"product_grant": ProductGrant("actor", "changed-authority")},
    {"swarm_id": "mission"},
    {"swarm_name": "other-mission"},
    {"delegation_depth": 1},
    {"delegation_generated": True},
    {"goal_generation": 1},
    {"grants": [replace(product_context().deps.grants[0], revision=2)]},
    {"grants": [replace(product_context().deps.grants[0], credential="changed-credential")]},
    {"grants": [replace(product_context().deps.grants[0], read_only=False)]},
    {"grants": [replace(product_context().deps.grants[0], policy={
        "product": {"repository_id": 8, "authority": "epoch"},
    })]},
    {"grants": [replace(product_context().deps.grants[0], policy={
        "product": {"repository_id": 7, "authority": "revoked-and-reconnected"},
    })]},
    {"grants": [replace(product_context().deps.grants[0], policy={
        "product": {"repository_id": 7, "authority": "epoch"}, "allowed_tools": ["read"],
    })]},
])
def test_product_schema_reuse_keeps_identity_grants_and_provenance_isolated(changes):
    catalog = ConnectionCatalog()

    def factory(name):
        return FunctionToolset(id="canonical")

    first = ProductCatalogToolset(catalog, product_context(), factory)
    same = ProductCatalogToolset(catalog, product_context(turn_id="next-turn"), factory)
    changed = ProductCatalogToolset(catalog, product_context(turn_id="next-turn", **changes), factory)
    assert first.key == same.key
    assert first.key != changed.key


async def test_product_discovery_rechecks_revocation_after_await_and_discards_definitions(monkeypatch):
    catalog, ctx = ConnectionCatalog(), product_context()
    entered, release = asyncio.Event(), asyncio.Event()
    revoked = False

    async def check(scope, grant):
        if revoked:
            raise PermissionError("Turn stopped")

    monkeypatch.setattr("moyai.runtime.current", lambda: SimpleNamespace(
        product_connections=SimpleNamespace(check=check),
    ))

    class SlowTools(FunctionToolset):
        async def get_tools(self, ctx):
            entered.set()
            await release.wait()
            return await super().get_tools(ctx)

    def factory(name):
        tools = SlowTools(id="canonical")
        tools.add_function(lambda: "ok", name="read")
        return tools

    async def discover():
        async with ProductCatalogToolset(catalog, ctx, factory) as tools:
            return await tools.get_tools(ctx)

    pending = asyncio.create_task(discover())
    await entered.wait()
    revoked = True
    release.set()
    with pytest.raises(PermissionError, match="stopped"):
        await pending
    assert not catalog.entries
    assert not catalog.loading


async def test_product_schema_drift_on_direct_execution_invalidates_shared_key(monkeypatch):
    catalog = ConnectionCatalog()

    async def check(scope, grant):
        pass

    monkeypatch.setattr("moyai.runtime.current", lambda: SimpleNamespace(
        product_connections=SimpleNamespace(check=check),
    ))
    read_only = True

    def factory(name):
        tools = FunctionToolset(id="canonical")
        tools.add_function(
            lambda: "ok", name="read", metadata={"annotations": {"readOnlyHint": read_only}},
        )
        return tools

    first, second = product_context(), product_context(turn_id="next-turn")
    async with ProductCatalogToolset(catalog, first, factory) as tools:
        original = (await tools.get_tools(first))["read"].tool_def
    read_only = False
    # Durable execution reconstructs directly, without first listing the catalog.
    async with ProductCatalogToolset(catalog, second, factory) as tools:
        with pytest.raises(ToolFailed, match="definition changed"):
            await tools.get_tool_for_tool_def(original, second)
    assert not catalog.entries
