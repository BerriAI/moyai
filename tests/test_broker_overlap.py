"""Verified broker generations share real PostgreSQL ownership and capacity."""
import asyncio
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.broker_ownership import BrokerMaintenance
from app.main import create_app
from app.schema_migrations import migrate
from app.spend import UsageCapture
from test_separate_broker import split_settings  # noqa: F401
from test_postgres_runtime import target  # noqa: F401


@pytest.fixture
def verified(split_settings, request):
    migrate(split_settings)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify', 'max_concurrent_model_requests': getattr(request, 'param', 2)})
    coordinator = create_app(settings)
    applications = []

    def broker():
        app = create_app(settings.model_copy(update={'moyai_runtime_role': 'broker',
            'data_dir': settings.data_dir / f'broker-{len(applications)}'}))
        applications.append(app)
        return app

    try:
        yield broker, settings
    finally:
        for app in applications:
            app.state.model_slots.close()
            app.state.store.close()
        coordinator.state.store.close()


def pending(app, label):
    store = app.state.store
    run = store.create_run(label, '', 'modal', [], chat_enabled=True)
    request_id = app.state.spend.begin(run, 'synthetic-model')
    operation_id = f'context-{request_id}'
    with store.connect() as conn:
        conn.execute("INSERT INTO context_jobs VALUES(?,?,?,'null','running')", (run['id'], operation_id, '{}'))
        store.broker_work.claim_in(conn, 'context', operation_id)
        # Legacy executing approvals are included in ownership-aware recovery,
        # although the current direct-tool protocol never creates these rows.
        conn.execute("INSERT INTO approvals VALUES(?,?,?,?,'executing',?,'')", (request_id, run['id'], 'fixture', '{}', '2026-01-01'))
        store.broker_work.claim_in(conn, 'approval', request_id)
    return request_id, operation_id


def status(app, table, key, value):
    return app.state.store.rows(f'SELECT status FROM {table} WHERE {key}=?', (value,))[0]['status']


async def test_replacement_start_preserves_live_work_and_recovers_only_dead_owner(verified):
    broker, _ = verified
    first = broker()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(first.router.lifespan_context(first))
        first_request, first_context = pending(first, 'old generation')
        replacement = broker()
        await stack.enter_async_context(replacement.router.lifespan_context(replacement))
        second_request, second_context = pending(replacement, 'replacement generation')
        assert replacement.state.broker_requests.work.recover() == 0
        for app, request, context in [(first, first_request, first_context), (replacement, second_request, second_context)]:
            assert status(app, 'model_requests', 'id', request) == 'pending'
            assert status(app, 'context_jobs', 'operation_id', context) == 'running'
            assert status(app, 'approvals', 'id', request) == 'executing'
        first.state.store.database.owner.close()
        assert replacement.state.broker_requests.work.recover() == 3
        assert status(replacement, 'model_requests', 'id', first_request) == 'interrupted'
        assert status(replacement, 'context_jobs', 'operation_id', first_context) == 'interrupted'
        assert status(replacement, 'approvals', 'id', first_request) == 'uncertain'
        assert status(replacement, 'model_requests', 'id', second_request) == 'pending'
        assert status(replacement, 'context_jobs', 'operation_id', second_context) == 'running'
        assert status(replacement, 'approvals', 'id', second_request) == 'executing'


async def test_global_capacity_is_shared_and_released_only_after_request_cleanup(verified):
    broker, _ = verified
    old, new = broker(), broker()
    old_slots, new_slots = old.state.model_slots, new.state.model_slots
    permit = await old_slots.acquire()
    other = await new_slots.acquire()
    assert old_slots.locked() and new_slots.locked()
    assert old_slots.snapshot()['active'] == new_slots.snapshot()['active'] == 2
    waiting = asyncio.create_task(new_slots.acquire())
    await asyncio.sleep(.1)
    assert not waiting.done()
    # Runtime ownership loss does not release the distinct slot session while
    # a still-live process is closing its provider transport.
    old.state.store.database.owner.close()
    await asyncio.sleep(.1)
    assert not waiting.done()
    old_slots.release(permit)
    third = await asyncio.wait_for(waiting, 2)
    assert new_slots.snapshot()['active'] == 2
    new_slots.release(other)
    new_slots.release(third)
    assert new_slots.snapshot()['active'] == 0
    with pytest.raises(RuntimeError, match='already released'):
        new_slots.release(third)


