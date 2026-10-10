"""Replicated API requests against real PostgreSQL, with provider boundaries mocked."""
import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.database import DatabaseError, PostgresConnection
from app.main import create_app
from app.schema_migrations import migrate
from app.temporal_runtime import TemporalRunManager
from test_postgres_runtime import target  # noqa: F401
from test_separate_broker import split_settings  # noqa: F401
from test_session_titles import completion, gateway
from test_slack import signed
from test_spend import sign_in


@pytest.fixture
def cluster(split_settings):
    migrate(split_settings)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify'})
    coordinator = create_app(settings)
    try:
        yield coordinator, settings.model_copy(update={'moyai_runtime_role': 'api'})
    finally:
        coordinator.state.store.close()


def client_for(app):
    return TestClient(app, base_url=app.state.settings.public_url, client=('127.0.0.1', 50000))


@pytest.mark.parametrize('changes', [{'moyai_schema_mode': 'auto'}, {'moyai_separate_broker': False}])
def test_api_requires_verified_schema_and_broker(split_settings, changes):
    values = {**split_settings.model_dump(), 'moyai_runtime_role': 'api', 'moyai_schema_mode': 'verify', **changes}
    with pytest.raises(ValueError, match='API replicas require'):
        Settings(_env_file=None, **values)
    # Constructor fences also protect internal callers that bypass validation.
    with pytest.raises(DatabaseError, match='API replicas require'):
        create_app(split_settings.model_copy(update=values))


@pytest.mark.parametrize('build', ['a' * 40, 'b' * 40])
def test_two_apis_share_auth_requests_and_shutdown_leaves_owner_work_intact(cluster, build):
    coordinator, settings = cluster
    settings = settings.model_copy(update={'google_client_id': 'google-client',
        'google_client_secret': 'google-secret', 'google_admin_emails': 'alice@berri.ai'})
    store = coordinator.state.store
    run = store.create_run('Existing session', '', 'demo', [], chat_enabled=True)
    request = coordinator.state.spend.begin(run, 'fixture-model')
    store.execute("INSERT INTO context_jobs VALUES(?,?,?,'null','running')", (run['id'], 'compaction', '{}'))
    store.execute("INSERT INTO approvals VALUES(?,?,?,?,'executing',?,'')", ('tool', run['id'], 'fixture', '{}', '2026-01-01'))
    store.execute("INSERT INTO slack_events(event_id,run_id,channel,thread_ts,user_id,created_at,reply_status) VALUES(?,?,?,?,'fixture-user','2026-01-01','sending')",
                  ('fixture-event', run['id'], 'fixture-channel', '1.1'))
    store.execute("INSERT INTO slack_outbox(run_id,dedupe_key,kind,text,status,slack_ts,created_at) VALUES(?,'fixture-receipt','answer','fixture','sending','2.2','2026-01-01')", (run['id'],))
    preserved = {name: store.rows('SELECT * FROM ' + name) for name in ('model_requests', 'context_jobs', 'approvals', 'slack_events', 'slack_outbox')}
    broker = create_app(settings.model_copy(update={'moyai_runtime_role': 'broker'}))
    try:
        with client_for(create_app(settings)) as first, client_for(create_app(settings.model_copy(update={'moyai_build_sha': build}))) as second:
            assert first.get('/health').status_code == second.get('/health').status_code == 200
            sign_in(first.app, first)
            second.cookies.update(first.cookies)
            second.headers.update({'Origin': settings.public_url, 'X-CSRF-Token': first.headers['X-CSRF-Token']})
            assert second.get('/api/session').json()['authenticated']
            assert second.get('/api/session').json()['csrf'] == first.headers['X-CSRF-Token']
            created = first.post('/api/runs', json={'prompt': 'Synthetic API request', 'mode': 'demo'})
            assert created.status_code == 201, created.text
            run_id = created.json()['id']
            assert second.get('/api/runs/' + run_id).json()['id'] == run_id
            # Candidate writes must remain readable by the old API for rollback.
            candidate_run = second.post('/api/runs', json={'prompt': 'Candidate API write', 'mode': 'demo'})
            assert candidate_run.status_code == 201, candidate_run.text
            assert first.get('/api/runs/' + candidate_run.json()['id']).json()['prompt'] == 'Candidate API write'
            # The API persisted a wake, but neither API has dispatched a workflow.
            assert store.rows('SELECT run_id FROM durable_sessions WHERE run_id=? AND revision>delivered', (run_id,))
            first.app.state.manager.temporal.start_workflow.assert_not_awaited()
            with pytest.raises(DatabaseError, match='Another coordinator'):
                create_app(settings.model_copy(update={'moyai_runtime_role': 'coordinator'}))
            with pytest.raises(DatabaseError, match='Another inference broker'):
                create_app(settings.model_copy(update={'moyai_runtime_role': 'broker'}))
        assert {name: store.rows('SELECT * FROM ' + name) for name in preserved} == preserved
        assert store.rows('SELECT status FROM model_requests WHERE id=?', (request,))[0]['status'] == 'pending'
    finally:
        broker.state.store.close()


