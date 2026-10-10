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
from agent.activity import ActivityReporter
from agent.trace_content import trace_content
from test_spend import active
from test_workspace import workspace


class Processor:
    def __init__(self):
        self.spans = []

    def on_end(self, span):
        self.spans.append(span)

    def shutdown(self):
        pass


@pytest.mark.parametrize('fails', [False, True])
async def test_runtime_step_spans_keep_original_phase_and_turn(tmp_path, fails):
    from app.durable_runner import DurableRunner
    from types import SimpleNamespace
    store, tracing, processor, run, message = setup(tmp_path)
    state = {'phase': 'install', 'message_id': message['id'], 'segment': 2}

    async def step(run_id, state):
        state['phase'] = 'launch'
        if fails:
            raise ValueError('private error body')
        return 'receipt'

    manager = SimpleNamespace(store=store, _step=step)
    if fails:
        with pytest.raises(ValueError, match='private error body'):
            await DurableRunner.step(manager, run['id'], state)
    else:
        assert await DurableRunner.step(manager, run['id'], state) == 'receipt'
    tracing.runtime_phase(run['id'], message['id'], 'activity_queue', 1000000, 3000000)
    store.finish_message(run['id'], message['id'], 'Done')
    phase, queue, root = processor.spans
    assert phase.name == 'runtime.install'
    assert phase.parent == queue.parent == root.context
    assert phase.attributes['moyai.runtime.segment'] == 2
    assert phase.attributes['moyai.runtime.duration_ms'] >= 0
    assert phase.status.is_ok is (not fails)
    assert queue.attributes['moyai.runtime.duration_ms'] == 2
    payload = encode_spans(processor.spans).SerializeToString()
    assert b'private error body' not in payload


