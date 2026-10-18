from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import func, select, update

from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage, NewSession
from moyai.goal_models import Goal, GoalTurn
from moyai.goal_tools import GoalCapability
from moyai.goals import is_goal_command, parse_command
from moyai.models import Connection, Message, Turn, TurnImage, Upload, User
from moyai.product_schema import GOAL_SCHEMA_VERSION, SLACK_ACTIVITY_SCHEMA_VERSION, application_metadata


async def command(runtime, actor, session_id, text, key=None, **kwargs):
    return await runtime.database.goals.command(
        actor, session_id, NewMessage(prompt=text, request_key=key or str(uuid4()), **kwargs),
    )


async def start(runtime, actor, prompt="/goal Finish the verified task."):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    result = await command(runtime, actor, session.id, prompt)
    return session, result["id"]


async def goal(runtime, session_id):
    async with runtime.database.sessions() as db:
        return runtime.database.goals.view(await db.get(Goal, session_id))


async def finish(runtime, *, decision=None, evidence="Verified", usage=None, deferred=False):
    turn = await runtime.database.claim_next()
    assert turn is not None
    prepared = await runtime.database.prepare(turn.id)
    if decision:
        async with runtime.database.sessions.begin() as db:
            binding = await db.get(GoalTurn, turn.id)
            binding.decision = decision
            binding.evidence = runtime.security.seal(evidence)
    await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="Progress saved.",
        workspace=None, usage=usage or {}, deferred=deferred,
    )
    if not deferred:
        await runtime.database.settle(turn.id)
    return turn, prepared


@pytest.mark.parametrize("text,expected", [
    ("/goal", True), ("/goal Do it", True), ("  /goal pause", True),
    ("/goals", False), ("/goal/file", False), ("Discuss /goal pause", False),
    ("    /goal pause", False), ("\t/goal pause", False),
])
def test_only_leading_command(text, expected):
    assert is_goal_command(text) is expected


@pytest.mark.parametrize("text", ["/goal edit", "/goal --budget 0 task", "/goal " + "x" * 4001])
def test_invalid_goal(text):
    with pytest.raises(HTTPException):
        parse_command(text)


def test_versioned_migration_preserves_previous_inventory():
    old = application_metadata("native", version=SLACK_ACTIVITY_SCHEMA_VERSION)
    new = application_metadata("native", version=GOAL_SCHEMA_VERSION)
    assert set(new.tables) - set(old.tables) == {
        "native.moyai_goals", "native.moyai_goal_turns", "native.moyai_goal_receipts",
    }


async def test_command_retries_and_concurrent_continuation_are_idempotent(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    one, two = await asyncio.gather(*[
        command(runtime, actor, session.id, "/goal Finish the task.", key="same") for _ in range(2)
    ])
    assert one == two
    with pytest.raises(HTTPException, match="another goal command"):
        await command(runtime, actor, session.id, "/goal Different task.", key="same")
    await finish(runtime)
    await asyncio.gather(*[runtime.database.goals.tick(session.id) for _ in range(4)])
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2
        messages = list(await db.scalars(select(Message).order_by(Message.id)))
        generated = next(row for row in messages if row.identity.startswith("goal:"))
        assert generated.role == "system" and generated.author_id is None
    assert (await goal(runtime, session.id))["turns"] == 1


async def test_verified_completion_stops_after_saved_answer(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime, decision="complete", usage={"input_tokens": 20, "output_tokens": 10})
    await runtime.database.goals.tick(session.id)
    view = await goal(runtime, session.id)
    assert view["status"] == "complete" and view["tokens_used"] == 30
    assert await runtime.database.claim_next() is None


async def test_pending_answer_does_not_earn_continuation(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime, deferred=True)
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "active"
    assert await runtime.database.claim_next() is None


@pytest.mark.parametrize("control", ["pause", "clear", "stop", "delete"])
async def test_controls_fence_work_and_never_restart(runtime, actor, control):
    session, _ = await start(runtime, actor)
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    if control == "stop":
        await runtime.database.stop(actor, session.id)
    elif control == "delete":
        await runtime.database.delete_session(actor, session.id)
    else:
        await command(runtime, actor, session.id, f"/goal {control}")
    with pytest.raises((PermissionError, HTTPException)):
        await runtime.database.authorize_scope(prepared.scope)
    await runtime.database.stopped(turn.id)
    await runtime.database.goals.reconcile()
    assert await runtime.database.claim_next() is None
    view = await goal(runtime, session.id)
    assert view is None if control == "clear" else view["status"] == "paused"


async def test_edit_and_resume_ignore_old_completion_and_reset_blocker_audit(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime, decision="complete")
    await command(runtime, actor, session.id, "/goal edit Verify the new acceptance criterion.")
    await command(runtime, actor, session.id, "/goal resume")
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "active"
    for index in range(3):
        await finish(runtime, decision="blocked", evidence="The required input file is missing.")
        await runtime.database.goals.tick(session.id)
        view = await goal(runtime, session.id)
        assert view["blocked_count"] == index + 1
        assert view["status"] == ("blocked" if index == 2 else "active")
    await command(runtime, actor, session.id, "/goal resume")
    assert (await goal(runtime, session.id))["blocked_count"] == 0


async def test_budget_precedes_completion(runtime, actor):
    session, _ = await start(runtime, actor, "/goal --budget 30 Verify the task.")
    await finish(runtime, decision="complete", usage={"input_tokens": 20, "output_tokens": 10})
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "budget_limited"
    with pytest.raises(HTTPException, match="budget is exhausted"):
        await command(runtime, actor, session.id, "/goal resume")
    await command(runtime, actor, session.id, "/goal edit --budget 50 Verify the task.")
    await command(runtime, actor, session.id, "/goal resume")
    assert (await goal(runtime, session.id))["tokens_used"] == 30


async def test_revoked_requester_cannot_gain_new_authority_on_continuation(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == actor.id).values(auth_version=User.auth_version + 1))
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "blocked"
    assert await runtime.database.claim_next() is None
    fresh = await runtime.database.user(actor.id)
    with pytest.raises(HTTPException, match="requester access changed"):
        await command(runtime, fresh, session.id, "/goal resume")


