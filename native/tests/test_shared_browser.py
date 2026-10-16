"""Supplementary transport doubles; no browser, server or Modal launched here."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.workspaces import WorkspaceRef
from test_application_boundaries import sign_in
from test_computer_stream import ready_computer

from moyai.browser import BrowserAction, agent_request, browser_capability, reserve_agent, settle
from moyai.computer import DESKTOP_VERSION, DesktopCommandFailed, lease
from moyai.computer_stream import bind, release, validate
from moyai.contracts import NewMessage
from moyai.models import ComputerDesktop, ComputerLease, ComputerOperation, new_id


async def running_browser(runtime, actor):
    session = await ready_computer(runtime, actor)
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Browse", request_key=new_id()))
    await runtime.database.claim_next()
    prepared = await runtime.database.prepare(turn.id)
    return session, prepared.scope


def workspace():
    return SimpleNamespace(ref=WorkspaceRef(provider="modal", id="scoped-computer"))


@pytest.mark.parametrize("command", [
    {"action": "evaluate", "expression": "alert(1)"},
    {"action": "open", "url": "file:///etc/passwd"},
    {"action": "open", "url": "javascript:alert(1)"},
    {"action": "open", "url": "https://user:secret@example.com"},
    {"action": "click"}, {"action": "click", "x": 1280, "y": 1},
    {"action": "fill", "text": "unobserved"},
    {"action": "key", "key": "Return; sh"},
    {"action": "select", "tab_id": "ws://127.0.0.1:9222/target"},
    {"action": "fill", "selector": "#a", "label": "also a"},
    {"action": "reload", "cdp": "Browser.close"},
])
def test_browser_contract_rejects_unstructured_or_unsafe_commands(command):
    with pytest.raises(ValidationError):
        BrowserAction.model_validate(command)


async def test_tools_register_as_native_sequential_capability(runtime):
    capability = browser_capability(runtime.database)
    assert capability.id == "moyai_browser"
    assert {tool.name for tool in capability.tools} == {"browser_open", "browser_read", "browser_action"}
    assert all(tool.sequential for tool in capability.tools)


async def test_human_control_and_hidden_panel_suspend_agent_writes_but_allow_reads_and_coding(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    access = await bind(runtime, session.id, actor, client, controlling=True)
    await release(runtime, access)  # The panel was hidden.
    calls = []
    async def guest(_workspace, body):
        calls.append(body)
        return {"tabs": [], "snapshot": "Observed page"}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    for command in ["open", "select", "close", "back", "forward", "reload", "click", "fill", "type", "key", "scroll"]:
        with pytest.raises(ToolFailed, match="person has control"):
            await agent_request(runtime.database, scope, workspace(), {"action": command}, writing=True)
    assert calls == []
    assert (await agent_request(runtime.database, scope, workspace(), {"action": "inspect"}, writing=False))["snapshot"]
    await runtime.database.authorize_scope(scope)  # No turn/agent stop or global effect barrier.
    await runtime.database.submit(actor, session.id, NewMessage(prompt="Keep coding", request_key=new_id()))
    assert len(calls) == 1
    await lease(runtime, session.id, actor, "release", client)
    await agent_request(runtime.database, scope, workspace(), {"action": "reload"}, writing=True)
    assert len(calls) == 2


async def test_agent_write_and_human_claim_have_one_winner(runtime, actor):
    session, scope = await running_browser(runtime, actor)
    outcomes = await asyncio.gather(
        reserve_agent(runtime.database, scope, starting=False),
        lease(runtime, session.id, actor, "claim", new_id()),
        return_exceptions=True,
    )
    assert sum(isinstance(value, Exception) for value in outcomes) == 1


async def test_unknown_dispatch_retains_fence_and_late_settlement_cannot_clear_new_operation(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    async def unknown(_workspace, body):
        raise TimeoutError("lost response after dispatch")
    monkeypatch.setattr("moyai.browser.guest_request", unknown)
    with pytest.raises(ToolFailed, match="uncertain"):
        await agent_request(runtime.database, scope, workspace(), {"action": "reload"}, writing=True)
    async with runtime.database.sessions() as db:
        old = await db.get(ComputerOperation, session.id)
        assert old and old.expires_at > time.time()
    with pytest.raises(HTTPException):
        await lease(runtime, session.id, actor, "claim", new_id())
    with pytest.raises(ToolFailed, match="uncertain outcome"):
        await reserve_agent(runtime.database, scope, starting=False)
    # Expiry is longer than the enforced guest deadline.
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerOperation, session.id)).expires_at = 0
    newer = await reserve_agent(runtime.database, scope, starting=False)
    await settle(runtime.database, session.id, old.identity)
    async with runtime.database.sessions() as db:
        assert (await db.get(ComputerOperation, session.id)).identity == newer
    await settle(runtime.database, session.id, newer)


async def test_expired_human_lease_cannot_bypass_an_agent_operation(runtime, actor):
    session, scope = await running_browser(runtime, actor)
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    stream = await bind(runtime, session.id, actor, client, controlling=True)
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerLease, session.id)).expires_at = 0
    identity = await reserve_agent(runtime.database, scope, starting=False)
    with pytest.raises(HTTPException):
        await lease(runtime, session.id, actor, "heartbeat", client)
    with pytest.raises(HTTPException):
        await validate(runtime, stream)
    await release(runtime, stream)
    async with runtime.database.sessions() as db:
        assert (await db.get(ComputerOperation, session.id)).identity == identity


async def test_agent_first_open_installs_guest_and_publishes_stream_readiness(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerDesktop, session.id)).version = "old"
    calls = []
    async def guest(_workspace, body):
        calls.append(body)
        return {"available": True, "tabs": [{"id": "target", "title": "Example"}]}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    await agent_request(runtime.database, scope, workspace(), {"action": "open", "url": "https://example.com"}, writing=True)
    assert [call["action"] for call in calls] == ["start", "browser"]
    async with runtime.database.sessions() as db:
        assert (await db.get(ComputerDesktop, session.id)).version == DESKTOP_VERSION
        assert await db.get(ComputerLease, session.id) is None
        assert await db.get(ComputerOperation, session.id) is None
    await validate(runtime, await bind(runtime, session.id, actor, new_id(), controlling=False))


async def test_confirmed_browser_failure_releases_only_its_operation(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    async def failed(*_args):
        raise DesktopCommandFailed()
    monkeypatch.setattr("moyai.browser.guest_request", failed)
    with pytest.raises(ToolFailed, match="returned an error"):
        await agent_request(runtime.database, scope, workspace(), {"action": "reload"}, writing=True)
    async with runtime.database.sessions() as db:
        assert await db.get(ComputerOperation, session.id) is None
    await lease(runtime, session.id, actor, "claim", new_id())


async def test_http_browser_viewer_cannot_write_and_stale_browser_cannot_steal_control(runtime, actor, monkeypatch):
    session = await ready_computer(runtime, actor)
    calls = []
    async def guest(_runtime, _session, body):
        calls.append(body)
        return {"tabs": []}
    monkeypatch.setattr("moyai.browser.request", guest)
    first, other = new_id(), new_id()
    async with sign_in(runtime) as client:
        path = f"/api/sessions/{session.id}/browser"
        assert (await client.get(path)).status_code == 200
        assert (await client.post(path, json={"action": "reload", "client_id": first})).status_code == 409
        await lease(runtime, session.id, actor, "claim", first)
        assert (await client.post(path, json={"action": "reload", "client_id": other})).status_code == 409
        assert (await client.post(path, json={"action": "reload", "client_id": first})).status_code == 200
        assert len(calls) == 2
        assert (await client.post(path, json={"action": "evaluate", "client_id": first})).status_code == 422
        await lease(runtime, session.id, actor, "release", first)
        assert (await client.post(path, json={"action": "reload", "client_id": first})).status_code == 409
