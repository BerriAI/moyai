"""Catalog reuse must not reuse live providers, validators, or authority."""

import asyncio
from dataclasses import replace

import pytest
from pydantic_ai import Agent, DeferredToolRequests, RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_core import ValidationError

from moyai.connection_catalog import CatalogToolset, ConnectionCatalog
from moyai.contracts import Grant, RunScope


def context(**changes):
    scope = RunScope(
        "session", "turn", "actor", "model",
        [Grant("connection", "github", 1, "sealed-token")],
    )
    return RunContext(deps=replace(scope, **changes), model=TestModel(), usage=RunUsage())


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
