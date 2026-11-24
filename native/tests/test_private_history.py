"""Real PostgreSQL authority regressions; actual-app model proof is separate."""

import os
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
import test_product_authority as authority
from fastapi import HTTPException
from pydantic_ai import Conversation, ConversationTypeAdapter, DeferredToolRequests, RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from sqlalchemy import delete, text, update

from moyai.automations.product_automation_models import ProductAutomationTurn
from moyai.contracts import ExecutionContext
from moyai.delegation_models import DelegatedSession
from moyai.models import Message, MessageSource, Session, Turn, User
from moyai.preferences.settings_models import SettingAutomation, SettingAutomationRun
from moyai.private_history import (
    MAX_CHATS,
    MAX_MESSAGES,
    MAX_PREFIX,
    READ_CHARS,
    SNIPPET_CHARS,
    InactiveHistory,
    PrivateHistory,
    PrivateHistoryRecall,
)

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
admitted_product = authority.admitted_product


async def seed(product, *, owner=None, visibility="private", surface="web", deleted=False, content=None):
    owner = owner or product.actor.id
    async with product.database.sessions.begin() as db:
        session = Session(
            owner_id=owner, title="Cobalt project notes", visibility=visibility, surface=surface,
            deleted=deleted, model="test", created_at="2025-01-01T00:00:00Z",
        )
        db.add(session)
        await db.flush()
        turn = Turn(
            session_id=session.id, actor_id=owner, request_key=str(uuid4()), request_fingerprint="f" * 64,
            prompt=product.database.security.seal("Never inspect hidden prompt Cobalt"),
            model="test", workflow_id=str(uuid4()), status="completed",
        )
        db.add(turn)
        await db.flush()
        if owner == product.actor.id:
            product.database._bind_product_grant(db, turn, product.principal.grant)
        message = Message(
            session_id=session.id, turn_id=turn.id, author_id=owner, role="user",
            content=product.database.security.seal(content or "Cobalt rendezvous is at Juniper Pier."),
            identity=f"user:{turn.id}", created_at="2025-01-01T00:00:00Z",
        )
        db.add(message)
        await db.flush()
        return session.id, message.id, turn.id


@pytest_asyncio.fixture
async def history(admitted_product):
    product = admitted_product
    source = await seed(product)
    _session, _turn, prepared = await authority.active_turn(product, private=True)
    context = RunContext(
        deps=prepared.scope, run_id=prepared.scope.root_run_id, model=TestModel(), usage=RunUsage(),
    )
    return product, PrivateHistory(product.database), context, source


async def test_search_and_read_grounded_bounded_untrusted_evidence(history):
    _product, service, ctx, (sid, mid, _) = history
    result = await service.search(ctx, "cobalt")
    assert [hit["message_id"] for hit in result["matches"]] == [mid]
    match = result["matches"][0]
    assert match["chat_url"] == f"/#{sid}"
    assert "Juniper Pier" in match["excerpt"]
    assert len(match["excerpt"]) <= SNIPPET_CHARS
    assert "untrusted" in result["evidence_policy"]
    assert "instructions" in result["evidence_policy"]
    read = await service.read(ctx, sid, mid, "Cobalt")
    assert read["source"]["message_id"] == mid
    assert len(read["source"]["excerpt"]) <= READ_CHARS
    assert (await service.search(ctx, "hidden prompt"))["matches"] == []


async def test_real_temporal_activity_context_needs_no_message_history(history):
    from moyai.agent import HarnessActivityContext

    _product, service, ctx, (sid, mid, _) = history
    activity = HarnessActivityContext.deserialize_run_context(
        HarnessActivityContext.serialize_run_context(ctx), ctx.deps,
    )
    assert [hit["message_id"] for hit in (await service.search(activity, "Cobalt"))["matches"]] == [mid]
    assert (await service.read(activity, sid, mid))["source"]["message_id"] == mid


@pytest.mark.parametrize("change", [
    {"visibility": "shared"}, {"surface": "slack"}, {"product_grant": None},
    {"execution_context": ExecutionContext("private", "automation")},
    {"execution_context": ExecutionContext("private", "web")},
    {"delegation_depth": 1}, {"delegation_generated": True},
    {"swarm_id": "team"}, {"goal_generation": 1},
])
async def test_scope_variants_hide_and_refuse_tools(history, change):
    product, service, ctx, source = history
    ctx.deps = replace(ctx.deps, **change)
    assert isinstance(await PrivateHistoryRecall(product.database).for_run(ctx), InactiveHistory)
    with pytest.raises(PermissionError):
        await service.search(ctx, "Cobalt")
    with pytest.raises(PermissionError):
        await service.read(ctx, source[0], source[1])


