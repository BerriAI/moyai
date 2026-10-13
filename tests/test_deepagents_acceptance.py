"""App HTTP -> broker -> real Temporal/Deep Agents acceptance checks.

Only model replies and external connector/computer transports are controlled.
The public intake, control receipts, session state and event stream are real.
"""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
import time

import httpx
import pytest
import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, ToolMessage
from temporalio import activity
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment

from app.context_budget import counting_input
from app.deepagents_workflow import TurnScope
from app.temporal_payloads import PrivatePayloadCodec
from deepagents_provider import answer, event, response_stream
from test_deepagents_runtime import configured_client, durable_app  # noqa: F401


async def until(predicate, seconds=10):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.02)


@asynccontextmanager
async def web_worker(app, *, activity_slots=1, port=0):
    manager, settings = app.state.manager, app.state.settings
    settings.modal_token_id, settings.modal_token_secret = 'fixture-id', 'fixture-secret'
    settings.litellm_api_key = 'fixture-provider-key'
    settings.litellm_api_base = 'http://127.0.0.1/unused-provider'
    settings.repository_context_enabled = False
    settings.temporal_worker_activities = activity_slots
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    settings.public_url = f'http://127.0.0.1:{listener.getsockname()[1]}'
    app.state.connectors.security.origin = settings.public_url
    manager.deepagents_runtime.broker_transport = None
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    async def dispatch():
        while True:
            await manager.dispatch()
            await asyncio.sleep(.02)
    try:
        await until(lambda: server.started)
        async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
            manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
            async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
                manager.ready.set()
                dispatcher = asyncio.create_task(dispatch())
                try:
                    async with httpx.AsyncClient(base_url=settings.public_url, timeout=10) as api:
                        # Synthetic signed identity at the authentication boundary;
                        # production cloud intake deliberately rejects preview mode.
                        settings.google_client_id, settings.google_client_secret = 'fixture-client', 'fixture-secret'
                        settings.google_admin_emails = 'fixture@berri.ai'
                        app.state.security.local = False
                        login = JSONResponse({})
                        sid = app.state.security.new_session(login, identity={
                            'sub': 'fixture', 'email': 'fixture@berri.ai', 'domain': 'berri.ai', 'name': 'Fixture'})
                        api.cookies.set('workspace_session', login.headers['set-cookie'].split('workspace_session=')[1].split(';')[0])
                        api.headers.update({'Origin': settings.public_url, 'X-CSRF-Token': app.state.security.csrf(sid)})
                        assert (await api.get('/api/session')).json()['authenticated']
                        yield api
                finally:
                    dispatcher.cancel()
                    await asyncio.gather(dispatcher, return_exceptions=True)
                    manager.ready.clear()
    finally:
        server.should_exit = True
        await serving
        listener.close()


async def create(api, prompt, plugins=None):
    response = await api.post('/api/runs', json={'prompt': prompt, 'mode': 'modal',
        'chat_enabled': True, 'plugins': plugins or []})
    assert response.status_code == 201, response.text
    return response.json()['id']


async def steer(api, run_id, text):
    started = time.monotonic()
    response = await api.post(f'/api/runs/{run_id}/messages', json={
        'content': text, 'client_id': 'correction-' + run_id, 'send_now': True})
    assert response.status_code == 202, response.text
    assert time.monotonic() - started < 1
    return response.json()['id']


def bridge(name, arguments, identity):
    return {'name': 'workspace_call', 'args': {'name': name, 'arguments_json': json.dumps(arguments)}, 'id': identity}


async def test_send_now_redirects_generating_turn_and_preserves_request(durable_app):
    app = durable_app
    store, manager = app.state.store, app.state.manager
    entered, cancelled = asyncio.Event(), asyncio.Event()
    contexts = []
    async def model(scope, messages, settings, parameters):
        contexts.append((scope.message_id, str(messages)))
        if 'Use the corrected plan' in str(messages):
            return AIMessage(content='Continuing the original task with the corrected plan.')
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Keep the original task and change its plan when asked.')
        await asyncio.wait_for(entered.wait(), 10)
        original = store.run(run_id)['active_message_id']
        correction = await steer(api, run_id, 'Use the corrected plan and continue.')
        await until(lambda: store.rows('SELECT status FROM messages WHERE id=?', (correction,))[0]['status'] == 'injected')
        await asyncio.wait_for(cancelled.wait(), 10)
        await until(lambda: store.run(run_id)['status'] == 'idle')
        detail = (await api.get('/api/runs/' + run_id)).json()
        assert detail['summary'] == 'Continuing the original task with the corrected plan.'
        assert {turn for turn, _ in contexts} == {original}
        assert 'Keep the original task' in contexts[-1][1]
        assert store.messages(run_id)[1]['steering_parent_id'] == original
        assert not detail['sandbox_id']
        receipts = [e for e in store.events(run_id, limit=1000) if e['data'].get('phase') == 'steering']
        assert [e['data']['message_id'] for e in receipts] == [correction]


