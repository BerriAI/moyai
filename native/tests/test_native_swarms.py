from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import func, select, update

from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage
from moyai.models import Message, Session, Turn, User, new_id
from moyai.swarm_contracts import MissionRun, NewSwarm, SwarmControl
from moyai.swarm_models import SwarmMember, SwarmMission, SwarmOperation, SwarmPost, SwarmTurn
from moyai.swarm_runtime import tick
from moyai.swarm_tools import operate


async def start(runtime, actor, *, key="one", workers=2, **kwargs):
    return await runtime.database.swarms.start(actor, NewSwarm(
        prompt="Investigate the test dataset and share evidence.", request_key=key, workers=workers, **kwargs,
    ))


async def prepared_team(runtime, actor, **kwargs):
    root, initial = await start(runtime, actor, **kwargs)
    turns = await runtime.database.claim_batch(30)
    prepared = {turn.session_id: (turn, await runtime.database.prepare(turn.id)) for turn in turns}
    view = await runtime.database.swarms.view(actor, root.id)
    workers = [member for member in view["members"] if not member["coordinator"]]
    return root, initial, prepared, workers


async def call(runtime, prepared, action, args, identity="call-1"):
    scope = prepared.scope
    return await operate(runtime.database.swarms, scope, scope.turn_id, identity, action, args)


async def test_atomic_creation_and_retried_start_recover_same_team(runtime, actor):
    one, two = await asyncio.gather(start(runtime, actor), start(runtime, actor))
    assert one[0].id == two[0].id
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 1
        assert await db.scalar(select(func.count()).select_from(SwarmMember)) == 3
        assert await db.scalar(select(func.count()).select_from(Turn)) == 3
        provenance = list(await db.scalars(select(SwarmTurn)))
        assert sum(item.generated for item in provenance) == 2
        human = list(await db.scalars(select(Message).where(Message.role == "user")))
        assert len(human) == 1 and human[0].turn_id == one[1].id
    listed = await runtime.database.list_sessions(actor)
    assert [session.id for session in listed] == [one[0].id]
    with pytest.raises(HTTPException, match="different swarm"):
        await start(runtime, actor, workers=4)


async def test_invalid_model_rolls_back_entire_creation(runtime, actor):
    with pytest.raises(HTTPException):
        await start(runtime, actor, worker_models=["unavailable"])
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 0
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 0


async def test_twenty_workers_have_distinct_persistent_sessions(runtime, actor):
    root, _ = await start(runtime, actor, workers=20)
    view = await runtime.database.swarms.view(actor, root.id)
    assert len(view["members"]) == 21
    assert len({member["session_id"] for member in view["members"]}) == 21
    assert all(member["model"] == "test" for member in view["members"])


async def test_first_leader_claim_is_atomic_and_native_operations_are_idempotent(runtime, actor):
    root, _, prepared, workers = await prepared_team(runtime, actor)
    results = await asyncio.gather(*(
        call(runtime, prepared[worker["session_id"]][1], "claim", {"plan": worker["name"]})
        for worker in workers
    ))
    assert sum(item["claimed"] for item in results) == 1
    leader = next(worker for worker in workers if worker["session_id"] == results[0]["leader_id"])
    context = prepared[leader["session_id"]][1]
    first = await call(runtime, context, "publish", {"message": "Verified finding."}, "publish-1")
    again = await call(runtime, context, "publish", {"message": "Verified finding."}, "publish-1")
    assert first == again
    with pytest.raises(ToolFailed, match="different operation"):
        await call(runtime, context, "publish", {"message": "Changed finding."}, "publish-1")
    view = await runtime.database.swarms.view(actor, root.id)
    assert len([post for post in view["posts"] if post["kind"] == "lead"]) == 1
    assert len([post for post in view["posts"] if post["kind"] == "finding"]) == 1


