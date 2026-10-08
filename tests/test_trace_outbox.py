import asyncio
from contextlib import contextmanager
import time

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest, ExportTraceServiceResponse,
)
import pytest

from app.agents import AgentCoordinator
from app.config import Settings
from app.db import Store
from app.tracing import AgentTracing


def setup(tmp_path):
    store = Store(tmp_path)
    settings = Settings(_env_file=None, litellm_trace_endpoint='https://traces.example/v1/traces',
                        litellm_trace_api_key='trace-secret')
    tracing = store.tracing = AgentTracing(store, settings)
    run = store.create_run('Read example.txt', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    return store, tracing, store.run(run['id']), message


def spans(payload):
    request = ExportTraceServiceRequest.FromString(payload)
    return [span for resource in request.resource_spans for scope in resource.scope_spans for span in scope.spans]


async def transport(tracing, handler, outbox=0):
    await tracing.outboxes[outbox].client.aclose()
    tracing.outboxes[outbox].client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def setup_both(tmp_path, endpoint='https://traces.example/v1/traces'):
    store = Store(tmp_path)
    settings = Settings(_env_file=None, litellm_trace_endpoint=endpoint,
                        litellm_trace_api_key='trace-secret', raindrop_write_key='raindrop-secret',
                        raindrop_project_id='moyai')
    tracing = store.tracing = AgentTracing(store, settings)
    run = store.create_run('Read example.txt', '', 'modal', [], chat_enabled=True)
    message = store.claim_message(run['id'])
    return store, tracing, store.run(run['id']), message


async def test_raindrop_alone_enables_export_without_litellm(tmp_path):
    store = Store(tmp_path)
    tracing = AgentTracing(store, Settings(_env_file=None, raindrop_write_key='raindrop-secret'))
    assert tracing.enabled
    assert [(o.table, o.endpoint) for o in tracing.outboxes] == [
        ('trace_outbox_raindrop', 'https://api.raindrop.ai/v1/traces')]
    assert 'X-Raindrop-Project-Id' not in tracing.outboxes[0].headers
    await tracing.close()


@pytest.mark.parametrize('endpoint', ['https://traces.example/v1/traces',
                                     'https://traces.example/lens-ingest/v1/traces'])
async def test_each_span_reaches_litellm_and_raindrop_with_their_own_keys(tmp_path, endpoint):
    store, tracing, run, message = setup_both(tmp_path, endpoint)
    store.finish_message(run['id'], message['id'], 'hello')
    received = {}
    def receiver(name):
        def handle(request):
            received[name] = request
            return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())
        return handle
    await transport(tracing, receiver('litellm'), 0)
    await transport(tracing, receiver('raindrop'), 1)
    assert all([await outbox.export_once() for outbox in tracing.outboxes])
    litellm, raindrop = received['litellm'], received['raindrop']
    assert str(litellm.url) == endpoint
    assert str(raindrop.url) == 'https://api.raindrop.ai/v1/traces'
    assert litellm.headers['authorization'] == 'Bearer trace-secret'
    assert raindrop.headers['authorization'] == 'Bearer raindrop-secret'
    assert raindrop.headers['x-raindrop-project-id'] == 'moyai'
    assert 'x-raindrop-project-id' not in litellm.headers
    assert litellm.content == raindrop.content
    span = spans(raindrop.content)[0]
    attrs = {a.key: a.value.string_value for a in span.attributes}
    assert attrs['traceloop.association.properties.convo_id'] == run['id']
    assert attrs['traceloop.association.properties.event_id'] == span.trace_id.hex()
    await tracing.close()