async def test_sdk_delegate_run_cannot_use_parent_scope(history):
    product, service, ctx, _ = history
    ctx.run_id = "sdk-child"
    assert isinstance(await PrivateHistoryRecall(product.database).for_run(ctx), InactiveHistory)
    with pytest.raises(PermissionError):
        await service.search(ctx, "Cobalt")


async def test_foreign_shared_slack_deleted_current_and_future_messages_are_excluded(history):
    product, service, ctx, source = history
    async with product.database.sessions.begin() as db:
        db.add(User(id="other", username="other", display_name="Other", password="unused"))
    sources = [
        await seed(product, owner="other"),
        await seed(product, visibility="shared"),
        await seed(product, surface="slack"),
        await seed(product, deleted=True),
        await seed(product),
    ]
    async with product.database.sessions.begin() as db:
        await db.execute(update(Message).where(Message.id == sources[-1][1]).values(created_at="2999"))
    assert [hit["message_id"] for hit in (await service.search(ctx, "Cobalt"))["matches"]] == [source[1]]
    for sid, mid, _ in sources:
        with pytest.raises(ToolFailed, match="unavailable"):
            await service.read(ctx, sid, mid)
    with pytest.raises(ToolFailed, match="unavailable"):
        await service.read(ctx, ctx.deps.session_id, source[1])


async def test_returning_to_older_chat_recalls_later_created_chat(history):
    product, service, ctx, (sid, mid, _) = history
    async with product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == ctx.deps.session_id).values(created_at="2020"))
        await db.execute(update(Session).where(Session.id == sid).values(created_at="2025"))
    assert [hit["message_id"] for hit in (await service.search(ctx, "Cobalt"))["matches"]] == [mid]
    assert (await service.read(ctx, sid, mid))["source"]["chat_id"] == sid


@pytest.mark.parametrize("kind", ["automated", "delegated", "slack_message"])
async def test_persisted_provenance_excluded_without_trusting_scope_fields(history, kind):
    product, service, ctx, (sid, mid, tid) = history
    async with product.database.sessions.begin() as db:
        if kind == "automated":
            db.add(ProductAutomationTurn(turn_id=tid, origin_turn_id=tid, visibility="private"))
        elif kind == "delegated":
            db.add(DelegatedSession(
                session_id=sid, parent_session_id=ctx.deps.session_id, parent_turn_id=ctx.deps.turn_id,
                initial_turn_id=tid, actor_id=ctx.deps.actor_id, tool_call_id="child",
                fingerprint="f" * 64, grants="[]", depth=1, name="Child", avatar=0,
            ))
        else:
            db.add(MessageSource(message_identity=f"user:{tid}", kind="slack", data="unused"))
    assert (await service.search(ctx, "Cobalt"))["matches"] == []
    with pytest.raises(ToolFailed):
        await service.read(ctx, sid, mid)


async def test_persisted_automation_caller_refused_even_with_web_scope(history):
    product, service, ctx, _ = history
    async with product.database.sessions.begin() as db:
        db.add(ProductAutomationTurn(
            turn_id=ctx.deps.turn_id, origin_turn_id=ctx.deps.turn_id, visibility="private",
        ))
    with pytest.raises(PermissionError):
        await service.search(ctx, "Cobalt")


async def interval_receipt(product, session_id, turn_id, *, linked):
    async with product.database.sessions.begin() as db:
        automation = SettingAutomation(
            owner_id=product.actor.id, owner_auth_version=1, session_id=session_id,
            name="Interval", prompt="encrypted-unused", model="test", interval_minutes=60,
        )
        db.add(automation)
        await db.flush()
        identity = uuid4().hex
        db.add(SettingAutomationRun(
            id=identity, automation_id=automation.id, turn_id=turn_id if linked else None,
            request="encrypted-unused",
        ))
        if not linked:
            await db.execute(update(Turn).where(Turn.id == turn_id).values(request_key=f"automation:{identity}"))


async def resume(product, ctx):
    database = product.database
    conversation = Conversation(
        conversation_id=ctx.deps.conversation_id,
        deferred_tool_requests=DeferredToolRequests(approvals=[ToolCallPart("write_file", {}, tool_call_id="write")]),
    )
    assert await database.save_result(
        ctx.deps, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        output="", workspace=None, usage={}, deferred=True,
    )
    turn = await database.resume(
        product.actor, ctx.deps.session_id, ctx.deps.turn_id, str(uuid4()), {"approvals": {"write": True}},
    )
    assert (await database.claim_next()).id == turn.id
    scope = (await database.admission.prepare(turn.id)).scope
    return RunContext(deps=scope, run_id=scope.root_run_id, model=TestModel(), usage=RunUsage())


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
async def test_interval_receipt_and_real_approval_resume_cannot_recall(history, linked, resumed):
    product, service, ctx, _ = history
    await interval_receipt(product, ctx.deps.session_id, ctx.deps.turn_id, linked=linked)
    if resumed:
        ctx = await resume(product, ctx)
    # Native interval turns retain private/web scope; persisted origin is decisive.
    assert ctx.deps.surface == "web" and ctx.deps.execution_context is None
    with pytest.raises(PermissionError):
        await service.search(ctx, "Cobalt")


