"""Application input/ownership regressions; live browser/Modal proof is separate."""
import asyncio
import io
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.testing import WorkflowEnvironment
from test_application_boundaries import sign_in

from moyai.api import COOKIE, create_app
from moyai.computer import lease
from moyai.contracts import NewMessage, NewSession
from moyai.models import ComputerLease, ComputerOperation, Connection, User, new_id
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (24, 24), "#44aaee").save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.parametrize("runtime", ["image"], indirect=True)
async def test_uploaded_image_reaches_native_temporal_input_and_saved_history(runtime, actor):
    raw = image_bytes()
    async with sign_in(runtime) as client:
        uploaded = await client.post("/api/uploads?name=clipboard.png", content=raw)
        assert uploaded.status_code == 201
        image = uploaded.json()
        session = (await client.post("/api/sessions", json={})).json()
        created = await client.post(f"/api/sessions/{session['id']}/messages", json={
            "attachments": [image["id"]], "request_key": "image-only-message",
        })
        assert created.status_code == 202
        turn = created.json()
        await runtime.database.claim_next()
        environment = await WorkflowEnvironment.start_local(
            plugins=[PydanticAIPlugin()], data_converter=encrypted_converter(runtime.security, runtime.objects),
        )
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                result = await asyncio.wait_for(runtime.client.execute_workflow(
                    TurnWorkflow.run, turn["id"], id=f"moyai-turn-{turn['id']}",
                    task_queue=runtime.settings.temporal_queue,
                ), 45)
        assert result["status"] == "completed"
        messages = (await client.get(f"/api/sessions/{session['id']}/messages")).json()
        assert messages[0]["attachments"] == [image]
        assert "PNG image received natively" in messages[1]["content"]
        assert messages[1]["attachments"] == []
        assert (await client.get(f"/api/uploads/{image['id']}")).content == raw


async def test_images_reject_invalid_content_and_cross_conversation_reuse(runtime):
    async with sign_in(runtime) as client:
        bad = await client.post("/api/uploads?name=image.png", content=b"<script>not an image</script>")
        assert bad.status_code == 422
        huge = await client.post("/api/uploads", content=b"x" * (8 * 1024 * 1024 + 1))
        assert huge.status_code == 413
        image = (await client.post("/api/uploads?name=picture.png", content=image_bytes())).json()
        first = (await client.post("/api/sessions", json={})).json()
        second = (await client.post("/api/sessions", json={"visibility": "shared"})).json()
        body = {"prompt": "Inspect it.", "attachments": [image["id"]], "request_key": "same-upload"}
        assert (await client.post(f"/api/sessions/{first['id']}/messages", json=body)).status_code == 202
        assert (await client.post(f"/api/sessions/{second['id']}/messages", json=body)).status_code == 404
        assert (await client.get(f"/api/sessions/{second['id']}/messages")).json() == []


async def test_corrupt_png_is_rejected_without_an_internal_server_error(runtime):
    raw = bytearray(image_bytes())
    raw[raw.index(b"IDAT") + 4] ^= 255
    async with sign_in(runtime) as client:
        response = await client.post("/api/uploads?name=corrupt.png", content=bytes(raw))
        assert response.status_code == 422


async def test_private_upload_is_not_readable_or_submittable_by_another_user(runtime):
    async with runtime.database.sessions.begin() as db:
        other = User(id=new_id(), username="other", display_name="Other", password="unused")
        db.add(other)
    async with sign_in(runtime) as owner, httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver",
    ) as client:
        image = (await owner.post("/api/uploads", content=image_bytes())).json()
        token, csrf = runtime.security.issue_session(other.id, other.auth_version)
        client.cookies.set(COOKIE, token)
        client.headers["X-Moyai-CSRF"] = csrf
        assert (await client.get(f"/api/uploads/{image['id']}")).status_code == 404
        session = (await client.post("/api/sessions", json={})).json()
        response = await client.post(f"/api/sessions/{session['id']}/messages", json={
            "attachments": [image["id"]], "request_key": "stolen-upload",
        })
        assert response.status_code == 404


async def finished_computer(runtime, actor, *, visibility="private"):
    session = await runtime.database.create_session(actor, NewSession(visibility=visibility))
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Set up", request_key=new_id()))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    await runtime.database.save_workspace_reference(prepared.scope, {"provider": "modal", "id": "scoped-computer"})
    await runtime.database.stopped(turn.id)
    return session


async def test_human_control_does_not_block_chat_or_coding(runtime, actor):
    session = await finished_computer(runtime, actor)
    await lease(runtime, session.id, actor, "claim")
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Next", request_key="under-human"))
    assert (await runtime.database.claim_next()).id == turn.id
    # People can keep controlling the desktop during active agent work.
    await lease(runtime, session.id, actor, "heartbeat")
    async with runtime.database.sessions() as db:
        assert (await db.get(ComputerLease, session.id)).actor_id == actor.id


async def test_expired_control_lease_does_not_hold_queue_forever(runtime, actor):
    session = await finished_computer(runtime, actor)
    await lease(runtime, session.id, actor, "claim")
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerLease, session.id)).expires_at = int(time.time()) - 1
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Next", request_key="after-expiry"))
    assert (await runtime.database.claim_next()).id == turn.id