async def test_raindrop_outage_does_not_block_or_resend_litellm(tmp_path):
    store, tracing, run, message = setup_both(tmp_path)
    store.finish_message(run['id'], message['id'], 'hello')
    litellm, raindrop = [], []
    def ok(request):
        litellm.append(request.content)
        return httpx.Response(200)
    def down(request):
        raindrop.append(request.content)
        return httpx.Response(503) if len(raindrop) == 1 else httpx.Response(200)
    await transport(tracing, ok, 0)
    await transport(tracing, down, 1)
    assert await tracing.outboxes[0].export_once()
    assert not await tracing.outboxes[1].export_once()
    assert store.rows('SELECT delivered_at FROM trace_outbox')[0]['delivered_at']
    assert store.rows('SELECT delivered_at FROM trace_outbox_raindrop')[0]['delivered_at'] is None
    store.execute('UPDATE trace_outbox_raindrop SET next_attempt_at=0')
    assert await tracing.outboxes[1].export_once()
    assert not await tracing.outboxes[0].export_once()
    assert len(litellm) == 1 and raindrop[0] == raindrop[1]
    await tracing.close()


def test_raindrop_endpoint_rejects_insecure_destinations():
    with pytest.raises(ValueError):
        Settings(_env_file=None, raindrop_trace_endpoint='http://api.raindrop.ai/v1/traces')


async def test_restart_delivers_persisted_payload_and_retains_receipt(tmp_path):
    store, tracing, run, message = setup(tmp_path)
    store.finish_message(run['id'], message['id'], 'hello')
    before = store.rows('SELECT * FROM trace_outbox')[0]
    await tracing.close()
    # No process memory or original Store instance is needed to deliver.
    reopened = Store(tmp_path)
    resumed = reopened.tracing = AgentTracing(reopened, tracing.settings)
    delivered = []
    def gateway(request):
        assert request.headers['authorization'] == 'Bearer trace-secret'
        assert request.headers['content-type'] == 'application/x-protobuf'
        delivered.append(request.content)
        return httpx.Response(200, content=ExportTraceServiceResponse().SerializeToString())
    await transport(resumed, gateway)
    assert await resumed.outboxes[0].export_once()
    assert delivered == [before['payload']]
    receipt = reopened.rows('SELECT * FROM trace_outbox')[0]
    assert receipt['delivered_at'] and receipt['payload'] is None
    # A repeated producer event must not re-send a span already acknowledged.
    resumed.finish_turn(run['id'], message['id'], 'hello', 'completed')
    assert not await resumed.outboxes[0].export_once()
    assert len(delivered) == 1
    assert len(reopened.rows('SELECT * FROM trace_outbox')) == 1
    await resumed.close()


@pytest.mark.parametrize('failure', ['timeout', 'http', 'partial-protobuf', 'partial-json', 'malformed'])
async def test_failed_delivery_retries_same_ids_and_payload(tmp_path, failure):
    store, tracing, run, message = setup(tmp_path)
    store.finish_message(run['id'], message['id'], 'hello')
    delivered = []
    def gateway(request):
        delivered.append(request.content)
        if len(delivered) > 1:
            return httpx.Response(200)
        if failure == 'timeout':
            # Receiver may have accepted it before its ACK was lost.
            raise httpx.ReadTimeout('no acknowledgment')
        if failure == 'http':
            return httpx.Response(503, text='unavailable')
        if failure == 'partial-protobuf':
            result = ExportTraceServiceResponse()
            result.partial_success.rejected_spans = 1
            return httpx.Response(200, content=result.SerializeToString())
        if failure == 'partial-json':
            return httpx.Response(200, json={'partialSuccess': {'rejectedSpans': '1'}})
        return httpx.Response(200, content=b'not protobuf')
    await transport(tracing, gateway)
    assert not await tracing.outboxes[0].export_once()
    row = store.rows('SELECT * FROM trace_outbox')[0]
    assert row['payload'] and row['delivered_at'] is None and row['attempts'] == 1
    assert row['next_attempt_at'] > time.time()
    assert not await tracing.outboxes[0].export_once()  # Backoff is enforced.
    store.execute('UPDATE trace_outbox SET next_attempt_at=0')
    assert await tracing.outboxes[0].export_once()
    assert delivered[0] == delivered[1]
    assert spans(delivered[0])[0].span_id.hex() == row['span_id']
    await tracing.close()