async def test_real_human_approval_resume_can_recall(history):
    product, service, ctx, (_, mid, _) = history
    ctx = await resume(product, ctx)
    assert [hit["message_id"] for hit in (await service.search(ctx, "Cobalt"))["matches"]] == [mid]


@pytest.mark.parametrize("linked", [False, True])
async def test_interval_source_messages_fail_before_decryption(history, monkeypatch, linked):
    product, service, ctx, (sid, mid, tid) = history
    await interval_receipt(product, sid, tid, linked=linked)
    monkeypatch.setattr(service, "text", lambda _: pytest.fail("Decrypted automation source"))
    assert (await service.search(ctx, "Cobalt"))["matches"] == []
    with pytest.raises(ToolFailed):
        await service.read(ctx, sid, mid)


async def test_interval_approval_ancestry_excluded_from_historical_sources(history, monkeypatch):
    product, service, ctx, _ = history
    # Produce a real resumptive turn whose own fields look human/private/web.
    await interval_receipt(product, ctx.deps.session_id, ctx.deps.turn_id, linked=False)
    resumed = await resume(product, ctx)
    database = product.database
    conversation = Conversation(conversation_id=resumed.deps.conversation_id)
    assert await database.save_result(
        resumed.deps, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
        output="Cobalt automated answer", workspace=None, usage={},
    )
    await database.settle(resumed.deps.turn_id)
    _session, _turn, prepared = await authority.active_turn(product, private=True)
    caller = RunContext(deps=prepared.scope, run_id=prepared.scope.root_run_id, model=TestModel(), usage=RunUsage())
    original = service.text

    def no_automated_text(message):
        assert message.turn_id != resumed.deps.turn_id
        return original(message)

    monkeypatch.setattr(service, "text", no_automated_text)
    assert all(hit["chat_id"] != ctx.deps.session_id for hit in (await service.search(caller, "Cobalt"))["matches"])


@pytest.mark.parametrize("change", ["deleted", "shared", "foreign", "message_deleted", "message_edited"])
async def test_source_rechecked_after_async_search(history, monkeypatch, change):
    product, service, ctx, (sid, mid, _) = history
    release = service.release

    async def revoked(*args):
        async with product.database.sessions.begin() as db:
            if change == "message_deleted":
                await db.execute(delete(Message).where(Message.id == mid))
            elif change == "message_edited":
                await db.execute(update(Message).where(Message.id == mid).values(
                    content=product.database.security.seal("An unrelated replacement"),
                ))
            else:
                values = {"deleted": True} if change == "deleted" else {"visibility": "shared"}
                if change == "foreign":
                    db.add(User(id="other", username="other", display_name="Other", password="unused"))
                    await db.flush()
                    values = {"owner_id": "other"}
                await db.execute(update(Session).where(Session.id == sid).values(**values))
        return await release(*args)

    monkeypatch.setattr(service, "release", revoked)
    assert (await service.search(ctx, "Cobalt"))["matches"] == []


@pytest.mark.parametrize("change", ["current_shared", "stopped", "disabled", "canonical_deleted"])
async def test_requester_rechecked_after_async_search(history, monkeypatch, change):
    product, service, ctx, _ = history
    release = service.release

    async def revoked(*args):
        async with product.database.sessions.begin() as db:
            if change == "current_shared":
                await db.execute(update(Session).where(Session.id == ctx.deps.session_id).values(visibility="shared"))
            elif change == "stopped":
                await db.execute(update(Turn).where(Turn.id == ctx.deps.turn_id).values(status="cancelled"))
            elif change == "disabled":
                await db.execute(update(User).where(User.id == ctx.deps.actor_id).values(enabled=False))
            else:
                await db.execute(text("DELETE FROM moyai.users WHERE id=:id"), {"id": ctx.deps.actor_id})
        return await release(*args)

    monkeypatch.setattr(service, "release", revoked)
    with pytest.raises((PermissionError, HTTPException)):
        await service.search(ctx, "Cobalt")


async def test_read_rechecks_current_scope_after_source_await(history, monkeypatch):
    product, service, ctx, source = history
    original = service.source
    calls = 0

    async def changed(*args):
        nonlocal calls
        result = await original(*args)
        calls += 1
        if calls == 1:
            async with product.database.sessions.begin() as db:
                await db.execute(update(Session).where(Session.id == ctx.deps.session_id).values(visibility="shared"))
        return result

    monkeypatch.setattr(service, "source", changed)
    with pytest.raises(PermissionError):
        await service.read(ctx, source[0], source[1])


