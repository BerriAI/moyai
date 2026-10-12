"""Catalog recovery must finish before dispatch and never replay a tool write."""
import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.responses import JSONResponse
from temporalio.exceptions import ApplicationError

from app.database import DatabaseBusyError, DatabaseError
from deepagents_provider import answer, response_stream
from test_deepagents_acceptance import web_worker, create, until
from test_deepagents_publication import active  # noqa: F401
from test_deepagents_runtime import durable_app  # noqa: F401


@pytest.fixture
def catalog_runtime(active, monkeypatch):
    manager, store, scope = active
    delays = []

    async def backoff(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr('app.deepagents_runtime.asyncio',
                        SimpleNamespace(**{**vars(asyncio), 'sleep': backoff}))
    return manager.deepagents_runtime, store, scope, delays


@pytest.mark.parametrize('failure', [408, 425, 429, 500, 502, 503, 504, 524,
                                   httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
async def test_catalog_retries_only_transient_reads(catalog_runtime, failure):
    runtime, store, scope, delays = catalog_runtime
    requests = []

    async def respond(request):
        requests.append(request)
        assert request.method == 'GET' and request.url.path.endswith('/tools')
        if len(requests) < 3:
            if isinstance(failure, int):
                return httpx.Response(failure, text='private-broker-body')
            raise failure('private-network-detail', request=request)
        return httpx.Response(200, json=[{'name': 'fixture_read'}])

    runtime.broker_transport = httpx.MockTransport(respond)
    tools = await runtime.catalog(scope)
    assert 'fixture_read' in {tool['name'] for tool in tools}
    assert len(requests) == 3 and delays == [1, 2]
    assert 'private-broker-body' not in str(store.events(scope.run_id, limit=1000))


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 413, 422, 501])
async def test_catalog_does_not_retry_nontransient_rejections(catalog_runtime, status):
    runtime, store, scope, delays = catalog_runtime
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text='private-broker-body')

    runtime.broker_transport = httpx.MockTransport(respond)
    with pytest.raises(ApplicationError, match=f'Broker request failed \\({status}\\)') as caught:
        await runtime.catalog(scope)
    assert 'private-broker-body' not in str(caught.value)
    assert len(requests) == 1 and delays == []


@pytest.mark.parametrize('busy', [True, False])
async def test_catalog_retries_busy_capability_reads_but_not_ownership_failure(catalog_runtime, monkeypatch, busy):
    runtime, store, scope, delays = catalog_runtime
    current = runtime.current
    checks = []

    def admission(scope):
        checks.append(scope)
        if len(checks) < 3:
            raise (DatabaseBusyError if busy else DatabaseError)('Postgres unavailable')
        return current(scope)

    monkeypatch.setattr(runtime, 'current', admission)
    if busy:
        assert await runtime.catalog(scope)
        assert len(checks) == 3 and delays == [1, 2]
    else:
        with pytest.raises(DatabaseError):
            await runtime.catalog(scope)
        assert len(checks) == 1 and delays == []


async def test_catalog_recovers_after_real_postgres_pool_exhaustion(catalog_runtime, monkeypatch):
    runtime, store, scope, delays = catalog_runtime
    if not store.database:
        pytest.skip('Pool contention requires the real PostgreSQL backend.')
    pool = store.database.pool
    errors = pool.get_stats().get('requests_errors', 0)
    pool.timeout = .1
    with ExitStack() as held:
        for index in range(pool.max_size):
            held.enter_context(pool.connection())

        async def release(delay):
            delays.append(delay)
            held.close()
            await asyncio.sleep(0)

        monkeypatch.setattr('app.deepagents_runtime.asyncio.sleep', release)
        assert await runtime.catalog(scope)
    assert pool.get_stats()['requests_errors'] == errors + 1
    assert delays == [1]
    assert store.rows('SELECT 1 AS ok') == [{'ok': 1}]


@pytest.mark.parametrize('field,value', [('status', 'stopping'), ('token_hash', 'revoked'),
                                        ('active_user_id', 'different-owner')])
async def test_catalog_revalidates_capability_before_retry(catalog_runtime, field, value):
    runtime, store, scope, delays = catalog_runtime
    requests = []

    def respond(request):
        requests.append(request)
        store.execute('UPDATE runs SET ' + field + '=? WHERE id=?', (value, scope.run_id))
        return httpx.Response(500)

    runtime.broker_transport = httpx.MockTransport(respond)
    with pytest.raises(ApplicationError, match='no longer active'):
        await runtime.catalog(scope)
    assert len(requests) == 1 and delays == [1]


async def test_recovered_catalog_cannot_authorize_a_removed_tool(catalog_runtime):
    runtime, store, scope, delays = catalog_runtime
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == 'GET'
        return httpx.Response(500) if len(requests) == 1 else httpx.Response(200, json=[])

    runtime.broker_transport = httpx.MockTransport(respond)
    result = await runtime._call_tool(scope, 'write-once', 'notion_append', {'page_id': 'a' * 32, 'text': 'Fixture'})
    assert result['isError'] and 'No action was attempted' in result['error']
    assert len(requests) == 2 and delays == [1]
    assert not store.rows('SELECT 1 FROM session_items WHERE run_id=?', (scope.run_id,))