async def test_assignment_reuses_worker_session_and_cannot_escape_mission(runtime, actor):
    _root, _, prepared, workers = await prepared_team(runtime, actor)
    leader = prepared[workers[0]["session_id"]][1]
    await call(runtime, leader, "claim", {"plan": "Divide investigation."})
    arguments = {"member_id": workers[1]["session_id"], "task": "Check boundary conditions."}
    first = await call(runtime, leader, "assign", arguments, "assign-1")
    assert await call(runtime, leader, "assign", arguments, "assign-1") == first
    with pytest.raises(ToolFailed, match="queued work"):
        await call(runtime, leader, "assign", arguments, "assign-2")
    other, _ = await start(runtime, actor, key="other")
    other_view = await runtime.database.swarms.view(actor, other.id)
    with pytest.raises(ToolFailed, match="actual roster"):
        await call(runtime, leader, "assign", {
            "member_id": other_view["members"][1]["session_id"], "task": "Cross-mission request.",
        }, "assign-3")
    async with runtime.database.sessions() as db:
        turn = await db.get(Turn, first["turn_id"])
        assert turn.session_id == workers[1]["session_id"]
        binding = await db.get(SwarmTurn, turn.id)
        message = await db.scalar(select(Message).where(Message.turn_id == turn.id))
        assert binding.generated and message.role == "system" and message.author_id is None


async def test_nested_delegate_cannot_impersonate_member(runtime, actor):
    _, _, prepared, workers = await prepared_team(runtime, actor)
    scope = prepared[workers[0]["session_id"]][1].scope
    with pytest.raises(ToolFailed, match="current admitted"):
        await operate(runtime.database.swarms, scope, "nested-run", "call", "claim", {"plan": "Impersonate."})
    with pytest.raises(PermissionError):
        await runtime.database.authorize_scope(replace(scope, swarm_id=new_id()))


async def test_pause_fences_every_member_and_resume_preserves_roster(runtime, actor):
    root, _, prepared, workers = await prepared_team(runtime, actor)
    before = await runtime.database.swarms.view(actor, root.id)
    await runtime.database.swarms.control(actor, root.id, SwarmControl(action="pause", revision=before["revision"]))
    for _, value in prepared.values():
        with pytest.raises(PermissionError):
            await runtime.database.authorize_scope(value.scope)
    assert await runtime.database.claim_batch(20) == []
    with pytest.raises(HTTPException, match="finish stopping"):
        await runtime.database.swarms.control(actor, root.id, SwarmControl(action="resume", revision=1))
    for turn, _ in prepared.values():
        await runtime.database.stopped(turn.id)
    await runtime.database.swarms.control(actor, root.id, SwarmControl(action="resume", revision=1))
    after = await runtime.database.swarms.view(actor, root.id)
    assert after["generation"] == 2
    assert [member["session_id"] for member in after["members"]] == [member["session_id"] for member in before["members"]]
    assert len(await runtime.database.claim_batch(20)) == 3
    old = prepared[workers[0]["session_id"]][1]
    with pytest.raises(ToolFailed):
        await call(runtime, old, "publish", {"message": "Late old-generation write."})


async def test_deadline_is_checked_at_dispatch_and_activity_boundaries(runtime, actor):
    root, _, prepared, _ = await prepared_team(runtime, actor, budget_seconds=60)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(SwarmMission).where(SwarmMission.id == root.id).values(
            ends_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        ))
    for _, value in prepared.values():
        with pytest.raises(PermissionError, match="expired"):
            await runtime.database.authorize_scope(value.scope)
    state = await tick(runtime.database.swarms, MissionRun(root.id, 1))
    assert not state.exists
    view = await runtime.database.swarms.view(actor, root.id)
    assert view["status"] == "paused"
    assert await runtime.database.claim_batch(20) == []


async def test_shared_viewer_cannot_direct_team_or_inspect_private_mission(runtime, actor):
    root, _ = await start(runtime, actor, visibility="shared")
    async with runtime.database.sessions.begin() as db:
        viewer = User(id=new_id(), username="viewer", display_name="Viewer", password="", role="member")
        db.add(viewer)
    view = await runtime.database.swarms.view(viewer, root.id)
    assert view is not None and not view["can_control"]
    with pytest.raises(HTTPException, match="Only the swarm owner"):
        await runtime.database.swarms.control(viewer, root.id, SwarmControl(action="stop", revision=0))
    with pytest.raises(HTTPException, match="Only the swarm owner"):
        await runtime.database.submit(viewer, root.id, NewMessage(prompt="New goal.", request_key="unauthorized"))
    private, _ = await start(runtime, actor, key="private")
    with pytest.raises(HTTPException):
        await runtime.database.swarms.view(viewer, private.id)