async def test_query_is_literal_and_long_hidden_or_oversized_content_is_not_returned(history):
    product, service, ctx, _ = history
    assert (await service.search(ctx, "%_"))["matches"] == []
    for query in ("", " ", "x", "x" * 161):
        with pytest.raises(ToolFailed):
            await service.search(ctx, query)
    for content in ("x" * MAX_PREFIX + " secret-tail", "secret-large " + "x" * 50_000):
        sid, mid, _ = await seed(product, content=content)
        assert (await service.search(ctx, "secret-"))["matches"] == []
    sid, mid, _ = await seed(product, content="x" * 500 + "Cobalt marker " + "y" * 3000)
    result = await service.read(ctx, sid, mid, "Cobalt")
    assert len(result["source"]["excerpt"]) == READ_CHARS
    assert "Cobalt marker" in result["source"]["excerpt"]


async def test_message_and_chat_search_budgets_are_reported_and_enforced(history):
    product, service, ctx, source = history
    sid, _, tid = source
    async with product.database.sessions.begin() as db:
        db.add_all(Message(
            session_id=sid, turn_id=tid, author_id=ctx.deps.actor_id, role="user",
            content=product.database.security.seal("Recent filler"), identity=str(uuid4()),
            created_at="2025-01-01T00:00:00Z",
        ) for _ in range(MAX_MESSAGES))
    result = await service.search(ctx, "Cobalt")
    assert result["matches"] == []
    assert result["limits"]["recent_messages"] == MAX_MESSAGES
    for _ in range(MAX_CHATS):
        await seed(product, content="Newer filler")
    result = await service.search(ctx, "Cobalt")
    assert result["matches"] == []
    assert result["limits"]["recent_chats"] == MAX_CHATS


async def test_history_tools_execute_in_real_temporal_and_replay(history, tmp_path):
    """Controlled model integration; the separate actual-app proof uses the real gateway."""
    import asyncio

    from pydantic_ai.capabilities import LocalWorkspace
    from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
    from pydantic_ai.messages import ModelResponse, TextPart, ToolReturnPart
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer
    from test_bundled_skills_startup import scripted_model

    from moyai.product_identity import ProductIdentityAdapter
    from moyai.runtime import Runtime
    from moyai.storage import encrypted_converter
    from moyai.stores import NativeStores
    from moyai.worker import make_worker
    from moyai.worker_capacity import ActivityCapacity
    from moyai.workflow import TurnWorkflow

    product, _service, ctx, (sid, mid, _) = history
    settings = product.settings.model_copy(update={"product_mode": "standalone"})
    provision = NativeStores(settings)
    try:
        await provision.start(provision=True)
    finally:
        await provision.close()
    runtime = Runtime(settings)
    runtime.product_identity = ProductIdentityAdapter(
        product.adapter.settings, product.source, schema="moyai", native_engine=runtime.database.engine,
    )
    runtime.database.product_identity = runtime.product_identity
    workspace = tmp_path / "computer"
    (workspace / "skills").mkdir(parents=True)
    calls = []

    def respond(messages, _info):
        results = {part.tool_name: part for message in messages for part in message.parts
                   if isinstance(part, ToolReturnPart)}
        if "private_history_search" not in results:
            calls.append("search")
            return ModelResponse(parts=[ToolCallPart(
                "private_history_search", {"query": "Cobalt"}, tool_call_id="history-search",
            )])
        search = results["private_history_search"]
        assert search.outcome == "success" and search.content["matches"][0]["message_id"] == mid
        if "private_history_read" not in results:
            calls.append("read")
            return ModelResponse(parts=[ToolCallPart(
                "private_history_read", {"chat_id": sid, "message_id": mid}, tool_call_id="history-read",
            )])
        read = results["private_history_read"]
        assert read.outcome == "success" and "Juniper Pier" in read.content["source"]["excerpt"]
        return ModelResponse(parts=[TextPart(f"Juniper Pier, source /#{sid}, message {mid}.")])

    try:
        await runtime.start(
            models={"test": scripted_model(respond)}, workspace=LocalWorkspace(workspace),
            skills_directory="skills",
        )
        converter = encrypted_converter(runtime.security, runtime.objects)
        async with await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ) as environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, ctx.deps.turn_id, id=f"moyai-turn-{ctx.deps.turn_id}",
                    task_queue=settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 45))["status"] == "completed"
                recorded = await handle.fetch_history()
            assert calls == ["search", "read"]
            await Replayer(
                interceptors=[ActivityCapacity()], workflows=[TurnWorkflow],
                plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(recorded)
            assert calls == ["search", "read"]  # Replay never dispatches a new tool/model action.
    finally:
        await runtime.close()