@pytest.mark.parametrize('change', [{'session_secret': 'different'},
                                   {'sandbox_prepared_pool_size': 1}])
def test_api_rejects_incompatible_owner_without_writes(cluster, monkeypatch, change):
    _, settings = cluster
    original = PostgresConnection.execute

    def read_only(conn, sql, params=()):
        assert sql.strip().upper().startswith('SELECT'), sql
        return original(conn, sql, params)

    monkeypatch.setattr(PostgresConnection, 'execute', read_only)
    with pytest.raises(DatabaseError, match='same shared runtime configuration'):
        create_app(settings.model_copy(update=change))
    app = create_app(settings)
    app.state.store.close()


@pytest.mark.parametrize('build', ['a' * 40, 'b' * 40])
def test_api_starts_no_singleton_jobs_and_does_not_own_hooks(cluster, monkeypatch, build):
    coordinator, settings = cluster
    app = create_app(settings.model_copy(update={'moyai_build_sha': build}))
    guarded = [(app.state.slack, 'recover'), (app.state.session_titles, 'start'),
               (app.state.session_lifecycle, 'start'), (app.state.environments, 'start'),
               (app.state.identities, 'start'), (app.state.automations, 'start'),
               (app.state.tracing, 'start'), (app.state.lens_feedback, 'start'),
               (app.state.memory_review, 'start'), (app.state.spend.recovery, 'start')]
    for service, method in guarded:
        monkeypatch.setattr(service, method, Mock(side_effect=AssertionError('API started singleton work')))
    manager = app.state.manager
    for service, method in [(manager, 'dispatch'), (manager, 'listen_dispatch'), (manager.prepared, 'serve')]:
        monkeypatch.setattr(service, method, AsyncMock(side_effect=AssertionError('API consumed work')))
    with client_for(app) as client:
        for path in ('/hooks', '/hooks/slack/events', '/hooks/slack/interactions', '/hooks/automations/test', '/broker/test/v1/models'):
            assert client.post(path, json={}).status_code == 503
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/hooks/slack/events'):
                pytest.fail('API accepted event socket')
        assert manager.worker is manager.prepared_task is manager.dispatch_listener_task is None
        manager.dispatch.assert_not_awaited()
        manager.listen_dispatch.assert_not_awaited()
        manager.prepared.serve.assert_not_awaited()
        for service, method in guarded:
            getattr(service, method).assert_not_called()
    # Hooks still pass through the coordinator's signature verification.
    coordinator.state.settings.slack_signing_secret = 'slack-test-signing-secret'
    with client_for(coordinator) as client:
        challenge = {'type': 'url_verification', 'challenge': 'synthetic-challenge'}
        assert client.post('/hooks/slack/events', **signed(challenge)).json() == {'challenge': 'synthetic-challenge'}
        assert client.post('/hooks/slack/events', json=challenge).status_code == 401


def test_api_unready_start_releases_owner_and_health_fails_on_owner_loss(cluster, monkeypatch):
    _, settings = cluster
    original = TemporalRunManager.connect_temporal

    async def unavailable(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(TemporalRunManager, 'connect_temporal', unavailable)
    failed = create_app(settings.model_copy(update={'temporal_startup_timeout_seconds': 1}))
    with pytest.raises(RuntimeError, match='startup deadline'):
        with client_for(failed):
            pytest.fail('API became ready without Temporal')
    assert failed.state.store.database.owner.closed
    assert failed.state.manager.closing
    monkeypatch.setattr(TemporalRunManager, 'connect_temporal', original)
    with client_for(create_app(settings)) as client:
        assert client.get('/health').status_code == 200
        client.app.state.manager.ready.clear()
        assert client.get('/health').status_code == 503
        client.app.state.manager.ready.set()
        client.app.state.store.database.owner.close()
        assert client.get('/health').status_code == 503


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.02)


