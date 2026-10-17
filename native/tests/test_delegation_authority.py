"""Private child presentation must not expand the originating execution context."""

import json

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import select
from test_delegated_sessions import finish
from test_workspace_overview import sign_in

from moyai.contracts import Grant, GuidanceChanged, NewMessage, NewSession
from moyai.delegation import DelegationService
from moyai.delegation_models import DelegatedSession
from moyai.models import Connection, Session, Turn
from moyai.settings_models import SettingSkill
from moyai.settings_runtime import SettingsSkills
from moyai.workspace_recovery import locked_scope


async def parent_context(runtime, actor, *, visibility="shared", surface="web", connections=None):
    database = runtime.database
    parent = await database.create_session(actor, NewSession(visibility=visibility))
    async with database.sessions.begin() as db:
        (await db.get(Session, parent.id)).surface = surface
    turn = await database.submit(actor, parent.id, NewMessage(
        prompt="Delegate bounded work.", request_key="parent", connections=connections or [],
    ))
    await database.claim_next()
    return parent, turn, await database.prepare(turn.id)


@pytest.mark.parametrize(("visibility", "surface"), [
    ("shared", "web"), ("shared", "slack"), ("private", "web"),
])
async def test_child_keeps_skill_and_memory_context_through_grandchildren_and_human_followups(
    runtime, actor, visibility, surface,
):
    database = runtime.database
    async with database.sessions.begin() as db:
        db.add_all([
            SettingSkill(id="personal", owner_id=actor.id, scope_key=actor.id, name="personal-only", description="Private skill",
                         content=database.security.seal("Synthetic personal instructions")),
            SettingSkill(id="shared", owner_id=actor.id, scope_key="shared", name="shared-only", description="Shared skill",
                         content=database.security.seal("Synthetic shared instructions")),
        ])
    parent, _, original = await parent_context(runtime, actor, visibility=visibility, surface=surface)
    expected = await SettingsSkills(database).catalog(original.scope)
    child = await DelegationService(database).start(original.scope, "child", "Bounded assignment")
    first = await database.claim_next()
    prepared = await database.prepare(first.id)
    assert prepared.scope.visibility == "private" and prepared.scope.surface == "web"
    assert await SettingsSkills(database).catalog(prepared.scope) == expected
    assert prepared.scope.memory_namespace.split("/")[0] == ("slack" if surface == "slack" else visibility)
    async with sign_in(runtime) as client:
        skills = (await client.get("/api/skills", params={"session_id": child["session_id"]})).json()["skills"]
        assert not any(item["reference"] == "personal:personal-only" for item in skills) or visibility == "private"
        if surface == "slack":
            assert skills == []
    grandchild = await DelegationService(database).start(prepared.scope, "grandchild", "Nested assignment")
    nested = await database.prepare((await database.claim_next()).id)
    assert nested.scope.session_id == grandchild["session_id"]
    assert await SettingsSkills(database).catalog(nested.scope) == expected
    await finish(runtime, nested, "Nested result")

    # A saved child computer remains usable across a direct follow-up.
    reference = {"provider": "local", "id": "child-computer"}
    await database.save_workspace_reference(prepared.scope, reference)
    async with database.sessions.begin() as db:
        await locked_scope(database, db, prepared.scope, status="running")
    steer = await database.submit(actor, child["session_id"], NewMessage(
        prompt="Correct the assignment.", request_key="steer", mode="asap",
    ))
    assert steer.status == "steering"
    await database.steering_receipt(steer.id, "enqueue")
    await finish(runtime, prepared, "Bounded result")
    follow = await database.submit(actor, child["session_id"], NewMessage(
        prompt="Explain your result.", request_key="follow",
    ))
    await database.claim_next()
    direct = await database.prepare(follow.id)
    assert not direct.scope.delegation_generated
    assert direct.workspace == reference and direct.scope.workspace_key == prepared.scope.workspace_key
    assert await SettingsSkills(database).catalog(direct.scope) == expected
    if visibility == "shared":
        with pytest.raises(HTTPException):
            await runtime.skill_invocations.expand("/personal:personal-only do the work", direct.scope)
    assert (await database.session(actor, parent.id)).active_turn == original.scope.turn_id