async def test_user_followup_runs_before_next_generated_turn(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime)
    followup = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Also verify the output checksum.", request_key="followup",
    ))
    await runtime.database.goals.tick(session.id)
    assert (await runtime.database.claim_next()).id == followup.id
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2
    assert (await goal(runtime, session.id))["objective"] == "Finish the verified task."


async def test_followup_invalidates_completion_based_on_older_input(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime, decision="complete")
    followup = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Before finishing, also verify the checksum.", request_key="new-criterion",
    ))
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "active"
    assert (await runtime.database.claim_next()).id == followup.id


async def test_goal_can_be_queued_while_ordinary_work_is_running(runtime, actor, active):
    session, old_turn, _ = active
    result = await command(runtime, actor, session.id, "/goal Verify the complete result.")
    assert result["status"] == "queued"
    await runtime.database.goals.tick(session.id)
    assert await runtime.database.claim_next() is None
    assert (await runtime.database.session(actor, session.id)).active_turn == old_turn.id


async def test_paused_goal_keeps_question_available_on_resume(runtime, actor):
    session, _ = await start(runtime, actor)
    turn, _ = await finish(runtime, deferred=True)
    await command(runtime, actor, session.id, "/goal pause")
    assert (await runtime.database.session(actor, session.id)).status == "waiting"
    await command(runtime, actor, session.id, "/goal resume")
    await runtime.database.goals.tick(session.id)
    assert (await runtime.database.session(actor, session.id)).active_turn == turn.id
    assert await runtime.database.claim_next() is None


async def test_explicit_resume_can_recover_a_failed_execution(runtime, actor):
    session, initial = await start(runtime, actor)
    await runtime.database.claim_next()
    await runtime.database.prepare(initial)
    await runtime.database.fail(initial, "Temporary model outage.")
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "blocked"
    await command(runtime, actor, session.id, "/goal resume")
    await runtime.database.goals.tick(session.id)
    assert (await runtime.database.claim_next()).id != initial


async def test_budget_can_be_removed_explicitly(runtime, actor):
    session, _ = await start(runtime, actor, "/goal --budget 30 Verify the result.")
    await command(runtime, actor, session.id, "/goal edit --budget none Verify the result.")
    assert (await goal(runtime, session.id))["token_budget"] is None


async def test_already_queued_continuation_cannot_use_revoked_native_identity(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime)
    await runtime.database.goals.tick(session.id)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == actor.id).values(auth_version=User.auth_version + 1))
    queued = await runtime.database.claim_next()
    with pytest.raises(PermissionError, match="requester authority"):
        await runtime.database.prepare(queued.id)


async def test_clearing_completed_goal_leaves_later_ordinary_work_running(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime, decision="complete")
    await runtime.database.goals.tick(session.id)
    later = await runtime.database.submit(actor, session.id, NewMessage(prompt="Independent task.", request_key="later"))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(later.id)
    await command(runtime, actor, session.id, "/goal clear")
    await runtime.database.authorize_scope(prepared.scope)
    assert (await runtime.database.session(actor, session.id)).status == "running"