async def test_runtime_trace_failure_cannot_replace_step_result(tmp_path, monkeypatch):
    from app.durable_runner import DurableRunner
    from types import SimpleNamespace
    store, tracing, _, run, message = setup(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError('trace export unavailable')

    async def step(*args):
        return 'completed'

    monkeypatch.setattr(tracing, 'emit', broken)
    manager = SimpleNamespace(store=store, _step=step)
    assert await DurableRunner.step(manager, run['id'],
                                   {'phase': 'finish', 'message_id': message['id']}) == 'completed'


def setup(tmp_path, *, build_sha='', environment='development'):
    store = Store(tmp_path)
    settings = Settings(_env_file=None, litellm_trace_endpoint='https://gateway-dev.litellm-sandbox.ai/lens-ingest/v1/traces',
                        litellm_trace_api_key='trace-secret', litellm_api_key='model-secret',
                        moyai_build_sha=build_sha, trace_environment=environment)
    processor = Processor()
    tracing = store.tracing = AgentTracing(store, settings, processor)
    run = store.create_run('Inspect a file', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    return store, tracing, processor, store.run(run['id']), message


@pytest.mark.parametrize('build_sha,environment', [('', 'development'), ('a1' * 20, 'lens-eval'),
                                                 ('b2' * 32, 'preview')])
def test_otlp_build_and_environment_metadata_comes_from_server_settings(tmp_path, build_sha, environment):
    store, tracing, processor, run, message = setup(tmp_path, build_sha=build_sha, environment=environment)
    stamp = time.time_ns()
    tracing.emit(run, message['id'], 'read_file', 'tool-1', stamp, stamp,
                 {'openinference.span.kind': 'TOOL', 'input.value': 'file.txt', 'output.value': 'hello',
                  'agent.version': 'request-controlled-version', 'deployment.environment': 'production'})
    store.finish_message(run['id'], message['id'], 'It says hello')

    # Inspect the actual OTLP protobuf that all configured destinations receive.
    encoded = encode_spans(processor.spans)
    resource_spans = encoded.resource_spans[0]
    resource = {attribute.key: attribute.value.string_value for attribute in resource_spans.resource.attributes}
    assert resource['deployment.environment'] == resource['deployment.environment.name'] == environment
    spans = [span for scope in resource_spans.scope_spans for span in scope.spans]
    assert {span.name for span in spans} == {'read_file', 'moyai'}
    span_attributes = [{attribute.key: attribute.value.string_value for attribute in span.attributes} for span in spans]
    for attributes in [resource, *span_attributes]:
        assert attributes['deployment.environment'] == environment
        if build_sha:
            assert attributes['agent.version'] == build_sha
        else:
            assert 'agent.version' not in attributes
    payload = encoded.SerializeToString()
    assert b'request-controlled-version' not in payload
    assert b'trace-secret' not in payload


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
    activity = ActivityReporter(lambda *args: events.append(args), tracing=True, omit_private_tool_payloads=True)
    activity.start('private', 'mcp_workspace_skills_load', {'instructions': 'secret-definition'})
    activity.complete('private', 'mcp_workspace_skills_load', {}, {'content': 'secret-definition'})
    assert 'secret-definition' not in json.dumps(events)
    assert next(event for event in events if event[0] == 'trace')[2]['output'] == '[private tool payload omitted]'


def test_lens_feedback_api_key_is_redacted_from_trace_content(tmp_path):
    store = Store(tmp_path)
    tracing = AgentTracing(store, Settings(_env_file=None, lens_feedback_api_key='feedback-secret'), Processor())
    assert 'feedback-secret' not in tracing.content('feedback-secret')


@pytest.mark.parametrize('name', ['memory_save', 'mcp_workspace_memory_save', 'mcp__workspace__memory_search',
                                       'mcp__moyai__skills_load', 'credentials_request', 'workspace_call', 'call'])
def test_server_omits_private_payloads_when_enabled_even_if_sandbox_did_not_scrub_it(tmp_path, name):
    store, tracing, processor, run, _ = setup(tmp_path)
    actor = store.identity({'method': 'google', 'identity': {'sub': 'alice', 'email': 'alice@example.com'}})
    store.execute('UPDATE messages SET user_id=? WHERE id=?', (actor, run['active_message_id']))
    store.execute('INSERT INTO user_preferences(user_id,omit_private_tool_payloads) VALUES(?,1)', (actor,))
    stamp = time.time_ns()
    tracing.tool(run['id'], {'tool': name, 'call_id': 'memory', 'start_ns': stamp, 'end_ns': stamp,
                            'input': 'private-memory-marker', 'output': 'private-memory-marker'})
    assert len(processor.spans) == 1
    assert 'private-memory-marker' not in str(processor.spans[0].attributes)


@pytest.mark.parametrize('name', ['skills_save', 'mcp__workspace__memory_search', 'credentials_request', 'workspace_call'])
@pytest.mark.parametrize('omit', [False, True])
def test_private_tool_capture_is_optional_and_secret_redaction_always_applies(tmp_path, name, omit):
    store, tracing, processor, run, _ = setup(tmp_path)
    events = []
    activity = ActivityReporter(lambda kind, msg, data: events.append((kind, data)),
                                tracing=True, omit_private_tool_payloads=omit)
    args = {'content': 'Useful tool evidence', 'token': 'credential-value'}
    result = {'result': 'Useful outcome', 'password': 'password-value'}
    activity.start('tool-1', name, args)
    activity.complete('tool-1', name, args, result)
    public = [data for kind, data in events if kind != 'trace']
    assert 'Useful tool evidence' not in str(public)
    data = next(data for kind, data in events if kind == 'trace')
    tracing.tool(run['id'], data)
    attrs = processor.spans[-1].attributes
    assert ('Useful tool evidence' in attrs['input.value']) is (not omit)
    assert ('Useful outcome' in attrs['output.value']) is (not omit)
    assert attrs['gen_ai.tool.call.arguments'] == attrs['input.value']
    assert attrs['gen_ai.tool.call.result'] == attrs['output.value']
    assert 'credential-value' not in str(events) + str(attrs)
    assert 'password-value' not in str(events) + str(attrs)


def test_private_tool_capture_is_on_by_default_in_reporter():
    events = []
    activity = ActivityReporter(lambda *event: events.append(event), tracing=True)
    activity.complete('tool-1', 'skills_save', {'content': 'Useful evidence'}, {'saved': True})
    assert 'Useful evidence' in next(event[2]['input'] for event in events if event[0] == 'trace')


def test_slack_turn_root_span_links_its_thread_for_lens(tmp_path):
    store, tracing, processor, web_run, web_message = setup(tmp_path)
    permalink = 'https://acme.slack.com/archives/C0123ABCD/p1759870000000100'
    run = store.create_slack_run('evt-1', 'Add Nate to the retro', [], 'C0123ABCD', '1759870000.000100',
                                 'U0AAAAAAA', team_id='T0AAAAAAA')
    store.execute('INSERT INTO slack_mention_names(team_id,user_id,name) VALUES(?,?,?)',
                  ('T0AAAAAAA', 'U0BBBBBBB', 'Mateo Wang'))
    store.execute('UPDATE slack_events SET context_status=?,context_json=? WHERE run_id=?', ('ready', json.dumps({
        'permalink': permalink, 'messages': [
            {'ts': '1759870000.000100', 'text': '<@U0BBBBBBB> can you add me\n to the   guestlist?'},
            {'ts': '1759870000.000200', 'text': 'a later reply'}]}), run['id']))
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'Done')
    store.finish_message(web_run['id'], web_message['id'], 'Done')
    slack_root, web_root = processor.spans
    assert slack_root.attributes['agent.source.type'] == 'slack'
    assert slack_root.attributes['agent.source.url'] == permalink
    assert slack_root.attributes['agent.source.title'] == '@Mateo Wang can you add me to the guestlist?'
    assert not any(key.startswith('agent.source.') for key in web_root.attributes)


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
                                     'https://example.com/v1/traces?key=secret', 'https://example.com/v1',
                                     'http://example.com/lens-ingest/v1/traces',
                                     'https://key@example.com/lens-ingest/v1/traces',
                                     'https://example.com/lens-ingest/v1/traces?key=secret',
                                     'https://example.com/lens-ingest/v1/traces#fragment',
                                     'https://example.com/lens-ingest/v1/traces/extra'])
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
    prompt = 'Inspect this file: ' + 'line of code\n' * 3000
    answer = 'Review findings\n' * 2000 + 'Done'
    def gateway(request):
        assert str(request.url) == 'https://existing-model-gateway.example/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer existing-model-key'
        assert json.loads(request.content)['messages'][-1]['content'] == prompt
        return httpx.Response(200, json={'choices': [{'message': {'content': answer}}], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1}})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.main.httpx.AsyncClient', lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    response = client.post(f"/broker/{run['id']}/v1/chat/completions", headers={'Authorization': 'Bearer capability'},
                           json={'messages': [{'role': 'system', 'content': 'private-system-prompt'}, {'role': 'user', 'content': prompt}]})
    assert response.status_code == 200
    assert response.json()['choices'][0]['message']['content'] == answer
    assert len(tracing.processor.spans) == 1
    span = tracing.processor.spans[0]
    assert 'private-system-prompt' not in str(span.attributes)
    for direction, text in [('input', prompt), ('output', answer)]:
        assert json.loads(span.attributes[f'{direction}.value'])[0]['content'] == text
        assert json.loads(span.attributes[f'gen_ai.{direction}.messages'])[0]['parts'] == [
            {'type': 'text', 'content': text}]