async def test_capacity_uses_one_connection_per_broker_not_per_request(verified):
    broker, _ = verified
    first, second = broker(), broker()
    first_slots, second_slots = first.state.model_slots, second.state.model_slots
    first_slots.check()
    second_slots.check()
    owners = {first_slots._connection.info.backend_pid, second_slots._connection.info.backend_pid}
    one, two = await first_slots.acquire(), await first_slots.acquire()
    assert first_slots._connection.info.backend_pid in owners
    assert second_slots.locked()
    rows = second.state.store.rows("""SELECT DISTINCT pid FROM pg_locks
        WHERE locktype='advisory' AND classid=726947 AND objsubid=2 AND granted
        AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND objid=ANY(?::oid[])""", (list(first_slots._unsigned_keys),))
    assert {row['pid'] for row in rows} <= owners
    first_slots.release(one)
    three = await second_slots.acquire()
    assert first_slots.snapshot()['active'] == 2
    first_slots.release(two)
    second_slots.release(three)


async def test_maintenance_transfer_waits_for_previous_owner_cleanup(verified):
    broker, _ = verified
    old, new = broker(), broker()
    cleaning, cleaned = asyncio.Event(), asyncio.Event()

    async def cleanup():
        cleaning.set()
        await cleaned.wait()

    first_memory = SimpleNamespace(start=AsyncMock(), close=AsyncMock(side_effect=cleanup))
    next_memory = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    # start() is deliberately synchronous, as in MemoryReview/SpendRecovery.
    calls = []
    first_memory.start = lambda: calls.append('old')
    next_memory.start = lambda: calls.append('new')
    spend = lambda: SimpleNamespace(start=lambda: None, close=AsyncMock())
    first = BrokerMaintenance(old.state.store, first_memory, spend())
    replacement = BrokerMaintenance(new.state.store, next_memory, spend())
    first.start()
    while calls != ['old']:
        await asyncio.sleep(.01)
    replacement.start()
    await asyncio.sleep(.3)
    assert calls == ['old']
    closing = asyncio.create_task(first.close())
    await asyncio.wait_for(cleaning.wait(), 2)
    await asyncio.sleep(.3)
    assert calls == ['old']
    cleaned.set()
    await closing
    async with asyncio.timeout(2):
        while calls != ['old', 'new']:
            await asyncio.sleep(.01)
    await replacement.close()


def test_completed_receipt_releases_ownership_atomically(verified):
    broker, _ = verified
    first, replacement = broker(), broker()
    request, _ = pending(first, 'completed model')
    first.state.spend.finish(request, UsageCapture(False), 'completed')
    assert not first.state.store.rows("SELECT 1 FROM broker_work_owners WHERE kind='model' AND work_id=?", (request,))
    first.state.store.database.owner.close()
    replacement.state.broker_requests.work.recover()
    assert status(replacement, 'model_requests', 'id', request) == 'completed'


def test_backend_pid_reuse_cannot_keep_dead_work_alive(verified):
    broker, _ = verified
    first = broker()
    request, _ = pending(first, 'stale backend identity')
    first.state.store.execute("UPDATE broker_work_owners SET owner_started='0' WHERE kind='model' AND work_id=?", (request,))
    assert first.state.broker_requests.work.recover() == 1
    assert status(first, 'model_requests', 'id', request) == 'interrupted'


