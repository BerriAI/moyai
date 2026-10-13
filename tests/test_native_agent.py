"""Exercise the actual SDK graph, durable replay and public Moyai HTTP intake."""
import asyncio
from dataclasses import replace
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from app.deepagents_context import deepagents_plugin
from app.deepagents_workflow import TurnInput, TurnScope
from app.native_agent import Session, build_agent
from app.native_agent_workflow import NativeAgentWorkflow
from test_deepagents_acceptance import web_worker, create, steer, until
from test_deepagents_runtime import (
    ScriptedRuntime, configured_client, converter, durable_app, worker,  # noqa: F401
)


def call(name, arguments, identity='fixture-call'):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': arguments, 'id': identity}])


class Services:
    def __init__(self):
        self.dispatched = []
        self.published = []
        self.schemas = []

    async def boundary(self, *args, **kwargs):
        return {}

    async def publish(self, identity, text, input_id):
        self.published.append(text)

    async def activity(self, *args):
        pass

    async def call_tool(self, identity, name, arguments, input_id):
        self.dispatched.append((name, arguments))
        return {'result': {'saved': True}}

    async def model(self, messages, settings, tools, identity, input_id):
        self.schemas.append(tools)
        count = len(self.schemas)
        if count == 1:
            return call('write_todos', {'todos': [{'content': 'Write a note', 'status': 'in_progress'}]})
        if count == 2:
            return call('write_file', {'file_path': '/notes.txt', 'content': 'native notebook'})
        if count == 3:
            return call('moyai_save_note', {'text': 'real application write'})
        if count == 4:
            return call('read_file', {'file_path': '/notes.txt'})
        assert 'native notebook' in str(messages[-1].content)
        return AIMessage(content='Saved the application note and read the notebook.')


