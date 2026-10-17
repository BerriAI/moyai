from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlalchemy import func, select, update

from moyai.contracts import NewMessage, NewSession
from moyai.models import Connection, Message, Session, Turn, TurnImage, Upload, User
from moyai.swarm_launch import launch
from moyai.swarm_models import SwarmMember, SwarmMission, SwarmOperation, SwarmTurn


def streaming_model(respond):
    async def stream(messages, info):
        for index, part in enumerate(respond(messages, info).parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {index: DeltaToolCall(
                    name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id,
                )}
    return FunctionModel(respond, stream_function=stream)


async def start(runtime, active, **overrides):
    _, turn, prepared = active
    arguments = {
        "goal": "Propose a three-item developer onboarding checklist.",
        "workers": 20, "budget_seconds": 7200, "input_id": turn.id,
    }
    arguments.update(overrides)
    return await launch(
        runtime.database.swarms, prepared.scope, turn.id, "launch-call", **arguments,
    )


async def test_requested_team_is_atomic_idempotent_and_inherits_chat_context(runtime, active):
    parent, _, prepared = active
    first, repeated = await asyncio.gather(start(runtime, active), start(runtime, active))
    assert first == repeated
    assert first["workers"] == 20 and first["budget_seconds"] == 7200
    assert first["model"] == prepared.scope.model and first["visibility"] == parent.visibility
    assert first["conversation_path"] == f"/#{first['session_id']}"
    async with runtime.database.sessions() as db:
        mission = await db.get(SwarmMission, first["swarm_id"])
        members = list(await db.scalars(select(SwarmMember)))
        assert len(members) == 21 and len({member.session_id for member in members}) == 21
        assert all(member.model == prepared.scope.model for member in members)
        assert mission.budget_seconds == 7200 and mission.ends_at is not None
        assert await db.scalar(select(func.count()).select_from(SwarmOperation)) == 1
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 1
        assert await db.scalar(select(func.count()).select_from(Turn)) == 22
        root_input = await db.scalar(select(Message).where(Message.turn_id == mission.source_turn_id))
        binding = await db.get(SwarmTurn, mission.source_turn_id)
        assert root_input.role == "system" and root_input.author_id is None and binding.generated
        prompt = runtime.security.unseal(mission.goal).decode()
        assert "Perform the task." in prompt and "not new human authorization" in prompt
    with pytest.raises(ToolFailed, match="different swarm"):
        await start(runtime, active, workers=2)


@pytest.mark.parametrize("arguments", [
    {"workers": 1}, {"workers": True}, {"budget_seconds": 59},
    {"budget_seconds": 2_592_001}, {"budget_seconds": True}, {"goal": "  "},
])
async def test_invalid_launch_creates_nothing(runtime, active, arguments):
    with pytest.raises(ToolFailed):
        await start(runtime, active, **arguments)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        assert await db.scalar(select(func.count()).select_from(SwarmOperation)) == 0


@pytest.mark.parametrize("change", ["stopped", "disabled", "steered", "nonweb"])
async def test_launch_rechecks_source_authority(runtime, active, actor, change):
    parent, _turn, _ = active
    if change == "steered":
        await runtime.database.submit(actor, parent.id, NewMessage(
            prompt="Do not start the swarm.", request_key="new-guidance", mode="asap",
        ))
    else:
        async with runtime.database.sessions.begin() as db:
            if change == "stopped":
                await db.execute(update(Session).where(Session.id == parent.id).values(status="stopped"))
            elif change == "disabled":
                await db.execute(update(User).where(User.id == actor.id).values(enabled=False))
            else:
                await db.execute(update(Session).where(Session.id == parent.id).values(surface="automation"))
    with pytest.raises(ToolFailed):
        await start(runtime, active)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 0


@pytest.mark.parametrize("changes", [
    {"swarm_id": "existing-mission"}, {"delegation_depth": 1}, {"surface": "slack"},
])
async def test_workers_and_nonweb_contexts_cannot_launch(runtime, active, changes):
    parent, turn, prepared = active
    with pytest.raises(ToolFailed, match="direct web chat"):
        await start(runtime, (parent, turn, replace(prepared, scope=replace(prepared.scope, **changes))))
    with pytest.raises(ToolFailed, match="direct web chat"):
        await launch(
            runtime.database.swarms, prepared.scope, "nested-run", "call",
            goal="Nested team", workers=2, budget_seconds=60, input_id=turn.id,
        )


async def test_attachment_copy_and_source_guidance_are_committed_with_team(runtime, active, actor):
    parent, turn, _ = active
    async with runtime.database.sessions.begin() as db:
        upload = Upload(
            owner_id=actor.id, session_id=parent.id, name="brief.txt",
            media_type="text/plain", digest="test-brief", size=12,
        )
        db.add(upload)
        await db.flush()
        db.add(TurnImage(turn_id=turn.id, upload_id=upload.id, position=0))
        original_id = upload.id
    guidance = await runtime.database.submit(actor, parent.id, NewMessage(
        prompt="Use only the attached brief; no external actions.", request_key="clarified", mode="asap",
    ))
    result = await start(runtime, active, workers=2, input_id=guidance.id)
    assert await start(runtime, active, workers=2, input_id=guidance.id) == result
    async with runtime.database.sessions() as db:
        mission = await db.get(SwarmMission, result["swarm_id"])
        assert "no external actions" in runtime.security.unseal(mission.goal).decode()
        copies = list(await db.scalars(select(Upload).where(Upload.id != original_id)))
        assert len(copies) == 3 and len({copy.session_id for copy in copies}) == 3
        assert all(copy.digest == "test-brief" for copy in copies)
        assert (await db.get(Upload, original_id)).session_id == parent.id


@pytest.mark.parametrize("visibility", ["private", "shared"])
async def test_selected_model_and_accounts_are_inherited_and_revocation_blocks_launch(runtime, actor, visibility):
    database = runtime.database
    runtime.settings.model_catalog = "selected-model"
    async with database.sessions.begin() as db:
        for identity in ("selected", "unselected"):
            db.add(Connection(
                id=identity, owner_id=actor.id, kind="linear", name=identity,
                shared=visibility == "shared", credential=database.security.seal("synthetic"),
            ))
    parent = await database.create_session(actor, NewSession(visibility=visibility, model="selected-model"))
    turn = await database.submit(actor, parent.id, NewMessage(
        prompt="Start a team using the selected account.", request_key="selected-account", connections=["selected"],
    ))
    await database.claim_next()
    prepared = await database.prepare(turn.id)
    result = await start(runtime, (parent, turn, prepared), workers=2)
    async with database.sessions() as db:
        mission = await db.get(SwarmMission, result["swarm_id"])
        members = list(await db.scalars(select(SwarmMember).where(SwarmMember.mission_id == mission.id)))
        assert json.loads(mission.connections) == ["selected"]
        assert mission.grants_key == prepared.scope.grants_key
        assert all(member.model == "selected-model" for member in members)
        assert set(await db.scalars(select(Session.visibility).where(
            Session.id.in_([member.session_id for member in members]),
        ))) == {visibility}
    async with database.sessions.begin() as db:
        await db.execute(update(Connection).where(Connection.id == "selected").values(revision=2))
    with pytest.raises(ToolFailed, match="Connection access changed"):
        await start(runtime, (parent, turn, prepared), workers=2)


async def test_partial_creation_rolls_back_with_receipt(runtime, active, monkeypatch):
    original = runtime.database.swarms.post

    async def fail_after_post(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("Admission interrupted")

    monkeypatch.setattr(runtime.database.swarms, "post", fail_after_post)
    with pytest.raises(RuntimeError, match="Admission interrupted"):
        await start(runtime, active)
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        assert await db.scalar(select(func.count()).select_from(SwarmMission)) == 0
        assert await db.scalar(select(func.count()).select_from(SwarmOperation)) == 0
    monkeypatch.setattr(runtime.database.swarms, "post", original)
    assert (await start(runtime, active))["workers"] == 20


async def test_registered_tool_launches_from_chat_and_is_absent_inside_swarm(runtime, active):
    _, turn, prepared = active
    observed = {}

    def model(messages, info):
        observed["tools"] = {tool.name for tool in info.function_tools}
        results = [part for message in messages for part in message.parts
                   if isinstance(part, ToolReturnPart) and part.tool_name == "start_swarm"]
        if results:
            observed["launch"] = results[-1].content
            return ModelResponse(parts=[TextPart("Team launched.")])
        return ModelResponse(parts=[ToolCallPart(
            "start_swarm", {"goal": "Check onboarding suggestions.", "workers": 2, "budget_seconds": 7200},
            tool_call_id="native-launch",
        )])

    result = await runtime.agent.run(
        "Start a two-worker swarm for two hours.", deps=prepared.scope, run_id=turn.id,
        model=streaming_model(model),
    )
    assert result.output == "Team launched." and "start_swarm" in observed["tools"]
    created = observed["launch"]
    if isinstance(created, str):
        created = json.loads(created)
    assert created["workers"] == 2 and created["budget_seconds"] == 7200
    turns = await runtime.database.claim_batch(10)
    coordinator = next(member for member in turns if member.session_id == created["session_id"])
    member_scope = (await runtime.database.prepare(coordinator.id)).scope

    def inspect(_messages, info):
        names = {tool.name for tool in info.function_tools}
        assert "start_swarm" not in names and "swarm_board" in names
        return ModelResponse(parts=[TextPart("Existing team.")])

    await runtime.agent.run(
        "Inspect tools.", deps=member_scope, run_id=coordinator.id, model=streaming_model(inspect),
    )
