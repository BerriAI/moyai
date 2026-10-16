"""Provider fault qualification through HTTP, the real SDK, graph and Temporal."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage
from temporalio.api.failure.v1 import Failure
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.exceptions import ActivityError, ApplicationError, TimeoutError, TimeoutType

from app.db import database
from app.deepagents_diagnostics import workflow_failure as terminal_failure
from app.workspace_diagnostics import failure_record
from deepagents_provider import answer, event, response_stream
from test_deepagents_runtime import durable_app  # noqa: F401
from test_deepagents_acceptance import web_worker, create, until
from test_deepagents_publication import active


def workflow_failure(cause, activity_type='deepagents.invoke_tool'):
    activity = ActivityError('PRIVATE activity message', scheduled_event_id=1, started_event_id=2,
        identity='PRIVATE worker identity', activity_type=activity_type, activity_id='71', retry_state=None)
    activity.__cause__ = cause
    return WorkflowFailureError(cause=activity)


async def fail_workflow(manager, store, scope, cause):
    state = {**manager.state(scope.run_id), 'phase': 'monitor', 'deepagents_workflow_id': 'fixture-workflow'}
    manager.save(scope.run_id, state)
    handle = SimpleNamespace(describe=AsyncMock(return_value=SimpleNamespace(status=WorkflowExecutionStatus.FAILED)),
                             result=AsyncMock(side_effect=cause))
    manager.temporal = SimpleNamespace(get_workflow_handle=lambda *args, **kwargs: handle)
    assert await manager.deepagents_execution.step(scope.run_id, state) is True
    return [event for event in store.events(scope.run_id) if event['data'].get('phase') == 'sdk_failure']


async def test_terminal_workflow_diagnostic_replaces_recovered_broker_error(active):
    manager, store, scope = active
    store.event(scope.run_id, 'error', 'Earlier model cancellation', {
        'phase': 'broker_failure', 'error_code': 'cancelled', 'http_status': 200,
        'request_id': 'recovered-model-request', 'exception_type': 'CancelledError'})
    cause = ApplicationError('PRIVATE response body', 500, type='BrokerRejected')
    events = await fail_workflow(manager, store, scope, workflow_failure(cause))
    assert len(events) == 1
    assert events[0]['data'] == {'phase': 'sdk_failure', 'version': 1, 'sdk': 'deepagents-temporal',
        'source': 'workflow_failure', 'activity_type': 'deepagents.invoke_tool',
        'exception_type': 'BrokerRejected', 'non_retryable': False, 'http_status': 500, 'turn_id': scope.message_id}
    assert 'PRIVATE' not in str(events)
    assert 'recovered-model-request' not in str(events)
    public = failure_record({**events[0], 'data': json.dumps(events[0]['data'])})
    assert public['sdk'] == 'deepagents-temporal'
    assert public['activity_type'] == 'deepagents.invoke_tool'
    assert public['exception_type'] == 'BrokerRejected'
    assert public['http_status'] == 500
    assert manager.state(scope.run_id)['phase'] == 'cleanup'
    assert manager.state(scope.run_id)['outcome'] == 'failed'
    assert 'Inspect saved action receipts' in store.run(scope.run_id)['error']


@pytest.mark.parametrize('status', [403, 500])
async def test_real_broker_status_survives_default_temporal_failure_conversion(active, status):
    manager, store, scope = active
    manager.deepagents_runtime.broker_transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={'error': 'PRIVATE provider body'}))
    with pytest.raises(ApplicationError) as caught:
        await manager.deepagents_runtime.broker(scope, '/tools')
    encoded = Failure()
    converter = DataConverter.default
    await converter.encode_failure(workflow_failure(caught.value).cause, encoded)
    decoded = WorkflowFailureError(cause=await converter.decode_failure(encoded))
    events = await fail_workflow(manager, store, scope, decoded)
    assert events[0]['data']['exception_type'] == 'BrokerRejected'
    assert events[0]['data']['http_status'] == status
    assert events[0]['data']['activity_type'] == 'deepagents.invoke_tool'
    assert decoded.cause.cause.non_retryable is (status == 403)
    assert 'PRIVATE provider body' not in str(encoded)
    assert 'PRIVATE' not in str(events)


@pytest.mark.parametrize('detail', [True, 99, 600, '500', {'response_body': 'PRIVATE', 'http_status': 500}])
async def test_workflow_diagnostic_does_not_infer_http_status_from_arbitrary_details(active, detail):
    manager, store, scope = active
    cause = ApplicationError('PRIVATE body (500)', detail, type='BrokerRejected')
    events = await fail_workflow(manager, store, scope, workflow_failure(cause))
    assert len(events) == 1
    assert events[0]['data']['exception_type'] == 'BrokerRejected'
    assert 'http_status' not in events[0]['data']
    assert 'PRIVATE' not in str(events)


async def test_workflow_diagnostic_keeps_ambiguous_tool_outcome_without_copying_exception_text(active):
    manager, store, scope = active
    cause = ApplicationError('PRIVATE request containing credentials', type='UncertainToolOutcome', non_retryable=True)
    events = await fail_workflow(manager, store, scope, workflow_failure(cause))
    assert len(events) == 1
    assert events[0]['data']['exception_type'] == 'UncertainToolOutcome'
    assert events[0]['data']['activity_type'] == 'deepagents.invoke_tool'
    assert 'PRIVATE' not in str(events)


async def test_workflow_diagnostic_does_not_copy_untrusted_activity_or_error_type(active):
    manager, store, scope = active
    cause = ApplicationError('PRIVATE body', type='<script>PRIVATE</script>')
    events = await fail_workflow(manager, store, scope, workflow_failure(cause, activity_type='PRIVATE activity'))
    assert len(events) == 1
    assert events[0]['data']['exception_type'] == 'ApplicationError'
    assert 'activity_type' not in events[0]['data']
    assert 'PRIVATE' not in str(events)


def test_workflow_timeout_diagnostic_omits_private_heartbeat_payloads():
    cause = TimeoutError('PRIVATE timeout message', type=TimeoutType.HEARTBEAT,
                         last_heartbeat_details=[{'content': 'PRIVATE tool arguments'}])
    diagnostic = terminal_failure(workflow_failure(cause))
    assert diagnostic['exception_type'] == 'TimeoutError'
    assert diagnostic['activity_type'] == 'deepagents.invoke_tool'
    assert 'PRIVATE' not in str(diagnostic)


def test_diagnostic_projection_does_not_admit_unknown_activity_names():
    row = {'id': 1, 'created_at': '2026-10-12T18:51:36Z', 'data': json.dumps({
        'phase': 'sdk_failure', 'sdk': 'deepagents-temporal', 'activity_type': 'PRIVATE',
        'exception_type': 'BrokerRejected', 'http_status': 500, 'details': 'PRIVATE'})}
    assert 'PRIVATE' not in str(failure_record(row))


async def test_real_workflow_reports_terminal_catalog_failure_after_recovered_model_error(durable_app, monkeypatch):
    app = durable_app
    manager, store = app.state.manager, app.state.store
    model_calls, rejected_reads, dispatched_writes = [], [], []

    async def model(scope, messages, settings, parameters):
        model_calls.append(scope)
        if len(model_calls) == 1:
            await database(store.event, scope.run_id, 'error', 'Recovered model request', {
                'phase': 'broker_failure', 'exception_type': 'CancelledError', 'error_code': 'cancelled',
                'http_status': 200, 'request_id': 'recovered-model-request'}, turn_id=scope.message_id)
        tool = {'name': 'workspace_tools', 'args': {}, 'id': f'catalog-{len(model_calls)}'}
        if len(model_calls) == 9:
            tool = {'name': 'workspace_call', 'args': {'name': 'bundled_skill_catalog', 'arguments_json': '{}'},
                    'id': 'unattempted-tool'}
        return AIMessage(content='', tool_calls=[tool])

    monkeypatch.setattr(manager.deepagents_runtime, 'model_request', model)
    async with web_worker(app, activity_slots=2) as api:
        async with httpx.AsyncHTTPTransport() as transport:
            async def broker(request):
                if request.url.path.endswith('/tools') and len(model_calls) >= 9:
                    rejected_reads.append(request)
                    return httpx.Response(500, json={'error': 'PRIVATE server detail'})
                if request.url.path.endswith('/tools/call'):
                    dispatched_writes.append(request)
                return await transport.handle_async_request(request)
            manager.deepagents_runtime.broker_transport = httpx.MockTransport(broker)
            run_id = await create(api, 'Verify terminal diagnostics without dispatching a write.', [])
            await until(lambda: store.run(run_id)['status'] in {'idle', 'failed', 'cancelled'}, seconds=40)
            assert store.run(run_id)['status'] == 'failed'
            assert len(model_calls) == 9
            assert len(rejected_reads) == 3
            assert dispatched_writes == []
            response = (await api.get(f'/api/runs/{run_id}')).json()
            answer = next(message for message in response['messages'] if message['role'] == 'assistant')
            terminal = [event for event in response['events'] if event['data'].get('phase') == 'sdk_failure']
            assert len(terminal) == 1
            assert terminal[0]['data']['turn_id'] == answer['response_to_id']
            assert terminal[0]['data']['sdk'] == 'deepagents-temporal'
            assert terminal[0]['data']['activity_type'] == 'deepagents.invoke_tool'
            assert terminal[0]['data']['exception_type'] == 'BrokerRejected'
            assert terminal[0]['data']['http_status'] == 500
            assert 'recovered-model-request' not in str(terminal)
            assert 'PRIVATE' not in str(response)


@pytest.mark.parametrize('after_write', [False, True])
@pytest.mark.parametrize('failure', ['truncated', 'incomplete', 'truncated_tool', 'incomplete_tool',
                                  'permission', 'policy', 'quota', 'rate_limit'])
async def test_provider_failure_never_completes_or_repeats_prior_tool(durable_app, monkeypatch, tmp_path, failure, after_write):
    app = durable_app
    store = app.state.store
    calls = []
    receipt = tmp_path / 'receipt.txt'
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')

    async def connected(name, arguments, **kwargs):
        assert name == 'notion_search'
        # The external transport creates a countable, non-idempotent receipt.
        # Everything before and after this transport runs through the real app.
        with receipt.open('a') as stream:
            stream.write('completed tool\n')
        return {'results': [{'id': 'fixture-result'}]}

    monkeypatch.setattr(app.state.connectors, 'call', connected)
    codes = {'permission': 'permission_error', 'policy': 'cyber_policy',
             'quota': 'insufficient_quota', 'rate_limit': 'rate_limit_error'}

    def tool_response(identity):
        response = answer(identity=identity)
        response['output'] = [{'type': 'function_call', 'id': 'fc_' + identity, 'call_id': 'call_' + identity,
            'name': 'workspace_call', 'status': 'completed', 'arguments': json.dumps({
                'name': 'notion_search', 'arguments_json': json.dumps({'query': identity})})}]
        return response

    async def upstream(request):
        if not request.url.path.endswith('/responses'):
            return httpx.Response(200, json={'total_tokens': 100})
        calls.append(json.loads(request.content))
        if after_write and len(calls) == 1:
            response = tool_response('completed')
            wire = list(response_stream(response))
        elif failure in codes:
            status = 403 if failure == 'permission' else 400 if failure == 'policy' else 429
            return httpx.Response(status, headers={'Retry-After': '1'},
                json={'error': {'code': codes[failure], 'message': 'private-provider-body'}})
        else:
            response = tool_response('unfinished') if failure.endswith('_tool') else answer('This answer is unfinished.')
            wire = list(response_stream(response))
            if failure.startswith('truncated'):
                wire = wire[:-1]
            else:
                response.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
                wire[-1] = event('response.incomplete', response=response)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=b''.join(wire))

    actual = httpx.AsyncClient
    class ProviderClient(actual):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(upstream), **kw)
    monkeypatch.setattr('app.harness_gateway.httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': ProviderClient}))
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Process the fixture, preserve the tool result, then answer.', ['notion'])
        await until(lambda: store.run(run_id)['status'] in {'idle', 'failed', 'cancelled'}, seconds=40)
        assert store.run(run_id)['status'] == 'failed'
        assert not [m for m in store.messages(run_id) if m['role'] == 'assistant' and m['status'] == 'completed']
        assert 1 <= len(calls) - int(after_write) <= 3
        if failure in {'permission', 'policy'} or failure.startswith('incomplete'):
            assert len(calls) == 1 + int(after_write)
        assert receipt.read_text() == 'completed tool\n' if after_write else not receipt.exists()
        journal = store.rows('SELECT * FROM session_items WHERE run_id=?', (run_id,))
        assert 'This answer is unfinished.' not in str(journal)
        assert ('fixture-result' in str(journal)) is after_write
        diagnostics = [e['data'].get('error_code') for e in store.events(run_id, limit=1000)
                       if e['data'].get('phase') == 'broker_failure']
        expected = codes.get(failure, 'stream_incomplete' if failure.startswith('truncated') else 'max_output_tokens')
        assert expected in diagnostics
        terminal = [event['data'] for event in store.events(run_id, limit=1000)
                    if event['data'].get('phase') == 'sdk_failure']
        assert len(terminal) == 1
        assert terminal[0]['sdk'] == 'deepagents-temporal'
        assert terminal[0]['source'] == 'workflow_failure'
        assert terminal[0]['activity_type'] == 'deepagents.invoke_model'
        assert 'private-provider-body' not in str(store.events(run_id, limit=1000))
