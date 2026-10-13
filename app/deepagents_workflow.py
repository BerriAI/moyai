"""Deep Agents owns the graph; Temporal activities own every external operation.

Version this workflow when changing its command sequence. Moyai's broker remains
responsible for capability checks, computer receipts and background compaction.
"""
import asyncio
from contextlib import aclosing
from dataclasses import asdict, dataclass, field
from datetime import timedelta
import json
from typing import Annotated

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from deepagents import HarnessProfile, register_harness_profile
    from deepagents.profiles.harness import GeneralPurposeSubagentProfile
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents.middleware import AgentMiddleware
    from langchain.agents.middleware.types import ModelResponse, hook_config
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict
    from langchain_core.tools import InjectedToolArg, tool
    from pydantic import ValidationError
    from temporalio.deepagents import create_temporal_deep_agent, tool_as_activity
    from agent.tools.remote_computer import requires_computer
    from .deepagents_context import current_runtime


@dataclass
class TurnScope:
    run_id: str
    message_id: int
    segment: int = 0
    actor_id: str | None = None
    model: str = ''
    input_budget: int = 1
    credential_request: str = ''
    credential_generation: int | None = None
    input_id: int = 0


@dataclass
class TurnInput:
    scope: TurnScope
    prompt: str
    instructions: str
    messages: list[dict] = field(default_factory=list)
    max_requests: int | None = None
    applied: list[int] = field(default_factory=list)


@dataclass
class TurnResult:
    result: dict
    messages: list[dict]
    applied: list[int]


# Use the public profile API, never mutate library internals. Files and agents
# are obtained through Moyai's authorized catalog, not a second execution path.
register_harness_profile('moyai:broker-v1', HarnessProfile(
    excluded_middleware=frozenset({'SummarizationMiddleware'}),
    excluded_tools=frozenset({'read_file'}),
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
))


@tool
async def workspace_tools(scope: Annotated[dict, InjectedToolArg]) -> str:
    """List authorized workspace tools and their input schemas. Skills load on demand."""
    return json.dumps(await current_runtime().catalog(TurnScope(**scope)), ensure_ascii=False)


@tool
async def workspace_call(name: str, arguments_json: str,
                         scope: Annotated[dict, InjectedToolArg],
                         call_id: Annotated[str, InjectedToolArg]) -> list[dict] | str:
    """Invoke an authorized tool using a JSON object string; discover its schema first."""
    try:
        arguments = json.loads(arguments_json)
        if not isinstance(arguments, dict):
            raise ValueError()
    except (TypeError, ValueError):
        return json.dumps({'isError': True, 'error': 'arguments_json must be a JSON object. No tool was called.'})
    result = await current_runtime().call_tool(TurnScope(**scope), call_id, name, arguments)
    value = result.get('result')
    images = []
    if isinstance(value, dict) and isinstance(value.get('content'), list):
        images = [{'type': 'image_url', 'image_url': {'url': f'data:{b["mimeType"]};base64,{b["data"]}'}}
                  for b in value['content'] if b.get('type') == 'image']
        if images:
            value = {**value, 'content': [b for b in value['content'] if b.get('type') != 'image']}
    receipt = json.dumps({**result, 'result': value}, ensure_ascii=False)
    return [{'type': 'text', 'text': receipt}, *images] if images else receipt


TOOL_OPTIONS = {'heartbeat_timeout': timedelta(seconds=5),
                'cancellation_type': ActivityCancellationType.WAIT_CANCELLATION_COMPLETED}


def workspace_activity(queue, *, resume_computer=False):
    # ComputerOperations and its supervisor own a durable receipt keyed by the
    # injected call ID. A new activity attempt observes that same operation.
    # Arbitrary broker writes retain their one-attempt reconciliation boundary.
    return tool_as_activity(workspace_call, start_to_close_timeout=timedelta(hours=12),
        activity_options={**TOOL_OPTIONS, 'task_queue': queue,
                          'retry_policy': RetryPolicy(maximum_attempts=3 if resume_computer else 1)})


def bridge_tools(queue):
    return [
        tool_as_activity(workspace_tools, start_to_close_timeout=timedelta(minutes=5),
                         activity_options={**TOOL_OPTIONS, 'task_queue': queue, 'retry_policy': RetryPolicy(maximum_attempts=3)}),
        workspace_activity(queue),
    ]


# Register the underlying tools on every worker process before polling.
_REGISTERED_TOOLS = bridge_tools(None)