async def test_revoked_requester_cannot_be_refreshed_by_generated_wake(runtime, actor):
    root, _ = await start(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == actor.id).values(auth_version=User.auth_version + 1))
    assert not (await tick(runtime.database.swarms, MissionRun(root.id, 1))).exists
    fresh = await runtime.database.user(actor.id)
    view = await runtime.database.swarms.view(fresh, root.id)
    assert view["status"] == "attention"
    with pytest.raises(HTTPException, match="original requester"):
        await runtime.database.swarms.control(fresh, root.id, SwarmControl(action="resume", revision=view["revision"]))


async def test_normal_stop_and_delete_fence_team(runtime, actor):
    root, _ = await start(runtime, actor)
    view = await runtime.database.swarms.view(actor, root.id)
    await runtime.database.stop(actor, view["members"][1]["session_id"])
    assert (await runtime.database.swarms.view(actor, root.id))["status"] == "stopped"
    assert await runtime.database.claim_batch(20) == []
    await runtime.database.delete_session(actor, root.id)
    with pytest.raises(HTTPException):
        await runtime.database.session(actor, view["members"][1]["session_id"])


async def test_public_result_handoff_is_saved_once_and_coalesced(runtime, actor):
    root, _, prepared, workers = await prepared_team(runtime, actor)
    for session_id, (turn, value) in prepared.items():
        await runtime.database.save_result(
            value.scope, conversation='{"messages":[]}', output=f"Observed result from {session_id}",
            workspace=None, usage={}, checkpoint_workspace=False,
        )
        await runtime.database.settle(turn.id)
    await tick(runtime.database.swarms, MissionRun(root.id, 1))
    await tick(runtime.database.swarms, MissionRun(root.id, 1))
    async with runtime.database.sessions() as db:
        posts = list(await db.scalars(select(SwarmPost).where(SwarmPost.kind == "result")))
        assert len(posts) == len(workers)
        summaries = list(await db.scalars(select(Turn).where(Turn.session_id == root.id, Turn.status == "queued")))
        assert len(summaries) == 1
        assert (await db.get(SwarmTurn, summaries[0].id)).generated
        assert await db.scalar(select(func.count()).select_from(SwarmOperation)) == 0


async def test_pause_resume_preserves_pending_question_until_owner_answers(runtime, actor):
    root, _, prepared, workers = await prepared_team(runtime, actor)
    waiting_turn, waiting = prepared[workers[0]["session_id"]]
    await runtime.database.save_result(
        waiting.scope, conversation='{"messages":[]}', output="", workspace=None, usage={}, deferred=True,
    )
    await runtime.database.swarms.control(actor, root.id, SwarmControl(action="pause", revision=0))
    for turn, _ in prepared.values():
        if turn.id != waiting_turn.id:
            await runtime.database.stopped(turn.id)
    with pytest.raises(HTTPException) as refusal:
        await runtime.database.resume(actor, waiting_turn.session_id, waiting_turn.id, "answer", {})
    assert refusal.value.status_code == 409
    await runtime.database.swarms.control(actor, root.id, SwarmControl(action="resume", revision=1))
    async with runtime.database.sessions() as db:
        session = await db.get(Session, waiting_turn.session_id)
        assert session.status == "waiting" and session.active_turn == waiting_turn.id
        assert await db.scalar(select(func.count()).select_from(Turn).where(
            Turn.session_id == waiting_turn.session_id,
        )) == 1
    answered = await runtime.database.resume(actor, waiting_turn.session_id, waiting_turn.id, "answer", {})
    async with runtime.database.sessions() as db:
        binding = await db.get(SwarmTurn, answered.id)
        assert binding.generation == 2 and answered.deferred_results is not None