@pytest.mark.parametrize('live_work', [False, True])
async def test_noop_recovery_does_not_wait_for_the_schema_writer(verified, live_work):
    from app.db import database

    broker, _ = verified
    app = broker()
    if live_work:
        pending(app, 'owned work')
    recovering = None
    try:
        with app.state.store.connect() as writer:
            writer.begin_write()
            # A real exclusive schema lock must not block a healthy scan.
            recovering = asyncio.create_task(database(app.state.broker_requests.work.recover))
            assert await asyncio.wait_for(asyncio.shield(recovering), 2) == 0
    finally:
        if recovering:
            await asyncio.gather(recovering, return_exceptions=True)


async def test_dead_recovery_skips_busy_schema_without_blocking_other_sessions(verified):
    from app.db import database

    broker, _ = verified
    old, replacement = broker(), broker()
    request, context = pending(old, 'dead owner')
    old.state.store.database.owner.close()
    store = replacement.state.store
    held = store.create_run('writer held open', '', 'modal', [])['id']
    independent = store.create_run('unrelated writer', '', 'modal', [])['id']
    recovering = None
    try:
        with store.connect(write_scope=held) as writer:
            writer.begin_write()
            recovering = asyncio.create_task(database(replacement.state.broker_requests.work.recover))
            assert await asyncio.wait_for(asyncio.shield(recovering), 2) == 0
            # Recovery must not queue an exclusive lock ahead of this second
            # real session writer while the first transaction is still open.
            await asyncio.wait_for(database(store.update_run, independent, summary='continued'), 2)
            assert status(replacement, 'model_requests', 'id', request) == 'pending'
    finally:
        if recovering:
            await asyncio.gather(recovering, return_exceptions=True)
    assert store.run(independent)['summary'] == 'continued'
    assert await database(replacement.state.broker_requests.work.recover) == 3
    assert status(replacement, 'model_requests', 'id', request) == 'interrupted'
    assert status(replacement, 'context_jobs', 'operation_id', context) == 'interrupted'
    assert status(replacement, 'approvals', 'id', request) == 'uncertain'
    assert not store.rows('SELECT 1 FROM broker_work_owners')


def test_recovery_rechecks_receipts_and_new_backend_identity_after_read_probe(verified, monkeypatch):
    from app.database import PostgresConnection

    broker, settings = verified
    old, recovering = broker(), broker()
    request, context = pending(old, 'transferred receipt')
    old.state.store.database.owner.close()
    original = PostgresConnection.execute
    reassigned = False

    def join_before_write(conn, statement, params=()):
        nonlocal reassigned
        if (not reassigned and conn.schema == settings.moyai_database_schema
                and 'pg_try_advisory_xact_lock(726941,' in statement):
            reassigned = True
            # Deterministically place a real new PostgreSQL backend and receipt
            # transfer between the read probe and recovery's write lock. Only
            # the timing is instrumented; ownership/locks/queries remain real.
            joined = broker()
            owner = joined.state.broker_requests.work
            joined.state.store.execute('UPDATE broker_work_owners SET owner_pid=?,owner_started=?',
                                       (owner.pid, owner.started))
        return original(conn, statement, params)

    monkeypatch.setattr(PostgresConnection, 'execute', join_before_write)
    assert recovering.state.broker_requests.work.recover() == 0
    assert reassigned
    assert status(recovering, 'model_requests', 'id', request) == 'pending'
    assert status(recovering, 'context_jobs', 'operation_id', context) == 'running'
    assert status(recovering, 'approvals', 'id', request) == 'executing'
    assert len(recovering.state.store.rows('SELECT 1 FROM broker_work_owners')) == 3


