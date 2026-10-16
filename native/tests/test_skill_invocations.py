from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from temporalio.testing import ActivityEnvironment
from test_native_settings import client_for

from moyai.activities import prepare_steering, prepare_turn
from moyai.contracts import NewMessage
from moyai.runtime import configure
from moyai.settings_models import SettingSkill
from moyai.skill_invocations import references


@pytest.mark.parametrize(("prompt", "names"), [
    ("/team inspect", ["team"]), ("/personal:team /org:review inspect", ["personal:team", "org:review"]),
    ("Please explain /team", []), ("/workspace/file.py", []), ("`/team`", []),
])
def test_only_leading_skill_tokens_invoke(prompt, names):
    assert references(prompt)[0] == names


async def save_skill(runtime, actor_id, name="team", scope=None, content="Report the violet verification code."):
    async with runtime.database.sessions.begin() as db:
        row = SettingSkill(name=name, owner_id=actor_id, description="Synthetic team skill", scope_key=scope or actor_id,
                           content=runtime.security.seal(content), enabled=True)
        db.add(row)
        await db.flush()
        return row.id


async def test_native_explicit_invocation_and_disabled_skill_recheck(runtime, actor, active):
    session, _, prepared = active
    identity = await save_skill(runtime, actor.id)
    service = runtime.skill_invocations
    assert await service.normalize("/team investigate", actor, session) == "/personal:team investigate"
    expanded = await service.expand("/personal:team investigate", prepared.scope)
    assert "Report the violet verification code." in expanded
    assert expanded.endswith("investigate")
    assert await service.expand("Explain /team", prepared.scope) == "Explain /team"
    listing = await service.listing(actor, shared=False, offset=0, limit=100)
    assert next(row for row in listing["skills"] if row["id"] == identity)["reference"] == "personal:team"
    assert "encrypted" not in str(listing) and "verification code" not in str(listing)
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(SettingSkill).where(SettingSkill.id == identity).values(enabled=False))
    with pytest.raises(HTTPException, match="422"):
        await service.expand("/personal:team investigate", prepared.scope)


async def test_duplicate_names_need_scope_and_shared_chats_exclude_personal(runtime, actor, active):
    session, _, prepared = active
    await save_skill(runtime, actor.id)
    await save_skill(runtime, actor.id, scope="shared", content="Shared procedure.")
    service = runtime.skill_invocations
    with pytest.raises(HTTPException):
        await service.normalize("/team investigate", actor, session)
    assert await service.normalize("/shared:team investigate", actor, session) == "/shared:team investigate"
    listing = await service.listing(actor, shared=True, offset=0, limit=100)
    assert not any(row["scope"] == "personal" for row in listing["skills"])
    with pytest.raises(HTTPException):
        await service.resolve("/personal:team", actor_id=actor.id, visibility="shared", surface="web", grant=None)
    with pytest.raises(HTTPException):
        await service.resolve("/shared:team", actor_id=actor.id, visibility="private", surface="slack", grant=None)
    with pytest.raises(PermissionError):
        await service.expand("/personal:team", replace(prepared.scope, actor_id="another-user"))


async def test_api_normalizes_short_names_keeps_retry_identity_and_edits_queued_skills(runtime, actor, active):
    session, _, _ = active
    await save_skill(runtime, actor.id)
    async with client_for(runtime, actor) as client:
        payload = {"prompt": "/team inspect", "request_key": "skill-admission"}
        first = await client.post(f"/api/sessions/{session.id}/messages", json=payload)
        assert first.status_code == 202
        replay = await client.post(f"/api/sessions/{session.id}/messages", json=payload)
        assert replay.json()["id"] == first.json()["id"]
        messages = (await client.get(f"/api/sessions/{session.id}/messages")).json()
        assert messages[-1]["content"] == "/personal:team inspect"
        assert (await client.patch(f"/api/turns/{first.json()['id']}", json={"prompt": "/team revised"})).status_code == 200
        messages = (await client.get(f"/api/sessions/{session.id}/messages")).json()
        assert messages[-1]["content"] == "/personal:team revised"
        assert (await client.post(f"/api/sessions/{session.id}/messages", json={
            **payload, "request_key": "missing", "prompt": "/missing inspect",
        })).status_code == 422
        shared = (await client.post("/api/sessions", json={"visibility": "shared"})).json()
        assert (await client.post(f"/api/sessions/{shared['id']}/messages", json=payload)).status_code == 422


async def test_preparation_and_asap_steering_load_skills_in_existing_activity_boundary(runtime, actor, active):
    session, turn, _ = active
    await save_skill(runtime, actor.id)
    configure(runtime)
    environment = ActivityEnvironment()
    # The active turn has no invocation; ordinary preparation stays unchanged.
    ordinary = await environment.run(prepare_turn, turn.id)
    assert ordinary.prompt == "Perform the task."
    correction = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="/personal:team inspect", request_key="steer-with-skill", mode="asap",
    ))
    assert correction.parent_turn == turn.id
    steering = await environment.run(prepare_steering, turn.id, correction.id)
    assert "Report the violet verification code." in steering.prompt
    assert steering.prompt.endswith("inspect")

