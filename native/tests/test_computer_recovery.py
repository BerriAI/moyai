"""Supplementary recovery fences; actual Modal/UI proof is recorded separately."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_application_boundaries import sign_in
from test_browser_login import waiting_login

from moyai.computer import lease
from moyai.computer_recovery import reconnect
from moyai.computer_stream import bind, validate
from moyai.models import ComputerDesktop, ComputerLease, ComputerOperation, Event, Session, Turn, User, new_id
from moyai.workspace_recovery import checkpoint

REFERENCE = {"provider": "modal", "id": "scoped-computer"}
REPLACEMENT = {"provider": "modal", "id": "restored-computer"}


async def saved_login(runtime, actor, monkeypatch):
    session, turn_id = await waiting_login(runtime, actor, checkpoint_workspace=True)
    monkeypatch.setattr(runtime.database.answers, "snapshots", SimpleNamespace(
        capture=AsyncMock(return_value="im-login-checkpoint"),
    ))
    await runtime.database.answers.save_computer(turn_id, REFERENCE, product=False)
    monkeypatch.setattr("moyai.computer_recovery.live_sandbox", AsyncMock(return_value=False))
    restored = AsyncMock(return_value=SimpleNamespace(
        object_id=REPLACEMENT["id"], poll=SimpleNamespace(aio=AsyncMock(return_value=None)),
    ))
    monkeypatch.setattr("moyai.computer_recovery.restored_sandbox", restored)
    guest = AsyncMock(return_value={"available": True})
    monkeypatch.setattr("moyai.computer_recovery.guest_request", guest)
    return session, turn_id, restored, guest


async def test_reconnect_restores_waiting_login_without_resuming_or_claiming_control(runtime, actor, monkeypatch):
    session, turn_id, restored, guest = await saved_login(runtime, actor, monkeypatch)
    viewer = await bind(runtime, session.id, actor, new_id(), controlling=False)
    async with runtime.database.sessions() as db:
        original_conversation = (await db.get(Turn, turn_id)).conversation
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/computer", json={
            "action": "reconnect", "client_id": new_id(),
        })
        assert response.status_code == 200, response.text
        assert response.json()["available"] and not response.json()["controlled"]
        view = (await client.get(f"/api/sessions/{session.id}")).json()
        assert view["session"]["status"] == "waiting"
        assert view["pending"]["metadata"]["login"]["workspace_id"] == REFERENCE["id"]
        async with runtime.database.sessions() as db:
            current = await db.get(Session, session.id)
            assert json.loads(current.workspace) == REPLACEMENT
            assert checkpoint(current.snapshot).workspace == REPLACEMENT
            assert (await db.get(Turn, turn_id)).conversation == original_conversation
            assert (await db.get(ComputerDesktop, session.id)).workspace_id == REPLACEMENT["id"]
            assert await db.get(ComputerLease, session.id) is None
            assert await db.get(ComputerOperation, session.id) is None
        with pytest.raises(HTTPException, match="computer changed"):
            await validate(runtime, viewer)
        # A proven restoration preserves the existing acknowledgement, but it
        # still requires the model to inspect the site rather than trusting it.
        completed = await client.post(f"/api/sessions/{session.id}/answers", json={
            "turn_id": turn_id, "request_key": "after-restore", "browser_logins": {"login": "signed_in"},
        })
        assert completed.status_code == 202, completed.text
        async with runtime.database.sessions() as db:
            resumed = await db.get(Turn, completed.json()["id"])
            answer = json.loads(runtime.security.unseal(resumed.deferred_results))["calls"]["login"]
            assert answer["status"] == "user_reported_signed_in" and "browser_read" in answer["instruction"]
            assert "mode='tabs'" in answer["instruction"] and "no tab_id" in answer["instruction"]
    restored.assert_awaited_once()
    assert guest.await_args.args[1]["action"] == "start"


async def test_live_reconnect_preserves_checkpoint_and_existing_control(runtime, actor, monkeypatch):
    session, _turn, restored, _guest = await saved_login(runtime, actor, monkeypatch)
    monkeypatch.setattr("moyai.computer_recovery.live_sandbox", AsyncMock(return_value=True))
    result = await reconnect(runtime, session.id, actor, new_id())
    assert result.available and not result.controlled
    async with runtime.database.sessions() as db:
        assert checkpoint((await db.get(Session, session.id)).snapshot)
    controller = new_id()
    await lease(runtime, session.id, actor, "claim", controller)
    assert (await reconnect(runtime, session.id, actor, controller)).controlled
    assert not (await reconnect(runtime, session.id, actor, new_id())).controlled
    restored.assert_not_awaited()


@pytest.mark.parametrize("blocker", ["missing", "digest", "human_changes", "lease", "operation", "stopped"])
async def test_recovery_requires_a_current_settled_checkpoint(runtime, actor, monkeypatch, blocker):
    session, turn_id, restored, _guest = await saved_login(runtime, actor, monkeypatch)
    async with runtime.database.sessions.begin() as db:
        current = await db.get(Session, session.id)
        if blocker == "missing":
            current.snapshot = None
        elif blocker == "digest":
            current.conversation = runtime.security.seal("{}")
        elif blocker == "lease":
            db.add(ComputerLease(session_id=session.id, actor_id=actor.id, expires_at=int(time.time()) + 90))
        elif blocker == "operation":
            db.add(ComputerOperation(session_id=session.id, identity=new_id(), expires_at=int(time.time()) + 90))
        elif blocker == "stopped":
            current.status = "stopped"
            (await db.get(Turn, turn_id)).status = "stopped"
    if blocker == "human_changes":
        await lease(runtime, session.id, actor, "claim", new_id())
        await lease(runtime, session.id, actor, "handoff")
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "reconnect"})
        assert response.status_code == 409, response.text
    restored.assert_not_awaited()
    async with runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, session.id)).workspace) == REFERENCE


async def test_provider_uncertainty_never_allocates_a_replacement(runtime, actor, monkeypatch):
    session, _turn, restored, _guest = await saved_login(runtime, actor, monkeypatch)
    monkeypatch.setattr("moyai.computer_recovery.live_sandbox", AsyncMock(side_effect=TimeoutError("private endpoint")))
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "reconnect"})
        assert response.status_code == 503 and "private endpoint" not in response.text
    restored.assert_not_awaited()


@pytest.mark.parametrize("race", ["stop", "revoke", "new_checkpoint"])
async def test_late_restoration_cannot_publish_after_authority_or_checkpoint_changes(runtime, actor, monkeypatch, race):
    session, turn_id, _restored, guest = await saved_login(runtime, actor, monkeypatch)
    async def change(*_args):
        async with runtime.database.sessions.begin() as db:
            current = await db.get(Session, session.id)
            if race == "stop":
                current.status = "stopped"
                (await db.get(Turn, turn_id)).status = "stopped"
            elif race == "revoke":
                (await db.get(User, actor.id)).auth_version += 1
            else:
                current.snapshot = None
        return {"available": True}
    guest.side_effect = change
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "reconnect"})
        assert response.status_code in {401, 403, 409}, response.text
    async with runtime.database.sessions() as db:
        assert json.loads((await db.get(Session, session.id)).workspace) == REFERENCE
        assert await db.get(ComputerOperation, session.id) is None
        assert not await db.scalar(select(Event.id).where(Event.kind == "browser_login_restored"))


async def test_concurrent_reconnect_and_signin_cannot_cross_recovery(runtime, actor, monkeypatch):
    session, turn_id, restored, guest = await saved_login(runtime, actor, monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def hold(*_args):
        entered.set()
        await finish.wait()
        return {"available": True}
    guest.side_effect = hold
    async with sign_in(runtime) as first, sign_in(runtime) as second:
        task = asyncio.create_task(first.post(f"/api/sessions/{session.id}/computer", json={"action": "reconnect"}))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            again = await second.post(f"/api/sessions/{session.id}/computer", json={"action": "reconnect"})
            assert again.status_code == 409
            complete = await second.post(f"/api/sessions/{session.id}/answers", json={
                "turn_id": turn_id, "request_key": "during-restore", "browser_logins": {"login": "signed_in"},
            })
            assert complete.status_code == 409
        finally:
            finish.set()
        assert (await task).status_code == 200
    restored.assert_awaited_once()


async def test_second_expiry_keeps_original_login_binding(runtime, actor, monkeypatch):
    session, turn_id, restored, _guest = await saved_login(runtime, actor, monkeypatch)
    await reconnect(runtime, session.id, actor, new_id())
    restored.return_value.object_id = "restored-again"
    await reconnect(runtime, session.id, actor, new_id())
    async with sign_in(runtime) as client:
        complete = await client.post(f"/api/sessions/{session.id}/answers", json={
            "turn_id": turn_id, "request_key": "after-second-restore", "browser_logins": {"login": "signed_in"},
        })
        assert complete.status_code == 202, complete.text


async def test_shared_viewer_cannot_restore_another_requesters_signin(runtime, actor, monkeypatch):
    session, _turn, restored, _guest = await saved_login(runtime, actor, monkeypatch)
    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).visibility = "shared"
        other = User(id=new_id(), username="other", display_name="Other", password="unused")
        db.add(other)
    with pytest.raises(HTTPException) as failure:
        await reconnect(runtime, session.id, other, new_id())
    assert failure.value.status_code == 403
    restored.assert_not_awaited()


async def test_visible_desktop_maintenance_retries_and_stops_with_its_stream(monkeypatch):
    from moyai.computer_stream import keep_desktop_alive

    monkeypatch.setattr("moyai.computer_stream.DESKTOP_KEEPALIVE_SECONDS", .01)
    refreshed = asyncio.Event()
    process = SimpleNamespace(wait=SimpleNamespace(aio=AsyncMock(return_value=0)))
    attempts = 0
    async def execute(*args, **kwargs):
        nonlocal attempts
        assert args == ("/bin/true",) and kwargs == {"timeout": 10}
        attempts += 1
        if attempts == 1:
            raise TimeoutError("provider details must not be logged")
        refreshed.set()
        return process
    sandbox = SimpleNamespace(exec=SimpleNamespace(aio=execute))
    task = asyncio.create_task(keep_desktop_alive(sandbox))
    await asyncio.wait_for(refreshed.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts == 2
