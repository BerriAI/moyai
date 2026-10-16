"""Stream ownership regressions. Live noVNC/Modal proof is recorded separately."""

import asyncio
import time
from contextlib import asynccontextmanager

import aiosqlite
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_application_boundaries import sign_in
from test_images_and_computer import finished_computer

from moyai.api import COOKIE, create_app
from moyai.computer import DESKTOP_VERSION, lease
from moyai.computer_stream import bind, relay, release, validate
from moyai.contracts import NewMessage, NewSession
from moyai.models import ComputerDesktop, ComputerLease, ComputerOperation, Connection, Session, User, new_id


async def ready_computer(runtime, actor, *, visibility="private"):
    session = await finished_computer(runtime, actor, visibility=visibility)
    async with runtime.database.sessions.begin() as db:
        db.add(ComputerDesktop(
            session_id=session.id, workspace_id="scoped-computer", version=DESKTOP_VERSION,
            control_client="", control_epoch=new_id(),
        ))
    return session


async def test_viewer_does_not_need_control_and_metadata_does_not_execute_modal(runtime, actor, monkeypatch):
    session = await ready_computer(runtime, actor)
    async def forbidden(*args):
        raise AssertionError("Metadata polling must not execute in Modal.")
    monkeypatch.setattr("moyai.computer.request", forbidden)
    access = await bind(runtime, session.id, actor, new_id(), controlling=False)
    assert not access.controlling
    await validate(runtime, access)
    async with sign_in(runtime) as client:
        response = await client.get(f"/api/sessions/{session.id}/computer")
        assert response.status_code == 200
        assert response.json()["available"]
        assert "frame" not in response.json()


async def test_only_the_claiming_browser_gets_a_control_stream(runtime, actor):
    session = await ready_computer(runtime, actor)
    first, second = new_id(), new_id()
    with pytest.raises(HTTPException):
        await bind(runtime, session.id, actor, first, controlling=True)
    await lease(runtime, session.id, actor, "claim", first)
    access = await bind(runtime, session.id, actor, first, controlling=True)
    await validate(runtime, access)
    for operation in (
        lease(runtime, session.id, actor, "claim", second),
        lease(runtime, session.id, actor, "release", second),
        bind(runtime, session.id, actor, second, controlling=True),
    ):
        with pytest.raises(HTTPException):
            await operation
    await lease(runtime, session.id, actor, "release", first)
    with pytest.raises(HTTPException):
        await validate(runtime, access)
    await lease(runtime, session.id, actor, "claim", second)
    await validate(runtime, await bind(runtime, session.id, actor, second, controlling=True))


async def test_late_disconnect_cannot_release_a_reconnected_controller(runtime, actor):
    session = await ready_computer(runtime, actor)
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    first = await bind(runtime, session.id, actor, client, controlling=True)
    second = await bind(runtime, session.id, actor, client, controlling=True)
    assert first.epoch != second.epoch
    with pytest.raises(HTTPException):
        await validate(runtime, first)
    await release(runtime, first)
    await validate(runtime, second)
    await release(runtime, second)
    async with runtime.database.sessions() as db:
        assert (await db.get(ComputerLease, session.id)).actor_id == actor.id
    # Disconnect suspends human ownership, explicit release ends it.
    with pytest.raises(HTTPException):
        await validate(runtime, second)
    await lease(runtime, session.id, actor, "release", client)
    async with runtime.database.sessions() as db:
        assert await db.get(ComputerLease, session.id) is None


async def test_disconnect_preserves_an_unsettled_http_action_fence(runtime, actor):
    session = await ready_computer(runtime, actor, visibility="shared")
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    access = await bind(runtime, session.id, actor, client, controlling=True)
    async with runtime.database.sessions.begin() as db:
        db.add(ComputerOperation(session_id=session.id, identity=new_id(), expires_at=int(time.time()) + 90))
        other = User(id=new_id(), username=new_id(), display_name="Other", password="unused")
        db.add(other)
    await release(runtime, access)
    with pytest.raises(HTTPException):
        await lease(runtime, session.id, other, "claim", new_id())
    replacement_client = client
    with pytest.raises(HTTPException):
        await lease(runtime, session.id, actor, "claim", new_id())
    await lease(runtime, session.id, actor, "heartbeat", client)
    with pytest.raises(HTTPException) as pending:
        await bind(runtime, session.id, actor, replacement_client, controlling=True)
    assert pending.value.status_code == 409
    async with runtime.database.sessions.begin() as db:
        await db.delete(await db.get(ComputerOperation, session.id))
    replacement = await bind(runtime, session.id, actor, replacement_client, controlling=True)
    await validate(runtime, replacement)


