"""Background database waits yield to asyncio under real Postgres pool pressure."""
import asyncio
from contextlib import asynccontextmanager
import threading
import time
from types import SimpleNamespace

from fastapi.responses import JSONResponse
import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
import pytest

from app.config import Settings
from app.context_maintenance import ContextMaintenance
from app.db import Store
from app.live_context import LiveContext
from app.model_request import ModelRequest
from app.model_slots import ModelSlots
from app.spend import Spend
from app.trace_outbox import TraceOutbox


@pytest.fixture
def latency_store(tmp_path, request):
    if not request.config.getoption('--postgres-backend'):
        pytest.skip('Pool contention requires --postgres-backend.')
    store = Store(tmp_path, database_pool_size=1)
    try:
        assert store.database is not None
        assert store.database.pool.max_size == 1
        yield store
    finally:
        store.close()


class PoolWait:
    """Release independently of asyncio, after an actual borrower queues."""
    def __init__(self, store):
        self.pool = store.database.pool
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.thread = None
        self.errors = []
        self.wait_started = self.released = None

    def hold(self):
        assert self.thread is None, 'Each case must hold just one pool connection.'
        self.thread = threading.Thread(target=self.holder, name='background-database-latency')
        self.thread.start()
        assert self.ready.wait(3), 'The holder did not acquire the pool connection.'
        assert not self.errors, self.errors

    def holder(self):
        try:
            with self.pool.connection(timeout=3):
                self.ready.set()
                deadline = time.monotonic() + 5
                while not self.pool.get_stats()['requests_waiting']:
                    if self.stop.wait(.005):
                        return
                    assert time.monotonic() < deadline, 'No real pool waiter was observed.'
                self.wait_started = time.monotonic()
                # An asyncio timer cannot rescue a synchronous baseline: the
                # holder thread must release even while the event loop stalls.
                self.stop.wait(.6)
            self.released = time.monotonic()
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self.ready.set()


@asynccontextmanager
async def responsive_pool(store, record_property):
    waiting = PoolWait(store)
    ticks = []

    async def heartbeat():
        previous = time.monotonic()
        while True:
            await asyncio.sleep(.01)
            current = time.monotonic()
            ticks.append((current, current - previous - .01))
            previous = current

    pulse = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0)  # Arm the heartbeat before the contested call.
        yield waiting
        await asyncio.sleep(.02)  # Capture the delayed tick on a blocking baseline.
    finally:
        waiting.stop.set()
        try:
            if waiting.thread is not None:
                await asyncio.to_thread(waiting.thread.join, 3)
                assert not waiting.thread.is_alive(), 'The pool holder did not stop.'
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    assert not waiting.errors, waiting.errors
    assert waiting.wait_started is not None and waiting.released is not None
    duration = waiting.released - waiting.wait_started
    lag = max(delay for _, delay in ticks)
    during = sum(waiting.wait_started <= stamp <= waiting.released for stamp, _ in ticks)
    record_property('pool_wait_seconds', duration)
    record_property('max_event_loop_lag_ms', lag * 1000)
    record_property('heartbeats_during_pool_wait', during)
    assert duration >= .55, f'The real pool wait was too short: {duration:.3f}s'
    assert lag < .3, f'Pool contention blocked asyncio for {lag:.3f}s'
    assert during >= 10, f'Only {during} heartbeats ran during the pool wait.'


@pytest.mark.parametrize('phase', ['collect', 'delivery_receipt'])
async def test_trace_export_pool_wait_keeps_event_loop_responsive(latency_store, record_property, phase):
    store = latency_store
    outbox = TraceOutbox(store, 'trace_outbox', 'https://traces.example/v1/traces', {})
    payload = ExportTraceServiceRequest()
    payload.resource_spans.add().scope_spans.add().spans.add(
        name='pool-contention', trace_id=bytes.fromhex('a' * 32), span_id=bytes.fromhex('b' * 16))
    serialized = payload.SerializeToString()
    store.execute('INSERT INTO trace_outbox(trace_id,span_id,payload,created_at) VALUES(?,?,?,?)',
                  ('a' * 32, 'b' * 16, serialized, time.time()))
    delivered = []

    async def receive(request):
        delivered.append(request.content)
        if phase == 'delivery_receipt':
            # Collection and encoding have finished; only receipt persistence
            # now has to wait for a connection after the receiver acknowledges.
            await asyncio.to_thread(waiting.hold)
        return httpx.Response(200)

    await outbox.client.aclose()
    outbox.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    try:
        async with responsive_pool(store, record_property) as waiting:
            if phase == 'collect':
                await asyncio.to_thread(waiting.hold)
            assert await asyncio.wait_for(outbox.export_once(), 5)
        receipt, = store.rows('SELECT delivered_at,payload,attempts,last_error FROM trace_outbox')
        assert receipt['delivered_at'] is not None
        assert receipt['payload'] is None
        assert receipt['attempts'] == 1 and receipt['last_error'] == ''
        assert not await outbox.export_once()
        assert delivered == [serialized]
    finally:
        await outbox.close()


