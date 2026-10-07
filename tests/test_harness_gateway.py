import json

import httpx
import pytest

from app.harness_gateway import NativeUsageCapture
from app.security import digest
from test_workspace import workspace


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
     b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_test","usage":{"input_tokens":11,"output_tokens":0}}}\n\nevent: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":3}}\n\nevent: message_stop\ndata: {"type":"message_stop"}\n\n'),
    ('responses', {'input': [{'type': 'function_call_output', 'call_id': 'one', 'output': 'receipt'}],
                   'tools': [{'type': 'custom', 'name': 'apply_patch', 'format': {'type': 'text'}}]},
     b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_test","status":"completed","usage":{"input_tokens":11,"output_tokens":3}}}\n\n'),
])
@pytest.mark.parametrize('model', ['openai/gpt-6-astra', 'anthropic/claude-opus-5-5', 'fireworks_ai/glm-5p3'])
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
    assert (row['prompt_tokens'], row['completion_tokens'], row['total_tokens']) == (11, 3, 14)
    app.state.store.update_run(run['id'], status='stopping', token_hash='')
    assert client.post(url, json=payload, headers={'Authorization': 'Bearer cap'}).status_code == 401
    assert len(seen) == 1


def test_native_error_event_is_not_accounted_as_success():
    capture = NativeUsageCapture(True)
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
    capture = NativeUsageCapture(False)
    capture.feed(b'{"usage":{"input_tokens":100,"input_tokens_details":{"cached_tokens":80},"output_tokens":5}}')
    capture.finish()
    assert capture.usage['prompt_tokens'] == 100 and capture.usage['total_tokens'] == 105