@pytest.mark.parametrize("controlling", [False, True])
async def test_cookie_expiry_revokes_open_stream_without_waiting_for_input(runtime, actor, monkeypatch, controlling):
    from itsdangerous.timed import TimestampSigner
    session = await ready_computer(runtime, actor)
    client = new_id()
    if controlling:
        await lease(runtime, session.id, actor, "claim", client)
    issued = int(time.time())
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: issued)
    token, _ = runtime.security.issue_session(actor.id, actor.auth_version)
    access = await bind(runtime, session.id, actor, client, controlling=controlling, session_token=token)
    await validate(runtime, access)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: issued + 7 * 86400 + 1)
    monkeypatch.setattr("moyai.computer_stream.RECHECK_SECONDS", .01)
    socket, remote = Socket(), Remote()
    with pytest.raises(HTTPException):
        await asyncio.wait_for(relay(runtime, socket, remote, access), 2)
    assert not remote.sent and not socket.sent


async def test_suspended_lease_expires_and_does_not_reappear_from_old_heartbeat(runtime, actor):
    session = await ready_computer(runtime, actor)
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    access = await bind(runtime, session.id, actor, client, controlling=True)
    await release(runtime, access)
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerLease, session.id)).expires_at = 0
    with pytest.raises(HTTPException):
        await lease(runtime, session.id, actor, "heartbeat", client)
    replacement = new_id()
    await lease(runtime, session.id, actor, "claim", replacement)
    latest = await bind(runtime, session.id, actor, replacement, controlling=True)
    await release(runtime, access)
    await validate(runtime, latest)


@pytest.mark.parametrize("change", ["disabled", "signout", "workspace", "lease"])
async def test_open_stream_rechecks_current_authority(runtime, actor, change):
    session = await ready_computer(runtime, actor)
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    access = await bind(runtime, session.id, actor, client, controlling=True)
    async with runtime.database.sessions.begin() as db:
        if change == "disabled":
            (await db.get(User, actor.id)).enabled = False
        elif change == "signout":
            (await db.get(User, actor.id)).auth_version += 1
        elif change == "workspace":
            (await db.get(Session, session.id)).workspace = '{"provider":"modal","id":"different"}'
        else:
            (await db.get(ComputerLease, session.id)).expires_at = 0
    with pytest.raises(HTTPException):
        await validate(runtime, access)


async def test_stream_rechecks_connection_grants(runtime, actor):
    async with runtime.database.sessions.begin() as db:
        connection = Connection(
            id=new_id(), owner_id=actor.id, kind="github", name="Shared test account",
            credential=runtime.security.seal("test-token"), shared=True,
        )
        db.add(connection)
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(
        prompt="Prepare", connections=[connection.id], request_key=new_id(),
    ))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    await runtime.database.save_workspace_reference(prepared.scope, {"provider": "modal", "id": "scoped-computer"})
    await runtime.database.stopped(turn.id)
    async with runtime.database.sessions.begin() as db:
        db.add(ComputerDesktop(session_id=session.id, workspace_id="scoped-computer", version=DESKTOP_VERSION))
    access = await bind(runtime, session.id, actor, new_id(), controlling=False)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Connection, connection.id)).revision += 1
    with pytest.raises(HTTPException):
        await validate(runtime, access)


async def test_concurrent_browser_claims_have_one_owner(runtime, actor):
    session = await ready_computer(runtime, actor)
    clients = [new_id(), new_id()]
    claims = await asyncio.gather(
        *(lease(runtime, session.id, actor, "claim", client) for client in clients),
        return_exceptions=True,
    )
    assert sum(isinstance(result, HTTPException) for result in claims) == 1
    winner = clients[next(i for i, result in enumerate(claims) if not isinstance(result, Exception))]
    await validate(runtime, await bind(runtime, session.id, actor, winner, controlling=True))