async def test_catalog_retry_wait_is_cancellable(catalog_runtime, monkeypatch):
    runtime, store, scope, delays = catalog_runtime
    waiting = asyncio.Event()
    requests = []

    async def backoff(delay):
        waiting.set()
        await asyncio.Event().wait()

    def respond(request):
        requests.append(request)
        return httpx.Response(500)

    monkeypatch.setattr('app.deepagents_runtime.asyncio.sleep', backoff)
    runtime.broker_transport = httpx.MockTransport(respond)
    task = asyncio.create_task(runtime.catalog(scope))
    try:
        await asyncio.wait_for(waiting.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(requests) == 1


async def test_catalog_has_a_total_read_deadline(catalog_runtime, monkeypatch):
    runtime, store, scope, delays = catalog_runtime
    deadlines, requests = [], []

    def deadline(seconds):
        deadlines.append(seconds)
        return asyncio.timeout(.1)

    async def respond(request):
        requests.append(request)
        await asyncio.Event().wait()

    monkeypatch.setattr('app.deepagents_runtime.asyncio.timeout', deadline)
    runtime.broker_transport = httpx.MockTransport(respond)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(runtime.catalog(scope), 2)
    assert deadlines == [45] and len(requests) == 1 and delays == []


@pytest.mark.parametrize('failure', ['catalog_recovers', 'catalog_exhausted', 'write_response_lost'])
async def test_catalog_failure_through_http_and_temporal_never_replays_writes(durable_app, monkeypatch, tmp_path, failure):
    app = durable_app
    store = app.state.store
    model_calls, catalog_requests, write_requests = [], [], []
    receipt = tmp_path / 'receipt.txt'
    app.state.connectors.save('notion', {'access_token': 'fixture-token'}, 'Fixture app')

    async def connected(name, arguments, **kwargs):
        assert name == 'notion_append'
        with receipt.open('a') as stream:
            stream.write('completed write\n')
        return {'id': 'confirmed-fixture-write'}

    monkeypatch.setattr(app.state.connectors, 'call', connected)

    @app.middleware('http')
    async def broker_fault(request, call_next):
        if request.url.path.endswith('/tools'):
            catalog_requests.append(request.url.path)
            if failure == 'catalog_exhausted' or (failure == 'catalog_recovers' and len(catalog_requests) < 3):
                return JSONResponse({'error': 'private-broker-body'}, status_code=500)
        if request.url.path.endswith('/tools/call'):
            write_requests.append(request.url.path)
            response = await call_next(request)
            if failure == 'write_response_lost':
                assert response.status_code == 200
                return JSONResponse({'error': 'private-broker-body'}, status_code=500)
            return response
        return await call_next(request)

    async def upstream(request):
        if not request.url.path.endswith('/responses'):
            return httpx.Response(200, json={'total_tokens': 100})
        model_calls.append(json.loads(request.content))
        response = answer('Saved exactly once.', identity=f'catalog-{len(model_calls)}')
        if len(model_calls) == 1:
            response['output'] = [{'type': 'function_call', 'id': 'fc_write', 'call_id': 'call_write',
                'name': 'workspace_call', 'status': 'completed', 'arguments': json.dumps({
                    'name': 'notion_append', 'arguments_json': json.dumps({'page_id': 'a' * 32, 'text': 'Fixture'})})}]
        return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                              content=b''.join(response_stream(response)))

    actual = httpx.AsyncClient

    class ProviderClient(actual):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(upstream), **kwargs)

    monkeypatch.setattr('app.harness_gateway.httpx', SimpleNamespace(**{**vars(httpx), 'AsyncClient': ProviderClient}))
    async with web_worker(app, activity_slots=2) as api:
        run_id = await create(api, 'Append the fixture note exactly once, then answer.', ['notion'])
        await until(lambda: store.run(run_id)['status'] in {'idle', 'failed', 'cancelled'}, seconds=40)
        assert len(catalog_requests) == (1 if failure == 'write_response_lost' else 3)
        assert len(write_requests) == (0 if failure == 'catalog_exhausted' else 1)
        if failure == 'catalog_exhausted':
            assert not receipt.exists()
        else:
            assert receipt.read_text() == 'completed write\n'
        if failure == 'catalog_recovers':
            assert store.run(run_id)['status'] == 'idle'
            assert store.run(run_id)['summary'] == 'Saved exactly once.'
            assert len(model_calls) == 2
            assert 'confirmed-fixture-write' in str(store.rows('SELECT * FROM session_items WHERE run_id=?', (run_id,)))
        else:
            assert store.run(run_id)['status'] == 'failed'
            assert len(model_calls) == 1
            assert not [message for message in store.messages(run_id)
                        if message['role'] == 'assistant' and message['status'] == 'completed']
        assert 'private-broker-body' not in str(store.events(run_id, limit=1000))