async def test_existing_native_stream_finishes_while_replacement_handles_new_inference(verified, monkeypatch):
    import httpx
    from app.security import digest

    broker, _ = verified
    first = broker()
    started, finish = asyncio.Event(), asyncio.Event()
    provider_calls = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'event: response.created\ndata: {"type":"response.created","response":{"id":"old-stream"}}\n\n'
            started.set()
            await finish.wait()
            yield b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"old-stream","status":"completed","usage":{"input_tokens":2,"output_tokens":1,"total_tokens":3}}}\n\n'

    def upstream(request):
        if request.url.path != '/v1/responses':
            return httpx.Response(200, json={'data': [], 'total_tokens': 10})
        provider_calls.append(request)
        if len(provider_calls) == 1:
            return httpx.Response(200, stream=Stream())
        return httpx.Response(200, json={'id': 'new-call', 'status': 'completed',
            'output': [], 'usage': {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3}})

    actual = httpx.AsyncClient
    monkeypatch.setattr('app.harness_gateway.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(upstream), **kw))

    def request(app, label):
        app.state.settings.litellm_api_base = 'https://synthetic.invalid/v1'
        from app.context_budget import ModelContextLimits
        app.state.settings.model_context_limits = {app.state.settings.resolve_model(): ModelContextLimits(
            context_window=65536, max_input_tokens=65536, max_output_tokens=8192)}
        run = app.state.store.create_run(label, '', 'modal', [])
        app.state.store.update_run(run['id'], status='running', token_hash=digest('cap'))
        return f"/broker/{run['id']}/v1/responses"

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(first.router.lifespan_context(first))
        first_client = await stack.enter_async_context(actual(transport=httpx.ASGITransport(app=first), base_url='http://127.0.0.1:8787'))
        running = asyncio.create_task(first_client.post(request(first, 'old stream'),
            headers={'Authorization': 'Bearer cap'}, json={'input': 'hello', 'stream': True}))
        try:
            try:
                await asyncio.wait_for(started.wait(), 3)
            except TimeoutError:
                if running.done():
                    response = await running
                    pytest.fail(f'Stream did not start: {response.status_code} {response.text}')
                raise
            original = first.state.store.rows('SELECT id FROM model_requests')[0]['id']
            replacement = broker()
            await stack.enter_async_context(replacement.router.lifespan_context(replacement))
            next_client = await stack.enter_async_context(actual(transport=httpx.ASGITransport(app=replacement), base_url='http://127.0.0.1:8787'))
            response = await next_client.post(request(replacement, 'new call'),
                headers={'Authorization': 'Bearer cap'}, json={'input': 'world', 'stream': False})
            assert response.status_code == 200, response.text
            assert response.json()['id'] == 'new-call'
            assert not running.done()
            assert status(replacement, 'model_requests', 'id', original) == 'pending'
            finish.set()
            streamed = await asyncio.wait_for(running, 3)
            assert streamed.status_code == 200 and 'response.completed' in streamed.text
            assert status(replacement, 'model_requests', 'id', original) == 'completed'
            assert len(provider_calls) == 2
            assert replacement.state.model_slots.snapshot()['active'] == 0
        finally:
            finish.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize('verified', [32, 128], indirect=True)
async def test_large_global_limit_does_not_multiply_during_overlap(verified):
    broker, settings = verified
    old, new = broker(), broker()
    slots = [old.state.model_slots, new.state.model_slots]
    permits = []
    for index in range(settings.max_concurrent_model_requests):
        owner = slots[index % 2]
        permits.append((owner, await owner.acquire()))
    assert all(owner.locked() for owner in slots)
    assert all(owner.snapshot()['active'] == settings.max_concurrent_model_requests for owner in slots)
    assert len({owner._connection.info.backend_pid for owner in slots}) == 2
    for owner, permit in permits:
        owner.release(permit)
    assert slots[0].snapshot()['active'] == 0


async def test_cross_process_capacity_and_crash_recovery(verified):
    import json
    import os
    import subprocess
    import sys

    broker, settings = verified
    survivor = broker()
    child_settings = settings.model_copy(update={'moyai_runtime_role': 'broker', 'data_dir': settings.data_dir / 'child'})
    source = '''
import asyncio, json, os
from app.config import Settings
from app.main import create_app
app = create_app(Settings.model_validate_json(os.environ['BROKER_TEST_SETTINGS']))
async def run():
    first = await app.state.model_slots.acquire()
    second = await app.state.model_slots.acquire()
    run = app.state.store.create_run('process crash fixture', '', 'modal', [])
    request = app.state.spend.begin(run, 'synthetic-model')
    print(json.dumps({'request': request}), flush=True)
    await asyncio.Event().wait()
asyncio.run(run())
'''
    child = subprocess.Popen([sys.executable, '-c', source], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env={**os.environ, 'BROKER_TEST_SETTINGS': child_settings.model_dump_json()})
    try:
        line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 5)
        assert line, 'Child broker did not become ready'
        request = json.loads(line)['request']
        assert survivor.state.model_slots.locked()
        assert survivor.state.broker_requests.work.recover() == 0
        assert status(survivor, 'model_requests', 'id', request) == 'pending'
        child.kill()
        await asyncio.to_thread(child.wait, 5)
        async with asyncio.timeout(3):
            while survivor.state.model_slots.locked():
                await asyncio.sleep(.02)
        assert survivor.state.broker_requests.work.recover() == 1
        assert status(survivor, 'model_requests', 'id', request) == 'interrupted'
        permit = await survivor.state.model_slots.acquire()
        survivor.state.model_slots.release(permit)
    finally:
        if child.poll() is None:
            child.kill()
            await asyncio.to_thread(child.wait, 5)
        child.stdout.close()
        child.stderr.close()