async def test_legacy_grant_payload_recovers_original_preparation_not_live_parent(runtime, actor):
    database = runtime.database
    parent, _, original = await parent_context(runtime, actor)
    child = await DelegationService(database).start(original.scope, "child", "Shared work")
    async with database.sessions.begin() as db:
        link = await db.get(DelegatedSession, child["session_id"])
        link.grants = database.security.seal(TypeAdapter(list[Grant]).dump_json(original.scope.grants))
        (await db.get(Session, parent.id)).visibility = "private"
        db.add(SettingSkill(id="private", owner_id=actor.id, scope_key=actor.id, name="private", description="Private",
                            content=database.security.seal("Private instructions")))
    prepared = await database.prepare((await database.claim_next()).id)
    assert await SettingsSkills(database).catalog(prepared.scope) == []
    assert prepared.scope.memory_namespace.startswith("shared/")
    grandchild = await DelegationService(database).start(prepared.scope, "nested", "Still shared work")
    async with database.sessions.begin() as db:
        link = await db.get(DelegatedSession, grandchild["session_id"])
        link.grants = database.security.seal(TypeAdapter(list[Grant]).dump_json(prepared.scope.grants))
        # Simulate a persisted preparation from before execution_context existed.
        initial = await db.get(Turn, prepared.scope.turn_id)
        payload = json.loads(database.security.unseal(initial.preparation))
        payload["scope"].pop("execution_context", None)
        initial.preparation = database.security.seal(payload)
    nested = await database.prepare((await database.claim_next()).id)
    assert await SettingsSkills(database).catalog(nested.scope) == []
    assert nested.scope.memory_namespace.startswith("shared/")


async def test_shared_child_cannot_select_personal_connections_on_followup(runtime, actor):
    database = runtime.database
    async with database.sessions.begin() as db:
        db.add(Connection(id="personal", owner_id=actor.id, kind="linear", name="Personal",
                          shared=False, credential=database.security.seal("synthetic")))
    _, _, original = await parent_context(runtime, actor)
    child = await DelegationService(database).start(original.scope, "child", "Shared work")
    async with sign_in(runtime) as client:
        catalog = (await client.get("/api/connections", params={"session_id": child["session_id"]})).json()
        assert not any(item["id"] == "personal" for item in catalog)
    with pytest.raises(HTTPException, match="private conversation"):
        await database.submit(actor, child["session_id"], NewMessage(
            prompt="Use a personal connection", request_key="upgrade", connections=["personal"],
        ))
    async with database.sessions() as db:
        assert len(list(await db.scalars(select(Turn).where(Turn.session_id == child["session_id"])))) == 1


async def test_promoting_an_older_message_makes_it_the_latest_guidance(runtime, actor, active):
    session, _, prepared = active
    database = runtime.database
    older = await database.submit(actor, session.id, NewMessage(prompt="Queued first", request_key="older"))
    newer = await database.submit(actor, session.id, NewMessage(
        prompt="Correction sent second", request_key="newer", mode="asap",
    ))
    await database.steering_receipt(newer.id, "second")
    await database.steer_queued(actor, older.id)
    service = DelegationService(database)
    with pytest.raises(GuidanceChanged):
        await service.start(prepared.scope, "stale", "Old work", input_id=newer.id)
    child = await service.start(prepared.scope, "latest", "Promoted work", input_id=older.id)
    assert child["session_id"]
    # Receipt retries must not reorder or restore a superseded input.
    await database.steering_receipt(newer.id, "second")
    with pytest.raises(GuidanceChanged):
        await service.start(prepared.scope, "stale-again", "Old work", input_id=newer.id)


async def test_pending_guidance_dispatches_in_admission_order(runtime, actor, active):
    session, _, _ = active
    database = runtime.database
    older = await database.submit(actor, session.id, NewMessage(prompt="Queued first", request_key="older"))
    newer = await database.submit(actor, session.id, NewMessage(
        prompt="Correction sent second", request_key="newer", mode="asap",
    ))
    await database.steer_queued(actor, older.id)
    assert [child.id for child, _ in await database.pending_steering()] == [newer.id, older.id]