async def test_dispatch_inspects_only_idle_goal_sessions(runtime, actor, monkeypatch):
    session, _ = await start(runtime, actor)
    turn = await runtime.database.claim_next()
    await runtime.database.prepare(turn.id)
    tick = AsyncMock()
    monkeypatch.setattr(runtime.database.goals, "tick", tick)
    await runtime.database.goals.reconcile()
    tick.assert_not_awaited()
    await runtime.database.fail(turn.id, "A failure leaves an idle conversation for inspection.")
    await runtime.database.goals.reconcile()
    tick.assert_awaited_once_with(session.id)


async def mismatched_followup(runtime, actor, session_id, mismatch):
    selected_actor, options = actor, {}
    if mismatch == "actor":
        async with runtime.database.sessions.begin() as db:
            selected_actor = User(username="teammate", display_name="Teammate", password="unused")
            db.add(selected_actor)
    elif mismatch == "model":
        runtime.settings.model_catalog = "other"
        options["model"] = "other"
    else:
        async with runtime.database.sessions.begin() as db:
            connection = Connection(
                owner_id=actor.id, name="Other account", kind="github", shared=True,
                credential=runtime.security.seal("synthetic credential"),
            )
            db.add(connection)
            await db.flush()
            options["connections"] = [connection.id]
    return await runtime.database.submit(selected_actor, session_id, NewMessage(
        prompt="Queue separate follow-up work.", request_key=f"followup-{mismatch}", **options,
    ))


@pytest.mark.parametrize("phase", ["starting", "running"])
@pytest.mark.parametrize("mismatch", ["actor", "model", "accounts"])
async def test_context_mismatch_pauses_continuation_without_revoking_admitted_turn(runtime, actor, phase, mismatch):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    await command(runtime, actor, session.id, "/goal Verify the original task.")
    original = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(original.id) if phase == "running" else None
    await mismatched_followup(runtime, actor, session.id, mismatch)
    assert (await goal(runtime, session.id))["status"] == "paused"
    prepared = prepared or await runtime.database.prepare(original.id)
    await runtime.database.authorize_scope(prepared.scope)
    tools = GoalCapability(runtime.database.goals).tools.tools
    ctx = RunContext(
        deps=prepared.scope, model=TestModel(), usage=RunUsage(),
        run_id=original.id, tool_call_id="goal-read",
    )
    assert (await tools["get_goal"].function(ctx))["goal"]["status"] == "paused"
    ctx.tool_call_id = "goal-finish"
    report = await tools["update_goal"].function(ctx, "complete", "The original task was verified.")
    assert report["pending_status"] == "complete"
    assert await runtime.database.save_result(
        prepared.scope, conversation='{"messages":[]}', output="Original work finished.",
        workspace=None, usage={},
    )
    await runtime.database.settle(original.id)
    await runtime.database.goals.reconcile()
    assert (await goal(runtime, session.id))["status"] == "paused"
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, original.id)).status == "completed"
        assert await db.scalar(select(func.count()).select_from(Turn)) == 2
        assert await db.scalar(select(func.count()).select_from(GoalTurn).where(GoalTurn.generated.is_(True))) == 0


async def test_resume_preserves_live_turn_generation_but_explicit_pause_revokes_it(runtime, actor):
    session, _ = await start(runtime, actor)
    original = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(original.id)
    followup = await mismatched_followup(runtime, actor, session.id, "model")
    resumed = await command(runtime, actor, session.id, "/goal resume")
    assert resumed["goal"]["generation"] == prepared.scope.goal_generation
    await runtime.database.authorize_scope(prepared.scope)
    await command(runtime, actor, session.id, "/goal pause")
    with pytest.raises(PermissionError):
        await runtime.database.authorize_scope(prepared.scope)
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, followup.id)).status == "queued"


async def test_context_hold_cancels_queued_automatic_work_and_resume_does_not_replay_it(runtime, actor):
    session, _ = await start(runtime, actor)
    await finish(runtime)
    await runtime.database.goals.tick(session.id)
    async with runtime.database.sessions() as db:
        automatic = await db.scalar(select(Turn).where(Turn.status == "queued"))
    manual = await mismatched_followup(runtime, actor, session.id, "model")
    async with runtime.database.sessions() as db:
        assert (await db.get(Turn, automatic.id)).status == "stopped"
        assert (await db.get(GoalTurn, automatic.id)).settled
    await command(runtime, actor, session.id, "/goal resume")
    next_turn, _ = await finish(runtime)
    assert next_turn.id == manual.id
    await runtime.database.goals.tick(session.id)
    next_turn = await runtime.database.claim_next()
    assert next_turn.id not in {automatic.id, manual.id}
    assert (await goal(runtime, session.id))["status"] == "active"


