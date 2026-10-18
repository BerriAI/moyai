"""SDK/database regressions for desktop control; Linux smoke covers the real guest."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from test_shared_browser import running_browser, workspace

from moyai.computer import lease
from moyai.effects import EffectReceipts, effect_id, requires_receipt
from moyai.models import ComputerDesktop, Effect, new_id
from moyai.tools.desktop import DesktopAction, desktop_capability


async def invocation(runtime, scope):
    ctx = SimpleNamespace(deps=scope, run_id=scope.turn_id, tool_call_id=new_id(), workspace=workspace())
    command = DesktopAction(action="click", app="Test", observation=new_id(), element_index="4")
    call = ToolCallPart("desktop_action", {"command": command}, tool_call_id=ctx.tool_call_id)
    definition = ToolDefinition(name="desktop_action")
    receipts = EffectReceipts(runtime.database)
    await receipts.before_tool_execute(ctx, call=call, tool_def=definition, args={"command": command})
    tools = {tool.name: tool.function for tool in desktop_capability(runtime.database).tools}
    return ctx, command, call, definition, receipts, tools


async def finish(items, value):
    ctx, command, call, definition, receipts, _ = items
    return await receipts.after_tool_execute(
        ctx, call=call, tool_def=definition, args={"command": command}, result=value,
    )


async def test_desktop_takeover_blocks_input_but_read_is_available(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    await lease(runtime, session.id, actor, "claim", new_id())
    dispatched = []
    async def guest(_workspace, body):
        dispatched.append(body)
        return {"text": "Observed", "images": []}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    value = await items[-1]["desktop_action"](items[0], items[1])
    with pytest.raises(ToolFailed, match="person has control"):
        await finish(items, value)
    assert not dispatched
    read = await items[-1]["desktop_read"](items[0], app="Test")
    assert read.return_value["text"] == "Observed"
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, effect_id(items[0], items[2]))).state == "not_dispatched"


async def test_desktop_uncertain_action_is_not_replayed_and_can_be_inspected(runtime, actor, monkeypatch):
    _, scope = await running_browser(runtime, actor)
    calls = []
    async def guest(_workspace, body):
        calls.append(body)
        if body["command"]["action"] == "read":
            return {"text": "Current state", "images": []}
        raise TimeoutError("lost reply")
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    for _ in range(2):
        value = await items[-1]["desktop_action"](items[0], items[1])
        with pytest.raises(ToolFailed, match="desktop outcome is uncertain"):
            await finish(items, value)
    assert len(calls) == 1
    assert (await items[-1]["desktop_read"](items[0], app="Test")).return_value["text"] == "Current state"
    with pytest.raises(ToolFailed, match="unconfirmed outcome"):
        await invocation(runtime, scope)


async def test_desktop_observation_context_changes_after_takeover(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    contexts = []
    async def guest(_workspace, body):
        contexts.append(body["command"]["context"])
        return {"text": "Current state", "images": []}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    read = items[-1]["desktop_read"]
    await read(items[0], app="Test")
    client = new_id()
    await lease(runtime, session.id, actor, "claim", client)
    await lease(runtime, session.id, actor, "release", client)
    await read(items[0], app="Test")
    assert contexts[0] != contexts[1]


async def test_helper_upgrade_does_not_require_a_write_to_inspect_pending_action(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerDesktop, session.id)).version = "previous-helper"
    calls = []
    async def guest(_workspace, body):
        calls.append(body)
        return {"text": "Current state", "images": []}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    result = await items[-1]["desktop_read"](items[0], app="Test")
    assert result.return_value["text"] == "Current state"
    assert [body["action"] for body in calls] == ["desktop"]


async def test_explicit_start_reinitializes_an_existing_computer(runtime, actor, monkeypatch):
    _, scope = await running_browser(runtime, actor)
    calls = []
    async def guest(_workspace, body):
        calls.append(body)
        return {"available": True, "text": "Apps", "images": []}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    result = await items[-1]["desktop_action"](items[0], DesktopAction(action="start"))
    await finish(items, result)
    assert [body["action"] for body in calls] == ["start", "desktop"]


@pytest.mark.parametrize("args", [
    {"action": "click", "app": "Test"},
    {"action": "click", "app": "Test", "observation": "seen"},
    {"action": "shell", "text": "arbitrary code"},
    {"action": "set_value", "app": "Test", "observation": "seen", "element_index": "1"},
])
def test_desktop_requires_observed_targets(args):
    with pytest.raises(ValidationError):
        DesktopAction.model_validate(args)


def test_desktop_observations_do_not_reserve_external_writes():
    assert requires_receipt(ToolDefinition(name="desktop_action"))
    assert not requires_receipt(ToolDefinition(name="desktop_read"))