def test_model_span_with_large_unicode_and_escaped_text_fits_lens_ingestion(tmp_path):
    _, tracing, processor, run, _ = setup(tmp_path)
    # Both UTF-8 width and JSON escaping count toward the upload limit. Exercise
    # all retained messages and choices, including the duplicated GenAI content.
    text = '🗿"\\\n' * 150_000 + ' model-secret <think>private-reasoning</think>'
    tracing.model(run, 'large-request', time.time_ns(), [{'role': 'user', 'content': text}] * 5,
                  {'choices': [{'message': {'content': text}}] * 5}, 'completed')
    assert len(processor.spans) == 1
    attrs = processor.spans[0].attributes
    for direction in ['input', 'output']:
        legacy = json.loads(attrs[f'{direction}.value'])
        genai = json.loads(attrs[f'gen_ai.{direction}.messages'])
        assert len(legacy) == len(genai) == 5
        for message, modern in zip(legacy, genai):
            assert message['content'].startswith(text[:16000])
            assert message['content'].endswith(' [truncated]')
            assert modern['content'] == message['content']
            assert modern['parts'] == [{'type': 'text', 'content': message['content']}]
        assert len(attrs[f'{direction}.value'].encode()) <= 2 * 1024 * 1024
        assert len(attrs[f'gen_ai.{direction}.messages'].encode()) <= 2 * 1024 * 1024
    payload = encode_spans(processor.spans).SerializeToString()
    assert len(payload) < 16 * 1024 * 1024
    assert b'model-secret' not in payload and b'private-reasoning' not in payload


