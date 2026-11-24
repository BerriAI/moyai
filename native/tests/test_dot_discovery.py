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
from moyai.dot_models import DotAttentionTurn, DotItem, DotNotification
from moyai.dot_sources import GITHUB_MCP, provider_partial, repositories
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


@pytest.mark.parametrize("payload", [
    {"items": [{"id": 1}], "total_count": 190},
    {"items": [], "incomplete_results": True},
    {"nodes": [], "pageInfo": {"hasNextPage": True}},
    {"items": [], "nextPageToken": "later"},
    {"items": [], "has_next_page": True},
])
def test_provider_pagination_is_partial(payload):
    assert provider_partial(payload)


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


async def test_verified_github_closure_resolves_existing_item_even_in_partial_scan(runtime, actor):
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
    _, conversation = await research(runtime, later, source=source, source_state="closed", version="two", coverage="partial")
    await publish(runtime, later, conversation, notify=False, state="resolved")
    assert (await runtime.database.dots.discovery.list(actor)).items[0].state == "resolved"


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


async def test_two_phase_workflow_uses_real_temporal_and_distinct_runs(runtime, actor, monkeypatch):
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
    monkeypatch.setattr("moyai.mcp_remote.remote_mcp_toolset", lambda *a, **kw: FunctionToolset())
    phases = []
    def response(messages, info):
        schema = info.model_request_parameters.output_object.json_schema
        research_phase = "observations" in schema["properties"]
        phases.append("research" if research_phase else "judgment")
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
    assert phases == ["research", "judgment"]
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, prepared.scope.turn_id)).outcome == "blocked"
        assert (await db.get(Session, destination)).conversation is None
        turn = await db.get(Turn, prepared.scope.turn_id)
        assert json.loads(turn.usage)["requests"] == 2