class Socket:
    def __init__(self):
        self.received = asyncio.Queue()
        self.sent = []

    async def receive(self):
        return await self.received.get()

    async def send_bytes(self, value):
        self.sent.append(value)


class Remote:
    def __init__(self):
        self.received = asyncio.Queue()
        self.sent = []

    async def send(self, value):
        self.sent.append(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.received.get()


async def wait_until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.005)


async def test_relay_checks_input_and_stops_idle_viewing_after_revocation(runtime, actor, monkeypatch):
    monkeypatch.setattr("moyai.computer_stream.RECHECK_SECONDS", .01)
    session = await ready_computer(runtime, actor)
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    access = await bind(runtime, session.id, actor, client, controlling=True)
    socket, remote = Socket(), Remote()
    task = asyncio.create_task(relay(runtime, socket, remote, access))
    await socket.received.put({"type": "websocket.receive", "bytes": b"authorized input"})
    await wait_until(lambda: bool(remote.sent))
    await lease(runtime, session.id, actor, "release", client)
    await socket.received.put({"type": "websocket.receive", "bytes": b"stale input"})
    with pytest.raises(HTTPException):
        await asyncio.wait_for(task, 2)
    assert remote.sent == [b"authorized input"]
    # A viewing socket must also be revoked without waiting for client input.
    viewer = await bind(runtime, session.id, actor, new_id(), controlling=False)
    task = asyncio.create_task(relay(runtime, Socket(), Remote(), viewer))
    async with runtime.database.sessions.begin() as db:
        (await db.get(User, actor.id)).enabled = False
    with pytest.raises(HTTPException):
        await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("source", ["input", "watch"])
async def test_relay_cancellation_drains_authorization_before_returning(runtime, actor, monkeypatch, source):
    session = await ready_computer(runtime, actor)
    client = new_id()
    if source == "input":
        await lease(runtime, session.id, actor, "claim", client)
    access = await bind(runtime, session.id, actor, client, controlling=source == "input")
    entered, finish = asyncio.Event(), asyncio.Event()
    execute = aiosqlite.Cursor.execute
    paused = False

    async def pause_read(cursor, sql, parameters=None):
        nonlocal paused
        result = await execute(cursor, sql, parameters)
        if not paused and sql.startswith("SELECT"):
            paused = True
            entered.set()
            await finish.wait()
        return result

    # Pause a real SQLite read before its rows/cursor are consumed. Canceling
    # the relay must let authorization release that cursor before it returns.
    monkeypatch.setattr(aiosqlite.Cursor, "execute", pause_read)
    monkeypatch.setattr("moyai.computer_stream.RECHECK_SECONDS", 60 if source == "input" else .01)
    socket, remote = Socket(), Remote()
    task = asyncio.create_task(relay(runtime, socket, remote, access))
    try:
        if source == "input":
            await socket.received.put({"type": "websocket.receive", "bytes": b"cancelled input"})
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=.05)
        assert not done
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not remote.sent and not socket.sent
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, actor.id)).enabled = False
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_websocket_requires_same_origin_cookie_and_view_access(runtime, actor, monkeypatch):
    session = await ready_computer(runtime, actor)
    connections = []

    @asynccontextmanager
    async def connected(access):
        connections.append(access)
        remote = Remote()
        remote.received.put_nowait(b"RFB 003.008\n")
        yield remote

    monkeypatch.setattr("moyai.computer_stream.upstream", connected)
    token, _ = runtime.security.issue_session(actor.id, actor.auth_version)
    url = f"/api/sessions/{session.id}/computer/stream?client_id={new_id()}"
    # Run the synchronous WebSocket client on its own thread.
    def exercise():
        with TestClient(create_app(runtime, run_dispatcher=False)) as client:
            with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, headers={"origin": "http://testserver"}):
                pass
            client.cookies.set(COOKIE, token)
            with pytest.raises(WebSocketDisconnect), client.websocket_connect(url, headers={"origin": "https://untrusted.example"}):
                pass
            with pytest.raises(WebSocketDisconnect), client.websocket_connect(url + "&control=true", headers={"origin": "http://testserver"}):
                pass
            with client.websocket_connect(url, headers={"origin": "http://testserver"}) as stream:
                assert stream.receive_bytes() == b"RFB 003.008\n"
    await asyncio.to_thread(exercise)
    assert len(connections) == 1 and not connections[0].controlling
