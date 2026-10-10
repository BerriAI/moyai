"""Real PostgreSQL fences and application lifecycles; no provider calls."""
from contextlib import ExitStack
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
import httpx
import uvicorn
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.database import DatabaseError
from app.main import create_app
from app.security import digest
from app.temporal_runtime import TemporalRunManager
from test_postgres_runtime import target  # noqa: F401
from test_cloudflare_access import ACCESS, access_key, assertion  # noqa: F401


@pytest.fixture
def split_settings(tmp_path, target, monkeypatch):
    url, schema = target
    # Exercise the actual recovery/serve logic, replacing only the remote
    # Temporal service. No workflows, sandboxes or provider calls are created.
    monkeypatch.setattr(TemporalRunManager, 'connect_temporal', AsyncMock(
        return_value=SimpleNamespace(start_workflow=AsyncMock())))
    monkeypatch.setattr(TemporalRunManager, 'make_worker',
                        lambda *args: pytest.fail('API/broker consumed execution work'))
    return Settings(_env_file=None, data_dir=tmp_path / 'api',
        moyai_database_url=url, moyai_database_schema=schema, moyai_database_initialize=True,
        moyai_runtime_role='coordinator', moyai_separate_broker=True,
        moyai_build_sha='a' * 40, temporal_enabled=True, temporal_tls=False,
        object_storage_bucket='synthetic-broker-test', session_secret='synthetic-session-key',
        encryption_key=Fernet.generate_key().decode(), session_titles_enabled=False,
        memory_review_enabled=False, litellm_spend_recovery_enabled=False)


def test_broker_configuration_requires_explicit_cluster_split():
    with pytest.raises(ValueError, match='coordinator and worker'):
        Settings(_env_file=None, moyai_separate_broker=True)
    with pytest.raises(ValueError, match='MOYAI_SEPARATE_BROKER'):
        Settings(_env_file=None, moyai_runtime_role='broker', temporal_enabled=True,
                 moyai_database_url='postgresql://localhost/test', object_storage_bucket='test',
                 encryption_key='test', session_secret='test')


def test_api_restart_preserves_broker_requests_compaction_and_tools(split_settings):
    api = create_app(split_settings)
    broker_settings = split_settings.model_copy(update={'moyai_runtime_role': 'broker',
                                                        'data_dir': split_settings.data_dir / 'broker'})
    with ExitStack() as broker_lifetime:
        with TestClient(api, base_url=split_settings.public_url, client=('127.0.0.1', 50000)) as api_client:
            broker = create_app(broker_settings)
            broker_client = TestClient(broker, base_url=split_settings.public_url, client=('127.0.0.1', 50000))
            broker_client = broker_lifetime.enter_context(broker_client)
            store = broker.state.store
            run = store.create_run('Synthetic live inference', '', 'modal', [], chat_enabled=True)
            broker.state.manager.submit(run)
            store.update_run(run['id'], status='running', token_hash=digest('synthetic-capability'))
            request_id = broker.state.spend.begin(store.run(run['id']), 'synthetic-model')
            store.execute("INSERT INTO context_jobs VALUES(?,?,?,'null','running')", (run['id'], 'compaction', '{}'))
            store.execute("INSERT INTO approvals VALUES(?,?,?,?,'executing',?,'')",
                          ('tool-write', run['id'], 'synthetic', '{}', '2026-01-01'))
            headers = {'Authorization': 'Bearer synthetic-capability'}
            route = f"/broker/{run['id']}/v1/models"
            assert broker_client.get(route).status_code == 401
            assert broker_client.get(route, headers=headers).status_code == 200
            assert api_client.get(route, headers=headers).status_code == 503
            assert broker_client.get('/api/session').status_code == 404
            assert broker_client.post('/hooks/slack/events', json={}).status_code == 404
        # Keep the broker's actual lifespan running while replacing the API.
        replacement = create_app(split_settings.model_copy(update={'data_dir': split_settings.data_dir / 'replacement'}))
        with TestClient(replacement, base_url=split_settings.public_url, client=('127.0.0.1', 50000)) as client:
            assert client.get('/health').status_code == 200
            assert store.rows('SELECT status FROM model_requests WHERE id=?', (request_id,))[0]['status'] == 'pending'
            assert store.rows('SELECT status FROM context_jobs')[0]['status'] == 'running'
            assert store.rows('SELECT status FROM approvals')[0]['status'] == 'executing'
            assert broker_client.get(route, headers=headers).status_code == 200
            assert broker.state.manager.worker is None
            assert broker.state.manager.temporal is not None
    # Broker replacement owns recovery of requests that really lost their owner.
    coordinator = create_app(split_settings)
    try:
        with TestClient(create_app(broker_settings), base_url=split_settings.public_url, client=('127.0.0.1', 50000)) as restarted:
            rows = restarted.app.state.store.rows('SELECT status FROM model_requests WHERE id=?', (request_id,))
            assert rows[0]['status'] == 'interrupted'
            assert restarted.app.state.store.rows('SELECT status FROM context_jobs')[0]['status'] == 'interrupted'
            assert restarted.app.state.store.rows('SELECT status FROM approvals')[0]['status'] == 'uncertain'
    finally:
        coordinator.state.store.close()


