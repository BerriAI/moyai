"""Discovery boundaries and durable owner choices; live proof is documented separately."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from sqlalchemy import select, update
from test_personal_dot import observer

from moyai.contracts import NewMessage
from moyai.dot_contracts import DotItemEdit, DotJudgment, DotResearch
from moyai.dot_discovery import DiscoverySnapshot
from moyai.dot_discovery_agent import DiscoveryBounds
from moyai.dot_models import DotAttentionTurn, DotDiscoveryTurn, DotItem, DotNotification
from moyai.dot_sources import GITHUB_MCP, provider_complete, provider_partial, repositories
from moyai.models import Connection, Message, Session, Turn


async def research(runtime, prepared, *, version="one", coverage="complete", title="Customer reply needed",
                   source="https://example.test/item", source_state="open"):
    account = prepared.scope.grants[0].id
    evidence = {"connection_id": account, "source": source, "source_version": version, "quote": title}
    read_tool = prepared.scope.grants[0].policy["allowed_tools"][0]
    report = DotResearch.model_validate({
        "coverage": coverage, "observations": [{"title": title, "summary": title, "evidence": [evidence]}],
    })
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{account[:8]}_{read_tool}", tool_call_id="scan",
            content={"dot_scan": True, "scanned": True, "limited": False, "error": False,
                     "records": [{"url": evidence["source"], "version": version, "title": title, "state": source_state}]},
        )]),
    ])).decode()
    payload = await runtime.database.dots.discovery.judgment_input(prepared.scope, report.model_dump_json(), conversation)
    return payload, conversation


async def publish(runtime, prepared, conversation, *, notify=True, state="open"):
    judgment = DotJudgment.model_validate({
        "items": [{"observation": 0, "state": state}],
        "notify_observation": 0 if notify else None,
        "message": "A customer is waiting. Would you like a reply draft?" if notify else "",
    })
    assert await runtime.database.save_result(
        prepared.scope, output=judgment.model_dump_json(), conversation=conversation, workspace=None, usage={},
    )
    await runtime.database.settle(prepared.scope.turn_id)


async def test_research_never_receives_private_context_but_judgment_does(runtime, actor):
    destination = await runtime.database.dots.open(actor)
    await runtime.database.admission.submit(actor, destination, NewMessage(
        prompt="PRIVATE launch priority", request_key=uuid4().hex,
    ))
    _, _, _, prepared = await observer(runtime, actor, destination, discovery=True)
    assert prepared.scope.discovery
    assert "PRIVATE" not in prepared.prompt and "Check the review deadline." not in prepared.prompt
    payload, _ = await research(runtime, prepared)
    snapshot = DiscoverySnapshot.model_validate_json(payload)
    assert any("PRIVATE launch priority" in item.text for item in snapshot.conversation)
    assert snapshot.priorities == "Check the review deadline."


@pytest.mark.parametrize("state", ["dismissed", "resolved", "snoozed"])
async def test_owner_controls_survive_new_versions_and_new_schedule(runtime, actor, state):
    destination, connection, _, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared)
    await publish(runtime, prepared, conversation)
    service = runtime.database.dots.discovery
    item = (await service.list(actor)).items[0]
    await service.edit(actor, item.id, DotItemEdit(
        revision=item.revision, state=state,
        snoozed_until=datetime.now(UTC) + timedelta(days=1) if state == "snoozed" else None,
    ))
    _, _, _, later = await observer(runtime, actor, destination, connection, discovery=True)
    _, conversation = await research(runtime, later, version="two")
    await publish(runtime, later, conversation)
    assert (await service.list(actor)).items[0].state == state
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(DotNotification)))) == 1


async def test_late_judgment_cannot_overwrite_owner_edit(runtime, actor):
    destination, connection, automation, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared)
    await publish(runtime, prepared, conversation, notify=False)
    _, _, _, later = await observer(runtime, actor, destination, connection, automation, discovery=True)
    before, conversation = await research(runtime, later, version="two")
    service = runtime.database.dots.discovery
    item = (await service.list(actor)).items[0]
    await service.edit(actor, item.id, DotItemEdit(revision=item.revision, state="dismissed"))
    after, _ = await research(runtime, later, version="two")
    assert after == before  # Retried preparation cannot reset the captured revision.
    await publish(runtime, later, conversation)
    assert (await service.list(actor)).items[0].state == "dismissed"
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(DotNotification)) is None


async def test_quiet_tracks_items_without_chat_and_reopen_allows_one_new_notice(runtime, actor):
    destination, connection, automation, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared)
    await publish(runtime, prepared, conversation, notify=False)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(Message).where(Message.session_id == destination)) is None
    for _ in range(2):
        _, _, _, later = await observer(runtime, actor, destination, connection, automation, discovery=True)
        _, conversation = await research(runtime, later)
        await publish(runtime, later, conversation)
    service = runtime.database.dots.discovery
    item = (await service.list(actor)).items[0]
    await service.edit(actor, item.id, DotItemEdit(revision=item.revision, state="open"))
    _, _, _, later = await observer(runtime, actor, destination, connection, automation, discovery=True)
    _, conversation = await research(runtime, later)
    await publish(runtime, later, conversation)
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(DotItem)))) == 1
        assert len(list(await db.scalars(select(DotNotification)))) == 2


async def test_connection_revocation_hides_items_and_fences_controls(runtime, actor):
    _, connection, _, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared)
    await publish(runtime, prepared, conversation)
    service = runtime.database.dots.discovery
    item = (await service.list(actor)).items[0]
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Connection).where(Connection.id == connection.id).values(enabled=False, revision=2))
    assert not (await service.list(actor)).items
    with pytest.raises(HTTPException):
        await service.edit(actor, item.id, DotItemEdit(revision=item.revision, state="open"))


async def test_item_identity_survives_replacing_chat_but_not_account(runtime, actor):
    destination, connection, _, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared)
    await publish(runtime, prepared, conversation)
    service = runtime.database.dots.discovery
    item = (await service.list(actor)).items[0]
    await service.edit(actor, item.id, DotItemEdit(revision=item.revision, state="dismissed"))
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, destination)).deleted = True
    assert await runtime.database.dots.open(actor) != destination
    assert (await service.list(actor)).items[0].state == "dismissed"
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Connection, connection.id)
        row.credential = runtime.security.seal("replacement-account")
        row.revision += 1
    assert not (await service.list(actor)).items


async def test_replacing_dot_chat_does_not_reset_discovery_delivery(runtime, actor):
    destination, connection, automation, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared)
    await publish(runtime, prepared, conversation)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, destination)).deleted = True
    replacement = await runtime.database.dots.open(actor)
    _, _, _, later = await observer(runtime, actor, replacement, connection, automation, discovery=True)
    _, conversation = await research(runtime, later)
    await publish(runtime, later, conversation)
    async with runtime.database.sessions() as db:
        assert len(list(await db.scalars(select(DotNotification)))) == 1
        assert await db.scalar(select(Message).where(Message.session_id == replacement)) is None


async def test_partial_is_not_a_clean_quiet_check(runtime, actor):
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared, coverage="partial")
    await publish(runtime, prepared, conversation, notify=False)
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, prepared.scope.turn_id)).outcome == "partial"


async def test_host_bounds_provider_results_and_rejects_oversized_page(runtime, actor):
    from pydantic_ai.exceptions import ModelRetry
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    bounds = DiscoveryBounds()
    ctx = SimpleNamespace(deps=prepared.scope)
    call = ToolCallPart(f"mcp_{prepared.scope.grants[0].id[:8]}_read_item", {}, "read")
    with pytest.raises(ModelRetry):
        await bounds.before_tool_execute(ctx, call=call, tool_def=None, args={"perPage": 100})
    result = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args={}, result={
        "items": [{"id": f"item-{i}", "version": "one", "body": "x" * 5000} for i in range(100)],
    })
    assert result["limited"] and result["scanned"] and len(json.dumps(result)) < 18000


async def test_discovery_retains_nested_mcp_records_without_claiming_complete_coverage(runtime, actor):
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    record = {"id": "item", "version": "one", "title": "Review due."}
    result = await DiscoveryBounds().after_tool_execute(
        SimpleNamespace(deps=prepared.scope),
        call=ToolCallPart(f"mcp_{prepared.scope.grants[0].id[:8]}_read_item", {}, "read"),
        tool_def=None, args={}, result=[[record]],
    )
    assert result["records"] == [record]
    assert result["scanned"] and result["limited"]


async def test_repository_scope_is_enforced_before_read_and_on_returned_records(runtime, actor):
    from pydantic_ai.exceptions import ModelRetry
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["list_pull_requests"], "mcp": {"url": GITHUB_MCP}}
    prepared.scope.discovery_repositories = repositories(
        [grant], [{"connection_id": grant.id, "source": "https://github.com/BerriAI/moyai"}],
    )
    bounds, ctx = DiscoveryBounds(), SimpleNamespace(deps=prepared.scope)
    call = ToolCallPart(f"mcp_{grant.id[:8]}_list_pull_requests", {}, "read")
    args = {"owner": "BerriAI", "repo": "moyai", "perPage": 10}
    assert await bounds.before_tool_execute(ctx, call=call, tool_def=None, args=args) == args
    with pytest.raises(ModelRetry, match="outside"):
        await bounds.before_tool_execute(ctx, call=call, tool_def=None, args={**args, "repo": "another"})
    result = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args=args, result={
        "items": [
            {"url": "https://github.com/BerriAI/moyai/pull/1", "version": "one", "title": "Allowed"},
            {"url": "https://github.com/BerriAI/another/pull/1", "version": "one", "title": "Outside"},
            {"version": "one", "title": "Unverifiable repository"},
        ],
    })
    assert "Outside" not in json.dumps(result) and result["limited"]
    assert "Unverifiable" not in json.dumps(result)
    grant.policy["mcp"]["url"] = "https://example.test/mcp"
    with pytest.raises(ValueError, match="official GitHub"):
        repositories([grant], [{"connection_id": grant.id, "source": "https://github.com/BerriAI/moyai"}])


async def test_restricted_github_dispatch_blocks_relationship_and_unknown_methods(runtime, actor):
    from pydantic_ai.exceptions import ModelRetry

    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["issue_read", "pull_request_read"], "mcp": {"url": GITHUB_MCP}}
    prepared.scope.discovery_repositories = {grant.id: [("berriai", "moyai")]}
    bounds, ctx = DiscoveryBounds(), SimpleNamespace(deps=prepared.scope)
    for tool, methods in {
        "issue_read": [None, "get", "get_parent", "get_sub_issues", "future_method"],
        "pull_request_read": [None, "get_status", "get_commits", "future_method"],
    }.items():
        call = ToolCallPart(f"mcp_{grant.id[:8]}_{tool}", {}, "read")
        for method in methods:
            with pytest.raises(ModelRetry, match="method"):
                await bounds.before_tool_execute(ctx, call=call, tool_def=None, args={
                    "owner": "BerriAI", "repo": "moyai", "method": method,
                })
        for method in (["get_comments"] if tool == "issue_read" else ["get", "get_comments"]):
            args = {"owner": "BerriAI", "repo": "moyai", "method": method}
            assert await bounds.before_tool_execute(ctx, call=call, tool_def=None, args=args) == args


async def test_restricted_github_schema_exposes_only_dispatchable_methods(runtime, actor):
    from copy import deepcopy

    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.toolsets import FunctionToolset

    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["issue_read"], "mcp": {"url": GITHUB_MCP}}
    prepared.scope.discovery_repositories = {grant.id: [("berriai", "moyai")]}
    tools = FunctionToolset()

    @tools.tool_plain
    def issue_read(method: str, owner: str, repo: str) -> str:
        return "unused"

    original = deepcopy(tools.tools["issue_read"].function_schema.json_schema)

    def response(messages, info):
        definition = next(tool for tool in info.function_tools if tool.name.endswith("_issue_read"))
        schema = definition.parameters_json_schema
        assert schema["properties"]["method"] == {"type": "string", "enum": ["get_comments"]}
        assert "method" in schema["required"]
        assert "owner" in schema["properties"] and "repo" in schema["properties"]
        return ModelResponse(parts=[TextPart("Scoped")])

    result = await Agent(
        FunctionModel(response), capabilities=[DiscoveryBounds()],
        toolsets=[tools.prefixed(f"mcp_{grant.id[:8]}")],
    ).run("Inspect", deps=prepared.scope)
    assert result.output == "Scoped"
    assert tools.tools["issue_read"].function_schema.json_schema == original


@pytest.mark.parametrize("payload", [
    {"items": [{"id": 1}], "total_count": 190},
    {"items": [], "incomplete_results": True},
    {"nodes": [], "pageInfo": {"hasNextPage": True}},
    {"items": [], "nextPageToken": "later"},
    {"items": [], "has_next_page": True},
    {"items": [], "coverage": {"has_more": False, "complete": False}},
    {"messages": [], "coverage": {"has_more": False, "partial": True}},
    {"id": "message", "content_complete": False},
])
def test_provider_pagination_is_partial(payload):
    assert provider_partial(payload)
    assert not provider_complete(payload)


@pytest.mark.parametrize("payload", [
    {"items": [], "coverage": {"has_more": False, "complete": True}},
    {"messages": [], "coverage": {"has_more": False, "partial": False, "page_complete": True}},
    {"id": "message", "content_complete": True},
])
def test_google_complete_reads_keep_explicit_coverage(payload):
    assert not provider_partial(payload)
    assert provider_complete(payload)


async def test_bare_and_explicit_github_pages_cannot_be_claimed_as_complete(runtime, actor):
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["list_pull_requests"], "mcp": {"url": GITHUB_MCP}}
    prepared.scope.discovery_repositories = {grant.id: [("berriai", "moyai")]}
    bounds, ctx = DiscoveryBounds(), SimpleNamespace(deps=prepared.scope)
    call = ToolCallPart(f"mcp_{grant.id[:8]}_list_pull_requests", {}, "read")
    row = {"url": "https://github.com/BerriAI/moyai/pull/1", "version": "one", "title": "Allowed"}
    for payload in [[row], [], {"items": [row], "has_next_page": True}]:
        result = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args={}, result=payload)
        assert result["scanned"] and result["limited"]
    terminal = {"items": [row], "pageInfo": {"hasNextPage": False}}
    result = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args={}, result=terminal)
    assert result["scanned"] and not result["limited"]
    for args in [{"page": 2}, {"after": "later-page"}]:
        result = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args=args, result=terminal)
        assert result["limited"]


async def test_github_unverified_issue_repositories_are_excluded_and_comments_keep_identity(runtime, actor):
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["list_issues", "issue_read"], "mcp": {"url": GITHUB_MCP}}
    prepared.scope.discovery_repositories = {grant.id: [("berriai", "moyai")]}
    bounds, ctx = DiscoveryBounds(), SimpleNamespace(deps=prepared.scope)
    args = {"owner": "BerriAI", "repo": "Moyai"}
    result = await bounds.after_tool_execute(ctx, call=ToolCallPart(
        f"mcp_{grant.id[:8]}_list_issues", {}, "read",
    ), tool_def=None, args=args, result={
        "issues": [
            # Number-only rows could belong to a renamed repository.
            {"number": 25, "title": "Reply needed", "updated_at": "2026-11-24T12:00:00Z", "state": "OPEN"},
            {"number": 26, "html_url": "https://github.com/BerriAI/moyai-renamed/issues/26", "title": "Renamed"},
            {"number": 27, "html_url": "https://github.com/BerriAI/Moyai/issues/27", "title": "Verified",
             "updated_at": "2026-11-24T12:00:00Z"},
        ],
        "pageInfo": {"hasNextPage": False},
    })
    assert result["scanned"] and result["limited"]
    assert result["records"] == [{
        "number": 27, "title": "Verified", "updated_at": "2026-11-24T12:00:00Z",
        "html_url": "https://github.com/berriai/moyai/issues/27",
    }]
    comment = {"html_url": "https://github.com/BerriAI/Moyai/issues/25#issuecomment-1",
               "updated_at": "2026-11-24T12:00:00Z", "body": "A reply"}
    result = await bounds.after_tool_execute(ctx, call=ToolCallPart(
        f"mcp_{grant.id[:8]}_issue_read", {}, "comments",
    ), tool_def=None, args={**args, "method": "get_comments"}, result={
        "content": [], "structuredContent": {"method": "get_comments", "comments": [comment]},
    })
    assert result["scanned"] and result["limited"]
    assert result["records"] == [{**comment, "html_url": comment["html_url"].lower()}]


async def test_judgment_cannot_resolve_an_open_item_from_a_quote(runtime, actor):
    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    _, conversation = await research(runtime, prepared, title="We attempted a fix")
    with pytest.raises(ValueError, match="closure evidence"):
        await publish(runtime, prepared, conversation, notify=False, state="resolved")
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(DotItem)) is None


async def test_github_resolution_requires_closed_field_in_same_verified_record(runtime, actor):
    from moyai.dot_contracts import DotEvidence
    from moyai.dot_evidence import verified_closed

    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["issue_read"], "mcp": {"url": GITHUB_MCP}}
    source = "https://github.com/BerriAI/moyai/issues/1"
    evidence = DotEvidence(connection_id=grant.id, source=source, source_version="one", quote="Fix verified")
    def conversation(state):
        return ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{grant.id[:8]}_issue_read", tool_call_id="read",
            content={"url": source, "version": "one", "title": "Fix verified", "state": state},
        )])])).decode()
    assert not verified_closed(evidence, conversation("open"), [grant])
    assert verified_closed(evidence, conversation("closed"), [grant])


@pytest.mark.parametrize("coverage", ["complete", "partial"])
async def test_verified_github_closure_requires_complete_scan(runtime, actor, coverage):
    async with runtime.database.sessions.begin() as db:
        account = Connection(
            owner_id=actor.id, kind="mcp", name="GitHub reader", credential=runtime.security.seal("synthetic"),
            read_only=True, policy=json.dumps({"allowed_tools": ["issue_read"], "mcp": {"url": GITHUB_MCP}}),
        )
        db.add(account)
        await db.flush()
    destination, _, automation, prepared = await observer(runtime, actor, connection=account, discovery=True)
    source = "https://github.com/BerriAI/moyai/issues/1"
    _, conversation = await research(runtime, prepared, source=source)
    await publish(runtime, prepared, conversation, notify=False)
    _, _, _, later = await observer(runtime, actor, destination, account, automation, discovery=True)
    payload, conversation = await research(runtime, later, source=source, source_state="closed", version="two", coverage=coverage)
    if coverage == "partial":
        assert DiscoverySnapshot.model_validate_json(payload).verified_closed == []
        with pytest.raises(ValueError, match="closure evidence"):
            await publish(runtime, later, conversation, notify=False, state="resolved")
    else:
        await publish(runtime, later, conversation, notify=False, state="resolved")
    assert (await runtime.database.dots.discovery.list(actor)).items[0].state == ("open" if coverage == "partial" else "resolved")


async def test_result_cap_cannot_hide_a_reopened_item_from_resolution_guard(runtime, actor):
    async with runtime.database.sessions.begin() as db:
        account = Connection(
            owner_id=actor.id, kind="mcp", name="GitHub reader", credential=runtime.security.seal("synthetic"),
            read_only=True, policy=json.dumps({"allowed_tools": ["pull_request_read", "list_pull_requests"],
                                              "mcp": {"url": GITHUB_MCP}}),
        )
        db.add(account)
        await db.flush()
    _, _, _, prepared = await observer(runtime, actor, connection=account, discovery=True)
    bounds, ctx = DiscoveryBounds(), SimpleNamespace(deps=prepared.scope)
    source, stamp = "https://github.com/berriai/moyai/pull/25", "2026-11-23T12:00:00Z"
    old = {"html_url": source, "updated_at": stamp, "state": "closed", "title": "Item"}
    newer = {**old, "updated_at": "2026-11-24T12:00:00Z", "state": "open"}
    returns = []
    for tool, payload in [
        ("pull_request_read", old),
        ("list_pull_requests", [{"html_url": f"https://github.com/berriai/moyai/pull/{i}",
                                "updated_at": stamp, "body": "x" * 2000} for i in range(30)] + [newer]),
    ]:
        name = f"mcp_{account.id[:8]}_{tool}"
        result = await bounds.after_tool_execute(
            ctx, call=ToolCallPart(name, {}, tool), tool_def=None, args={"method": "get"}, result=payload,
        )
        if tool == "list_pull_requests":
            assert result["limited"] and newer not in result["records"]
        returns.append(ToolReturnPart(name, result, tool_call_id=tool))
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=returns)])).decode()
    report = DotResearch.model_validate({"coverage": "complete", "observations": [{
        "title": "Item", "summary": "Closed in the older record", "evidence": [{
            "connection_id": account.id, "source": source, "source_version": stamp, "quote": "Item",
        }],
    }]})
    snapshot = DiscoverySnapshot.model_validate_json(await runtime.database.dots.discovery.judgment_input(
        prepared.scope, report.model_dump_json(), conversation,
    ))
    assert snapshot.research.coverage == "partial" and snapshot.verified_closed == []
    with pytest.raises(ValueError, match="closure evidence"):
        await publish(runtime, prepared, conversation, notify=False, state="resolved")


@pytest.mark.parametrize("row", ["unsupported row", None, {}, []])
@pytest.mark.parametrize("restricted", [False, True])
async def test_malformed_collection_rows_cannot_turn_an_earlier_read_into_closure(runtime, actor, row, restricted):
    async with runtime.database.sessions.begin() as db:
        account = Connection(
            owner_id=actor.id, kind="mcp", name="GitHub reader", credential=runtime.security.seal("synthetic"),
            read_only=True, policy=json.dumps({"allowed_tools": ["pull_request_read", "list_issues"],
                                              "mcp": {"url": GITHUB_MCP}}),
        )
        db.add(account)
        await db.flush()
    _, _, _, prepared = await observer(runtime, actor, connection=account, discovery=True)
    if restricted:
        async with runtime.database.sessions.begin() as db:
            occurrence = await db.get(DotDiscoveryTurn, prepared.scope.turn_id)
            occurrence.sources = runtime.security.seal(json.dumps([{
                "connection_id": account.id, "source": "https://github.com/BerriAI/moyai",
            }]))
            (await db.get(Turn, prepared.scope.turn_id)).preparation = None
        prepared = await runtime.database.admission.prepare(prepared.scope.turn_id)
    bounds, ctx = DiscoveryBounds(), SimpleNamespace(deps=prepared.scope)
    source, version = "https://github.com/berriai/moyai/pull/25", "2026-11-24T12:00:00Z"
    name = f"mcp_{account.id[:8]}_list_issues"
    call = ToolCallPart(name, {}, "page")
    args = {"owner": "BerriAI", "repo": "moyai"}
    empty = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args=args, result={
        "issues": [], "pageInfo": {"hasNextPage": False},
    })
    assert empty["scanned"] and not empty["limited"]
    malformed = await bounds.after_tool_execute(ctx, call=call, tool_def=None, args=args, result={
        "issues": [row], "pageInfo": {"hasNextPage": False},
    })
    assert malformed["limited"] and malformed["records"] == []
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=[
        ToolReturnPart(f"mcp_{account.id[:8]}_pull_request_read", {
            "dot_scan": True, "scanned": True, "limited": False, "error": False,
            "records": [{"url": source, "updated_at": version, "title": "Closed", "state": "closed"}],
        }, tool_call_id="earlier"),
        ToolReturnPart(name, malformed, tool_call_id="page"),
    ])])).decode()
    report = DotResearch.model_validate({"coverage": "complete", "observations": [{
        "title": "Closed", "summary": "Earlier read",
        "evidence": [{"connection_id": account.id, "source": source, "source_version": version, "quote": "Closed"}],
    }]})
    snapshot = DiscoverySnapshot.model_validate_json(await runtime.database.dots.discovery.judgment_input(
        prepared.scope, report.model_dump_json(), conversation,
    ))
    assert snapshot.research.coverage == "partial" and snapshot.verified_closed == []
    with pytest.raises(ValueError, match="closure evidence"):
        await publish(runtime, prepared, conversation, notify=False, state="resolved")


@pytest.mark.parametrize("outcome,content", [
    ("failed", "Provider read failed"),
    ("denied", "Access denied"),
    ("interrupted", "Read interrupted"),
    ("success", "Unrecognized result"),
    ("success", {"dot_scan": True, "scanned": False, "error": False, "limited": False, "records": []}),
])
async def test_incomplete_provider_read_cannot_leave_a_successful_scan_eligible_to_resolve(runtime, actor, outcome, content):
    async with runtime.database.sessions.begin() as db:
        account = Connection(
            owner_id=actor.id, kind="mcp", name="GitHub reader", credential=runtime.security.seal("synthetic"),
            read_only=True, policy=json.dumps({"allowed_tools": ["pull_request_read"], "mcp": {"url": GITHUB_MCP}}),
        )
        db.add(account)
        await db.flush()
    _, _, _, prepared = await observer(runtime, actor, connection=account, discovery=True)
    source, version, title = "https://github.com/berriai/moyai/pull/25", "2026-11-24T12:00:00Z", "Closed"
    name = f"mcp_{account.id[:8]}_pull_request_read"
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=[
        ToolReturnPart(name, {
            "dot_scan": True, "scanned": True, "limited": False, "error": False,
            "records": [{"url": source, "updated_at": version, "title": title, "state": "closed"}],
        }, tool_call_id="success"),
        ToolReturnPart(name, content, tool_call_id="incomplete", outcome=outcome),
    ])])).decode()
    report = DotResearch.model_validate({"coverage": "complete", "observations": [{
        "title": title, "summary": title,
        "evidence": [{"connection_id": account.id, "source": source, "source_version": version, "quote": title}],
    }]})
    snapshot = DiscoverySnapshot.model_validate_json(await runtime.database.dots.discovery.judgment_input(
        prepared.scope, report.model_dump_json(), conversation,
    ))
    assert snapshot.research.coverage == "partial"
    assert snapshot.verified_closed == []
    with pytest.raises(ValueError, match="closure evidence"):
        await publish(runtime, prepared, conversation, notify=False, state="resolved")


async def test_github_closure_uses_all_versions_and_rejects_stale_or_conflicting_state(runtime, actor):
    from moyai.dot_contracts import DotEvidence
    from moyai.dot_evidence import verified_closed

    _, _, _, prepared = await observer(runtime, actor, discovery=True)
    grant = prepared.scope.grants[0]
    grant.policy = {"allowed_tools": ["list_issues"], "mcp": {"url": GITHUB_MCP}}
    source = "https://github.com/BerriAI/moyai/issues/1"
    old, new = "2026-11-23T12:00:00Z", "2026-11-24T12:00:00Z"
    for states, versions, cited, expected in [
        (["closed", "open"], [old, new], old, False),
        (["open", "closed"], [old, new], new, False),
        (["closed", "open"], [new, new], new, False),
        (["closed", "closed"], [old, new], old, False),
        (["closed", "closed"], [old, new], new, True),
        (["closed", "closed"], ["opaque-a", "opaque-b"], "opaque-b", False),
    ]:
        evidence = DotEvidence(connection_id=grant.id, source=source, source_version=cited, quote="Item")
        conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{grant.id[:8]}_list_issues", tool_call_id="read",
            content={"items": [
                {"url": source, "updated_at": version, "title": "Item", "state": state}
                for state, version in zip(states, versions, strict=True)
            ]},
        )])])).decode()
        assert verified_closed(evidence, conversation, [grant]) is expected


@pytest.mark.parametrize("sequential_reads", [0, 11])
async def test_two_phase_workflow_uses_real_temporal_and_distinct_runs(runtime, actor, monkeypatch, sequential_reads):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.toolsets import FunctionToolset
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer

    from moyai.storage import encrypted_converter
    from moyai.worker import make_worker
    from moyai.worker_capacity import ActivityCapacity
    from moyai.workflow import TurnWorkflow

    destination, connection, _, prepared = await observer(runtime, actor, discovery=True)
    async with runtime.database.sessions.begin() as db:
        row = await db.get(Connection, connection.id)
        row.policy = json.dumps({"allowed_tools": ["read_item"], "mcp": {"url": "https://example.test/mcp"}})
        (await db.get(Turn, prepared.scope.turn_id)).preparation = None
    def read_item() -> list:
        return []
    monkeypatch.setattr("moyai.mcp_remote.remote_mcp_toolset", lambda *a, **kw: FunctionToolset(
        [read_item], metadata={"annotations": {"readOnlyHint": True}},
    ))
    phases = []
    def response(messages, info):
        schema = info.model_request_parameters.output_object.json_schema
        research_phase = "observations" in schema["properties"]
        phases.append("research" if research_phase else "judgment")
        if research_phase and sequential_reads:
            name = f"mcp_{connection.id[:8]}_read_item"
            if name not in {tool.name for tool in info.function_tools}:
                return ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["read_item"]}, "discover")])
            reads = sum(
                isinstance(part, ToolReturnPart) and part.tool_name == name
                for message in messages for part in message.parts
            )
            if reads < sequential_reads:
                return ModelResponse(parts=[ToolCallPart(name, {}, f"read-{reads}")])
        return ModelResponse(parts=[TextPart(json.dumps(
            {"coverage": "complete", "observations": []} if research_phase else
            {"items": [], "notify_observation": None, "message": ""},
        ))])
    runtime.models["test"].function = response
    async with await WorkflowEnvironment.start_local(
        plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, prepared.scope.turn_id, id=f"discovery-test-{uuid4()}",
                task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(handle.result(), 60))["status"] == "completed"
            await Replayer(
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()],
                data_converter=encrypted_converter(runtime.security, runtime.objects),
                interceptors=[ActivityCapacity()],
            ).replay_workflow(await handle.fetch_history())
    assert phases == ["research"] * (sequential_reads + (2 if sequential_reads else 1)) + ["judgment"]
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, prepared.scope.turn_id)).outcome == ("quiet" if sequential_reads else "blocked")
        assert (await db.get(Session, destination)).conversation is None
        turn = await db.get(Turn, prepared.scope.turn_id)
        assert json.loads(turn.usage)["requests"] == len(phases)
