import asyncio
import inspect
import json

import httpx
import pytest

from app.harness_gateway import NativeUsageCapture, authorized_payload
from app.model_slots import ModelSlots
from app.security import digest
from test_workspace import workspace


@pytest.mark.parametrize('route', ['messages', 'responses', 'chat/completions'])
@pytest.mark.parametrize('upstream_status', [401, 403, 429, 503])
def test_gateway_failure_keeps_status_ledger_and_request_correlation(workspace, monkeypatch, caplog, route, upstream_status):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(lambda req: httpx.Response(upstream_status,
            headers={'x-request-id': 'provider-123', 'x-litellm-call-id': 'gateway-456', 'x-private': 'private-header'},
            json={'error': {'message': 'private-provider-body'}})), **kw))
    run = app.state.store.create_run('private-prompt', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('private-capability'))
    correlation = '1' * 32
    caplog.set_level('INFO', logger='uvicorn.error.moyai.broker')
    response = client.post(f"/broker/{run['id']}/v1/{route}?secret=private-query",
        headers={'Authorization': 'Bearer private-capability', 'X-Moyai-Request-ID': correlation},
        json={'messages': [{'role': 'user', 'content': 'private-prompt'}], 'input': [], 'stream': False})
    assert response.status_code == 502
    assert response.headers['x-moyai-upstream-status'] == str(upstream_status)
    assert response.headers['x-moyai-request-id'] == correlation
    assert response.headers['x-request-id'] == 'provider-123'
    assert response.headers['x-litellm-call-id'] == 'gateway-456'
    row = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert response.headers['x-moyai-model-request-id'] == row['id']
    records = [json.loads(record.message) for record in caplog.records if record.name == 'uvicorn.error.moyai.broker']
    assert [record['event'] for record in records] == ['broker_request_started', 'broker_request_failed']
    assert all(record['request_id'] == correlation for record in records)
    assert records[-1]['upstream_status'] == upstream_status
    assert records[-1]['request_ids']['x-moyai-model-request-id'] == row['id']
    assert 'private' not in json.dumps(records)


@pytest.mark.parametrize('route', ['messages', 'chat/completions'])
def test_gateway_network_failure_keeps_exception_type_without_exception_text(workspace, monkeypatch, caplog, route):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    actual = httpx.AsyncClient
    def unavailable(request):
        raise httpx.ReadTimeout('private-url-and-token', request=request)
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(unavailable), **kw))
    run = app.state.store.create_run('network test', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    caplog.set_level('INFO', logger='uvicorn.error.moyai.broker')
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer cap'},
                           json={'messages': [], 'stream': False})
    assert response.status_code == 502
    assert response.headers['x-moyai-upstream-error-type'] == 'ReadTimeout'
    row = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert response.headers['x-moyai-model-request-id'] == row['id']
    records = [json.loads(record.message) for record in caplog.records if record.name == 'uvicorn.error.moyai.broker']
    assert records[-1]['upstream_error_type'] == 'ReadTimeout'
    assert 'private' not in json.dumps(records)


async def test_broker_diagnostics_capture_partial_stream_failure(caplog):
    from app.broker_diagnostics import BrokerDiagnosticsMiddleware
    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'partial', 'more_body': True})
        raise httpx.ReadError('private-response-body')
    replies = []
    async def send(message):
        replies.append(message)
    caplog.set_level('INFO', logger='uvicorn.error.moyai.broker')
    scope = {'type': 'http', 'path': '/broker/' + 'a' * 32 + '/v1/messages', 'method': 'POST',
             'headers': [(b'x-moyai-request-id', b'invalid secret')], 'query_string': b'private-query'}
    with pytest.raises(httpx.ReadError):
        await BrokerDiagnosticsMiddleware(app)(scope, None, send)
    records = [json.loads(record.message) for record in caplog.records if record.name == 'uvicorn.error.moyai.broker']
    assert len(records) == 2 and records[-1]['event'] == 'broker_request_failed'
    assert records[-1]['http_status'] == 200 and records[-1]['response_started']
    assert records[-1]['response_bytes'] == 7 and records[-1]['error_type'] == 'ReadError'
    assert len(records[-1]['request_id']) == 32
    assert 'private' not in json.dumps(records) and 'secret' not in json.dumps(records)


