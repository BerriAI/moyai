"""Real local persistence and model inputs for scheduled chat continuity."""
from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import pytest
import test_product_automations as fixtures
from fastapi import HTTPException
from pydantic_ai import Agent, Conversation, ConversationTypeAdapter, DeferredToolRequests
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select, update
from test_product_automation_access import invoke

from moyai.contracts import NewMessage, NewSession
from moyai.goal_models import Goal, GoalTurn
from moyai.models import Message, Session, Turn
from moyai.product_automation_models import ProductAutomationTurn
from moyai.product_automation_tools import CreateRequest
from moyai.product_automations import Mutation, definition_digest

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
automations = fixtures.automations


async def run_turn(ctx, turn_id, response, *, workspace=None):
    database = ctx.product.database
    assert (await database.claim_next()).id == turn_id
    prepared = await database.prepare(turn_id)
    conversation = (ConversationTypeAdapter.validate_json(prepared.conversation) if prepared.conversation
                    else Conversation(conversation_id=prepared.scope.conversation_id))
    result = await Agent(FunctionModel(response)).run(prepared.prompt, conversation=conversation)
    assert await database.save_result(
        prepared.scope, conversation=ConversationTypeAdapter.dump_json(result.conversation).decode(),
        output=result.output, workspace=workspace or prepared.workspace, usage={},
    )
    await database.settle(turn_id)
    return prepared, result.output


def test_old_definitions_keep_their_enabled_digest():
    definition = fixtures.definition()
    assert definition.session_id is None
    old = definition.model_dump(mode="json", exclude={"session_id"})
    assert definition_digest(definition) == hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("continued", [False, True])
async def test_repeated_runs_deliver_real_previous_messages_only_in_continued_chat(automations, continued):
    ctx, seen = automations, []
    database = ctx.product.database
    session = await database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    marker = "Context marker " + uuid4().hex
    human = await database.submit(ctx.product.actor, session.id, NewMessage(prompt=marker, request_key="human"))
    workspace = {"provider": "local", "id": "/tmp/synthetic-chat-computer"}
    await run_turn(ctx, human.id, lambda *_: ModelResponse(parts=[TextPart("Original chat answer")]), workspace=workspace)
    row, binding = await fixtures.activate(ctx, session_id=session.id if continued else None)

    def observe(messages, _):
        text = [part.content for message in messages for part in message.parts
                if isinstance(part, (TextPart, UserPromptPart)) and isinstance(part.content, str)]
        seen.append(text)
        return ModelResponse(parts=[TextPart("Saved run output " + str(len(seen)))])

    first = await fixtures.occurrence(ctx, row, binding)
    prepared, _ = await run_turn(ctx, first["turn_id"], observe)
    second = await ctx.service.run_now(ctx.grant, row["id"], Mutation(revision=row["revision"], request_key="manual-run"))
    second_prepared, _ = await run_turn(ctx, second["turn_id"], observe)
    assert (first["session_id"] == second["session_id"]) is continued
    assert (marker in seen[0]) is continued
    assert ("Original chat answer" in seen[0]) is continued
    assert ("Saved run output 1" in seen[1]) is continued
    assert prepared.workspace == second_prepared.workspace == (workspace if continued else None)
    assert prepared.scope.authority.surface == second_prepared.scope.authority.surface == "automation"
    async with database.sessions() as db:
        saved = await db.get(Session, first["session_id"])
        assert saved.surface == ("web" if continued else "automation")
        message = await db.scalar(select(Message).where(Message.turn_id == first["turn_id"], Message.role != "assistant"))
        assert message.role == "system" and message.author_id is None
        assert await db.get(ProductAutomationTurn, second["turn_id"]) is not None
    if continued:
        followup = await database.submit(ctx.product.actor, session.id, NewMessage(prompt="Change my automation", request_key="followup"))
        await database.claim_next()
        scope = (await database.prepare(followup.id)).scope
        assert scope.authority.surface == "web"
        assert (await ctx.service.authorize_tool(scope, scope.turn_id)).id == ctx.product.actor.id


@pytest.mark.parametrize("destination", [None, "new_chat"])
async def test_chat_tool_defaults_to_current_chat_with_explicit_standalone_choice(automations, destination):
    ctx = automations
    run = await fixtures.tool_context(ctx)
    request = CreateRequest(revision=0, request_key="tool-create", turn_id=run.deps.turn_id,
                            definition=fixtures.definition(), **({"destination": destination} if destination else {}))
    result = await invoke(ctx, run, "automation_create", request)
    preview = await ctx.service.preview(ctx.grant, result["id"])
    assert preview["definition"]["session_id"] == (None if destination else run.deps.session_id)


@pytest.mark.parametrize("change", ["private", "archived", "deleted", "owner"])
async def test_destination_changes_block_admission_and_already_queued_execution(automations, change):
    ctx = automations
    database = ctx.product.database
    session = await database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    row, binding = await fixtures.activate(ctx, session_id=session.id)
    first = await fixtures.occurrence(ctx, row, binding)
    # A real different native owner is needed to preserve the FK.
    if change == "owner":
        from moyai.models import User
        async with database.sessions.begin() as db:
            db.add(User(id="different-owner", username="different-owner", display_name="Other", password="unusable"))
    values = {"visibility": "private"} if change == "private" else {"owner_id": "different-owner"} if change == "owner" else {change: True}
    async with database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == session.id).values(**values))
    next_run = await fixtures.occurrence(ctx, row, binding)
    assert next_run["status"] != "admitted"
    assert "scheduled_chat_unavailable" in (await ctx.service.preview(ctx.grant, row["id"]))["blockers"]
    # Check the same policy used at prepare and before every tool call, even if
    # the scheduler no longer selects an archived/deleted session for dispatch.
    async with database.sessions() as db:
        saved = await db.get(Session, session.id)
        with pytest.raises(PermissionError):
            await database.execution_context(db, saved, turn_id=first["turn_id"])