@pytest.mark.parametrize('route', ['messages', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('enabled', [False, True])
def test_native_model_content_preserves_wire_and_spend(workspace, monkeypatch, route, stream, enabled):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    app.state.settings.litellm_api_key = 'configured-secret'
    tracing = app.state.tracing
    tracing.enabled = enabled
    tracing.processor = Processor()
    if not enabled:
        def unexpected_capture(*args):
            pytest.fail('Disabled tracing must not retain native content')
        monkeypatch.setattr('app.harness_gateway.NativeModelContent', unexpected_capture)
    run = active(app)
    input_prefix = 'Inspect ' + 'i' * 15_980 + ' '
    output_prefix = 'Hello ' + 'o' * 15_982 + ' '
    prompt = input_prefix + 'configured-secret ' + 'input-tail ' * 2000
    # Split a secret at the old 16,000-character limit and a stream delta
    # boundary. Long text must survive while redaction sees the complete value.
    parts = [output_prefix + 'configured-', 'secret <thi',
             'nk>private-thought</think>world ' + 'output-tail ' * 2000]
    public = ''.join(parts)
    expected_input = prompt.replace('configured-secret', '[redacted]')
    expected_output = output_prefix + '[redacted] world ' + 'output-tail ' * 2000
    usage = {'input_tokens': 12, 'output_tokens': 3}
    if route == 'messages':
        value = {'id': 'provider-id', 'content': [
            {'type': 'text', 'text': public},
            {'type': 'thinking', 'thinking': 'private-reasoning', 'signature': 'private-signature'},
            {'type': 'tool_use', 'name': 'read_file', 'input': {'path': 'private-arguments'}}], 'usage': usage}
        frames = [{'type': 'message_start', 'message': {'id': 'provider-id', 'content': [], 'usage': usage}},
                  {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                  *[{'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': part}}
                    for part in parts],
                  {'type': 'content_block_delta', 'index': 1,
                   'delta': {'type': 'thinking_delta', 'thinking': 'private-reasoning'}},
                  {'type': 'content_block_start', 'index': 2, 'content_block': value['content'][2]},
                  {'type': 'message_delta', 'usage': usage, 'x_litellm_response_cost': '0.00123'},
                  {'type': 'message_stop'}]
    else:
        value = {'id': 'provider-id', 'output': [
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'private-reasoning'}]},
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': public}]},
            {'type': 'function_call', 'name': 'read_file', 'arguments': 'private-arguments'}], 'usage': usage,
            'x_litellm_response_cost': '0.00123'}
        frames = [*({'type': 'response.output_text.delta', 'delta': part}
                    for part in parts),
                  {'type': 'response.output_item.added', 'item': value['output'][2]},
                  {'type': 'response.completed', 'response': value}]
    wire = (''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames).encode()
            if stream else json.dumps(value).encode())

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for start in range(0, len(wire), 4093):
                yield wire[start:start + 4093]

    def gateway(request):
        field = 'messages' if route == 'messages' else 'input'
        assert json.loads(request.content)[field] == messages
        return httpx.Response(200, stream=Chunks(), headers={'x-litellm-response-cost': '0.00123'})
    real = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient',
                        lambda **kw: real(transport=httpx.MockTransport(gateway), **kw))
    messages = [{'role': 'system', 'content': 'private-system'},
                {'role': 'developer', 'content': 'private-developer'},
                {'role': 'user', 'content': [
                    {'type': 'text' if route == 'messages' else 'input_text', 'text': prompt},
                    {'type': 'tool_result', 'content': 'private-result'}]}]
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer capability'},
                           json={'messages' if route == 'messages' else 'input': messages, 'stream': stream})
    assert response.status_code == 200, response.text
    assert response.content == wire
    row = app.state.store.rows('SELECT * FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert row['status'] == 'completed' and row['cost'] == '0.00123'
    assert row['prompt_tokens'] == 12 and row['completion_tokens'] == 3
    if not enabled:
        assert tracing.processor.spans == []
        return
    assert len(tracing.processor.spans) == 1
    attrs = tracing.processor.spans[0].attributes
    assert json.loads(attrs['input.value']) == [{'role': 'user', 'content': expected_input}]
    assert json.loads(attrs['output.value']) == [
        {'role': 'assistant', 'content': expected_output, 'tool_names': ['read_file']}]
    for direction, text in [('input', expected_input), ('output', expected_output)]:
        assert json.loads(attrs[f'gen_ai.{direction}.messages'])[0]['parts'] == [
            {'type': 'text', 'content': text}]
    assert 'private-' not in str(attrs) and 'configured-secret' not in str(attrs)
    assert attrs['gen_ai.usage.total_tokens'] == 15


@pytest.mark.parametrize('route,terminal', [('messages', None), ('responses', None),
                                          ('responses', 'failed'), ('responses', 'incomplete')])
def test_native_partial_stream_exports_observed_text_without_claiming_success(workspace, monkeypatch, route, terminal):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    tracing = app.state.tracing
    tracing.enabled = True
    tracing.processor = Processor()
    run = active(app)
    event = ({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'Partial answer'}}
             if route == 'messages' else {'type': 'response.output_text.delta', 'delta': 'Partial answer'})
    wire = ('data: ' + json.dumps(event) + '\n\n').encode()
    if terminal:
        wire += ('data: ' + json.dumps({'type': 'response.' + terminal,
                                       'response': {'status': terminal, 'output': []}}) + '\n\n').encode()
    real = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=wire)), **kw))
    response = client.post(f"/broker/{run['id']}/v1/{route}", headers={'Authorization': 'Bearer capability'},
        json={'messages': [{'role': 'user', 'content': 'Hello'}], 'input': 'Hello', 'stream': True})
    assert response.content == wire
    attrs = tracing.processor.spans[0].attributes
    assert attrs['moyai.status'] == 'failed'
    assert json.loads(attrs['output.value'])[0]['content'] == 'Partial answer'
    assert json.loads(attrs['input.value'])[0]['content'] == 'Hello'
    row = app.state.store.rows('SELECT status,cost FROM model_requests WHERE run_id=?', (run['id'],))[0]
    assert row == {'status': 'failed', 'cost': None}