async def test_owner_loss_cancels_routes_without_replaying_or_releasing_before_cleanup(verified):
    from app.broker_ownership import BrokerOwnershipMiddleware

    broker, _ = verified
    first, replacement = broker(), broker()
    started, cleaning, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def external_write(scope, receive, send):
        async with first.state.model_slots:
            calls.append('sent')
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await cleanup.wait()

    middleware = BrokerOwnershipMiddleware(external_write, first.state.broker_requests)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(first.router.lifespan_context(first))
        await stack.enter_async_context(replacement.router.lifespan_context(replacement))
        request = asyncio.create_task(middleware({'type': 'http', 'path': '/broker/fixture/tools/call'}, None, None))
        waiting = None
        other = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            other = await replacement.state.model_slots.acquire()
            first.state.store.database.owner.close()
            await asyncio.wait_for(cleaning.wait(), 2)
            assert first.state.broker_requests.failed
            waiting = asyncio.create_task(replacement.state.model_slots.acquire())
            await asyncio.sleep(.1)
            assert not waiting.done()
            assert calls == ['sent']
            cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await request
            permit = await asyncio.wait_for(waiting, 2)
            replacement.state.model_slots.release(permit)
            assert calls == ['sent']
        finally:
            cleanup.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            if waiting and not waiting.done():
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
            if other:
                replacement.state.model_slots.release(other)


def test_model_attempt_cannot_commit_without_its_ownership_record(verified, monkeypatch):
    broker, _ = verified
    first = broker()
    run = first.state.store.create_run('atomic owner', '', 'modal', [])

    def failure(*args):
        raise RuntimeError('synthetic ownership failure')

    monkeypatch.setattr(first.state.store.broker_work, 'claim_in', failure)
    with pytest.raises(RuntimeError, match='synthetic ownership failure'):
        first.state.spend.begin(run, 'synthetic-model')
    assert not first.state.store.rows('SELECT * FROM model_requests')
    assert not first.state.store.rows('SELECT * FROM broker_work_owners')


