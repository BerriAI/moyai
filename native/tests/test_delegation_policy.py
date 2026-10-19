"""Instructions, advertised tools and forced calls share one delegation policy."""

from dataclasses import replace

import pytest
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from moyai.contracts import RunScope
from moyai.delegation import InteractiveDelegation


@pytest.mark.parametrize(("interactive", "swarm", "depth", "expected"), [
    pytest.param(False, False, 0, "legacy", id="historical-root"),
    pytest.param(False, False, 2, "legacy", id="historical-sdk-depth"),
    pytest.param(True, False, 0, "interactive", id="ordinary-root"),
    pytest.param(True, False, 1, "interactive", id="ordinary-child"),
    pytest.param(True, False, 2, "disabled", id="grandchild"),
    pytest.param(True, False, 3, "disabled", id="beyond-depth-limit"),
    pytest.param(True, True, 0, "disabled", id="swarm-member"),
    pytest.param(True, True, 1, "disabled", id="swarm-with-child-context"),
])
async def test_delegation_contract_is_consistent_even_for_unadvertised_calls(
    runtime, monkeypatch, interactive, swarm, depth, expected,
):
    scope = RunScope(
        "session", "turn", "actor", "test", interactive_delegation=interactive,
        swarm_id="mission" if swarm else None, delegation_depth=depth,
    )
    ctx = RunContext(deps=scope, run_id=scope.turn_id, model=TestModel(), usage=RunUsage())
    capability = InteractiveDelegation(runtime.database)
    starts, forwards = [], []

    async def start(scope, call_id, task, input_id, *, messages, ctx):
        assert input_id == scope.turn_id
        assert messages is ctx.messages
        starts.append((scope, call_id, task))
        return {"session_id": "child", "name": "Child"}

    async def report(scope, session_id):
        return {"session_id": session_id, "status": "completed", "output": "Child result."}

    async def handler(args):
        forwards.append(args)
        return "SDK result."

    monkeypatch.setattr(capability, "start", start)
    monkeypatch.setattr(capability, "report", report)
    delegate, other = ToolDefinition(name="delegate_task"), ToolDefinition(name="read_file")
    definitions = await capability.prepare_tools(ctx, [delegate, other])
    assert (delegate in definitions) is (expected != "disabled")
    assert other in definitions
    assert bool(capability.get_instructions()(ctx)) is (expected == "interactive")

    args = {"agent_name": "self", "task": "A bounded assignment."}
    # Availability is a model-facing hint. Stale, forged or directly invoked
    # calls must obey the same policy before admitting work or invoking the SDK.
    call = ToolCallPart("delegate_task", args, tool_call_id="delegate")
    if expected == "disabled":
        with pytest.raises(ToolFailed, match="not available"):
            await capability.wrap_tool_execute(
                ctx, call=call, tool_def=delegate, args=args, handler=handler,
            )
        assert not starts and not forwards
    else:
        await capability.wrap_tool_execute(ctx, call=call, tool_def=delegate, args=args, handler=handler)
        assert len(starts) == (expected == "interactive")
        assert len(forwards) == (expected == "legacy")

    # The policy must never intercept an unrelated tool, even in a child run.
    starts.clear()
    forwards.clear()
    await capability.wrap_tool_execute(
        replace(ctx, run_id="sdk-child"), call=ToolCallPart("read_file", {}, tool_call_id="read"),
        tool_def=other, args={}, handler=handler,
    )
    assert not starts and forwards == [{}]


@pytest.mark.parametrize("changes", [
    {"agent_name": "another-agent"}, {"model": "another-model"},
    {"resume": "another-run"}, {"background": True}, {"task": " "},
])
async def test_unsupported_delegation_arguments_cannot_admit_work(runtime, monkeypatch, changes):
    scope = RunScope("session", "turn", "actor", "test", interactive_delegation=True)
    ctx = RunContext(deps=scope, run_id=scope.turn_id, model=TestModel(), usage=RunUsage())
    capability = InteractiveDelegation(runtime.database)

    async def forbidden(*args):
        pytest.fail("Unsupported delegation reached admission or the SDK handler.")

    monkeypatch.setattr(capability, "start", forbidden)
    args = {"agent_name": "self", "task": "Bounded work.", **changes}
    with pytest.raises(ToolFailed):
        await capability.wrap_tool_execute(
            ctx, call=ToolCallPart("delegate_task", args, tool_call_id="delegate"),
            tool_def=ToolDefinition(name="delegate_task"), args=args, handler=forbidden,
        )
