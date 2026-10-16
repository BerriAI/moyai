"""Native SDK + database regressions. Guest doubles are not live browser proof."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Capability, LocalWorkspace
from pydantic_ai.durable_exec._toolset import CallToolResult, unwrap_tool_call_result, wrap_tool_call_result
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, TemporalDurability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturn, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.tools import Tool, ToolDefinition
from pydantic_ai.workspaces import LocalWorkspaceBackend, WorkspaceRef
from sqlalchemy import select
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_shared_browser import running_browser, workspace

from moyai.agent import HarnessActivityContext
from moyai.browser import BrowserAction, browser_capability
from moyai.browser_outcomes import BROWSER_OUTCOME, browser_result
from moyai.computer import DesktopCommandFailed, lease
from moyai.contracts import NewMessage, NewSession, RunScope
from moyai.effects import EffectReceipts, claim_browser_dispatch, effect_id, requires_receipt
from moyai.models import ComputerOperation, Effect, User, new_id
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.workflow import TurnWorkflow


async def invocation(runtime, scope, name="browser_action", call_id=None):
    ctx = SimpleNamespace(
        deps=scope, run_id=scope.turn_id, tool_call_id=call_id or new_id(), workspace=workspace(),
    )
    args = (
        {"url": "https://example.test", "new_tab": True} if name == "browser_open"
        else {"command": BrowserAction(action="click", role="button", name="Submit")}
    )
    call = ToolCallPart(name, args, tool_call_id=ctx.tool_call_id)
    definition = ToolDefinition(name=name)
    receipt = EffectReceipts(runtime.database)
    await receipt.before_tool_execute(ctx, call=call, tool_def=definition, args=args)
    function = next(tool.function for tool in browser_capability(runtime.database).tools if tool.name == name)
    return ctx, args, call, definition, receipt, function


async def finish(items, result):
    ctx, args, call, definition, receipt, _ = items
    return await receipt.after_tool_execute(ctx, call=call, tool_def=definition, args=args, result=result)


async def saved(runtime, items):
    async with runtime.database.sessions() as db:
        return await db.get(Effect, effect_id(items[0], items[2]))


def test_browser_writes_require_receipts_but_inspection_does_not():
    assert requires_receipt(ToolDefinition(name="browser_open"))
    assert requires_receipt(ToolDefinition(name="browser_action"))
    assert not requires_receipt(ToolDefinition(name="browser_read"))
    assert requires_receipt(ToolDefinition(name="shell"))


@pytest.mark.parametrize("name", ["browser_open", "browser_action"])
async def test_human_refusal_is_not_dispatched_and_does_not_block_coding(runtime, actor, monkeypatch, name):
    session, scope = await running_browser(runtime, actor)
    client_id = new_id()
    await lease(runtime, session.id, actor, "claim", client_id)
    calls = []
    async def guest(*args):
        calls.append(args)
        return {}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope, name)
    result = await items[-1](items[0], **items[1])
    with pytest.raises(ToolFailed, match="person has control"):
        await finish(items, result)
    assert not calls and (await saved(runtime, items)).state == "not_dispatched"
    await lease(runtime, session.id, actor, "release", client_id)
    repeated = await items[-1](items[0], **items[1])
    with pytest.raises(ToolFailed, match="already refused"):
        await finish(items, repeated)
    assert not calls and (await saved(runtime, items)).state == "not_dispatched"
    await items[4].reserve(scope, "independent-coding", "shell", {"command": "write a local test file"})


@pytest.mark.parametrize("failure", ["timeout", "guest_error"])
async def test_unknown_web_write_survives_operation_expiry_and_blocks_new_ids(runtime, actor, monkeypatch, failure):
    session, scope = await running_browser(runtime, actor)
    calls = []
    async def guest(*args):
        calls.append(args)
        raise TimeoutError("response lost") if failure == "timeout" else DesktopCommandFailed()
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    result = await items[-1](items[0], **items[1])
    with pytest.raises(ToolFailed, match="uncertain"):
        await finish(items, result)
    async with runtime.database.sessions.begin() as db:
        operation = await db.get(ComputerOperation, session.id)
        if operation:
            operation.expires_at = 0
    assert (await saved(runtime, items)).state == "pending"
    with pytest.raises(PermissionError, match="reconciliation"):
        await invocation(runtime, scope)
    # Redelivery of the original activity also cannot repeat the guest action.
    repeated = await items[-1](items[0], **items[1])
    with pytest.raises(ToolFailed, match="uncertain"):
        await finish(items, repeated)
    assert len(calls) == 1 and (await saved(runtime, items)).state == "pending"


async def test_refusal_from_redelivery_cannot_clear_an_earlier_unknown_attempt(runtime, actor, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    async def guest(*args):
        raise TimeoutError("response lost")
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    await items[-1](items[0], **items[1])
    async with runtime.database.sessions.begin() as db:
        (await db.get(ComputerOperation, session.id)).expires_at = 0
    await lease(runtime, session.id, actor, "claim", new_id())
    refused = await items[-1](items[0], **items[1])
    assert refused.metadata[BROWSER_OUTCOME] == "not_dispatched"
    with pytest.raises(ToolFailed, match="uncertain"):
        await finish(items, refused)
    assert (await saved(runtime, items)).state == "pending"


async def test_confirmed_result_survives_cleanup_failure_and_cannot_be_dispatched_again(runtime, actor, monkeypatch):
    _, scope = await running_browser(runtime, actor)
    calls = []
    async def guest(*args):
        calls.append(args)
        return {"tabs": [], "completed": True}
    async def failed_cleanup(*args, **kwargs):
        raise RuntimeError("cleanup unavailable after success")
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    monkeypatch.setattr("moyai.browser.settle", failed_cleanup)
    items = await invocation(runtime, scope)
    result = await items[-1](items[0], **items[1])
    assert (await saved(runtime, items)).state == "confirmed"
    with pytest.raises(ToolFailed, match="completed"):
        await finish(items, result)
    assert not await items[4].not_dispatched(effect_id(items[0], items[2]))
    with pytest.raises(ToolFailed):
        await claim_browser_dispatch(runtime.database, scope, effect_id(items[0], items[2]), "browser_action")
    assert len(calls) == 1


async def test_cancellation_after_dispatch_keeps_durable_uncertainty(runtime, actor, monkeypatch):
    _, scope = await running_browser(runtime, actor)
    entered = asyncio.Event()
    async def guest(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    task = asyncio.create_task(items[-1](items[0], **items[1]))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await saved(runtime, items)).state == "pending"
    assert not await items[4].not_dispatched(effect_id(items[0], items[2]))


async def test_competing_workers_cannot_claim_the_same_browser_attempt(runtime, actor):
    _, scope = await running_browser(runtime, actor)
    items = await invocation(runtime, scope)
    identity = effect_id(items[0], items[2])
    outcomes = await asyncio.gather(*(
        claim_browser_dispatch(runtime.database, scope, identity, "browser_action") for _ in range(2)
    ), return_exceptions=True)
    assert sum(value is None for value in outcomes) == 1
    assert sum(isinstance(value, Exception) for value in outcomes) == 1


async def test_lost_reservation_ack_is_known_before_tool_dispatch(runtime, actor, monkeypatch):
    _, scope = await running_browser(runtime, actor)
    receipt = EffectReceipts(runtime.database)
    original = receipt.reserve
    async def lost_ack(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("reservation response lost before the tool activity was scheduled")
    monkeypatch.setattr(receipt, "reserve", lost_ack)
    ctx = SimpleNamespace(deps=scope, run_id=scope.turn_id)
    call = ToolCallPart("browser_open", {"url": "https://example.test"}, tool_call_id="reservation-ack")
    with pytest.raises(ToolFailed, match="did not execute"):
        await receipt.before_tool_execute(
            ctx, call=call, tool_def=ToolDefinition(name="browser_open"), args=call.args,
        )
    async with runtime.database.sessions() as db:
        assert (await db.get(Effect, effect_id(ctx, call))).state == "not_dispatched"


async def test_received_success_is_confirmed_even_when_local_authority_changes(runtime, actor, monkeypatch):
    _, scope = await running_browser(runtime, actor)
    async def guest(*args):
        async with runtime.database.sessions.begin() as db:
            (await db.get(User, actor.id)).enabled = False
        return {"private_result": "must not be delivered after revocation"}
    monkeypatch.setattr("moyai.browser.guest_request", guest)
    items = await invocation(runtime, scope)
    result = await items[-1](items[0], **items[1])
    assert "private_result" not in str(result.return_value)
    assert (await saved(runtime, items)).state == "confirmed"
    with pytest.raises(ToolFailed, match="completed"):
        await finish(items, result)


async def test_native_temporal_result_codec_preserves_trusted_outcome_metadata():
    async def value():
        return browser_result("not_dispatched", "Human control blocks this write.")
    wrapped = await wrap_tool_call_result(value())
    codec = TypeAdapter(CallToolResult)
    decoded = unwrap_tool_call_result(codec.validate_json(codec.dump_json(wrapped)))
    assert isinstance(decoded, ToolReturn)
    assert decoded.metadata[BROWSER_OUTCOME] == "not_dispatched"


async def test_real_sdk_hook_order_handles_refusal_then_coding(runtime, actor, tmp_path, monkeypatch):
    session, scope = await running_browser(runtime, actor)
    await lease(runtime, session.id, actor, "claim", new_id())
    class FixtureBackend(LocalWorkspaceBackend):
        @property
        def ref(self):
            return WorkspaceRef(provider="modal", id="scoped-computer")
    class FixtureWorkspace(LocalWorkspace):
        def get_workspace(self, ctx, *, ref):
            return FixtureBackend(self.working_dir)
    requests = []
    def model(messages, info):
        requests.append(messages)
        if len(requests) == 1:
            return ModelResponse(parts=[ToolCallPart(
                "browser_open", {"url": "https://example.test"}, tool_call_id="blocked-browser",
            )])
        if len(requests) == 2:
            return ModelResponse(parts=[ToolCallPart("shell", {"command": "safe local coding"}, tool_call_id="coding")])
        return ModelResponse(parts=[TextPart("Coding continues.")])
    agent = Agent(
        FunctionModel(model), deps_type=type(scope),
        capabilities=[FixtureWorkspace(tmp_path), EffectReceipts(runtime.database), browser_capability(runtime.database)],
    )
    @agent.tool
    async def shell(ctx: RunContext[RunScope], command: str) -> str:
        return command
    result = await agent.run("Exercise native tool hooks.", deps=scope)
    assert result.output == "Coding continues."
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Effect).where(Effect.session_id == session.id).order_by(Effect.created_at)))
    assert [(row.tool_name, row.state) for row in rows] == [
        ("browser_open", "not_dispatched"), ("shell", "confirmed"),
    ]


@pytest.mark.parametrize("old_history", [False, True])
async def test_native_temporal_browser_receipts_and_pre_guard_history_replay(runtime, actor, monkeypatch, old_history):
    """Actual SDK/Temporal history with a controlled dispatch, not browser E2E."""
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Controlled browser dispatch", request_key="browser-replay"),
    )
    await runtime.database.claim_next()
    calls = []
    async def controlled_request(database, scope, workspace, command, *, writing, before_dispatch=None, after_dispatch=None):
        if before_dispatch:
            await before_dispatch()
        calls.append(command)
        if after_dispatch:
            await after_dispatch()
        return {"tabs": [], "observed": True}
    monkeypatch.setattr("moyai.browser.agent_request", controlled_request)

    def respond(messages, info):
        if any(isinstance(part, ToolReturnPart) and part.tool_name == "browser_open"
               for message in messages for part in message.parts):
            return ModelResponse(parts=[TextPart("Controlled browser completed.")])
        return ModelResponse(parts=[ToolCallPart(
            "browser_open", {"url": "https://example.test", "new_tab": True}, tool_call_id="durable-browser",
        )])
    async def stream(messages, info):
        for part in respond(messages, info).parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args), tool_call_id=part.tool_call_id)}
    class BeforeBrowserReceipts(EffectReceipts):
        async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
            return await handler(args)
        async def prepare_tools(self, ctx, tool_defs):
            return tool_defs
        async def before_tool_execute(self, ctx, *, call, tool_def, args):
            return args
        async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
            return result
    async def old_open(ctx: RunContext[RunScope], url: str, new_tab: bool = True) -> dict:
        return await controlled_request(
            runtime.database, ctx.deps, ctx.workspace, {"url": url, "new_tab": new_tab}, writing=True,
        )
    runtime.models = {"test": FunctionModel(respond, stream_function=stream)}
    def build(legacy):
        runtime.durability = TemporalDurability(models=runtime.models, run_context_type=HarnessActivityContext)
        return Agent(runtime.models["test"], name="moyai", deps_type=RunScope, capabilities=[
            LocalWorkspace(runtime.settings.data_dir.parent / "computer"),
            (BeforeBrowserReceipts if legacy else EffectReceipts)(runtime.database),
            Capability(id="moyai_browser", tools=[Tool(old_open, name="browser_open", sequential=True)])
            if legacy else browser_capability(runtime.database),
            runtime.durability,
        ])
    runtime.agent = build(old_history)
    converter = encrypted_converter(runtime.security, runtime.objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            execution = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            assert (await asyncio.wait_for(execution.result(), 30))["status"] == "completed"
            history = await execution.fetch_history()
        runtime.agent = build(False)
        TurnWorkflow.__pydantic_ai_agents__ = [runtime.agent]
        await Replayer(
            workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    assert len(calls) == 1
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Effect).where(Effect.session_id == session.id)))
    assert [row.state for row in rows] == ([] if old_history else ["confirmed"])
