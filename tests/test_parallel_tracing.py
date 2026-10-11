import asyncio
import json
import time

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.tracing import AgentTracing, MODEL_JSON_BYTES
from test_trace_outbox import spans, transport


def settings(**overrides):
    values = dict(litellm_trace_endpoint='https://lens.example/v1/traces', litellm_trace_api_key='lens-secret',
                  raindrop_write_key='rain-secret', langfuse_public_key='pk-lf-test', langfuse_secret_key='lf-secret',
                  langsmith_api_key='ls-secret', braintrust_api_key='bt-secret', trace_environment='comparison',
                  langfuse_tracing_environment='comparison')
    return Settings(_env_file=None, **(values | overrides))


@pytest.mark.parametrize('field', ['langsmith_endpoint', 'braintrust_api_url'])
@pytest.mark.parametrize('value', ['http://example.com', 'https://key@example.com', 'https://example.com?key=x'])
def test_reject_unsafe_base_urls(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


@pytest.mark.parametrize('field,value', [('langsmith_project', 'bad\r\nheader'),
                                         ('langsmith_workspace_id', 'bad\nheader'),
                                         ('braintrust_parent', 'experiment_id:wrong-destination')])
def test_reject_invalid_routing(field, value):
    with pytest.raises(ValueError):
        settings(**{field: value})


@pytest.mark.parametrize('linked_slack', [False, True])
async def test_five_receivers_get_identical_tree_and_raindrop_gets_interaction(tmp_path, linked_slack):
    store = Store(tmp_path)
    tracing = store.tracing = AgentTracing(store, settings(langsmith_workspace_id='workspace-id'))
    actor = store.identity({'method': 'google', 'identity': {'sub': 'private-user', 'email': 'alice@example.com'}})
    if linked_slack:
        with store.connect() as conn:
            slack = store.slack_identity_in(conn, 'T12345678', 'U12345678')
            conn.execute("UPDATE users SET linked_user_id=?,email='outdated@example.com' WHERE id=?", (actor, slack))
        actor = slack
    run = store.create_run('Read probe.txt ls-secret', '', 'modal', [], chat_enabled=True,
                           model='openai/gpt-6-astra', user_id=actor)
    message = store.claim_message(run['id'])
    run = store.run(run['id'])
    stamp = time.time_ns()
    tracing.model(run, 'request-id', stamp, [{'role': 'system', 'content': 'private-system'},
                  {'role': 'user', 'content': 'Read probe.txt bt-secret'}],
                  {'model': 'gpt-6-astra', 'choices': [{'message': {'content': '15', 'reasoning_content': 'private-reasoning'}}],
                   'usage': {'prompt_tokens': 10, 'completion_tokens': 4}}, 'completed')
    tracing.tool(run['id'], {'tool': 'read_file', 'call_id': 'call-id', 'start_ns': stamp, 'end_ns': time.time_ns(),
                            'input': {'path': 'probe.txt'}, 'output': '15 rain-secret'})
    store.finish_message(run['id'], message['id'], '15 lf-secret')
    requests = {}
    for i, box in enumerate(tracing.outboxes):
        def receive(request, name=box.table):
            requests[name] = request
            return httpx.Response(200)
        await transport(tracing, receive, i)
    await tracing.events.client.aclose()
    tracing.events.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (requests.update(events=request), httpx.Response(204))[1]))
    assert all(await asyncio.gather(*(box.export_once() for box in tracing.exporters())))
    assert len(tracing.outboxes) == 5
    assert len({request.content for name, request in requests.items() if name != 'events'}) == 1
    ls = requests['trace_outbox_langsmith']
    assert str(ls.url) == 'https://api.smith.langchain.com/otel/v1/traces'
    assert ls.headers['x-api-key'] == 'ls-secret'
    assert ls.headers['langsmith-project'] == 'moyai'
    assert ls.headers['x-tenant-id'] == 'workspace-id'
    bt = requests['trace_outbox_braintrust']
    assert str(bt.url) == 'https://api.braintrust.dev/otel/v1/traces'
    assert bt.headers['authorization'] == 'Bearer bt-secret'
    assert bt.headers['x-bt-parent'] == 'project_name:moyai'
    exported = spans(ls.content)
    root = next(s for s in exported if not s.parent_span_id)
    assert all(s.parent_span_id == root.span_id for s in exported if s != root)
    attrs = lambda s: {a.key: a.value.string_value for a in s.attributes}
    model = next(s for s in exported if attrs(s)['openinference.span.kind'] == 'LLM')
    tool = next(s for s in exported if attrs(s)['openinference.span.kind'] == 'TOOL')
    assert attrs(tool)['gen_ai.tool.call.result'] == '15 [redacted]'
    assert json.loads(attrs(root)['gen_ai.input.messages'])[0]['content'] == 'Read probe.txt [redacted]'
    assert attrs(model)['gen_ai.system'] == 'openai'
    assert attrs(model)['gen_ai.response.model'] == 'gpt-6-astra'
    assert json.loads(attrs(model)['gen_ai.input.messages'])[0]['role'] == 'user'
    assert json.loads(attrs(model)['gen_ai.output.messages'])[0]['role'] == 'assistant'
    assert json.loads(attrs(model)['gen_ai.output.messages'])[0]['parts'][0]['content'] == '15'
    assert attrs(model)['langsmith.span.kind'] == attrs(model)['braintrust.span_attributes.type'] == 'llm'
    event_request = requests['events']
    assert str(event_request.url) == 'https://api.raindrop.ai/v1/events/track'
    event = json.loads(event_request.content)[0]
    assert event['event_id'] == root.trace_id.hex()
    assert event['ai_data']['convo_id'] == run['id']
    assert event['ai_data']['output'] == '15 [redacted]'
    assert event['user_id'] == 'alice@example.com'
    for s in exported:
        assert attrs(s)['user.id'] == 'alice@example.com'
        assert attrs(s)['traceloop.association.properties.user_id'] == event['user_id']
        assert attrs(s)['traceloop.association.properties.event_id'] == event['event_id']
        assert attrs(s)['langsmith.metadata.thread_id'] == run['id']
    for private in (b'ls-secret', b'bt-secret', b'rain-secret', b'lf-secret', b'private-system', b'private-reasoning', b'google:private-user', b'slack:T12345678:U12345678', b'outdated@example.com'):
        assert private not in ls.content + event_request.content
    assert not any(await asyncio.gather(*(box.export_once() for box in tracing.exporters())))
    await tracing.close()