CATALOG = [{'name': 'save_note', 'description': 'Save an application note.',
            'inputSchema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                            'required': ['text'], 'additionalProperties': False}}]


async def test_native_sdk_owns_planning_files_and_real_tool_schemas(tmp_path):
    services = Services()
    session = Session(services, TurnInput(TurnScope('native-test', 1), 'Write a note.', 'You are Moyai.'))
    result = await build_agent(session, CATALOG).ainvoke({'messages': [('user', 'Write a note.')]})
    schemas = {tool['function']['name']: tool['function'] for tool in services.schemas[0]}
    assert {'task', 'write_todos', 'write_file', 'read_file', 'moyai_save_note'} <= schemas.keys()
    assert not {'workspace_tools', 'workspace_call'} & schemas.keys()
    assert schemas['moyai_save_note']['parameters'] == CATALOG[0]['inputSchema']
    assert 'scope' not in schemas['moyai_save_note']['parameters']['properties']
    assert result['todos'][0]['content'] == 'Write a note'
    assert result['files']['/notes.txt']['content'] == 'native notebook'
    assert services.dispatched == [('save_note', {'text': 'real application write'})]
    assert services.published == ['Saved the application note and read the notebook.']
    assert not (tmp_path / 'notes.txt').exists()


async def test_sdk_subagent_inherits_capabilities_but_does_not_publish_private_answer():
    services = Services()
    async def model(messages, settings, tools, identity, input_id):
        if not identity:  # SDK-created subagent, rather than the parent.
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(content='Private delegated result.')
            return call('moyai_save_note', {'text': 'delegated write'})
        if isinstance(messages[-1], ToolMessage):
            assert 'Private delegated result.' in str(messages[-1].content)
            return AIMessage(content='Delegation finished.')
        return call('task', {'description': 'Save one note and report.', 'subagent_type': 'general-purpose'})
    services.model = model
    session = Session(services, TurnInput(TurnScope('subagent-test', 1), '', 'You are Moyai.'))
    await build_agent(session, CATALOG).ainvoke({'messages': [('user', 'Delegate a note.')]})
    assert services.dispatched == [('save_note', {'text': 'delegated write'})]
    assert services.published == ['Delegation finished.']


async def test_invalid_tool_arguments_cannot_cross_capability_boundary():
    services = Services()
    requests = 0
    async def model(messages, *args):
        nonlocal requests
        requests += 1
        if requests == 1:
            return call('moyai_save_note', {'text': 42})
        assert 'No action was attempted' in str(messages[-1].content)
        return AIMessage(content='Rejected invalid arguments.')
    services.model = model
    session = Session(services, TurnInput(TurnScope('schema-test', 1), '', 'You are Moyai.'))
    await build_agent(session, CATALOG).ainvoke({'messages': [('user', 'Invalid write.')]})
    assert services.dispatched == []


class NativeRuntime(ScriptedRuntime):
    def native_activity(self, *args):
        pass

    async def catalog(self, scope):
        return CATALOG

    async def model_request(self, scope, messages, settings, parameters):
        self.model_calls += 1
        if any(isinstance(message, ToolMessage) and message.name == 'moyai_save_note' for message in messages):
            self.after_write.set()
            await self.release.wait()
            return AIMessage(content='Saved exactly once with the native SDK.')
        if not any(isinstance(message, ToolMessage) for message in messages):
            return call('write_file', {'file_path': '/durable.txt', 'content': 'Notebook survived worker restart.'})
        return call('moyai_save_note', {'text': 'one write'})


async def test_native_worker_restart_replays_graph_without_repeating_write(tmp_path):
    runtime = NativeRuntime(tmp_path)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        first = worker(client, runtime)
        task = asyncio.create_task(first.run())
        handle = await client.start_workflow(NativeAgentWorkflow.run, TurnScope('test', 1),
            id='native-restart', task_queue='deepagents-test')
        try:
            await asyncio.wait_for(runtime.after_write.wait(), 25)
        finally:
            await first.shutdown()
            await task
        assert runtime.tool_calls == 1
        runtime.release.set()
        async with worker(client, runtime):
            result = await asyncio.wait_for(handle.result(), 25)
        assert result.result['message'] == 'Saved exactly once with the native SDK.'
        assert result.native_state['files']['/durable.txt']['content'] == 'Notebook survived worker restart.'
        assert runtime.tool_calls == 1
        assert (tmp_path / 'note.txt').read_text() == 'one write\n'
        history = await handle.fetch_history()
        assert 'Private test request' not in history.to_json()
        await Replayer(workflows=[NativeAgentWorkflow], data_converter=client.data_converter,
                       plugins=[deepagents_plugin()]).replay_workflow(history)


async def test_native_unknown_write_is_never_automatically_retried(tmp_path):
    runtime = NativeRuntime(tmp_path)
    runtime.fail_after_write = True
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        async with worker(client, runtime):
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(client.execute_workflow(NativeAgentWorkflow.run, TurnScope('test', 1),
                    id='native-uncertain', task_queue='deepagents-test'), 25)
        assert runtime.tool_calls == 1
        assert (tmp_path / 'note.txt').read_text() == 'one write\n'


async def test_native_full_http_session_executes_sdk_file_tools_and_saves_state(durable_app):
    app = durable_app
    app.state.settings.agent_harness = 'deepagents-native'
    requests = []
    async def model(scope, messages, settings, parameters):
        requests.append(parameters)
        if not any(isinstance(message, ToolMessage) for message in messages):
            return call('write_file', {'file_path': '/report.txt', 'content': 'Native graph proof'})
        assert 'write_file' in str(messages)
        return AIMessage(content='Created report.txt in the conversation notebook.')
    app.state.manager.deepagents_runtime.model_request = model
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Create a note with Deep Agents.')
        await until(lambda: app.state.store.run(run_id)['status'] in {'idle', 'failed'}, seconds=25)
        run = app.state.store.run(run_id)
        assert run['status'] == 'idle', run.get('error')
        assert run['harness'] == 'deepagents-native'
        assert run['sandbox_id'] == ''
        assert run['summary'] == 'Created report.txt in the conversation notebook.'
        assert len(requests) == 2
        assert 'workspace_call' not in str(requests)
        events = app.state.store.events(run_id, limit=1000)
        notebook = [event for event in events if event['data'].get('tool') == 'write_file']
        assert [event['data']['phase'] for event in notebook] == ['started', 'completed']
        assert 'Native graph proof' not in str(events)


async def test_native_http_send_now_cancels_obsolete_generation(durable_app):
    app = durable_app
    app.state.settings.agent_harness = 'deepagents-native'
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def model(scope, messages, settings, parameters):
        if 'Use the corrected plan' in str(messages):
            return AIMessage(content='Continued with the corrected plan.')
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    app.state.manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Keep the original task.')
        await asyncio.wait_for(entered.wait(), 15)
        correction = await steer(api, run_id, 'Use the corrected plan.')
        await asyncio.wait_for(cancelled.wait(), 15)
        await until(lambda: app.state.store.run(run_id)['status'] == 'idle', seconds=20)
        assert app.state.store.run(run_id)['summary'] == 'Continued with the corrected plan.'
        corrected = app.state.store.rows('SELECT * FROM messages WHERE id=?', (correction,))[0]
        assert corrected['steering_parent_id'] is not None
        assert corrected['status'] == 'completed'


@pytest.mark.parametrize('private_context,lost_prepare_ack', [(False, False), (True, False), (False, True)])
async def test_native_notebook_resumes_only_from_an_authorized_checkpoint(
        durable_app, private_context, lost_prepare_ack):
    from app.native_sessions import mark_private_context
    app = durable_app
    app.state.settings.agent_harness = 'deepagents-native'
    store = app.state.store
    # The normal fixture installs an organization skill directory, which
    # deliberately taints native state. Exercise an otherwise clean scope here.
    store.execute('UPDATE skills SET archived=1')
    checkpoint_results = []
    broker = app.state.manager.deepagents_runtime.broker
    async def observed_broker(scope, route, body=None):
        result = await broker(scope, route, body)
        if route == '/context/native':
            checkpoint_results.append((body['action'], result.get('reason'), bool(result.get('state'))))
        return result
    app.state.manager.deepagents_runtime.broker = observed_broker
    prepare = app.state.manager.deepagents_runtime.prepare
    first_turn, lost = None, False
    @activity.defn(name='deepagents_prepare_v1')
    async def retry_prepare(scope: TurnScope) -> TurnInput:
        nonlocal first_turn, lost
        prepared = await prepare(scope)
        if first_turn is None:
            first_turn = scope.message_id
        elif lost_prepare_ack and not lost:
            lost = True
            raise ConnectionError('Simulated lost preparation acknowledgment')
        return prepared
    app.state.manager.deepagents_runtime.prepare = retry_prepare
    async def model(scope, messages, settings, parameters):
        if 'Read the saved notebook' in str(messages):
            if isinstance(messages[-1], ToolMessage) and messages[-1].name == 'read_file':
                assert ('fixture notebook content' in str(messages[-1].content)) is not private_context
                return AIMessage(content='Checkpoint isolation verified.')
            return call('read_file', {'file_path': '/saved.txt'}, 'read-saved')
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content='Notebook prepared.')
        if private_context:
            mark_private_context(store, store.run(scope.run_id))
        return call('write_file', {'file_path': '/saved.txt', 'content': 'fixture notebook content'})
    app.state.manager.deepagents_runtime.model_request = model
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Prepare a notebook.')
        await until(lambda: store.run(run_id)['status'] == 'idle', seconds=20)
        assert checkpoint_results[-1][1] == ('private_context' if private_context else 'saved'), checkpoint_results
        response = await api.post(f'/api/runs/{run_id}/messages',
            json={'content': 'Read the saved notebook.', 'client_id': 'native-followup'})
        assert response.status_code == 202, response.text
        await until(lambda: len(checkpoint_results) >= 3)
        assert checkpoint_results[2][1] == ('private_context' if private_context else 'resumed'), checkpoint_results
        await until(lambda: store.run(run_id)['summary'] == 'Checkpoint isolation verified.', seconds=20)
        await until(lambda: store.run(run_id)['status'] == 'idle')
        assert lost is lost_prepare_ack


async def test_native_tool_wait_keeps_other_chats_responsive_and_fences_later_write(durable_app, monkeypatch):
    app = durable_app
    app.state.settings.agent_harness = 'deepagents-native'
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []
    async def connected(name, arguments, **kwargs):
        dispatched.append(arguments['query'])
        entered.set()
        await release.wait()
        return {'results': [{'id': 'confirmed-fixture-result'}]}
    monkeypatch.setattr(app.state.connectors, 'call', connected)
    async def model(scope, messages, settings, parameters):
        if 'Independent question' in str(messages):
            return AIMessage(content='Independent answer.')
        if 'Cancel the later search' in str(messages):
            assert 'confirmed-fixture-result' in str(messages)
            return AIMessage(content='Kept the first result and cancelled the second.')
        return AIMessage(content='', tool_calls=[
            {'name': 'moyai_notion_search', 'args': {'query': name}, 'id': name}
            for name in ('first', 'obsolete')])
    app.state.manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Search twice.', ['notion'])
        await asyncio.wait_for(entered.wait(), 15)
        correction = await steer(api, run_id, 'Cancel the later search.')
        await until(lambda: app.state.store.rows('SELECT status FROM messages WHERE id=?',
                    (correction,))[0]['status'] == 'injected')
        other = await create(api, 'Independent question')
        await until(lambda: app.state.store.run(other)['status'] == 'idle', seconds=20)
        assert not release.is_set()
        release.set()
        await until(lambda: app.state.store.run(run_id)['status'] == 'idle', seconds=20)
        assert dispatched == ['first']
        assert app.state.store.run(run_id)['summary'] == 'Kept the first result and cancelled the second.'


async def test_native_http_stop_revokes_model_and_prevents_late_publication(durable_app):
    app = durable_app
    app.state.settings.agent_harness = 'deepagents-native'
    entered, cancelled = asyncio.Event(), asyncio.Event()
    scopes = []
    async def model(scope, messages, settings, parameters):
        scopes.append(scope)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    runtime = app.state.manager.deepagents_runtime
    runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Wait until stopped.')
        await asyncio.wait_for(entered.wait(), 15)
        assert (await api.post(f'/api/runs/{run_id}/cancel')).status_code == 200
        await asyncio.wait_for(cancelled.wait(), 15)
        await until(lambda: app.state.store.run(run_id)['status'] == 'cancelled')
        runtime.stream_text(scopes[0], 'late', scopes[0].message_id, 'Obsolete answer.')
        assert 'Obsolete answer.' not in str(app.state.store.events(run_id, limit=1000))


async def test_native_real_responses_transport_loads_skills_and_executes_typed_tools(durable_app):
    from fastapi import Request
    from fastapi.responses import StreamingResponse
    from app.context_budget import counting_input, ModelContextLimits
    from agent.skills import catalog
    from deepagents_provider import answer, response_stream
    app = durable_app
    settings = app.state.settings
    settings.agent_harness = 'deepagents-native'
    # The fixture counter counts bytes conservatively; this test exercises
    # the actual provider wire, not a model's external token-count service.
    settings.model_context_limits = {'test-model': ModelContextLimits(
        context_window=1000000, max_input_tokens=900000, max_output_tokens=10000)}
    requests = []
    coding = next(skill for skill in catalog() if skill['name'] == 'coding')
    async def count(payload, **kwargs):
        return counting_input(payload)[1], 'fixture_bytes'
    app.state.context_budget.count = count
    @app.post('/fixture/v1/responses')
    async def provider(request: Request):
        body = await request.json()
        requests.append(body)
        offered = {tool['name'] for tool in body['tools']}
        assert {'write_file', 'task', 'write_todos', 'moyai_history_read', 'moyai_bundled_skill_read'} <= offered
        assert not {'workspace_call', 'workspace_tools'} & offered
        response = answer('Typed native transport verified.', identity=f'native-{len(requests)}')
        if len(requests) == 1:
            assert 'Establish the intended outcome and current repository state.' not in json.dumps(body['input'])
            response['output'] = [{'type': 'function_call', 'id': 'fc_skill', 'call_id': 'call_skill',
                'name': 'moyai_bundled_skill_read', 'status': 'completed',
                'arguments': json.dumps({'name': coding['name'], 'revision': coding['revision']})}]
        elif len(requests) == 2:
            assert 'Establish the intended outcome and current repository state.' in json.dumps(body['input'])
            response['output'] = [{'type': 'function_call', 'id': 'fc_history', 'call_id': 'call_history',
                'name': 'moyai_history_read', 'status': 'completed', 'arguments': '{"limit": 2}'}]
        else:
            assert 'function_call_output' in json.dumps(body['input'])
        return StreamingResponse(iter(response_stream(response)), media_type='text/event-stream')
    async with web_worker(app, activity_slots=2) as api:
        settings.litellm_api_base = settings.public_url + '/fixture'
        run_id = await create(api, 'Read the current conversation.')
        await until(lambda: app.state.store.run(run_id)['status'] in {'idle', 'failed'}, seconds=25)
        assert app.state.store.run(run_id)['summary'] == 'Typed native transport verified.'
        assert len(requests) == 3
        receipts = [event for event in app.state.store.events(run_id, limit=1000)
                    if event['kind'] == 'tool_dispatch' and event['data'].get('tool') == 'history_read']
        assert len(receipts) == 1 and receipts[0]['data']['outcome'] == 'completed'