async def test_live_context_watch_pool_wait_keeps_event_loop_responsive(latency_store, record_property):
    store = latency_store
    run = store.create_run('Retire an obsolete live context.', '', 'demo', [], model='test-model')
    live = LiveContext(SimpleNamespace(
        require_run=lambda run_id, request: store.run(run_id),
        settings=SimpleNamespace(resolve_model=lambda *, fallback: fallback)))
    state = live.state(run, '/v1/responses', run['model'])
    # A remote capability change must still be observed after the pool clears.
    store.update_run(run['id'], token_hash='replacement-capability')
    try:
        async with responsive_pool(store, record_property) as waiting:
            await asyncio.to_thread(waiting.hold)
            await asyncio.wait_for(live.watcher, 5)
        assert state.retired.is_set()
        assert not live.states
        assert store.run(run['id'])['token_hash'] == 'replacement-capability'
    finally:
        await live.close()


async def test_context_admission_pool_wait_keeps_event_loop_responsive(
        latency_store, monkeypatch, record_property):
    store = latency_store
    run = store.create_run('Admit a public context snapshot.', '', 'demo', [], model='test-model')
    snapshot = {'epoch': 'a' * 32, 'cursor': 0, 'summary': '',
                'entries': [{'seq': 1, 'excerpt': 'A completed receipt'}]}
    result = {'summary': 'A completed receipt', 'through_seq': 1}
    release_generation = asyncio.Event()
    summaries, checkpoints = [], []

    async def read_body(*args):
        return {'snapshot': snapshot}

    async def summarize(*args, **kwargs):
        summaries.append((args, kwargs))
        await release_generation.wait()
        return JSONResponse(result)

    async def flush():
        checkpoints.append(True)

    owner = ContextMaintenance(SimpleNamespace(
        store=store, require_run=lambda run_id, request: store.run(run_id),
        read_body=read_body, summarize=summarize, checkpoints=SimpleNamespace(flush=flush),
        model_slots=ModelSlots(1)))
    claim = owner.claim

    def contested_claim(*args, **kwargs):
        # Start pressure at the real admission boundary, after the preliminary
        # reads. The original claim still executes all validation and SQL.
        waiting.hold()
        return claim(*args, **kwargs)

    monkeypatch.setattr(owner, 'claim', contested_claim)
    try:
        async with responsive_pool(store, record_property) as waiting:
            job = await asyncio.wait_for(owner.exchange(run['id'], None), 5)
        assert job['status'] == 'running' and job['snapshot'] == snapshot
        assert owner.read(run['id']) == job
        generators = list(owner.tasks.values())
        assert len(generators) == 1
        release_generation.set()
        await asyncio.wait_for(asyncio.gather(*generators), 3)
        assert owner.read(run['id']) == {**job, 'status': 'completed', 'result': result}
        assert len(summaries) == 1 and checkpoints == [True, True]
        assert not owner.admissions and not owner.tasks and not owner.cleanups
    finally:
        release_generation.set()
        await owner.close()


@pytest.mark.parametrize('phase', ['admit', 'finish'])
async def test_model_request_pool_wait_keeps_event_loop_responsive(latency_store, record_property, phase):
    store = latency_store
    settings = Settings(_env_file=None, data_dir=store.path.parent, litellm_api_key='test-key',
                        litellm_api_base='https://gateway.example/v1')
    run = store.create_run('Persist a model request receipt.', '', 'demo', [],
                           model='test-model', chat_enabled=True, user_id='owner')
    store.claim_message(run['id'])
    store.update_run(run['id'], status='running', token_hash='capability')
    run = store.run(run['id'])
    checkpoints, traces = [], []

    async def flush():
        checkpoints.append(True)

    checkpoint = SimpleNamespace(flush=flush)
    spend = Spend(store, settings, None, checkpoint)
    request = ModelRequest(
        settings=settings, store=store, spend=spend, checkpoints=checkpoint,
        tracing=SimpleNamespace(model=lambda *args, **kwargs: traces.append((args, kwargs))),
        run=run, model=run['model'], started=time.time_ns())
    capture = SimpleNamespace(usage={'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14},
                              cost='0.25')
    if phase == 'finish':
        await request.admit(lambda: None, startup=True)
    async with responsive_pool(store, record_property) as waiting:
        await asyncio.to_thread(waiting.hold)
        if phase == 'admit':
            await asyncio.wait_for(request.admit(lambda: None, startup=True), 5)
        else:
            await asyncio.wait_for(request.finish(capture, 'completed', [], {}, error={}), 5)
    if phase == 'admit':
        assert store.rows('SELECT id,status FROM model_requests') == [{'id': request.id, 'status': 'pending'}]
        await request.finish(capture, 'completed', [], {}, error={})
    assert store.rows('SELECT id,status,cost,prompt_tokens,completion_tokens,total_tokens FROM model_requests') == [
        {'id': request.id, 'status': 'completed', 'cost': '0.25',
         'prompt_tokens': 11, 'completion_tokens': 3, 'total_tokens': 14}]
    current = store.run(run['id'])
    assert current['model_calls'] == current['turn_model_calls'] == 1
    assert checkpoints == [True]
    assert len(traces) == 1 and traces[0][0][1] == request.id and traces[0][0][5] == 'completed'