def test_native_gateway_preserves_discovered_tool_references(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    tools = [{'name': 'mcp__moyai__echo', 'defer_loading': True,
              'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}}}}]
    messages = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'search',
                  'name': 'ToolSearch', 'input': {'query': 'echo', 'max_results': 1}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'search',
                  'content': [{'type': 'tool_reference', 'tool_name': 'mcp__moyai__echo'}]}]}]
    beta = 'advanced-tool-use-2025-11-20'
    def upstream(request):
        body = json.loads(request.content)
        assert body['tools'] == tools and body['messages'] == messages
        assert request.headers['anthropic-beta'] == beta
        return httpx.Response(200, json={'id': 'fixture', 'usage': {'input_tokens': 10, 'output_tokens': 1}})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('tool search', '', 'modal', [], harness='claude-agent-sdk')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/messages",
        headers={'Authorization': 'Bearer cap', 'anthropic-beta': beta},
        json={'messages': messages, 'tools': tools, 'stream': False})
    assert response.status_code == 200


@pytest.mark.parametrize('route,body,wire', [
    ('messages', {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}],
                  'tools': [{'name': 'Read', 'input_schema': {'type': 'object'}}]},
     b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_test","usage":{"input_tokens":11,"output_tokens":0}}}\n\nevent: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":3,"cost":0.01234567890123456789}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'),
    ('responses', {'input': [{'type': 'function_call_output', 'call_id': 'one', 'output': 'receipt'}],
                   'tools': [{'type': 'custom', 'name': 'apply_patch', 'format': {'type': 'text'}}]},
     b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed","usage":{"input_tokens":11,"output_tokens":3,"cost":0.01234567890123456789}}}\n\n'),
])
@pytest.mark.parametrize('model', ['openai/gpt-6-astra', 'openai/gpt-6.1-sol', 'anthropic/claude-opus-5-5', 'fireworks_ai/glm-5p3'])
def test_native_gateway_preserves_protocol_stream_and_pins_access(workspace, monkeypatch, route, body, wire, model):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'server-only-key'
    seen = []
    def upstream(request):
        assert str(request.url) == 'https://gateway.example/v1/' + route
        assert request.headers['authorization'] == 'Bearer server-only-key'
        payload = json.loads(request.content)
        seen.append(payload)
        field = 'messages' if route == 'messages' else 'input'
        assert payload[field] == body[field]
        assert payload['tools'] == body['tools']
        assert payload['model'] == model
        assert payload['stream'] is True
        assert 'api_base' not in payload and 'api_key' not in payload
        return httpx.Response(200, content=wire, headers={'Content-Type': 'text/event-stream'})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('native gateway', '', 'modal', [], model=model)
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    url = f"/broker/{run['id']}/v1/{route}"
    payload = {**body, 'stream': True, 'model': 'override', 'api_key': 'override', 'api_base': 'https://evil.example'}
    assert client.post(url, json=payload).status_code == 401
    response = client.post(url, json=payload, headers={'Authorization': 'Bearer cap'})
    assert response.status_code == 200 and response.content == wire
    row = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert row['status'] == 'completed'
    assert row['cost'] == '0.01234567890123456789' and row['cost_source'] == 'response_usage'
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (11, 3, 14)
    app.state.store.update_run(run['id'], status='stopping', token_hash='')
    assert client.post(url, json=payload, headers={'Authorization': 'Bearer cap'}).status_code == 401
    assert len(seen) == 1


def test_native_error_event_is_not_accounted_as_success():
    capture = NativeUsageCapture(True, route='/v1/responses')
    capture.feed(b'data: {"type":"response.failed","response":{"status":"failed"}}\n\n')
    capture.finish()
    assert capture.done and capture.failed


@pytest.mark.parametrize('route', ['messages', 'responses'])
def test_native_nonstream_response_bytes_unchanged(workspace, monkeypatch, route):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    wire = b'{"id":"native-id", "usage":{"input_tokens":2,"output_tokens":1},"status":"completed"}'
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=wire)), **kw))
    run = app.state.store.create_run('test native', '', 'modal', [])
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer cap'},
                           json={'messages': [], 'input': [], 'stream': False})
    assert response.content == wire


def test_prompt_cache_policy_and_usage_survive_gateway(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    policy = {'type': 'ephemeral', 'ttl': '5m'}
    system = [{'type': 'text', 'text': 'Stable system prompt', 'cache_control': policy}]
    wire = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"cached",'
            b'"usage":{"input_tokens":5,"output_tokens":0,"cache_creation_input_tokens":20,"cache_read_input_tokens":100}}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":3}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    def upstream(request):
        payload = json.loads(request.content)
        assert payload['cache_control'] == policy
        assert payload['system'][-1] == system[0]
        return httpx.Response(200, content=wire, headers={'Content-Type': 'text/event-stream'})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(transport=httpx.MockTransport(upstream), **kw))
    run = app.state.store.create_run('cache test', '', 'modal', [], model='anthropic/claude-opus-5-5', harness='claude-agent-sdk')
    app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
    response = client.post(f"/broker/{run['id']}/v1/messages", headers={'Authorization': 'Bearer cap'},
                           json={'messages': [{'role': 'user', 'content': 'Hello'}], 'system': system,
                                 'stream': True, 'cache_control': policy})
    assert response.content == wire
    row = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert row['cache_read_input_tokens'] == 100 and row['cache_creation_input_tokens'] == 20
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (125, 3, 128)