def test_broker_is_singleton_and_requires_matching_build_and_topology(split_settings):
    api = create_app(split_settings)
    broker_settings = split_settings.model_copy(update={'moyai_runtime_role': 'broker'})
    broker = create_app(broker_settings)
    try:
        with pytest.raises(DatabaseError, match='Another inference broker'):
            create_app(broker_settings)
        with pytest.raises(DatabaseError, match='same shared runtime configuration'):
            create_app(split_settings.model_copy(update={'moyai_runtime_role': 'worker', 'moyai_separate_broker': False}))
    finally:
        broker.state.store.close()
    try:
        with pytest.raises(DatabaseError, match='same shared runtime configuration'):
            create_app(broker_settings.model_copy(update={'moyai_build_sha': 'b' * 40}))
    finally:
        api.state.store.close()


def test_only_broker_runs_inference_recovery_and_background_reviews(split_settings, monkeypatch):
    from app.memory_review import MemoryReview
    from app.skill_learning import SkillLearning
    from app.spend_recovery import SpendRecovery
    from app.context_maintenance import ContextMaintenance
    starts = []
    monkeypatch.setattr(SkillLearning, 'start', lambda self: starts.append(('skills', self.settings.moyai_runtime_role)))
    monkeypatch.setattr(MemoryReview, 'start', lambda self: starts.append(('memory', self.settings.moyai_runtime_role)))
    monkeypatch.setattr(SpendRecovery, 'start', lambda self: starts.append(('spend', self.spend.settings.moyai_runtime_role)))
    monkeypatch.setattr(ContextMaintenance, 'recover', lambda self: starts.append(('context', self.gateway.settings.moyai_runtime_role)))
    with TestClient(create_app(split_settings), base_url=split_settings.public_url, client=('127.0.0.1', 50000)):
        assert starts == []
        with TestClient(create_app(split_settings.model_copy(update={'moyai_runtime_role': 'broker'})),
                        base_url=split_settings.public_url, client=('127.0.0.1', 50000)):
            assert sorted(starts) == [('context', 'broker'), ('memory', 'broker'), ('skills', 'broker'), ('spend', 'broker')]


async def test_broker_serves_no_temporal_work_or_wake_dispatch(split_settings, monkeypatch):
    split_settings = split_settings.model_copy(update={'sandbox_prepared_pool_size': 1})
    api = create_app(split_settings)
    broker = create_app(split_settings.model_copy(update={'moyai_runtime_role': 'broker'}))
    manager = broker.state.manager
    monkeypatch.setattr(manager, 'dispatch', AsyncMock(side_effect=AssertionError('Broker dispatched wakes')))
    monkeypatch.setattr(manager, 'listen_dispatch', AsyncMock(side_effect=AssertionError('Broker subscribed to wakes')))
    monkeypatch.setattr(manager.prepared, 'serve', AsyncMock(side_effect=AssertionError('Broker prepared sandboxes')))
    try:
        await manager.recover()
        await asyncio.wait_for(manager.ready.wait(), 3)
        assert manager.worker is None
        manager.dispatch.assert_not_awaited()
        manager.listen_dispatch.assert_not_awaited()
        manager.prepared.serve.assert_not_awaited()
        assert manager.dispatch_listener_task is manager.prepared_task is None
    finally:
        await manager.shutdown()
        broker.state.store.close()
        api.state.store.close()


@pytest.mark.parametrize('role', ['worker', 'broker'])
@pytest.mark.parametrize('field,value', [('sandbox_prepared_pool_size', 1), ('sandbox_prepared_idle_seconds', 600)])
def test_split_runtime_rejects_prepared_pool_configuration_drift(split_settings, role, field, value):
    api = create_app(split_settings)
    try:
        with pytest.raises(DatabaseError, match='same shared runtime configuration'):
            create_app(split_settings.model_copy(update={'moyai_runtime_role': role, field: value}))
    finally:
        api.state.store.close()


