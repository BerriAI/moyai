import json
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
import pytest

from app.config import Settings
from app.db import Store
from app.tracing import AgentTracing
from sandbox.activity import ActivityReporter
from sandbox.trace_content import trace_content
from test_spend import active
from test_workspace import workspace


class Processor:
    def __init__(self):
        self.spans = []

    def on_end(self, span):
        self.spans.append(span)

    def shutdown(self):
        pass


def setup(tmp_path):
    store = Store(tmp_path)
    settings = Settings(_env_file=None, litellm_trace_endpoint='https://gateway-dev.litellm-sandbox.ai/v1/traces',
                        litellm_trace_api_key='trace-secret', litellm_api_key='model-secret')
    processor = Processor()
    tracing = store.tracing = AgentTracing(store, settings, processor)
    run = store.create_run('Inspect a file', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    return store, tracing, processor, store.run(run['id']), message


def test_trace_tree_contains_task_model_tool_and_answer_with_stable_turn_ids(tmp_path):
    store, tracing, processor, run, message = setup(tmp_path)
    events = []
    activity = ActivityReporter(lambda kind, msg, data: events.append((kind, data)), tracing=True)
    activity.start('call-1', 'read_file', {'path': '/workspace/example.txt'})
    activity.complete('call-1', 'read_file', {'path': '/workspace/example.txt'}, {'content': 'hello'})
    data = next(data for kind, data in events if kind == 'trace')
    tracing.tool(run['id'], data)
    tracing.model(run, 'request-1', time.time_ns(), [{'role': 'user', 'content': 'Inspect a file'}],
                  {'choices': [{'message': {'content': 'It says hello', 'reasoning_content': 'private-thought'}}],
                   'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}, 'completed')
    store.finish_message(run['id'], message['id'], 'It says hello')
    tool, model, root = processor.spans
    assert root.parent is None
    assert tool.parent == model.parent == root.context
    assert tool.context.trace_id == model.context.trace_id == root.context.trace_id
    assert root.attributes['input.value'] == 'Inspect a file'
    assert root.attributes['output.value'] == 'It says hello'
    assert 'hello' in tool.attributes['output.value']
    assert model.attributes['gen_ai.usage.input_tokens'] == 10
    assert 'private-thought' not in str(model.attributes)
    assert root.attributes['gen_ai.agent.name'] == 'moyai'
    assert {span.attributes['agent.name'] for span in processor.spans} == {root.name} == {'moyai'}
    assert model.attributes['llm.model_name'] == (run.get('active_model') or run['model'])
    # The real OTLP encoder accepts every span (IDs, timestamps, attributes).
    assert len(encode_spans(processor.spans).SerializeToString()) > 0
    # Duplicate turn saves do not emit duplicate roots; resumed tool events
    # keep their span identity for receiver-side deduplication.
    store.finish_message(run['id'], message['id'], 'It says hello')
    assert len(processor.spans) == 3
    tracing.tool(run['id'], data)
    assert processor.spans[-1].context == tool.context
    store.enqueue_message(run['id'], 'Next task', 'follow-up')
    store.claim_message(run['id'])
    tracing.tool(run['id'], data)
    assert processor.spans[-1].context.trace_id != root.context.trace_id


def test_sensitive_content_is_redacted_and_private_tools_are_omitted(monkeypatch):
    monkeypatch.setenv('WORKSPACE_RUN_TOKEN', 'capability-value')
    value = {'content': '<think>secret thought</think>hello capability-value',
             'authorization': 'Bearer some-credential', 'nested': '{"password":"private-password"}',
             'image': {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,IMAGE'}},
             'reasoning_content': 'private-reasoning-marker'}
    result = trace_content(value)
    for secret in ['secret thought', 'capability-value', 'some-credential', 'private-password', 'IMAGE', 'private-reasoning-marker']:
        assert secret not in result
    assert 'hello' in result
    assert len(trace_content('x' * 100000)) <= 16020
    events = []
    activity = ActivityReporter(lambda *args: events.append(args), tracing=True)
    activity.start('private', 'mcp_workspace_skills_load', {'instructions': 'secret-definition'})
    activity.complete('private', 'mcp_workspace_skills_load', {}, {'content': 'secret-definition'})
    assert 'secret-definition' not in json.dumps(events)
    assert next(event for event in events if event[0] == 'trace')[2]['output'] == '[private tool payload omitted]'


@pytest.mark.parametrize('name', ['memory_save', 'mcp_workspace_memory_save', 'mcp__workspace__memory_search'])
def test_server_drops_memory_payload_even_if_sandbox_did_not_scrub_it(tmp_path, name):
    store, tracing, processor, run, _ = setup(tmp_path)
    stamp = time.time_ns()
    tracing.tool(run['id'], {'tool': name, 'call_id': 'memory', 'start_ns': stamp, 'end_ns': stamp,
                            'input': 'private-memory-marker', 'output': 'private-memory-marker'})
    assert len(processor.spans) == 1
    assert 'private-memory-marker' not in str(processor.spans[0].attributes)


def test_disabled_and_broken_capture_cannot_break_agent_work(tmp_path):
    store, tracing, processor, run, message = setup(tmp_path)
    def broken(span):
        raise RuntimeError('Exporter unavailable')
    processor.on_end = broken
    store.finish_message(run['id'], message['id'], 'Answer survives')
    assert store.messages(run['id'])[-1]['content'] == 'Answer survives'
    tracing.tool(run['id'], {'invalid': 'payload'})
    disabled = AgentTracing(store, Settings(_env_file=None))
    disabled.finish_turn('does-not-exist', 0, '', 'failed')
    assert disabled.processor is None and disabled.outboxes == []


@pytest.mark.parametrize('endpoint', ['http://example.com/v1/traces', 'https://key@example.com/v1/traces',
                                     'https://example.com/v1/traces?key=secret', 'https://example.com/v1'])
def test_trace_endpoint_rejects_insecure_or_ambiguous_destinations(endpoint):
    with pytest.raises(ValueError):
        Settings(_env_file=None, litellm_trace_endpoint=endpoint)


def test_model_broker_emits_span_without_rerouting_or_exposing_system_prompts(workspace, monkeypatch):
    app, client = workspace
    settings = app.state.settings
    settings.litellm_api_base = 'https://existing-model-gateway.example/v1'
    settings.litellm_api_key = 'existing-model-key'
    tracing = app.state.tracing
    tracing.enabled = True
    tracing.processor = Processor()
    run = active(app)
    def gateway(request):
        assert str(request.url) == 'https://existing-model-gateway.example/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer existing-model-key'
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Done'}}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1}})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions", headers={'Authorization': 'Bearer capability'},
                           json={'messages': [{'role': 'system', 'content': 'private-system-prompt'}, {'role': 'user', 'content': 'Hello'}]})
    assert response.status_code == 200
    assert len(tracing.processor.spans) == 1
    span = tracing.processor.spans[0]
    assert 'private-system-prompt' not in str(span.attributes)
    assert 'Hello' in span.attributes['input.value'] and 'Done' in span.attributes['output.value']


@pytest.mark.parametrize('route,model', [('chat/completions', 'openai/gpt-6-astra'),
                                        ('chat/completions', 'anthropic/claude-opus-5-5'),
                                        ('responses', 'openai/gpt-6-astra'),
                                        ('messages', 'anthropic/claude-opus-5-5')])
@pytest.mark.parametrize('stream', [False, True])
def test_broker_exports_cost_inputs_and_distinct_provider_gateway_ids(
    workspace: tuple[FastAPI, TestClient], monkeypatch: pytest.MonkeyPatch, route: str, model: str, stream: bool,
) -> None:
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    tracing = app.state.tracing
    tracing.enabled = True
    tracing.processor = Processor()
    native = route == 'messages'
    anthropic = model.startswith('anthropic/')
    run = active(app, model=model)
    usage = ({'input_tokens': 5, 'output_tokens': 3, 'cache_read_input_tokens': 100,
              'cache_creation_input_tokens': 20, 'service_tier': 'standard',
              'cache_creation': {'ephemeral_5m_input_tokens': 15, 'ephemeral_1h_input_tokens': 5}}
             if native else {'input_tokens': 125, 'output_tokens': 3,
                             'input_tokens_details': {'cached_tokens': 100},
                             'output_tokens_details': {'reasoning_tokens': 2}})
    if route == 'chat/completions':
        usage = {'prompt_tokens': 125, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 100},
                 'completion_tokens_details': {'reasoning_tokens': 2}}
        if anthropic:
            usage['prompt_tokens_details'].update(cache_write_tokens=20,
                cache_creation_token_details={'ephemeral_5m_input_tokens': 15, 'ephemeral_1h_input_tokens': 5})
    value = {'id': 'provider-response', 'model': 'resolved-model', 'usage': usage,
             'choices': [{'message': {'content': 'Hello', 'reasoning_content': 'private-thought'}}]}
    if not native:
        value['service_tier'] = 'standard' if anthropic else 'flex'
    def gateway(request: httpx.Request) -> httpx.Response:
        assert request.headers['x-litellm-call-id'] != 'observed-gateway-id'
        if stream and route != 'chat/completions':
            frames = ([{'type': 'message_start', 'message': {**value, 'usage': {**usage, 'output_tokens': 0}}},
                       {'type': 'message_delta', 'usage': {'output_tokens': 3}}, {'type': 'message_stop'}]
                      if native else [{'type': 'response.completed', 'response': value}])
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames).encode()
        else:
            wire = json.dumps(value).encode()
        return httpx.Response(200, content=wire, headers={'x-litellm-call-id': 'observed-gateway-id'})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer capability'},
        json={'messages': [{'role': 'system', 'content': 'private-system'}, {'role': 'user', 'content': 'Hello'}],
              'input': 'Hello', 'stream': stream})
    assert response.status_code == 200
    assert len(tracing.processor.spans) == 1
    attrs = tracing.processor.spans[0].attributes
    assert attrs['gen_ai.response.id'] == 'provider-response'
    assert attrs['litellm.call_id'] == 'observed-gateway-id'
    assert attrs['gen_ai.response.model'] == 'resolved-model'
    assert attrs['gen_ai.usage.input_tokens'] == 125
    assert attrs['gen_ai.usage.output_tokens'] == 3
    assert attrs['gen_ai.usage.cache_read.input_tokens'] == 100
    assert attrs['gen_ai.usage.total_tokens'] == 128
    if native:
        assert 'gen_ai.usage.reasoning.output_tokens' not in attrs
    else:
        assert attrs['gen_ai.usage.reasoning.output_tokens'] == 2
    assert 'private-system' not in str(attrs) and 'private-thought' not in str(attrs)
    if anthropic:
        assert attrs['gen_ai.usage.cache_write.input_tokens'] == 20
        assert attrs['anthropic.usage.cache_creation.ephemeral_5m_input_tokens'] == 15
        assert attrs['anthropic.usage.cache_creation.ephemeral_1h_input_tokens'] == 5
        assert attrs['anthropic.response.service_tier'] == 'standard'
    else:
        assert 'gen_ai.usage.cache_write.input_tokens' not in attrs
        assert attrs['openai.response.service_tier'] == 'flex'
    row = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert row['cache_read_input_tokens'] == 100


