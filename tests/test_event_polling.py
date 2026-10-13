"""Stream polling remains fresh and replayable with bounded database reads."""
import asyncio
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

from fastapi import HTTPException
import pytest

from app.config import Settings
from app.db import now
from app.main import create_app
from scripts.event_polling_probe import Reads
from test_background_database_latency import responsive_pool


@pytest.fixture(params=[False, True], ids=['local', 'durable'])
def stream_app(tmp_path, request):
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787',
        temporal_enabled=request.param, bundled_skills_enabled=False, repository_context_enabled=False,
        moyai_database_pool_size=1))
    yield app
    app.state.store.close()


def request_for(app, *, headers=None, authenticated=True):
    security = app.state.session_lifecycle.security
    cookie = security.signer.dumps({'sid': 'event-poll-test', 'role': 'admin', 'method': 'local'})

    async def connected():
        return False

    return SimpleNamespace(headers=headers or {}, cookies={'workspace_session': cookie} if authenticated else {},
                           is_disconnected=connected)


async def open_stream(app, run_id, *, after=None, **kwargs):
    endpoint = next(route.endpoint for route in app.routes
                    if getattr(route, 'path', '') == '/api/runs/{run_id}/events')
    if after is None:
        after = app.state.store.rows('SELECT COALESCE(MAX(id),0) AS cursor FROM events WHERE run_id=?', (run_id,))[0]['cursor']
    response = await endpoint(run_id, request_for(app, **kwargs), after)
    return response.body_iterator


def data(record):
    return json.loads(record.split('data: ', 1)[1])


def idle_run(app, *, chat=True):
    store = app.state.store
    run = store.create_run('Saved conversation', '', 'demo', [], chat_enabled=chat)
    store.update_run(run['id'], status='idle' if chat else 'completed')
    return run['id']


async def test_idle_stream_bounds_reads_without_nesting_pool_connections(stream_app, monkeypatch):
    app, store = stream_app, stream_app.state.store
    run_id = idle_run(app)
    stream = await open_stream(app, run_id)
    reads = Reads(store)
    depth = threading.local()

    @contextmanager
    def bounded(**kwargs):
        assert not getattr(depth, 'active', False), 'Polling borrowed a nested pool connection'
        depth.active = True
        try:
            with reads.measured(**kwargs) as connection:
                yield connection
        finally:
            depth.active = False

    monkeypatch.setattr(store, 'connect', bounded)
    reads.active = True
    try:
        status = data(await anext(stream))
        assert status['status'] == 'idle' and status['active'] is False
        assert reads.checkouts == 2
        assert len(reads.statements) == 3
        assert not any('swarm_missions' in sql for sql in reads.statements)
    finally:
        await stream.aclose()


@pytest.mark.parametrize('chat', [False, True])
async def test_stream_drains_multiple_batches_and_reconnects_at_latest_cursor(stream_app, chat):
    app, store = stream_app, stream_app.state.store
    run_id = idle_run(app, chat=chat)
    with store.connect() as connection:
        ids = [connection.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?) RETURNING id',
            (run_id, 'result' if index == 404 else 'tool', f'event-{index}', '{}', now())).fetchone()[0]
            for index in range(405)]
    # Last-Event-ID takes precedence over the URL's older cursor.
    stream = await open_stream(app, run_id, after=ids[0], headers={'last-event-id': str(ids[1])})
    received = []
    try:
        while True:
            record = await anext(stream)
            if record.startswith('id: '):
                received.append(data(record))
            elif 'event: settled' in record or len(received) == 403:
                break
        assert [event['id'] for event in received] == ids[2:]
        assert received[-1]['kind'] == 'result' and received[-1]['message'] == 'event-404'
        if not chat:
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
    finally:
        await stream.aclose()


async def test_poll_snapshot_does_not_mix_status_and_events_from_later_commit(stream_app, monkeypatch):
    app, store = stream_app, stream_app.state.store
    run_id = idle_run(app)
    # A second pool models an independently running writer. Initialize before
    # opening the snapshot; schema maintenance is not part of the race.
    from app.db import Store
    other = Store(app.state.settings.data_dir, database_url=app.state.settings.moyai_database_url,
                  database_schema=store.database.schema if store.database else 'public',
                  database_initialize=False, database_pool_size=1)
    stream = await open_stream(app, run_id)
    started, release = threading.Event(), threading.Event()
    original = store.events

    def delayed(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, 'events', delayed)
    polling = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        # The pool-size-one fixture holds its only slot for the snapshot, so
        # use an independent Store (a second worker) to commit concurrently.
        with other.connect() as connection:
            connection.begin_write()
            connection.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            connection.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                               (run_id, 'tool', 'Another worker committed this event', '{}', now()))
        release.set()
        first = data(await polling)
        assert first['status'] == 'idle'
        event = data(await anext(stream))
        assert event['message'] == 'Another worker committed this event'
        assert data(await anext(stream))['status'] == 'running'
    finally:
        release.set()
        await polling
        await stream.aclose()
        other.close()