async def test_message_json_stays_valid_when_large_and_errors_keep_body_out_of_exception(tmp_path):
    store = Store(tmp_path)
    tracing = store.tracing = AgentTracing(store, settings())
    run = store.create_run('Check', '', 'modal', [], chat_enabled=True)
    store.claim_message(run['id'])
    content = 'ls-secret ' + '\\"\n' * 16000
    legacy, genai = tracing.model_content([{'role': 'user', 'content': content}] * 5)
    for value in (legacy, genai):
        assert len(value.encode()) <= MODEL_JSON_BYTES
        messages = json.loads(value)
        assert len(messages) == 5
        assert all(message['content'] == content.replace('ls-secret', '[redacted]') for message in messages)
        assert 'ls-secret' not in value
    legacy, genai = tracing.model_content([{'role': 'assistant', 'content': None, 'tool_names': ['read_file']}])
    assert json.loads(legacy)[0]['content'] is None
    assert json.loads(genai)[0]['parts'] == []
    tracing.tool(run['id'], {'tool': 'read_file', 'call_id': 'failed', 'start_ns': time.time_ns(),
                            'end_ns': time.time_ns(), 'status': 'error', 'output': 'Failure ls-secret'})
    row = store.rows('SELECT payload FROM trace_outbox')[0]
    span = spans(row['payload'])[0]
    attrs = {a.key: a.value.string_value for a in span.attributes}
    assert attrs['output.value'] == attrs['gen_ai.tool.call.result'] == 'Failure [redacted]'
    assert b'ls-secret' not in row['payload']
    assert span.status.code == 2  # OTLP STATUS_CODE_ERROR
    assert span.status.message == 'Operation error'
    event = span.events[0]
    assert event.name == 'exception'
    exception = {a.key: a.value.string_value for a in event.attributes}
    assert exception == {'exception.message': 'Operation error', 'exception.type': 'OperationError'}
    await tracing.close()


async def test_slow_receiver_does_not_block_other_workers_and_restart_replays_only_pending(tmp_path):
    store = Store(tmp_path)
    tracing = store.tracing = AgentTracing(store, settings())
    run = store.create_run('Say hello', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'hello')
    slow_started, delivered = asyncio.Event(), set()
    for box in tracing.exporters():
        async def receive(request, name=box.table):
            if name == 'trace_outbox_langsmith':
                slow_started.set()
                await asyncio.Event().wait()
            delivered.add(name)
            return httpx.Response(204 if name.endswith('_events') else 200)
        await box.client.aclose()
        box.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    tracing.start()
    await asyncio.wait_for(slow_started.wait(), 2)
    async def complete():
        while len(delivered) != 5:
            await asyncio.sleep(.01)
    await asyncio.wait_for(complete(), 2)
    await tracing.close()
    reopened = Store(tmp_path)
    resumed = AgentTracing(reopened, settings())
    sent = []
    for box in resumed.exporters():
        def receive(request, name=box.table):
            sent.append(name)
            return httpx.Response(204 if name.endswith('_events') else 200)
        await box.client.aclose()
        box.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    await asyncio.gather(*(box.export_once() for box in resumed.exporters()))
    assert sent == ['trace_outbox_langsmith']
    await resumed.close()


@pytest.mark.parametrize('success_status', [200, 204])
async def test_raindrop_event_outage_and_rate_limit_are_durable(tmp_path, success_status):
    store = Store(tmp_path)
    tracing = store.tracing = AgentTracing(store, settings())
    run = store.create_run('Say hello', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    store.finish_message(run['id'], message['id'], 'hello')
    box = tracing.events
    await box.client.aclose()
    box.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(429, headers={'Retry-After': '60'})))
    assert not await box.export_once()
    row = store.rows('SELECT * FROM trace_outbox_raindrop_events')[0]
    assert row['next_attempt_at'] >= time.time() + 59
    assert row['payload'] and not row['delivered_at']
    await tracing.close()
    resumed = AgentTracing(Store(tmp_path), settings())
    resumed.store.execute('UPDATE trace_outbox_raindrop_events SET next_attempt_at=0')
    seen = []
    await resumed.events.client.aclose()
    resumed.events.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (seen.append(request.content), httpx.Response(success_status))[1]))
    assert await resumed.events.export_once()
    assert json.loads(seen[0])[0]['event_id'] == row['trace_id']
    receipt = resumed.store.rows('SELECT * FROM trace_outbox_raindrop_events')[0]
    assert receipt['delivered_at'] and receipt['payload'] is None
    await resumed.close()