@pytest.mark.parametrize('reasoning', [None, True, -1, '2', 4, 0, 2])
def test_reasoning_usage_exports_only_reported_output_subsets(tmp_path: Path, reasoning: object) -> None:
    _, tracing, processor, run, _ = setup(tmp_path)
    tracing.model(run, 'local-request-id', time.time_ns(), [],
                  {'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'reasoning_tokens': reasoning}}, 'completed')
    attrs = processor.spans[0].attributes
    assert attrs['gen_ai.usage.output_tokens'] == 3
    assert attrs['gen_ai.usage.total_tokens'] == 8
    if type(reasoning) is int and 0 <= reasoning <= 3:
        assert attrs['gen_ai.usage.reasoning.output_tokens'] == reasoning
    else:
        assert 'gen_ai.usage.reasoning.output_tokens' not in attrs


@pytest.mark.parametrize('value', [True, -1, '10', None])
def test_model_usage_rejects_invalid_counts_and_preserves_explicit_zero(tmp_path: Path, value: object) -> None:
    _, tracing, processor, run, _ = setup(tmp_path)
    tracing.model(run, 'local-request-id', time.time_ns(), [],
                  {'usage': {'prompt_tokens': value, 'completion_tokens': 0,
                             'cache_read_input_tokens': 0, 'cache_creation_input_tokens': value}}, 'completed')
    attrs = processor.spans[0].attributes
    assert attrs['gen_ai.usage.output_tokens'] == attrs['gen_ai.usage.cache_read.input_tokens'] == 0
    for key in ('gen_ai.response.id', 'gen_ai.response.model', 'litellm.call_id', 'gen_ai.usage.input_tokens',
                'gen_ai.usage.total_tokens', 'gen_ai.usage.cache_write.input_tokens'):
        assert key not in attrs