async def test_stream_preserves_saving_deletion_and_child_root_errors(stream_app):
    app, store = stream_app, stream_app.state.store
    root, child = idle_run(app), idle_run(app)
    store.execute('UPDATE runs SET parent_run_id=? WHERE id=?', (root, child))
    store.execute("UPDATE runs SET status='running',active_message_id=42,pending_result=? WHERE id=?",
                  (json.dumps({'message_id': 42, 'completed': True, 'message': 'Answer saved'}), child))
    app.state.session_lifecycle.deletion_errors[root] = 'Cleanup will retry.'
    stream = await open_stream(app, child)
    try:
        status = data(await anext(stream))
        assert status['status'] == 'saving' and status['deletion_error'] == 'Cleanup will retry.'
        store.execute('UPDATE runs SET deletion_requested_at=? WHERE id=?', (now(), child))
        assert data(await anext(stream))['status'] == 'deleting'
        store.execute('UPDATE runs SET deleted_at=? WHERE id=?', (now(), child))
        assert await anext(stream) == 'event: deleted\ndata: {}\n\n'
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()


async def test_local_activity_uses_live_jobs_while_durable_activity_uses_snapshot(stream_app):
    app = stream_app
    run_id = idle_run(app)
    app.state.manager.jobs[run_id] = object()
    stream = await open_stream(app, run_id)
    try:
        status = data(await anext(stream))
        assert status['active'] is (not app.state.settings.temporal_enabled)
        app.state.manager.jobs.pop(run_id)
        assert data(await anext(stream))['active'] is False
    finally:
        app.state.manager.jobs.pop(run_id, None)
        await stream.aclose()


async def test_stream_revalidates_slack_mirroring_without_holding_snapshot_slot(stream_app):
    app, store = stream_app, stream_app.state.store
    settings = app.state.settings
    settings.slack_bot_enabled = settings.slack_thread_chat_enabled = True
    settings.slack_signing_secret, settings.slack_session_users = 'test-secret', '*'
    connectors = app.state.slack.connectors
    connectors.save('slack', {'bot': {'access_token': 'synthetic', 'team': {'id': 'T1'}}}, 'Test Slack')
    run_id = idle_run(app)
    store.execute('INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts) VALUES(?,?,?,?,?)',
                  ('T1', 'C1', '1234.0', run_id, '1234.0'))
    stream = await open_stream(app, run_id)
    try:
        assert data(await anext(stream))['slack_mirroring'] == 'active'
        store.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
        assert data(await anext(stream))['slack_mirroring'] == 'paused'
        store.execute("INSERT INTO connection_policies(provider,enabled,read_only) VALUES('slack',0,0) "
                      'ON CONFLICT(provider) DO UPDATE SET enabled=0')
        assert data(await anext(stream))['slack_mirroring'] is None
        store.execute("UPDATE connection_policies SET enabled=1 WHERE provider='slack'")
        connectors.save('slack', {'bot': {'access_token': 'synthetic', 'team': {'id': 'T2'}}}, 'Changed team')
        assert data(await anext(stream))['slack_mirroring'] is None
    finally:
        await stream.aclose()


async def test_stream_authentication_is_off_loop_and_still_required(stream_app, monkeypatch):
    app = stream_app
    run_id = idle_run(app)
    security = app.state.session_lifecycle.security
    original = security.require
    loop_thread = threading.get_ident()
    calls = []

    def checked(*args, **kwargs):
        calls.append(threading.get_ident())
        assert calls[-1] != loop_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(security, 'require', checked)
    with pytest.raises(HTTPException) as error:
        await open_stream(app, run_id, authenticated=False)
    assert error.value.status_code == 401
    with pytest.raises(HTTPException) as error:
        await open_stream(app, 'missing')
    assert error.value.status_code == 404
    stream = await open_stream(app, run_id)
    await stream.aclose()
    assert len(calls) == 3


async def test_pool_waiting_stream_keeps_event_loop_responsive(stream_app, record_property):
    app, store = stream_app, stream_app.state.store
    if not store.database:
        pytest.skip('Pool exhaustion requires PostgreSQL')
    assert store.database.pool.max_size == 1
    run_id = idle_run(app)
    stream = await open_stream(app, run_id)
    store.database.pool.timeout = 2
    polling = None
    try:
        async with responsive_pool(store, record_property) as waiting:
            # The holder owns the checkout outside the polling task's context.
            await asyncio.to_thread(waiting.hold)
            polling = asyncio.create_task(anext(stream))
            await asyncio.wait_for(asyncio.sleep(.05), .5)
            assert not polling.done()
            assert data(await asyncio.wait_for(polling, 1))['status'] == 'idle'
    finally:
        if polling is not None:
            await asyncio.gather(polling, return_exceptions=True)
        await stream.aclose()