@pytest.mark.parametrize('status', ['failed', 'incomplete'])
@pytest.mark.parametrize('snapshot', [[], [{'type': 'reasoning', 'summary': 'private'}],
    [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Partial'}]}]])
def test_unsuccessful_snapshot_cannot_erase_or_shorten_observed_output(status, snapshot):
    from app.native_trace import NativeModelContent
    content = NativeModelContent({}, '/v1/responses')
    content.consume({'type': 'response.output_text.delta', 'delta': 'Partial answer'})
    content.consume({'type': 'response.output_item.added', 'item': {'type': 'function_call', 'name': 'read_file'}})
    event = {'type': 'response.' + status, 'response': {'status': status, 'output': snapshot}}
    content.consume(event)
    assert content.choices == [{'message': {'content': 'Partial answer',
                                            'tool_calls': [{'function': {'name': 'read_file'}}]}}]
    # With no stream, the same partial snapshot is still useful evidence.
    snapshot_only = NativeModelContent({}, '/v1/responses')
    snapshot_only.consume(event)
    assert snapshot_only.choices == ([{'message': {'content': 'Partial', 'tool_calls': []}}]
                                     if snapshot and snapshot[0]['type'] == 'message' else [])


@pytest.mark.parametrize('route', ['/v1/messages', '/v1/responses'])
def test_native_projection_bounds_and_private_blocks_never_enter_usage(route):
    from app.harness_gateway import NativeUsageCapture
    from app.native_trace import MODEL_TEXT_LIMIT, OMITTED, NativeModelContent

    field = 'messages' if route == '/v1/messages' else 'input'
    messages = [{'role': 'user', 'content': str(i)} for i in range(8)]
    content = NativeModelContent({field: messages}, route)
    assert [m['content'] for m in content.messages] == ['3', '4', '5', '6', '7']
    for values in [[{'role': 'user', 'content': 'private-prefix' + 'x' * MODEL_TEXT_LIMIT}],
                   [{'role': 'user', 'content': 'x' * (MODEL_TEXT_LIMIT // 5 + 1)} for _ in range(5)]]:
        assert NativeModelContent({field: values}, route).messages == [{'role': 'user', 'content': OMITTED}]
    private = [None, 'private-string', {'type': []}, {'type': 'image', 'source': 'private-image'},
               {'type': 'reasoning', 'summary': 'private-reasoning'},
               {'type': 'tool_result', 'content': 'private-result'}, {'type': 'text', 'text': {'private': 1}}]
    assert NativeModelContent({field: [{'role': 'user', 'content': private}]}, route).messages == []
    blocks = [{'type': 'text' if route == '/v1/messages' else 'output_text',
               'text': 'private-prefix' + 'x' * MODEL_TEXT_LIMIT}, *private]
    tools = [{'type': 'tool_use' if route == '/v1/messages' else 'custom_tool_call',
              'name': 'read_file', 'arguments': 'private-arguments'} for _ in range(101)]
    value = ({'content': blocks + tools} if route == '/v1/messages' else
             {'output': [{'type': 'message', 'role': 'assistant', 'content': blocks}, *tools]})
    capture = NativeUsageCapture(False, route=route, content=content)
    capture.feed(json.dumps({**value, 'usage': {'input_tokens': 2, 'output_tokens': 1}}).encode())
    capture.finish()
    assert content.choices[0]['message']['content'] == OMITTED
    assert len(content.choices[0]['message']['tool_calls']) == 100
    assert 'private-' not in json.dumps(content.choices)
    assert capture.response == {'usage': {'input_tokens': 2, 'output_tokens': 1,
                                        'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}
    streamed = NativeModelContent({}, route)
    for part in ['private-prefix', 'x' * MODEL_TEXT_LIMIT, 'private-suffix']:
        event = ({'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': part}}
                 if route == '/v1/messages' else {'type': 'response.output_text.delta', 'delta': part})
        streamed.consume(event)
    assert streamed.choices[0]['message']['content'] == OMITTED


def test_internal_compaction_traces_remain_content_free(workspace, monkeypatch):
    app, client = workspace
    app.state.settings.litellm_api_base = 'https://gateway.example/v1'
    tracing = app.state.tracing
    tracing.enabled = True
    tracing.processor = Processor()
    run = active(app)
    def unexpected_capture(*args):
        pytest.fail('Compaction must not retain private trace content')
    monkeypatch.setattr('app.harness_gateway.NativeModelContent', unexpected_capture)
    from test_context_gateway import summary_response
    real = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: real(
        transport=httpx.MockTransport(lambda request: summary_response('private-summary')), **kw))
    response = client.post(f"/broker/{run['id']}/context/compact", headers={'Authorization': 'Bearer capability'},
        json={'summary': '', 'entries': [{'seq': 1, 'excerpt': 'private-journal'}]})
    assert response.status_code == 200
    attrs = tracing.processor.spans[0].attributes
    assert attrs['input.value'] == attrs['output.value'] == '[]'
    assert 'private-' not in str(attrs)
    assert attrs['gen_ai.usage.input_tokens'] == 100


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


def test_nested_trace_keeps_root_trace_and_direct_parent_span(tmp_path):
    from app.agents import AgentCoordinator
    from app.db import now
    store = Store(tmp_path)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    AgentCoordinator(store, settings, None)
    tracing = AgentTracing(store, settings, processor=Processor())
    root, batch, reviewer = [store.create_run('Trace task', '', 'demo', [], chat_enabled=True) for _ in range(3)]
    for parent, child, group in [(root, batch, 'batch-group'), (batch, reviewer, 'review-group')]:
        message_id = store.messages(parent['id'])[0]['id']
        store.execute("INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,status,created_at) VALUES(?,?,?,'trace','{}','running',?)", (group, parent['id'], message_id, now()))
        store.execute('UPDATE runs SET parent_run_id=?,agent_group_id=? WHERE id=?', (parent['id'], group, child['id']))
    identities = [tracing.identity(store.run(r['id']), store.messages(r['id'])[0]['id']) for r in (root, batch, reviewer)]
    assert len({identity[0] for identity in identities}) == 1
    assert [identity[3] for identity in identities] == [root['id']] * 3
    assert identities[2][2] == identities[1][1]
    store.execute("UPDATE agent_groups SET status='completed'")
    assert tracing.identity(store.run(reviewer['id']), store.messages(reviewer['id'])[0]['id']) == identities[2]


def test_failed_spans_use_structured_errors_not_model_output(tmp_path):
    store, tracing, processor, run, message = setup(tmp_path)
    diagnostic = {'error_code': 'cyber_policy', 'stage': 'upstream', 'http_status': 400,
                  'sdk_error': 'invalid_request', 'request_id': 'broker-123', 'message': 'private-error-body'}
    tracing.model(run, 'request-1', time.time_ns(), [], {'choices': []}, 'failed', error=diagnostic)
    store.event(run['id'], 'error', 'private-event-message', {'phase': 'sdk_failure', **diagnostic})
    store.finish_message(run['id'], message['id'], 'Public final answer', status='failed')
    model, root = processor.spans
    for span in (model, root):
        assert span.attributes['error.type'] == 'cyber_policy'
        assert span.status.description == span.events[0].attributes['exception.message']
        assert 'cybersecurity policy' in span.status.description
        assert 'HTTP 400' in span.status.description
    payload = encode_spans(processor.spans).SerializeToString()
    assert b'private-error-body' not in payload and b'private-event-message' not in payload
    assert b'cyber_policy' in payload