async def test_send_now_during_answer_publication_stays_in_the_same_turn(durable_app, monkeypatch):
    app = durable_app
    store, manager = app.state.store, app.state.manager
    publishing, release = asyncio.Event(), asyncio.Event()
    original_publish = manager.deepagents_runtime.publish
    contexts = []

    @activity.defn(name='deepagents_publish_v1')
    async def held_publish(scope: TurnScope, identity: str, text: str, input_id: int = 0) -> None:
        if text == 'Original answer.':
            publishing.set()
            await release.wait()
        await original_publish(scope, identity, text, input_id)

    async def model(scope, messages, settings, parameters):
        contexts.append((scope.message_id, str(messages)))
        return AIMessage(content='Corrected final answer.' if 'Correct the final answer' in str(messages)
                         else 'Original answer.')

    monkeypatch.setattr(manager.deepagents_runtime, 'publish', held_publish)
    manager.deepagents_runtime.model_request = model
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Answer the original question.')
        await asyncio.wait_for(publishing.wait(), 10)
        original = store.run(run_id)['active_message_id']
        correction = await steer(api, run_id, 'Correct the final answer before finishing.')
        await until(lambda: store.rows('SELECT status FROM messages WHERE id=?', (correction,))[0]['status'] == 'injected')
        release.set()
        await until(lambda: store.run(run_id)['status'] == 'idle')
        assert store.run(run_id)['summary'] == 'Corrected final answer.'
        assert {turn for turn, _ in contexts} == {original}
        assert len(contexts) == 2 and 'Answer the original question.' in contexts[-1][1]
        assert store.messages(run_id)[1]['steering_parent_id'] == original
        assert 'Original answer.' not in str(store.rows('SELECT * FROM session_items WHERE run_id=?', (run_id,)))
        assert 'Original answer.' not in str(store.events(run_id, limit=1000))
        assert 'Original answer.' not in str(store.messages(run_id))


async def test_tool_wait_accepts_steer_and_keeps_unrelated_chat_responsive(durable_app, monkeypatch):
    app = durable_app
    store, manager = app.state.store, app.state.manager
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')
    entered, release = asyncio.Event(), asyncio.Event()
    dispatched = []
    async def connected(name, arguments, **kwargs):
        dispatched.append(arguments['query'])
        entered.set()
        await release.wait()
        return {'results': [{'id': 'fixture-result'}]}
    monkeypatch.setattr(app.state.connectors, 'call', connected)
    async def model(scope, messages, settings, parameters):
        text = str(messages)
        if 'Unrelated short question' in text:
            return AIMessage(content='Independent answer.')
        if 'Cancel the later search' in text:
            assert any(isinstance(m, ToolMessage) and 'fixture-result' in str(m.content) for m in messages)
            return AIMessage(content='Kept the first result and cancelled the later search.')
        return AIMessage(content='', tool_calls=[bridge('notion_search', {'query': query}, query)
                                                for query in ('dispatched', 'obsolete')])
    manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Search the connected app twice.', ['notion'])
        await asyncio.wait_for(entered.wait(), 10)
        correction = await steer(api, run_id, 'Cancel the later search; keep the first result.')
        await until(lambda: store.rows('SELECT status FROM messages WHERE id=?', (correction,))[0]['status'] == 'injected')
        assert not release.is_set(), 'Acceptance must precede the running tool completion'
        other = await create(api, 'Unrelated short question')
        await until(lambda: store.run(other)['status'] == 'idle')
        assert store.run(other)['summary'] == 'Independent answer.'
        assert store.run(run_id)['sandbox_id'] == ''
        release.set()
        await until(lambda: store.run(run_id)['status'] == 'idle')
        assert dispatched == ['dispatched']
        assert store.run(run_id)['summary'] == 'Kept the first result and cancelled the later search.'