def test_split_broker_preserves_machine_audience_and_run_capability(split_settings, monkeypatch, access_key):
    settings = split_settings.model_copy(update={**ACCESS, 'public_url': 'https://workspace.example',
                                                 'workspace_password': 'synthetic-password'})
    actual = httpx.AsyncClient
    monkeypatch.setattr('app.cloudflare_access.httpx.AsyncClient', lambda **kw: actual(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'keys': [access_key[1]]})), **kw))
    with TestClient(create_app(settings), base_url=settings.public_url) as api:
        broker = create_app(settings.model_copy(update={'moyai_runtime_role': 'broker'}))
        with TestClient(broker, base_url=settings.public_url) as client:
            run = broker.state.store.create_run('Synthetic access check', '', 'modal', [])
            broker.state.store.update_run(run['id'], status='running', token_hash=digest('synthetic-capability'))
            route = f"/broker/{run['id']}/v1/models"
            machine = {'Cf-Access-Jwt-Assertion': assertion(access_key[0], 'broker-audience', sub='', common_name='test-service')}
            employee = {'Cf-Access-Jwt-Assertion': assertion(access_key[0])}
            capability = {'Authorization': 'Bearer synthetic-capability'}
            assert client.get(route, headers=capability).status_code == 401
            assert client.get(route, headers=employee | capability).status_code == 401
            assert client.get(route, headers=machine).status_code == 401
            assert client.get(route, headers=machine | capability).status_code == 200
            assert api.get(route, headers=machine | capability).status_code == 503
            assert client.get('/api/session', headers=employee).status_code == 404
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(route, headers=machine | capability):
                    pytest.fail('Broker accepted a WebSocket')
            broker.state.store.update_run(run['id'], status='completed')
            assert client.get(route, headers=machine | capability).status_code == 401


def test_broker_health_fails_when_database_ownership_is_lost(split_settings):
    with TestClient(create_app(split_settings), base_url=split_settings.public_url, client=('127.0.0.1', 50000)):
        broker = create_app(split_settings.model_copy(update={'moyai_runtime_role': 'broker'}))
        with TestClient(broker, base_url=split_settings.public_url, client=('127.0.0.1', 50000)) as client:
            client.portal.call(asyncio.wait_for, broker.state.manager.ready.wait(), 3)
            assert client.get('/health').status_code == 200
            broker.state.store.database.owner.close()
            assert client.get('/health').status_code == 503


@pytest.mark.parametrize('role', ['broker', 'api'])
async def test_role_does_not_accept_tcp_until_temporal_is_ready(split_settings, monkeypatch, role):
    if role == 'api':
        from app.schema_migrations import migrate
        migrate(split_settings)
        split_settings = split_settings.model_copy(update={'moyai_schema_mode': 'verify'})
    connecting, connected = asyncio.Event(), asyncio.Event()

    async def delayed(self):
        connecting.set()
        await connected.wait()
        return SimpleNamespace(start_workflow=AsyncMock())

    monkeypatch.setattr(TemporalRunManager, 'connect_temporal', delayed)
    coordinator = create_app(split_settings)
    broker = create_app(split_settings.model_copy(update={'moyai_runtime_role': role}))
    with socket.socket() as available:
        available.bind(('127.0.0.1', 0))
        port = available.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(broker, host='127.0.0.1', port=port, log_level='error'))
    serving = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(connecting.wait(), 3)
        assert not server.started
        with pytest.raises(OSError):
            await asyncio.open_connection('127.0.0.1', port)
        connected.set()
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}') as client:
            assert (await client.get('/health')).status_code == 200
            broker.state.manager.ready.clear()
            assert (await client.get('/health')).status_code == 503
    finally:
        connected.set()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        coordinator.state.store.close()


def test_broker_startup_timeout_releases_ownership(split_settings, monkeypatch):
    async def unavailable(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(TemporalRunManager, 'connect_temporal', unavailable)
    coordinator = create_app(split_settings)
    options = split_settings.model_copy(update={'moyai_runtime_role': 'broker',
                                                'temporal_startup_timeout_seconds': 1})
    broker = create_app(options)
    try:
        with pytest.raises(RuntimeError, match='startup deadline'):
            with TestClient(broker):
                pytest.fail('An unready broker completed startup')
        assert broker.state.manager.closing
        # The failed process must release the broker fence for the next attempt.
        replacement = create_app(options)
        replacement.state.store.close()
    finally:
        coordinator.state.store.close()


def test_split_capacity_does_not_report_local_zeros_as_broker_usage(split_settings):
    from test_spend import sign_in
    api = create_app(split_settings)
    with TestClient(api, base_url=split_settings.public_url, client=('127.0.0.1', 50000)) as client:
        sign_in(api, client)
        model = client.get('/api/admin/capacity').json()['model']
        assert model.pop('source') == 'separate_broker'
        assert model.pop('capacity') == split_settings.max_concurrent_model_requests
        assert model and all(value is None for value in model.values())