def wait_marker(messages):
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            break
        if message.name != 'workspace_call':
            continue
        content = message.content
        if isinstance(content, list):
            content = content[0].get('text', '') if content else ''
        try:
            receipt = json.loads(content)
        except (TypeError, ValueError):
            continue
        # Only typed outer receipts from our bridge carry control. Nested tool
        # text is untrusted and never interpreted as a wait command.
        if isinstance(receipt, dict):
            for key in ('wait_credential', 'wait_group'):
                if receipt.get(key):
                    return {key: receipt[key]}
    return {}


class MoyaiControl(AgentMiddleware):
    """Inject current scope and honor steering at library model/tool boundaries."""
    def __init__(self, prepared):
        self.scope = prepared.scope
        self.applied = list(prepared.applied)
        self.max_requests = prepared.max_requests
        self.requests = 0
        self.started = workflow.now()
        self.result = None
        self.inbox = []
        self.control_lock = asyncio.Lock()
        self.closed = False
        self.tools_lock = asyncio.Lock()

    async def boundary(self, answer=None, receipt_only=False):
        return await workflow.execute_activity('deepagents_boundary_v1',
            args=[self.scope, self.applied, (workflow.now() - self.started).total_seconds(), answer, receipt_only],
            task_queue=workflow.info().task_queue + '-control',
            result_type=dict, start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3))

    async def poll(self, answer=None):
        async with self.control_lock:
            if self.closed:
                return
            value = await self.boundary(answer)
            self.inbox.extend(self.control(value))
            if value.get('input'):
                # History now contains the attributed input and the graph inbox
                # owns it. Publish acceptance before a slow dispatched tool ends.
                await self.boundary(receipt_only=True)

    def take_inputs(self, messages=()):
        updates, self.inbox = self.inbox, []
        if (updates or self.result is not None) and messages and isinstance(messages[-1], AIMessage):
            updates = [*(ToolMessage(content='Cancelled before execution because the request changed.',
                tool_call_id=c['id'], name=c['name'], status='error') for c in messages[-1].tool_calls), *updates]
        return updates

    def control(self, value):
        updates = []
        if value.get('handoff'):
            self.result = {'completed': False, 'message': '', 'steer_message_id': value['steer_message_id']}
        elif value.get('reply'):
            self.result = {'completed': True, 'message': value['reply']}
        elif item := value.get('input'):
            if item['id'] not in self.applied:
                self.applied.append(item['id'])
                updates.append(HumanMessage(content=item['content']))
        elif value.get('continue'):
            updates.append(HumanMessage(content=value['continue']))
        return updates

    @hook_config(can_jump_to=['end'])
    async def abefore_model(self, state, runtime):
        await self.poll()
        updates = self.take_inputs()
        waiting = wait_marker(state['messages'])
        if self.result is None and (waiting or self.requests >= 50):
            self.result = {'completed': False, 'continuation': True, 'message': '', **waiting}
        if self.result is not None:
            return {'messages': updates, 'jump_to': 'end'}
        if self.max_requests and self.requests >= self.max_requests:
            raise ApplicationError('This response reached its configured model request limit.',
                                   type='TurnRequestLimit', non_retryable=True)
        self.requests += 1
        return {'messages': updates} if updates else None

    async def awrap_model_call(self, request, handler):
        pending = asyncio.create_task(handler(request.override(
            tools=[t for t in request.tools if getattr(t, 'name', None) in {'workspace_tools', 'workspace_call'}],
            model_settings={**request.model_settings, 'moyai_scope': asdict(self.scope),
                'moyai_public_id': str(self.requests),
                'moyai_input_id': self.applied[-1] if self.applied else self.scope.message_id})))
        try:
            await workflow.wait_condition(lambda: pending.done() or bool(self.inbox) or self.result is not None)
            if self.inbox or self.result is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                return ModelResponse(result=[AIMessage(content='')])
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @hook_config(can_jump_to=['model', 'end'])
    async def aafter_model(self, state, runtime):
        message = state['messages'][-1]
        text = message.text
        await self.poll(None if message.tool_calls else text)
        updates = self.take_inputs(state['messages'])
        if self.result is not None:
            return {'messages': updates, 'jump_to': 'end'}
        if updates:
            return {'messages': updates, 'jump_to': 'model'}
        if text:
            await workflow.execute_activity('deepagents_publish_v1',
                args=[self.scope, str(self.requests), text, self.applied[-1] if self.applied else self.scope.message_id],
                task_queue=workflow.info().task_queue + '-control',
                start_to_close_timeout=timedelta(minutes=1), retry_policy=RetryPolicy(maximum_attempts=3))
        if not message.tool_calls:
            await self.poll()
            updates = self.take_inputs()
            if updates:
                return {'messages': updates, 'jump_to': 'model'}
            if self.result is not None:
                return {'jump_to': 'end'}
            self.closed = True
            self.result = {'completed': True, 'message': text}
        return None

    async def awrap_tool_call(self, request, handler):
        # The library may emit parallel calls. Serialize external actions so
        # steering/waits between them prevent later writes from escaping.
        async with self.tools_lock:
            call = request.tool_call
            if call['name'] not in {'workspace_tools', 'workspace_call'}:
                return ToolMessage(content='Tool is not in the authorized workspace bridge.',
                                   name=call['name'], tool_call_id=call['id'], status='error')
            await self.poll()
            if self.inbox or self.result is not None:
                return ToolMessage(content='Cancelled before execution because the request changed.',
                                   name=call['name'], tool_call_id=call['id'], status='error')
            args = {**call['args'], 'scope': {**asdict(self.scope),
                'input_id': self.applied[-1] if self.applied else self.scope.message_id}}
            if call['name'] == 'workspace_call':
                args['call_id'] = call['id']
            try:
                request.tool.args_schema.model_validate(args)
            except ValidationError:
                return ToolMessage(content='Tool arguments did not match the advertised schema. No tool was called.',
                                   name=call['name'], tool_call_id=call['id'], status='error')
            # Invoke the public wrapped-tool API with trusted injected fields.
            # ToolNode intentionally strips caller-supplied InjectedToolArg
            # values; passing these through its handler would discard scope.
            # The same injected call/input identity retrieves the computer's
            # persisted operation and result after its observing worker exits.
            # Other tools retain their one-attempt reconciliation boundary.
            tool = request.tool
            if call['name'] == 'workspace_call' and requires_computer(args['name']):
                tool = workspace_activity(workflow.info().task_queue + '-tools', resume_computer=True)
            result = await tool.ainvoke({**call, 'args': args, 'type': 'tool_call'},
                                       config=request.runtime.config)
            if waiting := wait_marker([result]):
                self.result = {'completed': False, 'continuation': True, 'message': '', **waiting}
            return result