@pytest.mark.parametrize('action', ['steer', 'stop'])
async def test_computer_startup_keeps_control_responsive_and_never_dispatches_obsolete_action(durable_app, monkeypatch, action):
    from test_durable import Cloud
    app = durable_app
    store, manager = app.state.store, app.state.manager
    cloud = Cloud(monkeypatch, store, '')
    cloud.attach(manager)
    manager.settings.sandbox_idle_seconds = 0
    entered, release = asyncio.Event(), asyncio.Event()
    ensure = manager.agent_runtime.ensure_computer
    async def starting(run_id, state):
        entered.set()
        await release.wait()
        return await ensure(run_id, state)
    monkeypatch.setattr(manager.agent_runtime, 'ensure_computer', starting)
    executed = []
    async def command(machine, action, directory, value, **kwargs):
        if action == 'start' and directory not in machine.operations:
            spec = machine.spec
            if spec.get('prepare_computer'):
                result = {'workspace': '/workspace'}
            elif spec.get('finish_computer'):
                result = {'saved': True}
            else:
                executed.append(spec['name'])
                result = {'isError': False}
            machine.operations[directory] = result
        return json.dumps({'state': 'done', 'events': [], 'cursor': 0, 'exit_code': 0,
                           'final': {'tool_result': machine.operations[directory]}})
    monkeypatch.setattr(manager, 'command', command)
    async def model(scope, messages, settings, parameters):
        text = str(messages)
        if 'Unrelated' in text:
            return AIMessage(content='Other chat stays responsive.')
        if 'Do not write' in text:
            return AIMessage(content='Continued without the obsolete write.')
        return AIMessage(content='', tool_calls=[bridge('computer_write_file',
            {'path': 'obsolete.txt', 'text': 'must never be written'}, 'obsolete-file')])
    manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        cloud.run_id = run_id = await create(api, 'Prepare a computer and write the file.')
        await asyncio.wait_for(entered.wait(), 10)
        if action == 'steer':
            correction = await steer(api, run_id, 'Do not write the file; continue this conversation.')
            await until(lambda: store.rows('SELECT status FROM messages WHERE id=?', (correction,))[0]['status'] == 'injected')
            assert store.rows('SELECT status FROM computer_operations WHERE run_id=?', (run_id,))[0]['status'] == 'cancelled'
        else:
            started = time.monotonic()
            response = await api.post(f'/api/runs/{run_id}/cancel')
            assert response.status_code == 200
            assert time.monotonic() - started < 1
            assert store.run(run_id)['token_hash'] == ''
        other = await create(api, 'Unrelated short question')
        await until(lambda: store.run(other)['status'] == 'idle')
        assert not release.is_set()
        release.set()
        await until(lambda: store.run(run_id)['status'] == ('idle' if action == 'steer' else 'cancelled'))
        assert executed == []
        assert all(row['status'] == 'cancelled' for row in store.rows('SELECT status FROM computer_operations WHERE run_id=?', (run_id,)))


@pytest.mark.parametrize('computer', [False, True])
async def test_correction_during_catalog_lookup_fences_late_dispatch(durable_app, monkeypatch, computer):
    app = durable_app
    store, manager = app.state.store, app.state.manager
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')
    entered, release = asyncio.Event(), asyncio.Event()
    catalog = manager.deepagents_runtime.catalog
    async def delayed(scope):
        result = await catalog(scope)
        entered.set()
        await release.wait()
        return result
    monkeypatch.setattr(manager.deepagents_runtime, 'catalog', delayed)
    async def forbidden(*args, **kwargs):
        pytest.fail('Obsolete action crossed the dispatch fence')
    monkeypatch.setattr(app.state.connectors, 'call', forbidden)
    monkeypatch.setattr(manager.agent_runtime, 'ensure_computer', forbidden)
    async def model(scope, messages, settings, parameters):
        if 'Cancel that action' in str(messages):
            return AIMessage(content='No obsolete action was dispatched.')
        name, args = ('computer_write_file', {'path': 'obsolete', 'text': 'old'}) if computer else ('notion_search', {'query': 'old'})
        return AIMessage(content='', tool_calls=[bridge(name, args, 'delayed')])
    manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Look up the tool and perform the action.', ['notion'])
        await asyncio.wait_for(entered.wait(), 10)
        correction = await steer(api, run_id, 'Cancel that action and continue.')
        await until(lambda: store.rows('SELECT status FROM messages WHERE id=?', (correction,))[0]['status'] == 'injected')
        release.set()
        await until(lambda: store.run(run_id)['status'] == 'idle')
        assert store.run(run_id)['summary'] == 'No obsolete action was dispatched.'


