"""Real Temporal histories, real PydanticAI graph, scripted provider and local tools."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import DataConverter, DefaultFailureConverterWithEncodedAttributes
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from app.blob_storage import ObjectStorage
from app.pydantic_context import RuntimeInterceptor
from app.pydantic_workflow import PydanticTurnWorkflow, TurnInput, TurnScope
from app.temporal_payloads import PrivatePayloads, PrivatePayloadCodec


class ScriptedRuntime:
    def __init__(self, directory):
        self.directory = directory
        self.model_calls, self.tool_calls = 0, 0
        self.after_write = asyncio.Event()
        self.release = asyncio.Event()
        self.fail_after_write = False
        self.controls = []
        self.published = []

    @activity.defn(name='pydantic_prepare_v1')
    async def prepare(self, scope: TurnScope) -> TurnInput:
        return TurnInput(replace(scope, model='scripted', input_budget=10000),
            'Private test request: write once, then report.', 'Use workspace tools.')

    @activity.defn(name='pydantic_boundary_v1')
    async def boundary(self, scope: TurnScope, applied: list[int], elapsed: float, answer: str | None) -> dict:
        self.controls.append((list(applied), answer))
        return {}

    @activity.defn(name='pydantic_publish_v1')
    async def publish(self, scope: TurnScope, identity: str, text: str) -> None:
        self.published.append(text)

    async def catalog(self, scope):
        return [{'name': 'write_note', 'inputSchema': {'type': 'object'}}]

    async def model_request(self, scope, messages, settings, parameters):
        self.model_calls += 1
        if any(isinstance(part, ToolReturnPart) for m in messages for part in m.parts):
            self.after_write.set()
            await self.release.wait()
            return ModelResponse(parts=[TextPart('Saved exactly once.')], model_name='scripted')
        return ModelResponse(parts=[ToolCallPart('workspace_call',
            {'name': 'write_note', 'arguments_json': '{}'}, tool_call_id='write-1')], model_name='scripted')

    async def call_tool(self, scope, call_id, name, arguments):
        self.tool_calls += 1
        with (self.directory / 'note.txt').open('a') as out:
            out.write('one write\n')
        if self.fail_after_write:
            raise ConnectionError('Acknowledgment lost after external write')
        return {'result': {'saved': True}}


def converter(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path, encryption_key=Fernet.generate_key().decode())
    payloads = PrivatePayloads(SimpleNamespace(objects=ObjectStorage()), settings)
    return DataConverter(payload_codec=PrivatePayloadCodec(payloads),
                         failure_converter_class=DefaultFailureConverterWithEncodedAttributes)


def configured_client(client, conversion):
    config = client.config()
    config.update(data_converter=conversion, plugins=[PydanticAIPlugin()])
    return Client(**config)


def worker(client, runtime):
    return Worker(client, task_queue='pydantic-test', workflows=[PydanticTurnWorkflow],
        activities=[runtime.prepare, runtime.boundary, runtime.publish],
        interceptors=[RuntimeInterceptor(runtime)], graceful_shutdown_timeout=timedelta(seconds=.1))


async def test_real_temporal_restart_does_not_replay_completed_tool(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    conversion = converter(tmp_path)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, conversion)
        first = worker(client, runtime)
        task = asyncio.create_task(first.run())
        handle = await client.start_workflow(PydanticTurnWorkflow.run, TurnScope('test', 1),
            id='pydantic-restart', task_queue='pydantic-test')
        try:
            await asyncio.wait_for(runtime.after_write.wait(), 25)
        finally:
            await first.shutdown()
            await task
        assert runtime.tool_calls == 1
        runtime.release.set()
        async with worker(client, runtime):
            result = await asyncio.wait_for(handle.result(), 30)
        assert result.result == {'completed': True, 'message': 'Saved exactly once.'}
        assert (tmp_path / 'note.txt').read_text() == 'one write\n'
        assert runtime.tool_calls == 1
        history = await handle.fetch_history()
        assert 'Private test request' not in history.to_json()
        assert 'Saved exactly once' not in history.to_json()
        await Replayer(workflows=[PydanticTurnWorkflow], data_converter=client.data_converter,
                       plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_uncertain_write_is_not_automatically_retried(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    runtime.fail_after_write = True
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        async with worker(client, runtime):
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(client.execute_workflow(PydanticTurnWorkflow.run, TurnScope('test', 1),
                    id='pydantic-uncertain', task_queue='pydantic-test'), 25)
        assert runtime.tool_calls == 1
        assert (tmp_path / 'note.txt').read_text() == 'one write\n'


async def test_private_payloads_offload_large_data_and_decode_legacy(tmp_path):
    from temporalio.api.common.v1 import Payload
    codec = converter(tmp_path).payload_codec
    original = Payload(metadata={'encoding': b'json/plain'}, data=b'private' * 500000)
    encoded = await codec.encode([original])
    assert len(encoded[0].data) < 400
    assert b'private' not in encoded[0].data
    assert (await codec.decode(encoded))[0] == original
    legacy = Payload(metadata={'encoding': b'json/plain'}, data=b'"run-id"')
    assert (await codec.decode([legacy]))[0] == legacy
    saved = next((tmp_path / 'temporal-payloads').iterdir())
    assert b'private' not in saved.read_bytes()


def test_ephemeral_checkpoint_hosts_require_durable_payload_storage(tmp_path):
    from app.config import Settings
    common = dict(_env_file=None, temporal_enabled=True, checkpoint_dir=tmp_path,
                  encryption_key=Fernet.generate_key().decode(), session_secret='stable-test-secret')
    with pytest.raises(ValueError, match='shared object storage'):
        Settings(**common, object_storage_bucket='')
    assert Settings(**common, object_storage_bucket='private-payloads').temporal_enabled


async def test_steering_uses_library_cancellation_before_unexecuted_tools(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    runtime.release.set()
    original = runtime.boundary
    async def redirect(scope, applied, elapsed, answer):
        if len(runtime.controls) == 1:
            runtime.controls.append((applied, answer))
            return {'input': {'id': 2, 'content': 'Do not write anything; just reply.'}}
        return await original(scope, applied, elapsed, answer)
    # Bound methods retain their Temporal activity definitions via a small test adapter.
    @activity.defn(name='pydantic_boundary_v1')
    async def boundary(scope: TurnScope, applied: list[int], elapsed: float, answer: str | None) -> dict:
        return await redirect(scope, applied, elapsed, answer)
    runtime.boundary = boundary
    async def corrected_model(scope, messages, settings, parameters):
        if any('Do not write' in str(m) for m in messages):
            return ModelResponse(parts=[TextPart('No file was written.')], model_name='scripted')
        return ModelResponse(parts=[ToolCallPart('workspace_call', {'name': 'write_note', 'arguments_json': '{}'},
            tool_call_id='not-executed')], model_name='scripted')
    runtime.model_request = corrected_model
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        async with worker(client, runtime):
            result = await asyncio.wait_for(client.execute_workflow(PydanticTurnWorkflow.run, TurnScope('test', 1),
                id='pydantic-steering', task_queue='pydantic-test'), 25)
        assert result.result['message'] == 'No file was written.'
        assert result.applied == [2]
        assert runtime.tool_calls == 0
        assert not (tmp_path / 'note.txt').exists()
        # The library supplies the closed protocol snapshot; no orphaned call.
        calls = {p.tool_call_id for m in result.messages for p in m.parts if isinstance(p, ToolCallPart)}
        returns = {p.tool_call_id for m in result.messages for p in m.parts if isinstance(p, ToolReturnPart)}
        assert calls == returns == {'not-executed'}


async def test_background_compaction_keeps_working_preserves_tail_and_replays(durable_app, tmp_path, monkeypatch):
    import httpx
    import httpx2
    import time
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from app.context_budget import counting_input, ModelContextLimits
    from app.harness_gateway import HarnessGateway
    from app.pydantic_runtime import SealedModelTransport
    from app.security import digest

    app = durable_app
    manager, store = app.state.manager, app.state.store
    app.state.settings.litellm_api_base = 'https://scripted-provider.invalid/v1'
    app.state.settings.model_context_limits['test-model'] = ModelContextLimits(
        context_window=25000, max_input_tokens=25000, max_output_tokens=8192, default_output_tokens=4096)
    runtime = ScriptedRuntime(tmp_path)
    summary_started, release_summary, pressured = (asyncio.Event() for _ in range(3))
    summaries, inferences = [], []
    proof = SimpleNamespace(owner=None)
    original_forward = HarnessGateway.forward
    async def forward(self, *args):
        proof.owner = self.live_context
        return await original_forward(self, *args)
    monkeypatch.setattr(HarnessGateway, 'forward', forward)
    original_measure = app.state.context_budget.measure
    async def measure(payload, **kwargs):
        budget = await original_measure(payload, **kwargs)
        if budget.input_tokens > budget.input_budget and payload.get('tools'):
            pressured.set()
        return budget
    monkeypatch.setattr(app.state.context_budget, 'measure', measure)

    # Keep the installed Responses SDK and sealing transport; bridge only the
    # network socket to the real ASGI broker. Only the upstream provider is fake.
    actual_client = httpx.AsyncClient
    class LocalBroker(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async with actual_client(transport=httpx.ASGITransport(app=app)) as client:
                response = await client.request(request.method, str(request.url),
                    headers=dict(request.headers), content=await request.aread())
            return httpx2.Response(response.status_code, headers=dict(response.headers), content=response.content)
    class LocalSealedTransport(SealedModelTransport):
        def __init__(self, token):
            self.token, self.transport = token, LocalBroker()
    monkeypatch.setattr('app.pydantic_runtime.SealedModelTransport', LocalSealedTransport)

    async def upstream(request):
        body = json.loads(request.content)
        if request.url.path == '/utils/token_counter':
            return httpx.Response(200, json={'total_tokens': len(body['messages'][0]['content'][0]['text'].encode())})
        if request.url.path == '/v1/chat/completions':
            assert not body.get('tools')
            summaries.append(body)
            summary_started.set()
            print('BACKGROUND: summary started and held at the scripted provider.' if not release_summary.is_set()
                  else 'BACKGROUND: next prefix summarized after adoption.', flush=True)
            await release_summary.wait()
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
                'content': 'private-async-summary: older receipts are complete; preserve the original task.'}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
        assert request.url.path == '/v1/responses'
        inferences.append(body)
        number = len(inferences)
        if number <= 2:
            assert not release_summary.is_set()
            assert 'private-async-summary' not in json.dumps(body)
            output = [{'type': 'function_call', 'id': f'fc_{number}', 'call_id': f'write-{number}',
                'name': 'workspace_call', 'arguments': json.dumps({'name': 'write_note', 'arguments_json': '{}'})}]
            print(f'FOREGROUND: model step {number} completed while the summary is pending.', flush=True)
        else:
            assert number == 3 and release_summary.is_set()
            assert 'private-async-summary' in json.dumps(body)
            returns = [item for item in body['input'] if item.get('type') == 'function_call_output']
            assert [item['call_id'] for item in returns] == ['write-1', 'write-2']
            assert json.loads(returns[0]['output']) == {'result': 'new-tail-1'}
            assert json.loads(returns[1]['output']) == {'result': 'new-tail-2 ' + 'z' * 6000}
            assert 'Original task: preserve receipts.' in json.dumps(body)
            assert 'Latest correction: do not deploy.' in json.dumps(body)
            output = [{'type': 'message', 'id': 'msg_3', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'Finished with both new receipts.', 'annotations': []}]}]
            print('ADOPTED: summary plus both exact new tool receipts; original task and correction retained.', flush=True)
        tokens = counting_input(body)[1]
        return httpx.Response(200, json={'id': f'resp_{number}', 'object': 'response', 'status': 'completed',
            'model': 'test-model', 'created_at': 1770000000, 'output': output,
            'usage': {'input_tokens': tokens, 'output_tokens': 40, 'total_tokens': tokens + 40}})
    class ProviderClient(actual_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(upstream), **kwargs)
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', ProviderClient)

    run = store.create_run('Original task: preserve receipts.', '', 'modal', [], chat_enabled=True,
        harness=manager.settings.default_harness(), model='test-model', user_id='owner')
    assert run['harness'] == 'pydantic-ai'
    manager.submit(run)
    message = store.claim_message(run['id'])
    manager.save(run['id'], {'message_id': message['id'], 'segment': 0, 'turn_started': time.time()})
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], message['id'])))
    scope = TurnScope(run['id'], message['id'], actor_id='owner')
    @activity.defn(name='pydantic_prepare_v1')
    async def prepare(scope: TurnScope) -> TurnInput:
        history = [ModelRequest(parts=[UserPromptPart('Original task: preserve receipts.')])]
        history += [ModelResponse(parts=[TextPart(f'older-receipt-{i}: ' + 'x' * 1000)]) for i in range(14)]
        return TurnInput(replace(scope, model='test-model', input_budget=16808),
            'Latest correction: do not deploy.', 'Keep instructions and receipts in order.', history)
    async def call_tool(scope, call_id, name, arguments):
        await asyncio.wait_for(summary_started.wait(), 10)
        runtime.tool_calls += 1
        with (tmp_path / 'note.txt').open('a') as stream:
            stream.write(f'new-tail-{runtime.tool_calls}\n')
        print(f'TOOL: real file write {runtime.tool_calls} completed while the summary is pending.', flush=True)
        return {'result': 'new-tail-1' if runtime.tool_calls == 1 else 'new-tail-2 ' + 'z' * 6000}
    runtime.prepare, runtime.call_tool = prepare, call_tool
    runtime.model_request = manager.pydantic_runtime.model_request
    try:
        async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
            client = configured_client(env.client, converter(tmp_path))
            first = worker(client, runtime)
            task = asyncio.create_task(first.run())
            handle = await client.start_workflow(PydanticTurnWorkflow.run, scope,
                id='pydantic-background-compaction', task_queue='pydantic-test')
            try:
                await asyncio.wait_for(pressured.wait(), 25)
                assert len(inferences) == runtime.tool_calls == 2 and len(summaries) == 1
                assert not release_summary.is_set()
                print('PRESSURE: only the oversized third request waits; two file writes already finished.', flush=True)
            finally:
                await first.shutdown()
                await task
            assert runtime.tool_calls == 2
            print('RECOVERY: worker stopped during the context wait; starting a replacement.', flush=True)
            release_summary.set()
            async with worker(client, runtime):
                result = await asyncio.wait_for(handle.result(), 25)
            assert result.result['message'] == 'Finished with both new receipts.'
            assert runtime.tool_calls == 2 and (tmp_path / 'note.txt').read_text() == 'new-tail-1\nnew-tail-2\n'
            # Projection never mutates the typed checkpoint or public journal.
            assert 'private-async-summary' not in str(result.messages)
            assert 'older-receipt-0' in str(result.messages)
            assert 'private-async-summary' not in str(store.messages(run['id'])) + str(store.events(run['id']))
            history = await handle.fetch_history()
            assert 'Original task' not in history.to_json()
            await Replayer(workflows=[PydanticTurnWorkflow], data_converter=client.data_converter,
                           plugins=[PydanticAIPlugin()]).replay_workflow(history)
            assert len(inferences) == 3 and runtime.tool_calls == 2
            print('VERIFIED: deterministic Temporal replay; no repeated writes; private summary stays private.', flush=True)
    finally:
        release_summary.set()
        if proof.owner:
            await proof.owner.close()


@pytest.fixture
def durable_app(tmp_path):
    import httpx
    from app.config import Settings
    from app.main import create_app
    settings = Settings(_env_file=None, data_dir=tmp_path, temporal_enabled=True,
        session_secret='test-session-secret', encryption_key=Fernet.generate_key().decode(),
        agent_model='test-model', session_titles_enabled=False, memory_review_enabled=False,
        model_context_limits={'test-model': {'context_window': 100000, 'max_input_tokens': 90000,
                                             'max_output_tokens': 10000}})
    app = create_app(settings)
    app.state.manager.pydantic_runtime.broker_transport = httpx.ASGITransport(app=app)
    yield app
    app.state.store.close()


async def drive_session(manager, run_id, predicate=None):
    async with asyncio.timeout(25):
        while True:
            await manager.advance(run_id)
            state = manager.state(run_id)
            if predicate(state) if predicate else state.get('phase') == 'idle':
                return state
            await asyncio.sleep(.02)


async def test_default_engine_uses_real_broker_skills_and_settles_answer_without_computer(durable_app):
    from agent.skills import catalog, read
    app = durable_app
    manager, store = app.state.manager, app.state.store
    skill = catalog()[0]
    content = read(skill['name'], skill['revision'])['content']
    prepared_prompts = []
    async def model(scope, messages, settings, parameters):
        prepared_prompts.append(str(messages))
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            assert any(isinstance(p, ToolReturnPart) and p.content.get('result', {}).get('content') == content
                       for m in messages for p in m.parts)
            return ModelResponse(parts=[TextPart('Loaded the requested skill.')], model_name='test-model')
        assert content not in str(messages)  # Catalog metadata, not hardcoded skill procedures.
        return ModelResponse(parts=[ToolCallPart('workspace_call', {'name': 'bundled_skill_read',
            'arguments_json': json.dumps({'name': skill['name'], 'revision': skill['revision']})},
            tool_call_id='skill-1')], model_name='test-model')
    manager.pydantic_runtime.model_request = model
    run = store.create_run('Load the relevant skill.', '', 'modal', [], chat_enabled=True,
        harness=manager.settings.default_harness(), model='test-model', user_id='owner')
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(
            payload_codec=PrivatePayloadCodec(manager.payloads),
            failure_converter_class=DefaultFailureConverterWithEncodedAttributes))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            state = await drive_session(manager, run['id'])
        current = store.run(run['id'])
        assert current['status'] == 'idle', current['error']
        assert current['summary'] == 'Loaded the requested skill.'
        assert current['sandbox_id'] == '' and not state.get('computer')
        assert state['independent_runtime']
        assert len(prepared_prompts) == 2
        assert store.messages(run['id'])[-1]['status'] == 'completed'
        assert store.rows('SELECT 1 FROM session_items WHERE run_id=?', (run['id'],))


async def test_credential_wait_resumes_saved_turn_and_acknowledges_resolution(durable_app):
    from app.credentials import Resolve
    manager, store = durable_app.state.manager, durable_app.state.store
    async def model(scope, messages, settings, parameters):
        if 'ACCESS REQUEST RESOLVED' in str(messages):
            return ModelResponse(parts=[TextPart('Access declined; no external work attempted.')], model_name='test-model')
        return ModelResponse(parts=[ToolCallPart('workspace_call', {'name': 'credentials_request',
            'arguments_json': json.dumps({'provider': 'openai', 'reason': 'Run evaluation', 'request_key': 'test'})},
            tool_call_id='request-access')], model_name='test-model')
    manager.pydantic_runtime.model_request = model
    run = store.create_run('Run the evaluation.', '', 'modal', [], chat_enabled=True,
        harness='pydantic-ai', model='test-model', user_id='owner')
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            state = await drive_session(manager, run['id'], lambda s: s.get('phase') == 'waiting_credential')
            request_id, message_id = state['wait_credential'], state['message_id']
            assert state.get('pydantic_checkpoint') and not state.get('computer')
            manager.credentials.resolve(request_id, Resolve(decision='decline'), 'owner', False)
            await drive_session(manager, run['id'])
        assert store.run(run['id'])['summary'] == 'Access declined; no external work attempted.'
        assert manager.credentials.row(request_id)['resolution_pending'] == 0
        assert len(store.rows('SELECT id FROM credential_requests WHERE run_id=?', (run['id'],))) == 1
        manager.credentials.reconcile_resolutions(run['id'])
        assert not store.rows("SELECT id FROM messages WHERE run_id=? AND status='queued'", (run['id'],))
        assert store.rows('SELECT status FROM messages WHERE id=?', (message_id,))[0]['status'] == 'completed'


async def test_private_checkpoint_is_rebuilt_from_public_journal(durable_app):
    from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, UserPromptPart
    from app.native_sessions import mark_private_context
    from app.security import digest
    manager, store = durable_app.state.manager, durable_app.state.store
    run = store.create_run('Continue the public task.', '', 'modal', [], chat_enabled=True,
        harness='pydantic-ai', model='test-model', user_id='owner')
    manager.submit(run)
    manager.begin_turn(run['id'], store.run(run['id']))
    state = manager.state(run['id'])
    state['segment'] = 1
    checkpoint = {'scope': [run['id'], state['message_id'], 'owner', 'test-model'], 'applied': [],
        'messages': ModelMessagesTypeAdapter.dump_json([ModelRequest(parts=[UserPromptPart('private-secret-context')])]).decode()}
    state['pydantic_checkpoint'] = manager.payloads.put(json.dumps(checkpoint).encode())
    manager.save(run['id'], state)
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], state['message_id'])))
    scope = TurnScope(run['id'], state['message_id'], segment=1, actor_id='owner')
    prepared = await manager.pydantic_runtime.prepare(scope)
    assert 'private-secret-context' in str(prepared.messages)
    mark_private_context(store, store.run(run['id']))
    prepared = await manager.pydantic_runtime.prepare(scope)
    assert 'private-secret-context' not in str(prepared)
    assert 'Private context must be loaded again' in prepared.prompt


@pytest.mark.parametrize('field,value', [('status', 'stopping'), ('active_user_id', 'other'),
                                        ('active_message_id', 999), ('token_hash', 'revoked')])
async def test_default_activity_scope_rejects_stop_and_changed_ownership(durable_app, field, value):
    from temporalio.exceptions import ApplicationError
    from app.security import digest
    app = durable_app
    manager, store = app.state.manager, app.state.store
    run = store.create_run('Task', '', 'modal', [], chat_enabled=True, harness='pydantic-ai', user_id='owner')
    manager.submit(run)
    manager.begin_turn(run['id'], store.run(run['id']))
    state = manager.state(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], state['message_id'])))
    scope = TurnScope(run['id'], state['message_id'], actor_id='owner')
    assert manager.pydantic_runtime.current(scope)['id'] == run['id']
    store.execute('UPDATE runs SET ' + field + '=? WHERE id=?', (value, run['id']))
    with pytest.raises(ApplicationError, match='no longer active'):
        await manager.pydantic_runtime.catalog(scope)


async def test_stop_cancels_inflight_pydantic_workflow(durable_app):
    app = durable_app
    manager, store = app.state.manager, app.state.store
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def model(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    manager.pydantic_runtime.model_request = model
    run = store.create_run('Wait for a response.', '', 'modal', [], chat_enabled=True,
        harness='pydantic-ai', model='test-model', user_id='owner')
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            await drive_session(manager, run['id'], lambda s: entered.is_set())
            await manager.cancel(run['id'])
            await drive_session(manager, run['id'])
            await asyncio.wait_for(cancelled.wait(), 10)
        assert store.run(run['id'])['status'] == 'cancelled'
        assert store.run(run['id'])['token_hash'] == ''
        assert not store.rows("SELECT 1 FROM messages WHERE run_id=? AND role='assistant' AND status='completed'", (run['id'],))


@pytest.mark.parametrize('model_name', ['openai/gpt-test', 'anthropic/claude-test'])
async def test_official_provider_sdk_uses_sealed_responses_broker(model_name):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters
    from app.pydantic_runtime import PydanticRuntime
    from sandbox.broker_transport import CONTENT_TYPE, unseal
    calls = []
    class Edge(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            assert self.path == '/broker/test/v1/responses'
            assert self.headers['Authorization'] == 'Bearer scoped-token'
            assert self.headers['Content-Type'] == CONTENT_TYPE
            body = json.loads(unseal('scoped-token', '/v1/responses', self.rfile.read(int(self.headers['Content-Length']))))
            calls.append(body)
            raw = json.dumps({'id': 'resp_test', 'object': 'response', 'status': 'completed', 'model': model_name,
                'created_at': 1770000000, 'output': [{'id': 'msg_1', 'type': 'message', 'role': 'assistant',
                    'status': 'completed', 'content': [{'type': 'output_text', 'text': 'SDK response.', 'annotations': []}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 4, 'total_tokens': 14}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=edge.serve_forever, daemon=True)
    thread.start()
    runtime = object.__new__(PydanticRuntime)
    runtime.runner = SimpleNamespace(settings=SimpleNamespace(public_url=f'http://127.0.0.1:{edge.server_port}',
        cloudflare_access_client_id='', cloudflare_access_client_secret=''), token=lambda *args: 'scoped-token')
    runtime.current = lambda scope: {}
    try:
        result = await runtime._model_request(TurnScope('test', 1, model=model_name, input_budget=10000),
            [ModelRequest(parts=[UserPromptPart('Safe transport test.')])], None, ModelRequestParameters())
        assert result.parts[0].content == 'SDK response.'
        assert calls[0]['model'] == model_name and calls[0]['store'] is False
        assert 'previous_response_id' not in calls[0]
        assert len(calls) == 1
    finally:
        edge.shutdown()
        edge.server_close()
        thread.join()
