from contextlib import asynccontextmanager

import httpx
from test_application_boundaries import sign_in

from moyai.api import COOKIE, create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import SessionPreference, Turn, User, new_id


@asynccontextmanager
async def member_client(runtime):
    async with runtime.database.sessions.begin() as db:
        user = User(id=new_id(), username=new_id(), display_name="Teammate", password="unused")
        db.add(user)
    token, csrf = runtime.security.issue_session(user.id, user.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver", cookies={COOKIE: token}, headers={"X-Moyai-CSRF": csrf},
    ) as client:
        yield client, user


async def test_personal_folders_rename_memberships_and_remove_without_archiving(runtime, actor):
    shared = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    async with sign_in(runtime) as owner, member_client(runtime) as (member, _):
        path = f"/api/sessions/{shared.id}"
        folder = (await owner.post("/api/session-folders", json={"name": "Research"})).json()
        other_folder = (await member.post("/api/session-folders", json={"name": "Research"})).json()
        assert (await owner.post("/api/session-folders", json={"name": " research "})).status_code == 409
        assert (await owner.patch(path, json={"folder": "Research"})).status_code == 200
        assert (await member.patch(path, json={"folder": "Research"})).status_code == 200
        assert (await member.patch(f"/api/session-folders/{folder['id']}", json={"name": "Stolen"})).status_code == 404
        assert (await owner.patch(f"/api/session-folders/{folder['id']}", json={"name": "Today"})).status_code == 200
        assert (await owner.get("/api/sessions")).json()[0]["folder"] == "Today"
        assert (await member.get("/api/sessions")).json()[0]["folder"] == "Research"
        assert (await owner.patch(path, json={"folder": "Research"})).status_code == 404
        assert (await owner.delete(f"/api/session-folders/{folder['id']}")).status_code == 200
        row = (await owner.get("/api/sessions")).json()[0]
        assert row["folder"] == "" and not row["archived"]
        assert (await member.get("/api/session-folders")).json() == [other_folder]
        assert (await member.patch(path, json={"title": "changed"})).status_code == 403
        assert (await member.patch(path, json={"archived": True})).status_code == 403


async def test_participation_read_state_and_preferences_do_not_reorder_other_peoples_sessions(runtime, actor):
    shared = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    private = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as owner, member_client(runtime) as (member, other):
        assert [row["id"] for row in (await member.get("/api/sessions")).json()] == [shared.id]
        assert not (await member.get("/api/sessions")).json()[0]["participated"]
        # Viewing is not participation.
        assert (await member.get(f"/api/sessions/{shared.id}")).status_code == 200
        turn = await runtime.database.submit(other, shared.id, NewMessage(prompt="Contribute", request_key="participate"))
        row = (await member.get("/api/sessions")).json()[0]
        assert row["participated"]
        message = (await member.get(f"/api/sessions/{shared.id}/messages")).json()[0]
        path = f"/api/sessions/{shared.id}"
        assert (await member.post(f"{path}/read", json={"message_id": message["id"]})).status_code == 200
        stamp = (await runtime.database.session(actor, shared.id)).updated_at
        assert (await member.patch(path, json={"unread": True, "pinned": True})).status_code == 200
        assert (await member.get("/api/sessions")).json()[0]["unread"]
        assert (await runtime.database.session(actor, shared.id)).updated_at == stamp
        assert not next(item for item in (await owner.get("/api/sessions")).json() if item["id"] == shared.id)["pinned"]
        assert (await member.post(f"/api/sessions/{private.id}/read", json={"message_id": 10**9})).status_code == 404
        # A caller cannot mark unseen future messages read with an arbitrary high id.
        await member.post(f"{path}/read", json={"message_id": 10**9})
        await runtime.database.submit(other, shared.id, NewMessage(prompt="Another", request_key="next"))
        assert (await member.get("/api/sessions")).json()[0]["unread"]
        assert (await member.get("/api/sessions")).json()[0]["id"] == turn.session_id


async def test_legacy_folder_names_are_adopted_on_additive_schema_startup(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    async with runtime.database.sessions.begin() as db:
        db.add(SessionPreference(user_id=actor.id, session_id=session.id, folder="Saved earlier"))
    await runtime.database.start()
    await runtime.database.start()
    async with sign_in(runtime) as client:
        folders = (await client.get("/api/session-folders")).json()
        assert len(folders) == 1 and folders[0]["name"] == "Saved earlier"
        assert (await client.get("/api/sessions")).json()[0]["folder"] == "Saved earlier"


async def test_queued_steering_reuses_the_saved_message_and_preserves_actor_boundary(runtime, active, actor):
    session, parent, _ = active
    async with runtime.database.sessions.begin() as db:
        (await db.get(Turn, parent.id)).status = "running"
    child = await runtime.database.submit(actor, session.id, NewMessage(prompt="Correct the task", request_key="queue-first"))
    async with sign_in(runtime) as client, member_client(runtime) as (other, _):
        path = f"/api/turns/{child.id}/steer"
        assert (await other.post(path)).status_code == 404
        for _ in range(2):
            result = await client.post(path)
            assert result.status_code == 202
            assert result.json()["id"] == child.id
            assert result.json()["parent_turn"] == parent.id
            assert result.json()["status"] == "steering"
        assert len((await client.get(f"/api/sessions/{session.id}/messages")).json()) == 2
        assert (await client.patch(f"/api/turns/{child.id}", json={"prompt": "Too late"})).status_code == 409