def test_response_cache_tokens_are_not_double_counted():
    capture = NativeUsageCapture(False, route='/v1/responses')
    capture.feed(b'{"usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":80},"output_tokens":5}}')
    capture.finish()
    assert capture.usage['prompt_tokens'] == 100 and capture.usage['total_tokens'] == 105


@pytest.mark.parametrize('route,field,limit', [('/v1/messages', 'messages', 'max_tokens'),
                                            ('/v1/responses', 'input', 'max_output_tokens')])
def test_native_generation_budget_is_owned_by_the_model_and_runtime(route, field, limit):
    body = {field: [{'role': 'user', 'content': 'Continue'}], limit: 65536}
    assert authorized_payload(body, route, 'selected', '')[limit] == 65536
    body.pop(limit)
    assert limit not in authorized_payload(body, route, 'selected', '')
    from fastapi import HTTPException
    for value in [True, 0, -1, '65536']:
        with pytest.raises(HTTPException):
            authorized_payload({**body, limit: value}, route, 'selected', '')


@pytest.mark.parametrize('capacity', [1, 3])
async def test_model_slots_preempt_maintenance_without_exceeding_shared_capacity(capacity: int) -> None:
    slots = ModelSlots(capacity)
    background_count = 1 if capacity == 1 else 2
    occupied = capacity - background_count
    for _ in range(occupied):
        await slots.acquire()
    active, peak = occupied, occupied
    cleaning, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    background_started = [asyncio.Event() for _ in range(background_count)]
    foreground_started = [asyncio.Event() for _ in range(background_count + 1)]

    async def maintenance(index: int) -> None:
        nonlocal active, peak
        async with slots:
            active += 1
            peak = max(peak, active)
            background_started[index].set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await cleanup.wait()
                active -= 1

    async def foreground(index: int) -> None:
        nonlocal active, peak
        assert not slots.locked()
        async with slots:
            active += 1
            peak = max(peak, active)
            foreground_started[index].set()
            try:
                await release.wait()
            finally:
                active -= 1

    background = [slots.run_maintenance(maintenance(i)) for i in range(background_count)]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in background_started)), 2)
    ordinary = [asyncio.create_task(foreground(i)) for i in range(background_count + 1)]
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        # No caller may use a cancelling holder's slot before its cleanup completes.
        assert not any(event.is_set() for event in foreground_started)
        cleanup.set()
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in foreground_started[:-1])), 2)
        assert slots.locked() and not foreground_started[-1].is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*ordinary), 2)
        outcomes = await asyncio.gather(*background, return_exceptions=True)
        assert all(isinstance(value, asyncio.CancelledError) for value in outcomes)
        assert peak == capacity and active == occupied
    finally:
        cleanup.set()
        release.set()
        for task in background + ordinary:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*background, *ordinary, return_exceptions=True)
    for _ in range(occupied):
        slots.release()
    assert not slots.locked()


async def test_model_slot_cancelled_foreground_preserves_background_cleanup() -> None:
    slots = ModelSlots(1)
    started, cleaning, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def maintenance() -> None:
        async with slots:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await cleanup.wait()

    background = slots.run_maintenance(maintenance())
    await asyncio.wait_for(started.wait(), 2)
    foreground = asyncio.create_task(slots.acquire())
    await asyncio.wait_for(cleaning.wait(), 2)
    foreground.cancel()
    with pytest.raises(asyncio.CancelledError):
        await foreground
    assert not background.done()
    cleanup.set()
    with pytest.raises(asyncio.CancelledError):
        await background
    async with slots:
        assert slots.locked()


async def test_model_slots_keep_foreground_contention_and_maintenance_outcomes() -> None:
    slots = ModelSlots(1)
    async with slots:
        assert slots.locked()

        async def rejected() -> str:
            assert slots.locked()
            return 'queued'

        assert await slots.run_maintenance(rejected()) == 'queued'
        waiting = asyncio.create_task(slots.acquire())
        await asyncio.sleep(0)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    assert not slots.locked()

    async def failed() -> None:
        async with slots:
            raise ValueError('fixture failure')

    with pytest.raises(ValueError, match='fixture failure'):
        await slots.run_maintenance(failed())
    assert not slots.locked()
    unstarted = failed()
    task = slots.run_maintenance(unstarted)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inspect.getcoroutinestate(unstarted) == inspect.CORO_CLOSED
    assert not slots.locked()