async def test_authenticated_api_and_sidebar_keep_workers_inside_the_mission(runtime, actor):
    token, csrf = runtime.security.issue_session(actor.id, actor.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver",
    ) as client:
        client.cookies.set(COOKIE, token)
        body = {"prompt": "Verify one shared goal.", "workers": 2, "request_key": "api-team"}
        assert (await client.post("/api/swarms", json=body)).status_code == 403
        client.headers["x-moyai-csrf"] = csrf
        response = await client.post("/api/swarms", json=body)
        assert response.status_code == 200
        root_id = response.json()["session"]["id"]
        assert response.json()["session"]["swarm_id"] == root_id
        repeated = await client.post("/api/swarms", json=body)
        assert repeated.json()["session"]["id"] == root_id
        listed = await client.get("/api/history/sessions?scope=mine")
        assert [item["id"] for item in listed.json()["sessions"]] == [root_id]
        board = (await client.get(f"/api/sessions/{root_id}/swarm")).json()
        child = board["members"][1]["session_id"]
        detail = await client.get(f"/api/sessions/{child}")
        assert detail.status_code == 200 and detail.json()["session"]["swarm_id"] == root_id


async def test_queued_human_edits_and_cancellations_are_not_broadcast_before_admission(runtime, actor):
    root, initial, prepared, _ = await prepared_team(runtime, actor)
    queued = await runtime.database.submit(actor, root.id, NewMessage(prompt="Old direction.", request_key="edit"))
    cancelled = await runtime.database.submit(actor, root.id, NewMessage(prompt="Never act on this.", request_key="cancel"))
    token, csrf = runtime.security.issue_session(actor.id, actor.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver", headers={"x-moyai-csrf": csrf},
    ) as client:
        client.cookies.set(COOKIE, token)
        assert (await client.patch(f"/api/turns/{initial.id}", json={"prompt": "Change the accepted goal."})).status_code == 409
        assert (await client.patch(f"/api/turns/{queued.id}", json={"prompt": "Corrected direction."})).status_code == 200
        assert (await client.delete(f"/api/turns/{cancelled.id}")).status_code == 200
    await tick(runtime.database.swarms, MissionRun(root.id, 1))
    before = await runtime.database.swarms.view(actor, root.id)
    assert not any(post["kind"] == "human" for post in before["posts"])
    for turn, value in prepared.values():
        await runtime.database.save_result(
            value.scope, conversation='{"messages":[]}', output="Saved initial result.", workspace=None, usage={},
        )
        await runtime.database.settle(turn.id)
    claimed = await runtime.database.claim_batch(3)
    assert [turn.id for turn in claimed] == [queued.id]
    await runtime.database.prepare(queued.id)
    await tick(runtime.database.swarms, MissionRun(root.id, 1))
    after = await runtime.database.swarms.view(actor, root.id)
    guidance = [post for post in after["posts"] if post["kind"] == "human"]
    assert len(guidance) == 1 and guidance[0]["content"].endswith("Corrected direction.")
    assert "Never act on this." not in str(after)


async def test_worker_models_are_pinned_and_balanced_on_creation(runtime, actor):
    runtime.settings.model_catalog = "test,second-enabled-model"
    root, _ = await start(runtime, actor, workers=4, worker_models=["test", "second-enabled-model"])
    first = await runtime.database.swarms.view(actor, root.id)
    repeated, _ = await start(runtime, actor, workers=4, worker_models=["test", "second-enabled-model"])
    assert repeated.id == root.id
    assert [member["model"] for member in first["members"][1:]] == [
        "test", "second-enabled-model", "test", "second-enabled-model",
    ]


async def test_ordinary_chat_retains_native_delegation_without_swarm_tools(runtime, active):
    _, turn, prepared = active
    offered = set()

    def inspect_tools(messages, info):
        offered.update(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart("Ordinary chat.")])

    async def stream(messages, info):
        inspect_tools(messages, info)
        yield "Ordinary chat."

    result = await runtime.agent.run(
        "Inspect the ordinary tool catalog.", deps=prepared.scope, run_id=turn.id,
        model=FunctionModel(inspect_tools, stream_function=stream),
    )
    assert result.output == "Ordinary chat."
    assert "delegate_task" in offered
    assert not any(name.startswith("swarm_") for name in offered)
