"""Real Temporal histories, real Deep Agents graph, scripted provider and local tools."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import DataConverter, DefaultFailureConverterWithEncodedAttributes
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from deepagents_provider import response_stream
from app.blob_storage import ObjectStorage
from app.deepagents_context import RuntimeInterceptor, deepagents_plugin
from app.deepagents_workflow import DeepAgentsTurnWorkflow, TurnInput, TurnScope
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

    @activity.defn(name='deepagents_prepare_v1')
    async def prepare(self, scope: TurnScope) -> TurnInput:
        return TurnInput(replace(scope, model='scripted', input_budget=10000),
            'Private test request: write once, then report.', 'Use workspace tools.')

    @activity.defn(name='deepagents_boundary_v1')
    async def boundary(self, scope: TurnScope, applied: list[int], elapsed: float, answer: str | None, receipt_only: bool = False) -> dict:
        self.controls.append((list(applied), answer))
        return {}

    @activity.defn(name='deepagents_publish_v1')
    async def publish(self, scope: TurnScope, identity: str, text: str, input_id: int = 0) -> None:
        self.published.append(text)

    async def catalog(self, scope):
        return [{'name': 'write_note', 'inputSchema': {'type': 'object'}}]

    async def model_request(self, scope, messages, settings, parameters):
        self.model_calls += 1
        if any(isinstance(m, ToolMessage) for m in messages):
            self.after_write.set()
            await self.release.wait()
            return AIMessage(content='Saved exactly once.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {'name': 'write_note', 'arguments_json': '{}'}, 'id': 'write-1'}])

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
    config.update(data_converter=conversion, plugins=[deepagents_plugin()])
    return Client(**config)


def worker(client, runtime):
    from app.deepagents_workers import agent_workers
    return agent_workers(client, runtime, 'deepagents-test', graceful_shutdown_timeout=timedelta(seconds=.1))


async def test_real_temporal_restart_does_not_replay_completed_tool(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    conversion = converter(tmp_path)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, conversion)
        first = worker(client, runtime)
        task = asyncio.create_task(first.run())
        handle = await client.start_workflow(DeepAgentsTurnWorkflow.run, TurnScope('test', 1),
            id='deepagents-restart', task_queue='deepagents-test')
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
        await Replayer(workflows=[DeepAgentsTurnWorkflow], data_converter=client.data_converter,
                       plugins=[deepagents_plugin()]).replay_workflow(history)


async def test_accepted_steering_survives_worker_restart_and_replays_once(tmp_path):
    class SteeringRuntime(ScriptedRuntime):
        def __init__(self, directory):
            super().__init__(directory)
            self.offer = False
            self.accepted, self.correcting = asyncio.Event(), asyncio.Event()

        @activity.defn(name='deepagents_boundary_v1')
        async def boundary(self, scope: TurnScope, applied: list[int], elapsed: float,
                           answer: str | None, receipt_only: bool = False) -> dict:
            if receipt_only and 2 in applied:
                self.accepted.set()
            if self.offer and 2 not in applied:
                return {'input': {'id': 2, 'content': 'Keep the write, change the explanation.'}}
            return {}

        async def model_request(self, scope, messages, settings, parameters):
            if 'Keep the write, change the explanation.' in str(messages):
                self.correcting.set()
                await self.release.wait()
                return AIMessage(content='Kept the write and revised the explanation.')
            return await super().model_request(scope, messages, settings, parameters)

    runtime = SteeringRuntime(tmp_path)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        first = worker(client, runtime)
        task = asyncio.create_task(first.run())
        handle = await client.start_workflow(DeepAgentsTurnWorkflow.run, TurnScope('test', 1),
            id='deepagents-steering-restart', task_queue='deepagents-test')
        try:
            await asyncio.wait_for(runtime.after_write.wait(), 25)
            runtime.offer = True
            await handle.signal(DeepAgentsTurnWorkflow.steer)
            await asyncio.wait_for(runtime.accepted.wait(), 10)
            await asyncio.wait_for(runtime.correcting.wait(), 10)
        finally:
            await first.shutdown()
            await task
        runtime.release.set()
        async with worker(client, runtime):
            result = await asyncio.wait_for(handle.result(), 30)
        assert result.applied == [2]
        assert result.result['message'] == 'Kept the write and revised the explanation.'
        assert sum(m['data']['content'] == 'Keep the write, change the explanation.' for m in result.messages) == 1
        assert runtime.tool_calls == 1 and (tmp_path / 'note.txt').read_text() == 'one write\n'
        await Replayer(workflows=[DeepAgentsTurnWorkflow], data_converter=client.data_converter,
                       plugins=[deepagents_plugin()]).replay_workflow(await handle.fetch_history())


async def test_uncertain_write_is_not_automatically_retried(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    runtime.fail_after_write = True
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        async with worker(client, runtime):
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(client.execute_workflow(DeepAgentsTurnWorkflow.run, TurnScope('test', 1),
                    id='deepagents-uncertain', task_queue='deepagents-test'), 25)
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
    @activity.defn(name='deepagents_boundary_v1')
    async def boundary(scope: TurnScope, applied: list[int], elapsed: float, answer: str | None, receipt_only: bool = False) -> dict:
        return await redirect(scope, applied, elapsed, answer)
    runtime.boundary = boundary
    async def corrected_model(scope, messages, settings, parameters):
        if any('Do not write' in str(m) for m in messages):
            return AIMessage(content='No file was written.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {'name': 'write_note', 'arguments_json': '{}'}, 'id': 'not-executed'}])
    runtime.model_request = corrected_model
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        async with worker(client, runtime):
            result = await asyncio.wait_for(client.execute_workflow(DeepAgentsTurnWorkflow.run, TurnScope('test', 1),
                id='deepagents-steering', task_queue='deepagents-test'), 25)
        assert result.result['message'] == 'No file was written.'
        assert result.applied == [2]
        assert runtime.tool_calls == 0
        assert not (tmp_path / 'note.txt').exists()
        # Middleware closes cancelled calls in the library message protocol.
        calls = {c['id'] for m in messages_from_dict(result.messages) if isinstance(m, AIMessage) for c in m.tool_calls}
        returns = {m.tool_call_id for m in messages_from_dict(result.messages) if isinstance(m, ToolMessage)}
        assert calls == returns == {'not-executed'}


async def test_background_compaction_keeps_working_preserves_tail_and_replays(durable_app, tmp_path, monkeypatch):
    import httpx
    import time
    from app.context_budget import counting_input, ModelContextLimits
    from app.harness_gateway import HarnessGateway
    from app.deepagents_runtime import SealedModelTransport
    from app.security import digest

    app = durable_app
    manager, store = app.state.manager, app.state.store
    # Keep this byte-counted pressure fixture independent of the bundled skill
    # catalog size. Skill loading is exercised separately through the real broker.
    store.execute("UPDATE skills SET archived=1 WHERE owner_id=?", ('system:bundled-coding-skills',))
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
    class LocalBroker(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async with actual_client(transport=httpx.ASGITransport(app=app)) as client:
                response = await client.request(request.method, str(request.url),
                    headers=dict(request.headers), content=await request.aread())
            return httpx.Response(response.status_code, headers=dict(response.headers), content=response.content)
    class LocalSealedTransport(SealedModelTransport):
        def __init__(self, token):
            self.token, self.transport = token, LocalBroker()
    monkeypatch.setattr('app.deepagents_runtime.SealedModelTransport', LocalSealedTransport)

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
        value = {'id': f'resp_{number}', 'object': 'response', 'status': 'completed',
            'model': 'test-model', 'created_at': 1770000000, 'output': output,
            'usage': {'input_tokens': tokens, 'output_tokens': 40, 'total_tokens': tokens + 40}}
        return httpx.Response(200, content=b''.join(response_stream(value)), headers={'content-type': 'text/event-stream'})
    class ProviderClient(actual_client):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(upstream), **kwargs)
    monkeypatch.setattr('app.harness_gateway.httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': ProviderClient}))

    run = store.create_run('Original task: preserve receipts.', '', 'modal', [], chat_enabled=True,
        harness=manager.settings.default_harness(), model='test-model', user_id='owner')
    assert run['harness'] == 'deepagents-temporal'
    manager.submit(run)
    message = store.claim_message(run['id'])
    manager.save(run['id'], {'message_id': message['id'], 'segment': 0, 'turn_started': time.time()})
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], message['id'])))
    scope = TurnScope(run['id'], message['id'], actor_id='owner')
    @activity.defn(name='deepagents_prepare_v1')
    async def prepare(scope: TurnScope) -> TurnInput:
        history = [HumanMessage(content='Original task: preserve receipts.')]
        history += [AIMessage(content=f'older-receipt-{i}: ' + 'x' * 1000) for i in range(14)]
        return TurnInput(replace(scope, model='test-model', input_budget=16808),
            'Latest correction: do not deploy.', 'Keep instructions and receipts in order.', messages_to_dict(history))
    async def call_tool(scope, call_id, name, arguments):
        await asyncio.wait_for(summary_started.wait(), 10)
        runtime.tool_calls += 1
        with (tmp_path / 'note.txt').open('a') as stream:
            stream.write(f'new-tail-{runtime.tool_calls}\n')
        print(f'TOOL: real file write {runtime.tool_calls} completed while the summary is pending.', flush=True)
        return {'result': 'new-tail-1' if runtime.tool_calls == 1 else 'new-tail-2 ' + 'z' * 6000}
    runtime.prepare, runtime.call_tool = prepare, call_tool
    runtime.model_request = manager.deepagents_runtime.model_request
    try:
        async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
            client = configured_client(env.client, converter(tmp_path))
            first = worker(client, runtime)
            task = asyncio.create_task(first.run())
            handle = await client.start_workflow(DeepAgentsTurnWorkflow.run, scope,
                id='deepagents-background-compaction', task_queue='deepagents-test')
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
            await Replayer(workflows=[DeepAgentsTurnWorkflow], data_converter=client.data_converter,
                           plugins=[deepagents_plugin()]).replay_workflow(history)
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
    settings = Settings(_env_file=None, data_dir=tmp_path, temporal_enabled=True, agent_harness='deepagents-temporal',
        session_secret='test-session-secret', encryption_key=Fernet.generate_key().decode(),
        agent_model='test-model', session_titles_enabled=False, memory_review_enabled=False,
        model_context_limits={'test-model': {'context_window': 100000, 'max_input_tokens': 90000,
                                             'max_output_tokens': 10000}})
    app = create_app(settings)
    app.state.manager.deepagents_runtime.broker_transport = httpx.ASGITransport(app=app)
    yield app
    app.state.store.close()


@pytest.fixture
async def traced_tool_runtime(durable_app, request):
    from dataclasses import asdict
    from temporalio.testing import ActivityEnvironment
    from app.config import Settings
    from app.deepagents_context import _runtime
    from app.deepagents_workflow import workspace_call
    from app.security import digest
    from app.tracing import AgentTracing

    manager, store = durable_app.state.manager, durable_app.state.store
    tracing = store.tracing = AgentTracing(store, Settings(_env_file=None,
        litellm_trace_endpoint='https://traces.example/v1/traces', litellm_trace_api_key='trace-secret',
        raindrop_write_key='', langfuse_public_key='', langfuse_secret_key='', langsmith_api_key='', braintrust_api_key=''))
    owner = store.identity({'method': 'google', 'identity': {'sub': 'trace-owner', 'email': 'trace@example.com'}})
    run = store.create_run('Trace a tool.', '', 'modal', [], chat_enabled=True,
        harness='deepagents-temporal', user_id=owner, private_owner_id=owner if getattr(request, 'param', False) else '')
    manager.submit(run)
    manager.begin_turn(run['id'], store.run(run['id']))
    scope = TurnScope(run['id'], manager.state(run['id'])['message_id'], actor_id=owner)
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], scope.message_id)))
    runtime = manager.deepagents_runtime
    runtime.journal.exchange(store.run(run['id']), {'action': 'import', 'entries': [],
        'manifest': {'id': 'empty', 'count': 0}, 'complete': True})

    async def invoke():
        token = _runtime.set(runtime)
        try:
            message = await ActivityEnvironment().run(workspace_call.ainvoke, {
                'name': 'workspace_call', 'id': 'trace-call', 'type': 'tool_call', 'args': {
                    'name': 'github_checkout', 'arguments_json': '{"repository":"private-argument-sentinel"}',
                    'scope': asdict(scope), 'call_id': 'trace-call'}})
            return json.loads(message.content)
        finally:
            _runtime.reset(token)

    yield SimpleNamespace(runtime=runtime, store=store, scope=scope, tracing=tracing, invoke=invoke)
    await tracing.close()


@pytest.mark.parametrize('failed', [False, True])
async def test_tool_invocation_exports_real_bridge_outcome(traced_tool_runtime, monkeypatch, failed):
    import httpx
    from agent.tools import mcp_bridge
    from sandbox.broker_transport import unseal
    from test_trace_outbox import spans, transport

    probe = traced_tool_runtime
    catalog = [{'name': 'github_checkout', 'inputSchema': {'type': 'object'}}]
    paths, dispatched, received = [], [], []
    monkeypatch.delenv('MOYAI_REMOTE_COMPUTER', raising=False)
    monkeypatch.setattr(mcp_bridge, 'broker', lambda *args: catalog)

    def checkout(name, arguments, *args, **kwargs):
        assert arguments == {'repository': 'private-argument-sentinel'}
        dispatched.append(name)
        if failed:
            raise ValueError('private-exception-sentinel', {'authorization': 'private-credential-sentinel'})
        return {'workspace': '/workspace', 'content': 'private-result-sentinel'}

    monkeypatch.setattr(mcp_bridge.github_tools, 'call', checkout)

    def broker(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/tools'):
            return httpx.Response(200, json=catalog)
        assert request.url.path.endswith('/agent-runtime')
        token = probe.runtime.runner.token(probe.scope.run_id, probe.scope.message_id)
        receipt = mcp_bridge.call_tool(json.loads(unseal(token, '/agent-runtime', request.content)), catalog)
        return httpx.Response(200, json={'status': 'completed', 'result': receipt})

    def receiver(request):
        received.append(request.content)
        return httpx.Response(200)

    probe.runtime.broker_transport = httpx.MockTransport(broker)
    await transport(probe.tracing, receiver)
    result = await probe.invoke()
    assert result['result']['isError'] is failed
    assert dispatched == ['github_checkout'] and len(paths) == 2
    assert await probe.tracing.outboxes[0].export_once()
    exported = spans(received[0])
    assert len(exported) == 1
    span = exported[0]
    attrs = {attr.key: attr.value.string_value for attr in span.attributes}
    trace_id, parent_id, *_ = probe.tracing.identity(probe.store.run(probe.scope.run_id), probe.scope.message_id)
    assert span.trace_id.hex() == format(trace_id, '032x') and span.parent_span_id.hex() == format(parent_id, '016x')
    assert span.name == attrs['gen_ai.tool.name'] == 'github_checkout'
    assert attrs['openinference.span.kind'] == 'TOOL' and attrs['gen_ai.operation.name'] == 'execute_tool'
    assert attrs['moyai.turn_id'] == str(probe.scope.message_id)
    assert span.status.code == (2 if failed else 1)
    if failed:
        assert attrs['error.type'] == 'ValueError'
        assert span.status.message == 'Operation error'
        assert {attr.key: attr.value.string_value for attr in span.events[0].attributes} == {
            'exception.type': 'ValueError', 'exception.message': 'Operation error'}
    else:
        assert 'error.type' not in attrs and not span.events
    assert all(secret not in received[0] for secret in (
        b'private-argument-sentinel', b'private-exception-sentinel', b'private-credential-sentinel',
        b'private-result-sentinel', b'trace-secret', b'The action was not confirmed'))


@pytest.mark.parametrize('traced_tool_runtime,outcome', [(outcome == 'private', outcome) for outcome in
    ('rejected', 'raised', 'cancelled', 'persistence', 'private', 'disabled', 'capture', 'capture_raised',
     'untyped', 'invalid_type')], indirect=['traced_tool_runtime'])
async def test_tool_invocation_tracing_preserves_execution(traced_tool_runtime, monkeypatch, outcome):
    import httpx
    from temporalio.exceptions import ApplicationError
    from test_trace_outbox import spans

    probe = traced_tool_runtime
    dispatched = []
    dispatch_started = asyncio.Event()

    async def broker(request):
        if request.url.path.endswith('/tools'):
            return httpx.Response(200, json=[] if outcome == 'rejected' else [{'name': 'github_checkout'}])
        dispatched.append(request.url.path)
        if outcome in {'raised', 'capture_raised'}:
            raise ValueError('private-transport-sentinel')
        if outcome == 'cancelled':
            dispatch_started.set()
            await asyncio.Event().wait()
        if outcome in {'untyped', 'invalid_type'}:
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'ValueError: private-error-sentinel'}]}
            if outcome == 'invalid_type':
                result['error_type'] = 'ValueError(private-error-sentinel)'
            return httpx.Response(200, json={'status': 'completed', 'result': result})
        return httpx.Response(200, json={'status': 'completed', 'result': {'isError': False}})

    def fail(*args, **kwargs):
        if outcome == 'persistence':
            probe.store.execute('UPDATE runs SET active_message_id=? WHERE id=?',
                                (probe.scope.message_id + 100, probe.scope.run_id))
        raise RuntimeError('private-persistence-sentinel')

    probe.runtime.broker_transport = httpx.MockTransport(broker)
    if outcome == 'persistence':
        monkeypatch.setattr(probe.runtime, 'append', fail)
    elif outcome == 'disabled':
        probe.tracing.enabled = False
    elif outcome in {'capture', 'capture_raised'}:
        monkeypatch.setattr(probe.tracing, 'tool', fail)
    error = {'raised': ApplicationError, 'capture_raised': ApplicationError, 'persistence': RuntimeError}.get(outcome)
    if outcome == 'cancelled':
        async with asyncio.TaskGroup() as tasks:
            invocation = tasks.create_task(probe.invoke())
            await asyncio.wait_for(dispatch_started.wait(), 5)
            invocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await invocation
    elif error:
        with pytest.raises(error) as raised:
            await probe.invoke()
        if error is ApplicationError:
            assert raised.value.non_retryable and raised.value.type == 'UncertainToolOutcome'
    else:
        result = await probe.invoke()
        assert result.get('isError') if outcome == 'rejected' else result['result']['isError'] is (outcome in {'untyped', 'invalid_type'})
    assert len(dispatched) == (0 if outcome == 'rejected' else 1)
    rows = probe.store.rows('SELECT payload FROM trace_outbox')
    if outcome in {'private', 'disabled', 'capture', 'capture_raised'}:
        assert rows == []
        return
    assert len(rows) == 1
    span = spans(rows[0]['payload'])[0]
    attrs = {attr.key: attr.value.string_value for attr in span.attributes}
    assert attrs['moyai.turn_id'] == str(probe.scope.message_id)
    assert span.status.code == (1 if outcome == 'persistence' else 2)
    expected = {'rejected': 'ToolRejected', 'raised': 'ValueError', 'cancelled': 'CancelledError',
                'untyped': 'ToolError', 'invalid_type': 'ToolError'}
    assert attrs.get('error.type') == expected.get(outcome)
    assert all(secret not in rows[0]['payload'] for secret in (
        b'private-transport-sentinel', b'private-persistence-sentinel', b'private-error-sentinel'))


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
        if any(isinstance(m, ToolMessage) for m in messages):
            assert any(isinstance(m, ToolMessage) and json.loads(m.content).get('result', {}).get('content') == content
                       for m in messages)
            return AIMessage(content='Loaded the requested skill.')
        assert content not in str(messages)  # Catalog metadata, not hardcoded skill procedures.
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {'name': 'bundled_skill_read',
            'arguments_json': json.dumps({'name': skill['name'], 'revision': skill['revision']})}, 'id': 'skill-1'}])
    manager.deepagents_runtime.model_request = model
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


@pytest.mark.parametrize('provider_name', ['modal', 'substrate', 'lambda'])
async def test_discovered_tools_acquire_one_computer_and_preserve_file_receipts(durable_app, tmp_path, monkeypatch, provider_name):
    from agent.tools.computer_tools import call as computer_call
    from test_durable import Cloud

    manager, store = durable_app.state.manager, durable_app.state.store
    manager.settings.sandbox_idle_seconds = 0
    run = store.create_run('Discover tools, write the requested file, and verify it.', '', 'modal', [],
        chat_enabled=True, harness='deepagents-temporal', model='test-model', user_id='owner', sandbox_provider=provider_name)
    cloud = Cloud(monkeypatch, store, run['id'])
    cloud.attach(manager)
    selected = []
    provider = manager.provider

    async def create_machine(**kwargs):
        machine = await cloud.create(**kwargs)
        if provider_name != 'modal':
            machine.object_id = provider_name + ':' + machine.object_id
        return machine

    async def find_machine(name, **kwargs):
        return await cloud.from_name('fixture', name)

    def selected_provider(*args, **kwargs):
        backend = provider(*args, **kwargs)
        selected.append(backend.name)
        assert backend.name == provider_name
        # Control only the external transport; retain the actual provider
        # factory, persisted selection, and identity-based recovery routing.
        backend.create, backend.find, backend.get = create_machine, find_machine, cloud.from_id
        return backend

    def no_control_guest():
        pytest.fail('Deep Agents must never provision a Modal control guest')

    monkeypatch.setattr(manager, 'provider', selected_provider)
    monkeypatch.setattr(manager.agent_runtime, 'provider', no_control_guest)
    workspace = tmp_path / 'computer'
    workspace.mkdir()
    executions, calls = [], []

    async def command(machine, action, directory, value, **kwargs):
        assert '/computer-operations/' in directory
        if action == 'start' and directory not in machine.operations:
            spec = machine.spec
            if spec.get('prepare_computer'):
                result = {'workspace': str(workspace)}
            elif spec.get('finish_computer'):
                result = {'saved': True}
            else:
                executions.append(spec['name'])
                result = {'content': [{'type': 'text', 'text': json.dumps(
                    computer_call(spec['name'], spec['arguments'], workspace))}], 'isError': False}
            machine.operations[directory] = result
        return json.dumps({'state': 'done', 'events': [], 'cursor': 0, 'exit_code': 0,
                           'final': {'tool_result': machine.operations[directory]}})

    monkeypatch.setattr(manager, 'command', command)

    async def model(scope, messages, settings, parameters):
        calls.append(scope)
        step = len(calls)
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if step == 1:
            assert not cloud.machines
            return AIMessage(content='', tool_calls=[{'name': 'workspace_tools', 'args': {}, 'id': 'discover'}])
        if step == 2:
            assert not cloud.machines
            catalog = {t['name'] for t in json.loads(results[-1].content)}
            assert {'skills_search', 'skills_load', 'agents_fanout', 'credentials_request',
                    'computer_write_file', 'computer_read_file', 'computer_terminal'} <= catalog
            name, arguments = 'computer_write_file', {'path': 'receipt.txt', 'text': 'verified-file'}
        elif step == 3:
            assert len(cloud.machines) == 1
            assert (workspace / 'receipt.txt').read_text() == 'verified-file'
            name, arguments = 'computer_read_file', {'path': 'receipt.txt'}
        else:
            assert step == 4 and len(cloud.machines) == 1
            receipt = json.loads(results[-1].content)['result']
            assert json.loads(receipt['content'][0]['text'])['text'] == 'verified-file'
            return AIMessage(content='Created and verified the requested file.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {
            'name': name, 'arguments_json': json.dumps(arguments)}, 'id': f'file-{step}'}])

    manager.deepagents_runtime.model_request = model
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            await drive_session(manager, run['id'])
    assert executions == ['computer_write_file', 'computer_read_file']
    assert selected and set(selected) == {provider_name}
    assert len({s.message_id for s in calls}) == 1
    assert cloud.snapshots == 1 and all(not machine.alive for machine in cloud.machines)
    current = store.run(run['id'])
    assert current['summary'] == 'Created and verified the requested file.'
    assert current['snapshot_id'] == 'im-1' and not current['sandbox_id']
    receipts = store.rows('SELECT name,status FROM computer_operations WHERE run_id=? ORDER BY seq', (run['id'],))
    assert receipts == [{'name': name, 'status': 'completed'} for name in executions]


@pytest.mark.parametrize('actor,model_name', [('owner', 'test-model'), ('collaborator', 'anthropic/claude-opus-5-5')])
async def test_queued_followup_keeps_history_and_rotates_capability(durable_app, actor, model_name):
    manager, store = durable_app.state.manager, durable_app.state.store
    manager.settings.model_context_limits[model_name] = manager.settings.model_context_limits['test-model']
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def model(scope, messages, settings, parameters):
        calls.append((scope, str(messages), manager.token(scope.run_id, scope.message_id)))
        if len(calls) == 1:
            entered.set()
            await release.wait()
            return AIMessage(content='The first answer is saved.')
        assert 'The first answer is saved.' in str(messages)
        assert 'Continue with the saved answer.' in str(messages)
        return AIMessage(content='The followup is complete.')

    manager.deepagents_runtime.model_request = model
    run = store.create_run('Answer the first request.', '', 'modal', [], chat_enabled=True,
        harness='deepagents-temporal', model='test-model', user_id='owner')
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            try:
                await drive_session(manager, run['id'], lambda state: entered.is_set())
                args = (run['id'], 'Continue with the saved answer.', 'followup-once', model_name)
                followup, created = store.enqueue_message(*args, user_id=actor)
                assert created
                assert not store.enqueue_message(*args, user_id=actor)[1]
                manager.submit(store.run(run['id']))
            finally:
                release.set()
            # A completed turn briefly becomes idle before the controller
            # claims its queued successor. Drive both turns to settlement.
            await drive_session(manager, run['id'], lambda state:
                state.get('phase') == 'idle' and not store.has_queued_messages(run['id']))

    assert len(calls) == 2
    first, second = calls
    assert second[0].message_id == followup['id'] != first[0].message_id
    assert (second[0].actor_id, second[0].model) == (actor, model_name)
    assert second[2] != first[2]
    messages = store.messages(run['id'])
    assert [m['content'] for m in messages if m['role'] == 'assistant'] == [
        'The first answer is saved.', 'The followup is complete.']
    assert all(m['status'] == 'completed' for m in messages)
    assert store.run(run['id'])['harness'] == 'deepagents-temporal'
    assert store.run(run['id'])['token_hash'] == ''
    assert not store.has_queued_messages(run['id'])


async def test_credential_wait_resumes_saved_turn_and_acknowledges_resolution(durable_app):
    from app.credentials import Resolve
    manager, store = durable_app.state.manager, durable_app.state.store
    async def model(scope, messages, settings, parameters):
        if 'ACCESS REQUEST RESOLVED' in str(messages):
            return AIMessage(content='Access declined; no external work attempted.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {'name': 'credentials_request',
            'arguments_json': json.dumps({'provider': 'openai', 'reason': 'Run evaluation', 'request_key': 'test'})}, 'id': 'request-access'}])
    manager.deepagents_runtime.model_request = model
    run = store.create_run('Run the evaluation.', '', 'modal', [], chat_enabled=True,
        harness='deepagents-temporal', model='test-model', user_id='owner')
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            state = await drive_session(manager, run['id'], lambda s: s.get('phase') == 'waiting_credential')
            request_id, message_id = state['wait_credential'], state['message_id']
            assert state.get('deepagents_checkpoint') and not state.get('computer')
            manager.credentials.resolve(request_id, Resolve(decision='decline'), 'owner', False)
            await drive_session(manager, run['id'])
        assert store.run(run['id'])['summary'] == 'Access declined; no external work attempted.'
        assert manager.credentials.row(request_id)['resolution_pending'] == 0
        assert len(store.rows('SELECT id FROM credential_requests WHERE run_id=?', (run['id'],))) == 1
        manager.credentials.reconcile_resolutions(run['id'])
        assert not store.rows("SELECT id FROM messages WHERE run_id=? AND status='queued'", (run['id'],))
        assert store.rows('SELECT status FROM messages WHERE id=?', (message_id,))[0]['status'] == 'completed'


async def test_child_wait_resumes_parent_once_with_saved_results(durable_app):
    manager, store = durable_app.state.manager, durable_app.state.store
    calls = []

    async def model(scope, messages, settings, parameters):
        calls.append(scope.run_id)
        if store.run(scope.run_id)['parent_run_id']:
            return AIMessage(content='The child verified its assignment.')
        if 'PARALLEL WORKERS HAVE SETTLED' in str(messages):
            assert 'The child verified its assignment.' in str(messages)
            return AIMessage(content='The parent integrated the saved result.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'args': {
            'name': 'agents_fanout', 'arguments_json': json.dumps({'request_key': 'one-child', 'wait': True,
                'tasks': [{'label': 'Verification', 'prompt': 'Verify the assigned result.'}]})}, 'id': 'fanout'}])

    manager.deepagents_runtime.model_request = model
    run = store.create_run('Delegate verification and integrate the result.', '', 'modal', [], chat_enabled=True,
        harness='deepagents-temporal', model='test-model', user_id='owner')
    manager.submit(run)
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        manager.temporal = configured_client(env.client, DataConverter(payload_codec=PrivatePayloadCodec(manager.payloads)))
        async with manager.make_worker(manager.temporal), manager.make_agent_worker(manager.temporal):
            waiting = await drive_session(manager, run['id'], lambda state: state.get('phase') == 'waiting_children')
            children = manager.coordinator.children(waiting['wait_group'])
            assert len(children) == 1 and children[0]['harness'] == 'deepagents-temporal'
            assert not store.run(run['id'])['token_hash']
            await drive_session(manager, children[0]['id'])
            await drive_session(manager, run['id'])
    assert calls == [run['id'], children[0]['id'], run['id']]
    assert len(manager.coordinator.children(waiting['wait_group'])) == 1
    assert store.run(run['id'])['summary'] == 'The parent integrated the saved result.'
    assert store.run(run['id'])['status'] == 'idle'
    assert store.run(children[0]['id'])['summary'] == 'The child verified its assignment.'
    assert not store.run(run['id'])['sandbox_id']


async def test_private_checkpoint_is_rebuilt_from_public_journal(durable_app):
    from app.native_sessions import mark_private_context
    from app.security import digest
    manager, store = durable_app.state.manager, durable_app.state.store
    # Start without private skill-index context, then explicitly taint the turn.
    store.execute("UPDATE skills SET archived=1 WHERE owner_id=?", ('system:bundled-coding-skills',))
    run = store.create_run('Continue the public task.', '', 'modal', [], chat_enabled=True,
        harness='deepagents-temporal', model='test-model', user_id='owner')
    manager.submit(run)
    manager.begin_turn(run['id'], store.run(run['id']))
    state = manager.state(run['id'])
    state['segment'] = 1
    checkpoint = {'scope': [run['id'], state['message_id'], 'owner', 'test-model'], 'applied': [],
        'messages': messages_to_dict([HumanMessage(content='private-secret-context')])}
    state['deepagents_checkpoint'] = manager.payloads.put(json.dumps(checkpoint).encode())
    manager.save(run['id'], state)
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], state['message_id'])))
    scope = TurnScope(run['id'], state['message_id'], segment=1, actor_id='owner')
    prepared = await manager.deepagents_runtime.prepare(scope)
    assert 'private-secret-context' in str(prepared.messages)
    mark_private_context(store, store.run(run['id']))
    prepared = await manager.deepagents_runtime.prepare(scope)
    assert 'private-secret-context' not in str(prepared)
    assert 'Private context must be loaded again' in prepared.prompt


@pytest.mark.parametrize('field,value', [('status', 'stopping'), ('active_user_id', 'other'),
                                        ('active_message_id', 999), ('token_hash', 'revoked')])
async def test_default_activity_scope_rejects_stop_and_changed_ownership(durable_app, field, value):
    from temporalio.exceptions import ApplicationError
    from app.security import digest
    app = durable_app
    manager, store = app.state.manager, app.state.store
    run = store.create_run('Task', '', 'modal', [], chat_enabled=True, harness='deepagents-temporal', user_id='owner')
    manager.submit(run)
    manager.begin_turn(run['id'], store.run(run['id']))
    state = manager.state(run['id'])
    store.update_run(run['id'], status='running', token_hash=digest(manager.token(run['id'], state['message_id'])))
    scope = TurnScope(run['id'], state['message_id'], actor_id='owner')
    assert manager.deepagents_runtime.current(scope)['id'] == run['id']
    store.execute('UPDATE runs SET ' + field + '=? WHERE id=?', (value, run['id']))
    with pytest.raises(ApplicationError, match='no longer active'):
        await manager.deepagents_runtime.catalog(scope)


async def test_stop_cancels_inflight_deepagents_workflow(durable_app):
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
    manager.deepagents_runtime.model_request = model
    run = store.create_run('Wait for a response.', '', 'modal', [], chat_enabled=True,
        harness='deepagents-temporal', model='test-model', user_id='owner')
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
    from app.deepagents_runtime import DeepAgentsRuntime
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
            raw = b''.join(response_stream(json.loads(raw)))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    edge = ThreadingHTTPServer(('127.0.0.1', 0), Edge)
    thread = threading.Thread(target=edge.serve_forever, daemon=True)
    thread.start()
    runtime = object.__new__(DeepAgentsRuntime)
    runtime.runner = SimpleNamespace(settings=SimpleNamespace(public_url=f'http://127.0.0.1:{edge.server_port}',
        cloudflare_access_client_id='', cloudflare_access_client_secret=''), token=lambda *args: 'scoped-token')
    runtime.current = lambda scope: {}
    try:
        result = await runtime._model_request(TurnScope('test', 1, model=model_name, input_budget=10000),
            [HumanMessage(content='Safe transport test.')], None, [])
        assert result.text == 'SDK response.'
        assert calls[0]['model'] == model_name and calls[0]['store'] is False
        assert 'previous_response_id' not in calls[0]
        assert len(calls) == 1
    finally:
        edge.shutdown()
        edge.server_close()
        thread.join()


async def execute_scripted(runtime, tmp_path, identity):
    async with await WorkflowEnvironment.start_local(dev_server_log_level='error') as env:
        client = configured_client(env.client, converter(tmp_path))
        async with worker(client, runtime):
            return await asyncio.wait_for(client.execute_workflow(DeepAgentsTurnWorkflow.run, TurnScope('test', 1),
                id=identity, task_queue='deepagents-test'), 30)


async def test_tool_screenshot_survives_graph_and_saved_conversation(tmp_path):
    pixels = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF9sAAAAASUVORK5CYII='

    class ScreenshotRuntime(ScriptedRuntime):
        async def call_tool(self, scope, call_id, name, arguments):
            return {'result': {'content': [{'type': 'text', 'text': 'Screenshot captured.'},
                {'type': 'image', 'mimeType': 'image/png', 'data': pixels}], 'isError': False}}

        async def model_request(self, scope, messages, settings, parameters):
            if isinstance(messages[-1], ToolMessage):
                assert messages[-1].content[1]['image_url']['url'] == 'data:image/png;base64,' + pixels
                return AIMessage(content='Screenshot received.')
            return AIMessage(content='', tool_calls=[{'name': 'workspace_call',
                'args': {'name': 'write_note', 'arguments_json': '{}'}, 'id': 'capture'}])

    result = await execute_scripted(ScreenshotRuntime(tmp_path), tmp_path, 'screenshot-content')
    saved = next(m for m in messages_from_dict(result.messages) if isinstance(m, ToolMessage))
    assert saved.content[1]['image_url']['url'] == 'data:image/png;base64,' + pixels
    assert result.result['message'] == 'Screenshot received.'


async def test_only_scoped_bridge_tools_are_exposed_and_model_cannot_forge_scope(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    observed = []
    async def model(scope, messages, settings, schemas):
        assert {t['function']['name'] for t in schemas} == {'workspace_tools', 'workspace_call'}
        assert all(not {'scope', 'call_id'} & t['function']['parameters']['properties'].keys() for t in schemas)
        if any(isinstance(m, ToolMessage) for m in messages):
            return AIMessage(content='Used the authorized scope.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'id': 'trusted-call', 'args': {
            'name': 'write_note', 'arguments_json': '{}', 'scope': {'run_id': 'victim'}, 'call_id': 'forged'}}])
    async def call(scope, call_id, name, arguments):
        observed.append((scope.run_id, scope.message_id, call_id))
        return {'result': 'saved'}
    runtime.model_request, runtime.call_tool = model, call
    result = await execute_scripted(runtime, tmp_path, 'scope-injection')
    assert result.result['completed']
    assert observed == [('test', 1, 'trusted-call')]


async def test_wait_in_parallel_tool_batch_cancels_later_actions_and_closes_calls(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    marker = 'a' * 32
    async def model(*args):
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'id': f'call-{i}',
            'args': {'name': 'write_note', 'arguments_json': '{}'}} for i in range(2)])
    async def call(*args):
        runtime.tool_calls += 1
        return {'result': 'Waiting for access.', 'wait_credential': marker}
    runtime.model_request, runtime.call_tool = model, call
    result = await execute_scripted(runtime, tmp_path, 'batch-wait')
    assert result.result == {'completed': False, 'continuation': True, 'message': '', 'wait_credential': marker}
    assert runtime.tool_calls == 1
    returns = [m for m in messages_from_dict(result.messages) if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in returns} == {'call-0', 'call-1'}
    assert returns[1].status == 'error'


async def test_segment_yields_after_fifty_completed_model_tool_steps(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    async def model(*args):
        runtime.model_calls += 1
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'id': f'call-{runtime.model_calls}',
            'args': {'name': 'write_note', 'arguments_json': '{}'}}])
    runtime.model_request = model
    result = await execute_scripted(runtime, tmp_path, 'bounded-history')
    assert result.result == {'completed': False, 'continuation': True, 'message': ''}
    assert runtime.model_calls == runtime.tool_calls == 50
    returns = [m for m in messages_from_dict(result.messages) if isinstance(m, ToolMessage)]
    assert len(returns) == 50
    assert returns[-1].tool_call_id == 'call-50'


async def test_model_limit_fails_without_executing_an_extra_request(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    original_prepare = runtime.prepare
    @activity.defn(name='deepagents_prepare_v1')
    async def prepare(scope: TurnScope) -> TurnInput:
        return replace(await original_prepare(scope), max_requests=1)
    runtime.prepare = prepare
    with pytest.raises(WorkflowFailureError):
        await execute_scripted(runtime, tmp_path, 'request-limit')
    assert runtime.model_calls == runtime.tool_calls == 1


async def test_invalid_tool_arguments_return_error_before_dispatch(tmp_path):
    runtime = ScriptedRuntime(tmp_path)
    async def model(scope, messages, settings, schemas):
        if any(isinstance(m, ToolMessage) for m in messages):
            assert messages[-1].status == 'error'
            return AIMessage(content='No tool was called.')
        return AIMessage(content='', tool_calls=[{'name': 'workspace_call', 'id': 'invalid', 'args': {}}])
    runtime.model_request = model
    result = await execute_scripted(runtime, tmp_path, 'invalid-schema')
    assert result.result['message'] == 'No tool was called.'
    assert runtime.tool_calls == 0