async def test_slot_wait_keeps_event_loop_responsive_when_runtime_pool_is_full(verified):
    from contextlib import ExitStack

    broker, _ = verified
    first = broker()
    with ExitStack() as occupied:
        for _ in range(first.state.store.database.pool.max_size):
            occupied.enter_context(first.state.store.database.pool.connection())
        waiting = asyncio.create_task(first.state.model_slots.acquire())
        beats = 0
        for _ in range(10):
            await asyncio.sleep(.01)
            beats += 1
        assert beats == 10 and not waiting.done()
    permit = await asyncio.wait_for(waiting, 2)
    await first.state.model_slots.release_async(permit)


async def test_cancelled_admission_drains_and_releases_an_acquired_slot(verified, monkeypatch):
    import threading

    broker, _ = verified
    first = broker()
    slots = first.state.model_slots
    acquired, finish = threading.Event(), threading.Event()
    original = slots._claim

    def delayed(current):
        permit = original(current)
        acquired.set()
        assert finish.wait(3)
        return permit

    monkeypatch.setattr(slots, '_claim', delayed)
    claiming = asyncio.create_task(slots.acquire())
    try:
        assert await asyncio.to_thread(acquired.wait, 2)
        claiming.cancel()
        await asyncio.sleep(.05)
        assert not claiming.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await claiming
        assert slots.snapshot()['active'] == slots.active == 0
        assert not slots._permits
    finally:
        finish.set()
        await asyncio.gather(claiming, return_exceptions=True)


@pytest.mark.parametrize('released_at_server', [False, True])
async def test_unacknowledged_unlock_never_reenters_or_silently_leaks_a_slot(verified, monkeypatch, released_at_server):
    import psycopg
    from app.database import DatabaseError

    broker, _ = verified
    first = broker()
    slots = first.state.model_slots
    permit = await slots.acquire()
    connection = slots._connection

    class Unacknowledged:
        def __getattr__(self, name):
            return getattr(connection, name)

        def execute(self, sql, params=()):
            if 'pg_advisory_unlock' in sql:
                if released_at_server:
                    connection.execute(sql, params)
                raise psycopg.OperationalError('synthetic lost acknowledgement')
            return connection.execute(sql, params)

    monkeypatch.setattr(slots, '_connection', Unacknowledged())
    with pytest.raises(DatabaseError, match='not acknowledged'):
        await slots.release_async(permit)
    assert permit in slots._permits
    with pytest.raises(DatabaseError, match='ownership was lost'):
        await slots.acquire()
    with pytest.raises(DatabaseError, match='ownership was lost'):
        slots.check()


@pytest.mark.parametrize('close_error', [False, True])
async def test_lifespan_drains_live_route_cleanup_even_when_a_component_close_fails(verified, monkeypatch, close_error):
    from app.broker_ownership import BrokerOwnershipMiddleware

    broker, _ = verified
    first, replacement = broker(), broker()
    started, cleaning, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handler(scope, receive, send):
        async with first.state.model_slots:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await cleanup.wait()

    async def fail_close():
        raise RuntimeError('synthetic component close failure')

    context = first.router.lifespan_context(first)
    await context.__aenter__()
    if close_error:
        monkeypatch.setattr(first.state.memory_review, 'close', fail_close)
    route = asyncio.create_task(BrokerOwnershipMiddleware(handler, first.state.broker_requests)(
        {'type': 'http', 'path': '/broker/fixture/v1/responses'}, None, None))
    await asyncio.wait_for(started.wait(), 2)
    other = await replacement.state.model_slots.acquire()
    closing = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(cleaning.wait(), 2)
        assert not closing.done()
        assert await replacement.state.model_slots.is_full()
        assert not first.state.model_slots._closed
        cleanup.set()
        if close_error:
            with pytest.raises(RuntimeError, match='component close failure'):
                await asyncio.wait_for(closing, 2)
        else:
            await asyncio.wait_for(closing, 2)
        assert first.state.model_slots._closed
        assert not await replacement.state.model_slots.is_full()
    finally:
        cleanup.set()
        await asyncio.gather(route, closing, return_exceptions=True)
        replacement.state.model_slots.release(other)