async def test_queued_run_waits_for_human_and_cannot_join_an_active_goal(automations):
    ctx = automations
    run = await fixtures.tool_context(ctx)
    database = ctx.product.database
    async with database.sessions.begin() as db:
        db.add(Goal(session_id=run.deps.session_id, owner_id=ctx.product.actor.id,
                    auth_version=ctx.product.actor.auth_version, objective=database.security.seal("Goal"),
                    model="test", connections="[]", grants_key=run.deps.grants_key, source_turn_id=run.deps.turn_id))
    row, binding = await fixtures.activate(ctx, session_id=run.deps.session_id)
    admitted = await fixtures.occurrence(ctx, row, binding)
    assert await database.claim_next() is None
    async with database.sessions() as db:
        turn = await db.get(Turn, admitted["turn_id"])
        assert turn.status == "queued" and turn.parent_turn is None
        assert await db.get(GoalTurn, turn.id) is None


@pytest.mark.parametrize("question", [False, True])
async def test_resumed_run_keeps_automation_authority_and_occurrence_identity(automations, question):
    ctx = automations
    database = ctx.product.database
    session = await database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    row, binding = await fixtures.activate(ctx, session_id=session.id, self_management={"instructions": True})
    first = await fixtures.occurrence(ctx, row, binding)
    await database.claim_next()
    scope = (await database.prepare(first["turn_id"])).scope
    with pytest.raises(HTTPException):
        await ctx.service.authorize_tool(scope, scope.turn_id)
    call = ToolCallPart("ask_user_question", {"questions": [{"header": "Color", "question": "Choose a color", "options": [{"label": "Blue"}, {"label": "Green"}]}]}, tool_call_id="color")
    pending = DeferredToolRequests(calls=[call]) if question else DeferredToolRequests(approvals=[ToolCallPart("write_file", {}, tool_call_id="write")])
    conversation = Conversation(conversation_id=scope.conversation_id, deferred_tool_requests=pending)
    assert await database.save_result(scope, conversation=ConversationTypeAdapter.dump_json(conversation).decode(),
                                      output="", workspace=None, usage={}, deferred=True)
    if question:
        reply = NewMessage(prompt="Blue", request_key="answer", reply_to=scope.turn_id)
        resumed = await database.submit(ctx.product.actor, session.id, reply)
        assert (await database.submit(ctx.product.actor, session.id, reply)).id == resumed.id
    else:
        resumed = await database.resume(ctx.product.actor, session.id, scope.turn_id, "answer", {"approvals": {"write": True}})
    await database.claim_next()
    resumed_scope = (await database.prepare(resumed.id)).scope
    assert resumed_scope.authority.surface == "automation"
    with pytest.raises(HTTPException):
        await ctx.service.authorize_tool(resumed_scope, resumed_scope.turn_id)
    async with database.sessions() as db:
        _, source = await ctx.service.access.source(db, resumed_scope)
        assert source.turn_id == first["turn_id"] and source.automation_id == row["id"]
        assert (await db.get(ProductAutomationTurn, resumed.id)).origin_turn_id == scope.turn_id
        assert await db.get(GoalTurn, resumed.id) is None
    history = await ctx.service.history(ctx.grant, row["id"])
    assert history["history"][0]["status"] == "running"


async def test_multiple_schedules_in_one_chat_keep_separate_scoped_permissions(automations):
    ctx = automations
    database = ctx.product.database
    session = await database.create_session(ctx.product.actor, NewSession(visibility="shared"))
    first, first_binding = await fixtures.activate(ctx, session_id=session.id, self_management={"instructions": True})
    second, second_binding = await fixtures.activate(ctx, session_id=session.id, self_management={"schedule": True})
    first_run = await fixtures.occurrence(ctx, first, first_binding)
    await run_turn(ctx, first_run["turn_id"], lambda *_: ModelResponse(parts=[TextPart("Earlier schedule result")]))
    second_run = await fixtures.occurrence(ctx, second, second_binding)
    await database.claim_next()
    prepared = await database.prepare(second_run["turn_id"])
    async with database.sessions() as db:
        _, source = await ctx.service.access.source(db, prepared.scope)
        assert source.automation_id == second["id"]
    assert "Earlier schedule result" in prepared.conversation
    with pytest.raises(HTTPException):
        await ctx.service.access.authorize_history(prepared.scope, prepared.scope.turn_id, first["id"])


@pytest.mark.parametrize("change", ["accounts", "project"])
async def test_changed_chat_selections_require_explicit_reenable(automations, change):
    from moyai.product_continuation_models import ProductExecutionContext
    from moyai.product_continuation_workspace import fingerprint
    ctx = automations
    run = await fixtures.tool_context(ctx)
    database = ctx.product.database
    row, binding = await fixtures.activate(ctx, session_id=run.deps.session_id)
    async with database.sessions.begin() as db:
        if change == "accounts":
            (await db.get(Turn, run.deps.turn_id)).connections = '["new-account-selection"]'
        else:
            context = {"selection": {"environment_id": "changed"}}
            db.add(ProductExecutionContext(turn_id=run.deps.turn_id, actor_id=ctx.product.actor.id,
                                           descriptor=database.security.seal(context), fingerprint=fingerprint(context)))
    assert (await fixtures.occurrence(ctx, row, binding))["status"] == "blocked"