async def test_desktop_input_keeps_its_fence_without_blocking_chat(runtime, actor, monkeypatch):
    session = await finished_computer(runtime, actor)
    await lease(runtime, session.id, actor, "claim")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def held_input(_runtime, _session, _body):
        entered.set()
        await finish.wait()
        return {"available": True}

    monkeypatch.setattr("moyai.computer.request", held_input)
    async with sign_in(runtime) as first, sign_in(runtime) as second:
        command = asyncio.create_task(first.post(
            f"/api/sessions/{session.id}/computer", json={"action": "type", "text": "test"},
        ))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            released = await second.post(f"/api/sessions/{session.id}/computer", json={"action": "release"})
            assert released.status_code == 409
            queued = await runtime.database.submit(actor, session.id, NewMessage(
                prompt="Wait for input.", request_key="input-in-flight",
            ))
            assert (await runtime.database.claim_next()).id == queued.id
        finally:
            finish.set()
        assert (await command).status_code == 200
        assert (await second.post(f"/api/sessions/{session.id}/computer", json={"action": "release"})).status_code == 200
        assert await runtime.database.claim_next() is None


async def test_control_is_not_acquired_for_an_unstarted_computer(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "start"})
        assert response.status_code == 409
    async with runtime.database.sessions() as db:
        assert await db.get(ComputerLease, session.id) is None


@pytest.mark.parametrize("failure", ["exit", "reported"])
async def test_confirmed_guest_failure_allows_immediate_retry_release_and_agent_work(runtime, actor, monkeypatch, failure):
    session = await finished_computer(runtime, actor)

    class FailedWorkspace:
        async def write_bytes(self, *_args):
            pass

        async def run(self, *_args, **_kwargs):
            return SimpleNamespace(exit_code=1 if failure == "exit" else 0, stdout='{"error":"failed"}')

    monkeypatch.setattr("moyai.computer.Workspace", lambda _backend: FailedWorkspace())
    async with sign_in(runtime) as client:
        for _ in range(2):
            failed = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "start"})
            assert failed.status_code == 422
            async with runtime.database.sessions() as db:
                assert await db.get(ComputerOperation, session.id) is None
                assert await db.get(ComputerLease, session.id) is None
        assert (await client.post(f"/api/sessions/{session.id}/computer", json={"action": "release"})).status_code == 200
    queued = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Continue without the desktop.", request_key="after-confirmed-guest-failure",
    ))
    assert (await runtime.database.claim_next()).id == queued.id


async def test_ambiguous_desktop_timeout_keeps_operation_ownership(runtime, actor, monkeypatch):
    session = await finished_computer(runtime, actor)

    async def timeout(*_args):
        raise TimeoutError("Response was lost.")

    monkeypatch.setattr("moyai.computer.request", timeout)
    async with sign_in(runtime) as client:
        failed = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "start"})
        assert failed.status_code == 503
        assert (await client.post(f"/api/sessions/{session.id}/computer", json={"action": "release"})).status_code == 409
    async with runtime.database.sessions() as db:
        assert await db.get(ComputerOperation, session.id) is not None
        assert await db.get(ComputerLease, session.id) is not None


async def test_shared_computer_allows_members_but_one_human_controller(runtime, actor, monkeypatch):
    session = await finished_computer(runtime, actor, visibility="shared")
    private = await finished_computer(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        other = User(id=new_id(), username="viewer", display_name="Viewer", password="unused")
        db.add(other)
    async def frame(*_args):
        return {"available": True}
    monkeypatch.setattr("moyai.computer.request", frame)
    token, csrf = runtime.security.issue_session(other.id, other.auth_version)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver", cookies={COOKIE: token}, headers={"X-Moyai-CSRF": csrf},
    ) as client, sign_in(runtime) as owner:
        path = f"/api/sessions/{session.id}/computer"
        assert (await client.get(path)).status_code == 200
        assert (await owner.post(path, json={"action": "claim"})).status_code == 200
        state = (await client.get(path)).json()
        assert state["controller"] == actor.display_name and not state["controlled"]
        assert (await client.post(path, json={"action": "claim"})).status_code == 409
        assert (await owner.post(path, json={"action": "release"})).status_code == 200
        assert (await client.post(path, json={"action": "claim"})).status_code == 200
        assert (await client.get(f"/api/sessions/{private.id}/computer")).status_code == 404
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, other.id)).enabled = False
        assert (await client.get(path)).status_code == 401


async def test_computer_access_rechecks_revoked_connection_grants(runtime, actor):
    async with runtime.database.sessions.begin() as db:
        account = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Test account",
            credential=runtime.security.seal("test-token"),
        )
        db.add(account)
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Prepare", connections=[account.id], request_key="scoped-computer",
    ))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    await runtime.database.save_workspace_reference(prepared.scope, {"provider": "modal", "id": "scoped-computer"})
    await runtime.database.stopped(turn.id)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Connection, account.id)).enabled = False
    async with sign_in(runtime) as client:
        assert (await client.get(f"/api/sessions/{session.id}/computer")).status_code == 403
        assert (await client.post(f"/api/sessions/{session.id}/computer", json={"action": "claim"})).status_code == 403