async def test_context_hold_does_not_bypass_revoked_requester_authority(runtime, actor):
    session, _ = await start(runtime, actor)
    original = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(original.id)
    await mismatched_followup(runtime, actor, session.id, "model")
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == actor.id).values(auth_version=User.auth_version + 1))
    with pytest.raises(PermissionError, match="requester authority"):
        await runtime.database.authorize_scope(prepared.scope)


async def test_new_goal_retains_authorized_attachments_and_retries_atomically(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    digest = await runtime.objects.put(b"goal input")
    async with runtime.database.sessions.begin() as db:
        upload = Upload(owner_id=actor.id, name="input.txt", media_type="text/plain", size=10, digest=digest)
        db.add(upload)
    first = await command(runtime, actor, session.id, "/goal Read the input.", key="files", attachments=[upload.id])
    assert await command(runtime, actor, session.id, "/goal Read the input.", key="files", attachments=[upload.id]) == first
    async with runtime.database.sessions() as db:
        assert (await db.get(Upload, upload.id)).session_id == session.id
        assert await db.get(TurnImage, (first["id"], upload.id)) is not None
        assert await db.scalar(select(func.count()).select_from(Turn)) == 1
    with pytest.raises(HTTPException, match="Attach files when starting"):
        await command(runtime, actor, session.id, "/goal pause", attachments=[upload.id])
    assert (await goal(runtime, session.id))["status"] == "active"


async def test_goal_cannot_bind_someone_elses_upload(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    async with runtime.database.sessions.begin() as db:
        other = User(username="other-upload-owner", display_name="Other", password="unused")
        db.add(other)
        await db.flush()
        upload = Upload(owner_id=other.id, name="private.txt", media_type="text/plain", size=1, digest="a" * 64)
        db.add(upload)
    with pytest.raises(HTTPException):
        await command(runtime, actor, session.id, "/goal Read the input.", attachments=[upload.id])
    async with runtime.database.sessions() as db:
        assert await db.get(Goal, session.id) is None
        assert await db.scalar(select(func.count()).select_from(Turn)) == 0
        assert (await db.get(Upload, upload.id)).session_id is None


async def test_goal_tools_are_builtin_and_complete_via_native_agent(runtime, actor):
    session, _ = await start(runtime, actor)
    turn = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    offered = set()
    calls = 0

    def respond(messages, info):
        nonlocal calls
        offered.update(tool.name for tool in info.function_tools)
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("update_goal", {"status": "complete", "evidence": "Verified result."}, "goal-finish")])
        return ModelResponse(parts=[TextPart("Verified result.")])

    async def stream(messages, info):
        for part in respond(messages, info).parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}

    result = await runtime.agent.run(
        "Finish the verified task.", deps=prepared.scope, run_id=turn.id,
        model=FunctionModel(respond, stream_function=stream),
    )
    assert {"get_goal", "update_goal"} <= offered
    assert (await goal(runtime, session.id))["status"] == "active"
    await runtime.database.save_result(
        prepared.scope, conversation=json.dumps({"messages": []}), output=str(result.output),
        workspace=None, usage={},
    )
    await runtime.database.settle(turn.id)
    await runtime.database.goals.tick(session.id)
    assert (await goal(runtime, session.id))["status"] == "complete"


async def test_slash_command_routes_before_skills_and_is_owner_only(runtime, actor):
    app = create_app(runtime, run_dispatcher=False)
    token, csrf = runtime.security.issue_session(actor.id, actor.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        cookies={COOKIE: token}, headers={"x-moyai-csrf": csrf, "origin": "http://test"},
    ) as client:
        session = (await client.post("/api/sessions", json={"visibility": "shared"})).json()
        sent = await client.post(f"/api/sessions/{session['id']}/messages", json={
            "prompt": "/goal Complete the native task.", "request_key": "http-goal",
        })
        assert sent.status_code == 202, sent.text
        detail = (await client.get(f"/api/sessions/{session['id']}")).json()
        assert detail["goal"]["status"] == "active"
    async with runtime.database.sessions.begin() as db:
        viewer = User(username="other", display_name="Other", password="unused")
        db.add(viewer)
    with pytest.raises(HTTPException, match="Only the conversation owner"):
        await command(runtime, viewer, session["id"], "/goal pause")
