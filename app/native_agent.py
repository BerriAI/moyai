"""Moyai built on the public Deep Agents SDK.

This graph has no dependency on Moyai's harness adapters or Temporal. The host
supplies authorized services; Deep Agents owns tools, files, delegation and
summarization. The notebook is graph state, never the web server's filesystem.
"""
import asyncio
import json

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from jsonschema import Draft202012Validator, ValidationError
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain.agents.middleware.types import ModelResponse, hook_config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import ConfigDict


def tool_content(value):
    """Keep images as native LangChain content blocks instead of JSON strings."""
    if not isinstance(value, dict) or not isinstance(value.get('content'), list):
        return json.dumps(value, ensure_ascii=False)
    blocks = []
    for block in value['content']:
        if block.get('type') == 'image':
            blocks.append({'type': 'image_url', 'image_url': {
                'url': f'data:{block["mimeType"]};base64,{block["data"]}'}})
        elif block.get('type') == 'text':
            blocks.append({'type': 'text', 'text': block['text']})
    return blocks or json.dumps(value, ensure_ascii=False)


class Session:
    """Per-execution control shared by the parent and SDK-created subagents."""
    def __init__(self, services, prepared, *, wait_for_model=None):
        self.services = services
        self.prepared = prepared
        self.applied = list(prepared.applied)
        self.inbox = []
        self.result = None
        self.requests = 0
        self.dispatches = 0
        self.activities = 0
        self.root_requests = 0
        self.control_lock = asyncio.Lock()
        self.action_lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.closed = False
        self.wait_for_model = wait_for_model or self._wait_for_model

    async def _wait_for_model(self, pending):
        changed = asyncio.create_task(self.changed.wait())
        try:
            await asyncio.wait([pending, changed], return_when=asyncio.FIRST_COMPLETED)
        finally:
            changed.cancel()
            await asyncio.gather(changed, return_exceptions=True)

    @property
    def input_id(self):
        return self.applied[-1] if self.applied else self.prepared.scope.message_id

    async def poll(self, answer=None):
        async with self.control_lock:
            if self.closed:
                return
            value = await self.services.boundary(self.applied, answer)
            if value.get('handoff'):
                self.result = {'completed': False, 'message': '',
                               'steer_message_id': value['steer_message_id']}
            elif value.get('reply'):
                self.result = {'completed': True, 'message': value['reply']}
            elif item := value.get('input'):
                if item['id'] not in self.applied:
                    self.applied.append(item['id'])
                    self.inbox.append(HumanMessage(content=item['content']))
                    await self.services.boundary(self.applied, None, receipt_only=True)
            elif value.get('continue'):
                self.inbox.append(HumanMessage(content=value['continue']))
            if self.inbox or self.result is not None:
                self.changed.set()

    def take_inputs(self, message=None):
        updates, self.inbox = self.inbox, []
        self.changed.clear()
        if message is not None and (updates or self.result is not None):
            updates = [*(ToolMessage(content='Cancelled before execution because the request changed.',
                tool_call_id=call['id'], name=call['name'], status='error')
                for call in message.tool_calls), *updates]
        return updates

    async def model(self, messages, settings):
        # All SDK-created subagents and summarizers share the same request cap
        # and capability. They cannot silently fall back to a provider model.
        if self.prepared.max_requests and self.requests >= self.prepared.max_requests:
            raise ValueError('This response reached its configured model request limit.')
        self.requests += 1
        visible = settings.pop('moyai_native_visible', False)
        if not visible and (self.inbox or self.result is not None):
            return AIMessage(content='Delegated work was interrupted by a change to the parent request.')
        tools = settings.pop('tools', [])
        return await self.services.model(messages, settings, tools,
            str(self.requests) if visible else '', self.input_id)

    async def call(self, name, arguments, validator):
        # SDK subagents may run in parallel. Serialize external side effects
        # across all of them, checking control before every dispatch.
        async with self.action_lock:
            await self.poll()
            if self.inbox or self.result is not None:
                return 'Cancelled before execution because the request changed.'
            try:
                validator.validate(arguments)
            except ValidationError:
                return 'Arguments did not match the tool schema. No action was attempted.'
            self.dispatches += 1
            receipt = await self.services.call_tool(
                f'native-{self.dispatches}', name, arguments, self.input_id)
            for field in ('wait_credential', 'wait_group'):
                if receipt.get(field):
                    self.result = {'completed': False, 'continuation': True, 'message': '',
                                   field: receipt[field]}
                    self.changed.set()
            # Only the trusted service receipt can control the conversation.
            # Nested tool output is data, including any wait-like strings.
            return tool_content(receipt.get('result', receipt))


class SessionModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    session: Session

    @property
    def _llm_type(self):
        return 'moyai-native-deepagents'

    def bind_tools(self, tools, **kwargs):
        from langchain_core.utils.function_calling import convert_to_openai_tool
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, *args, **kwargs):
        raise NotImplementedError('Moyai uses asynchronous model requests.')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if stop is not None:
            kwargs['stop'] = stop
        result = await self.session.model(messages, kwargs)
        return ChatResult(generations=[ChatGeneration(message=result)])


class ConversationControl(AgentMiddleware):
    """Moyai inbox and publication policy around the SDK's parent graph."""
    def __init__(self, session):
        self.session = session

    @hook_config(can_jump_to=['end'])
    async def abefore_model(self, state, runtime):
        s = self.session
        await s.poll()
        updates = s.take_inputs()
        if s.result is None and s.root_requests >= 25:
            s.result = {'completed': False, 'continuation': True, 'message': ''}
        if s.result is not None:
            return {'messages': updates, 'jump_to': 'end'}
        s.root_requests += 1
        return {'messages': updates} if updates else None

    async def awrap_model_call(self, request, handler):
        s = self.session
        pending = asyncio.create_task(handler(request.override(
            model_settings={**request.model_settings, 'moyai_native_visible': True})))
        try:
            await s.wait_for_model(pending)
            if s.inbox or s.result is not None:
                return ModelResponse(result=[AIMessage(content='')])
            return await pending
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    @hook_config(can_jump_to=['model', 'end'])
    async def aafter_model(self, state, runtime):
        s = self.session
        message = state['messages'][-1]
        await s.poll(None if message.tool_calls else message.text)
        updates = s.take_inputs(message)
        if s.result is not None:
            return {'messages': updates, 'jump_to': 'end'}
        if updates:
            return {'messages': updates, 'jump_to': 'model'}
        if message.text:
            await s.services.publish(str(s.requests), message.text, s.input_id)
        if not message.tool_calls:
            # A correction arriving during publication must still enter this
            # turn. Publication itself is fenced by the current input in SQL.
            await s.poll()
            updates = s.take_inputs()
            if updates:
                return {'messages': updates, 'jump_to': 'model'}
            if s.result is None:
                s.result = {'completed': True, 'message': message.text}
            s.closed = True
            return {'jump_to': 'end'}

    async def awrap_tool_call(self, request, handler):
        s = self.session
        await s.poll()
        if s.inbox or s.result is not None:
            call = request.tool_call
            return ToolMessage(content='Cancelled before execution because the request changed.',
                               name=call['name'], tool_call_id=call['id'], status='error')
        call = request.tool_call
        if call['name'].startswith('moyai_'):
            return await handler(request)  # Application services already report these tools.
        s.activities += 1
        identity = f'sdk-{s.activities}'
        input_id = s.input_id
        await s.services.activity(identity, call['name'], 'started', input_id)
        result = await handler(request)
        await s.services.activity(identity, call['name'],
            'error' if getattr(result, 'status', None) == 'error' else 'completed', input_id)
        return result


def capability_tools(session, catalog):
    tools = []
    for entry in catalog:
        name = entry['name']
        schema = entry.get('inputSchema', {'type': 'object', 'properties': {}})
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)

        def build_call(name, validator):
            async def call(**arguments):
                return await session.call(name, arguments, validator)
            return call

        tools.append(StructuredTool.from_function(coroutine=build_call(name, validator),
            name='moyai_' + name, description=entry.get('description') or name,
            args_schema=schema))
    return tools


def build_agent(session, catalog, *, model=None):
    """Compose public SDK features; never strip or reimplement its tool loop."""
    return create_deep_agent(
        model=model if model is not None else SessionModel(session=session),
        tools=capability_tools(session, catalog),
        system_prompt=session.prepared.instructions,
        backend=StateBackend(),
        middleware=[TodoListMiddleware(), ConversationControl(session)],
        name='moyai',
    )