async def test_title_intent_survives_api_exit_and_only_coordinator_generates(cluster, monkeypatch):
    coordinator, settings = cluster
    for options in (settings, coordinator.state.settings):
        options.session_titles_enabled = True
        options.litellm_api_base = 'https://gateway.example/v1'
        options.litellm_api_key = 'fixture-key'
        options.session_title_backfill_limit = 0
    requests = []
    gateway(monkeypatch, lambda request: requests.append(request) or completion())
    api = create_app(settings)
    async with api.router.lifespan_context(api):
        run = api.state.store.create_run('Synthetic title', '', 'demo', [], chat_enabled=True)
        await api.state.session_titles.request(run['id'])
        await api.state.session_titles.request(run['id'])
        assert not requests and not api.state.session_titles.workers
    titles = coordinator.state.session_titles
    store = coordinator.state.store
    assert len(store.rows('SELECT * FROM session_title_requests')) == 1
    titles.start()
    try:
        await eventually(lambda: bool(store.run(run['id'])['display_title']))
        await eventually(lambda: not store.rows('SELECT * FROM session_title_requests'))
        # Also pick up requests arriving after the coordinator has started.
        second_api = create_app(settings)
        async with second_api.router.lifespan_context(second_api):
            second = store.create_run('Another title', '', 'demo', [], chat_enabled=True)
            await second_api.state.session_titles.request(second['id'])
        await eventually(lambda: bool(store.run(second['id'])['display_title']))
    finally:
        await titles.close()
    titles.start()
    await titles.queue.join()
    await titles.close()
    assert len(requests) == 2


async def test_deletion_intent_survives_api_exit_and_coordinator_restart(cluster, monkeypatch):
    coordinator, settings = cluster
    store = coordinator.state.store
    run = store.create_run('Delete fixture', '', 'demo', [], chat_enabled=True, user_id='google:alice')
    api = create_app(settings)
    cleanup = AsyncMock()
    monkeypatch.setattr(api.state.manager, 'stop_for_deletion', cleanup)
    with client_for(api) as client:
        sign_in(api, client)
        response = client.delete('/api/runs/' + run['id'])
        assert response.status_code == 202, response.text
        assert not api.state.session_lifecycle.deletions
    cleanup.assert_not_awaited()
    assert store.run(run['id'])['deletion_requested_at']
    lifecycle = coordinator.state.session_lifecycle
    # First coordinator exits while cleanup is still waiting. The next one retries.
    entered = asyncio.Event()

    async def waiting(run_id):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(coordinator.state.manager, 'stop_for_deletion', waiting)
    lifecycle.start()
    await asyncio.wait_for(entered.wait(), 3)
    await lifecycle.close()
    assert not store.run(run['id'])['deleted_at']

    async def stopped(run_id):
        store.update_run(run_id, status='cancelled')

    monkeypatch.setattr(coordinator.state.manager, 'stop_for_deletion', stopped)
    monkeypatch.setattr(coordinator.state.manager, 'cleanup', AsyncMock())
    lifecycle.start()
    try:
        await eventually(lambda: bool(store.run(run['id'])['deleted_at']))
        coordinator.state.manager.cleanup.assert_awaited_once()
    finally:
        await lifecycle.close()


async def test_coordinator_intent_scans_yield_under_pool_contention(split_settings, monkeypatch):
    migrate(split_settings)
    settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify', 'moyai_database_pool_size': 1})
    app = create_app(settings)
    store = app.state.store
    queried = threading.Event()
    original = store.rows

    def rows(sql, *args, **kwargs):
        queried.set()
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(store, 'rows', rows)
    tasks = []
    try:
        with store.database.pool.connection():
            started = time.monotonic()
            tasks = [asyncio.create_task(app.state.session_titles.watch_pending()),
                     asyncio.create_task(app.state.session_lifecycle.watch())]
            assert await asyncio.to_thread(queried.wait, 1)
            # A health/webhook coroutine must still run while both scans await
            # the only pooled connection. A synchronous scan stalls here.
            await asyncio.wait_for(asyncio.sleep(.03), .5)
            assert time.monotonic() - started < 1
            assert not any(task.done() for task in tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()