async def test_shutdown_during_request_leaves_payload_for_restart(tmp_path):
    store, tracing, run, message = setup(tmp_path)
    store.finish_message(run['id'], message['id'], 'hello')
    started = asyncio.Event()
    async def gateway(request):
        started.set()
        await asyncio.Event().wait()
    await transport(tracing, gateway)
    tracing.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(tracing.close(), timeout=2)
    row = store.rows('SELECT * FROM trace_outbox')[0]
    assert row['payload'] and row['delivered_at'] is None


async def test_answer_and_root_span_rollback_together(tmp_path, monkeypatch):
    store, tracing, run, message = setup(tmp_path)
    original = store.connect
    @contextmanager
    def interrupted_commit():
        with original() as conn:
            yield conn
            raise RuntimeError('process interrupted before commit')
    with monkeypatch.context() as patch:
        patch.setattr(store, 'connect', interrupted_commit)
        with pytest.raises(RuntimeError, match='before commit'):
            store.finish_message(run['id'], message['id'], 'hello')
    assert not store.rows('SELECT * FROM trace_outbox')
    assert not store.rows('SELECT * FROM trace_contexts')
    assert not [m for m in store.messages(run['id']) if m['role'] == 'assistant']
    store.finish_message(run['id'], message['id'], 'hello')
    assert len(store.rows('SELECT * FROM trace_outbox')) == 1
    assert len([m for m in store.messages(run['id']) if m['role'] == 'assistant']) == 1
    await tracing.close()


async def test_child_name_parent_and_identity_survive_group_completion_and_restart(tmp_path):
    store, tracing, parent, parent_message = setup(tmp_path)
    AgentCoordinator(store, tracing.settings, None)
    store.execute('''INSERT INTO agent_groups(id,parent_id,message_id,request_key,payload,created_at,status)
                     VALUES('group',?,?,'fanout','{}','now','running')''', (parent['id'], parent_message['id']))
    for index, label in enumerate(['Cases 1–20', 'Cases 21–40']):
        child = store.create_run('Run assigned cases', '', 'modal', [], chat_enabled=True)
        store.execute("UPDATE runs SET parent_run_id=?,agent_group_id='group',agent_label=? WHERE id=?",
                      (parent['id'], label, child['id']))
        message = store.claim_message(child['id'])
        child = store.run(child['id'])
        tracing.model(child, f'request-{index}', time.time_ns(), [], {'choices': []}, 'completed')
        if index == 0:
            # The group can settle before a late tool journal/root is replayed.
            store.execute("UPDATE agent_groups SET status='completed'")
            store.execute("UPDATE runs SET agent_label='Renamed worker' WHERE id=?", (child['id'],))
            await tracing.close()
            tracing = store.tracing = AgentTracing(store, tracing.settings)
        tracing.tool(child['id'], {'call_id': f'tool-{index}', 'tool': 'terminal',
                                  'start_ns': time.time_ns()-100, 'end_ns': time.time_ns(),
                                  'input': 'pwd', 'output': '/workspace'})
        store.finish_message(child['id'], message['id'], 'done')
        store.execute("UPDATE agent_groups SET status='running'")
    store.finish_message(parent['id'], parent_message['id'], 'all done')
    exported = [span for row in store.rows('SELECT payload FROM trace_outbox') for span in spans(row['payload'])]
    attrs = lambda span: {a.key: a.value.string_value for a in span.attributes}
    parent_span = next(s for s in exported if s.name == 'moyai')
    assert len({s.trace_id for s in exported}) == 1
    for label in ['Cases 1–20', 'Cases 21–40']:
        worker = [s for s in exported if attrs(s)['agent.name'] == label]
        assert len(worker) == 3
        root = next(s for s in worker if s.name == label)
        assert root.parent_span_id == parent_span.span_id
        assert all(s.parent_span_id == root.span_id for s in worker if s != root)
        assert all(attrs(s)['gen_ai.agent.name'] == label for s in worker)
    for row in store.rows('SELECT payload FROM trace_outbox'):
        request = ExportTraceServiceRequest.FromString(row['payload'])
        assert request.resource_spans[0].resource.attributes[0].value.string_value == 'moyai'
    await tracing.close()