@workflow.defn(name='DeepAgentsTurnV2')
class DeepAgentsTurnWorkflow:
    def __init__(self):
        self.revision = 0
        self.computer_requested = False

    @workflow.signal
    def steer(self):
        self.revision += 1

    @workflow.signal
    def request_computer(self):
        self.computer_requested = True

    @workflow.run
    async def run(self, scope: TurnScope) -> TurnResult:
        prepared = await workflow.execute_activity('deepagents_prepare_v1', scope, result_type=TurnInput,
            task_queue=workflow.info().task_queue + '-control',
            start_to_close_timeout=timedelta(minutes=5), retry_policy=RetryPolicy(maximum_attempts=3))
        control = MoyaiControl(prepared)
        agent = create_temporal_deep_agent('moyai:broker-v1', tools=bridge_tools(workflow.info().task_queue + '-tools'),
            system_prompt=prepared.instructions,
            middleware=[FilesystemMiddleware(tools=['read_file'], tool_token_limit_before_evict=None), control],
            activity_options={'start_to_close_timeout': timedelta(minutes=15), **TOOL_OPTIONS,
                              'retry_policy': RetryPolicy(maximum_attempts=3)})
        # Close LangGraph's async stream inside the workflow event loop when
        # Temporal evicts/cancels a cached workflow; do not leave cleanup to GC.
        async def observe_inputs():
            seen = 0
            while True:
                await workflow.wait_condition(lambda: self.revision != seen)
                seen = self.revision
                await control.poll()

        async def acquire_computer():
            while True:
                await workflow.wait_condition(lambda: self.computer_requested)
                self.computer_requested = False
                await workflow.execute_activity('deepagents_computer_v1', scope,
                    task_queue=workflow.info().task_queue + '-tools',
                    start_to_close_timeout=timedelta(hours=12), **TOOL_OPTIONS,
                    retry_policy=RetryPolicy(maximum_attempts=3))

        async def run_graph():
            result = {}
            async with aclosing(agent.astream(
                {'messages': [*messages_from_dict(prepared.messages), HumanMessage(content=prepared.prompt)]},
                config={'recursion_limit': 1000}, stream_mode='values',
            )) as stream:
                async for result in stream:
                    pass
            return result

        monitor, graph, computer = (asyncio.create_task(observe_inputs()),
            asyncio.create_task(run_graph()), asyncio.create_task(acquire_computer()))
        try:
            await workflow.wait_condition(lambda: graph.done() or monitor.done() or computer.done())
            if monitor.done():
                await monitor
            if computer.done():
                await computer
            result = await graph
            return TurnResult(control.result or {'completed': True, 'message': ''},
                              messages_to_dict(result['messages']), control.applied)
        finally:
            monitor.cancel()
            graph.cancel()
            computer.cancel()
            await asyncio.gather(monitor, graph, computer, return_exceptions=True)