@pytest.mark.parametrize('blocked_tool', [False, True])
async def test_http_stop_cancels_generation_or_tool_and_fences_late_text(durable_app, monkeypatch, blocked_tool):
    app = durable_app
    store, manager = app.state.store, app.state.manager
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scopes = []
    async def connected(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'results': [{'id': 'late-external-result'}]}
    monkeypatch.setattr(app.state.connectors, 'call', connected)
    async def model(scope, messages, settings, parameters):
        scopes.append(scope)
        if blocked_tool:
            return AIMessage(content='', tool_calls=[bridge('notion_search', {'query': 'wait'}, 'wait')])
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    manager.deepagents_runtime.model_request = model
    async with web_worker(app) as api:
        run_id = await create(api, 'Wait until I stop you.', ['notion'])
        await asyncio.wait_for(entered.wait(), 10)
        started = time.monotonic()
        response = await api.post(f'/api/runs/{run_id}/cancel')
        assert response.status_code == 200 and time.monotonic() - started < 1
        if not blocked_tool:
            await asyncio.wait_for(cancelled.wait(), 10)
        release.set()
        await until(lambda: store.run(run_id)['status'] == 'cancelled')
        manager.deepagents_runtime.stream_text(scopes[0], 'late', scopes[0].message_id, 'Late text must not revive the turn.')
        assert not any(e['message'] == 'Late text must not revive the turn.' for e in store.events(run_id, limit=1000))
        assert len(scopes) == 1


async def test_manual_computer_wake_does_not_occupy_controller_or_steering(durable_app, monkeypatch):
    app = durable_app
    store, manager = app.state.store, app.state.manager
    generating, starting, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def computer(run_id, state):
        starting.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(manager.agent_runtime, 'computer_step', computer)
    async def model(scope, messages, settings, parameters):
        if 'Other chat' in str(messages):
            return AIMessage(content='Other chat completed during computer startup.')
        if 'Continue without' in str(messages):
            return AIMessage(content='Continued the original task without waiting for a computer.')
        generating.set()
        await asyncio.Event().wait()
    manager.deepagents_runtime.model_request = model
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Keep thinking while I request a computer.')
        await asyncio.wait_for(generating.wait(), 10)
        response = await api.post(f'/api/runs/{run_id}/computer', json={'action': 'wake'})
        assert response.status_code == 200, response.text
        await asyncio.wait_for(starting.wait(), 10)
        other = await create(api, 'Other chat')
        await until(lambda: store.run(other)['status'] == 'idle')
        correction = await steer(api, run_id, 'Continue without waiting for the computer.')
        await until(lambda: store.rows('SELECT status FROM messages WHERE id=?', (correction,))[0]['status'] == 'injected')
        await asyncio.wait_for(cancelled.wait(), 10)
        await until(lambda: store.run(run_id)['status'] == 'idle')


async def test_real_sdk_stream_reaches_public_events_before_model_completion(durable_app, monkeypatch):
    app = durable_app
    store = app.state.store
    release, emitted = asyncio.Event(), asyncio.Event()
    async def count(payload, **kwargs):
        return counting_input(payload)[1], 'fixture_bytes'
    monkeypatch.setattr(app.state.context_budget, 'count', count)
    @app.post('/fixture/v1/responses')
    async def provider(request: Request):
        body = await request.json()
        assert body['stream'] is True
        final = answer('First visible sentence. Final sentence.')
        async def output():
            for chunk in response_stream(final):
                if b'response.output_text.delta' in chunk:
                    yield event('response.output_text.delta', output_index=0, content_index=0,
                                item_id='msg_fixture', delta='First visible sentence. ')
                    emitted.set()
                    await release.wait()
                    yield event('response.output_text.delta', output_index=0, content_index=0,
                                item_id='msg_fixture', delta='Final sentence.')
                else:
                    yield chunk
        return StreamingResponse(output(), media_type='text/event-stream')
    async with web_worker(app) as api:
        app.state.settings.litellm_api_base = app.state.settings.public_url + '/fixture'
        run_id = await create(api, 'Return two public sentences.')
        try:
            await asyncio.wait_for(emitted.wait(), 10)
            async with api.stream('GET', f'/api/runs/{run_id}/events') as stream:
                async with asyncio.timeout(5):
                    async for line in stream.aiter_lines():
                        if line.startswith('data:') and 'First visible sentence.' in line:
                            partial = json.loads(line.removeprefix('data:'))
                            assert partial['data']['phase'] == 'streaming'
                            break
                    else:
                        pytest.fail('No public partial text arrived')
            assert not release.is_set()
            assert store.run(run_id)['status'] == 'running'
        finally:
            release.set()
        await until(lambda: store.run(run_id)['status'] == 'idle')
        assert store.run(run_id)['summary'] == 'First visible sentence. Final sentence.'
        final = [e for e in store.events(run_id, limit=1000) if e['data'].get('public_update')]
        assert len(final) == 1 and final[0]['message'] == 'First visible sentence. Final sentence.'
