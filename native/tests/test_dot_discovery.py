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
from moyai.models import Connection, Message, Session, Turn


async def research(runtime, prepared, *, version="one", coverage="complete", title="Customer reply needed"):
    account = prepared.scope.grants[0].id
    evidence = {"connection_id": account, "source": "https://example.test/item", "source_version": version, "quote": title}
    report = DotResearch.model_validate({
        "coverage": coverage, "observations": [{"title": title, "summary": title, "evidence": [evidence]}],
    })
    conversation = ConversationTypeAdapter.dump_json(Conversation(messages=[
        ModelRequest(parts=[ToolReturnPart(
            tool_name=f"mcp_{account[:8]}_read_item", tool_call_id="scan",
            content={"dot_scan": True, "scanned": True, "limited": False, "error": False,
                     "records": [{"url": evidence["source"], "version": version, "title": title}]},
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


async def test_two_phase_workflow_uses_real_temporal_and_distinct_runs(runtime, actor, monkeypatch):
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.toolsets import FunctionToolset
    from temporalio.testing import WorkflowEnvironment

    from moyai.storage import encrypted_converter
    from moyai.worker import make_worker
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
    assert phases == ["research", "judgment"]
    async with runtime.database.sessions() as db:
        assert (await db.get(DotAttentionTurn, prepared.scope.turn_id)).outcome == "blocked"
        assert (await db.get(Session, destination)).conversation is None
        turn = await db.get(Turn, prepared.scope.turn_id)
        assert json.loads(turn.usage)["requests"] == 2
