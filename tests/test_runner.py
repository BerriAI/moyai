import asyncio
import io
import json
from types import SimpleNamespace
import zipfile

import pytest

from app.config import Settings
from app.db import Store
from app.runner import RunManager, refresh_sandbox_files


async def test_repeated_workspace_checks_retain_one_real_modal_client(tmp_path, monkeypatch):
    import gc
    from modal._utils import async_utils, grpc_utils
    from modal.client import _Client
    from app.temporal_runtime import TemporalRunManager

    async def channel(server_url, metadata):
        # Real SDK clients, TLS channels, stubs and shutdown hooks; no cloud I/O.
        return grpc_utils.create_channel('https://localhost:443', metadata)

    monkeypatch.setattr(grpc_utils, 'create_channel_with_fallbacks', channel)
    settings = Settings(_env_file=None, data_dir=tmp_path, modal_token_id='test-id', modal_token_secret='test-secret')
    manager = TemporalRunManager(Store(tmp_path), settings)
    before = len(async_utils._shutdown_tasks)
    retained = sum(type(item) is _Client for item in gc.get_objects())
    clients = await asyncio.gather(*(manager.provider(name='modal').client() for _ in range(100)))
    assert len({id(client) for client in clients}) == 1
    assert len(async_utils._shutdown_tasks) - before == 1
    del clients
    gc.collect()
    assert sum(type(item) is _Client for item in gc.get_objects()) - retained == 1
    client = await manager.client()
    await manager.modal_clients.close()
    assert client.is_closed()


async def test_modal_client_initialization_survives_cancelled_waiter_and_rotation(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, call
    import modal
    from app.modal_clients import ModalClients

    entered, release = asyncio.Event(), asyncio.Event()
    original, replacement = (SimpleNamespace(__aexit__=AsyncMock()) for _ in range(2))

    async def create(token_id, secret):
        entered.set()
        await release.wait()
        return original if secret == 'first' else replacement

    factory = AsyncMock(side_effect=create)
    monkeypatch.setattr(modal.Client, 'from_credentials', aio(factory))
    owner = ModalClients()
    settings = Settings(_env_file=None, data_dir=tmp_path, modal_token_id='id', modal_token_secret='first')
    cancelled = asyncio.create_task(owner.get(settings))
    await entered.wait()
    survivor = asyncio.create_task(owner.get(settings))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    assert await survivor is original
    settings.modal_token_secret = 'second'
    assert await owner.get(settings) is replacement
    assert factory.await_args_list == [call('id', 'first'), call('id', 'second')]
    original.__aexit__.assert_not_awaited()
    await owner.close()
    original.__aexit__.assert_awaited_once()
    replacement.__aexit__.assert_awaited_once()
    with pytest.raises(RuntimeError, match='shutting down'):
        await owner.get(settings)


async def test_failed_modal_initialization_can_retry_and_shutdown_drains_pending(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    import modal
    from app.modal_clients import ModalClients

    entered, release = asyncio.Event(), asyncio.Event()
    client = SimpleNamespace(__aexit__=AsyncMock())

    async def create(*credentials):
        entered.set()
        await release.wait()
        return client

    factory = AsyncMock(side_effect=RuntimeError('connection failed'))
    monkeypatch.setattr(modal.Client, 'from_credentials', aio(factory))
    owner = ModalClients()
    settings = Settings(_env_file=None, data_dir=tmp_path)
    with pytest.raises(RuntimeError, match='connection failed'):
        await owner.get(settings)
    factory.side_effect = create
    lookup = asyncio.create_task(owner.get(settings))
    await entered.wait()
    closing = asyncio.create_task(owner.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    assert await lookup is client
    await closing
    assert factory.await_count == 2
    client.__aexit__.assert_awaited_once()


@pytest.mark.parametrize('temporal', [False, True])
def test_app_shares_modal_clients_and_closes_after_consumers_stop(tmp_path, monkeypatch, temporal):
    from datetime import date
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    import modal
    from app.main import create_app
    from app import billing_sources
    from app.temporal_runtime import TemporalRunManager

    # Keep external Temporal services out of this application-lifecycle test.
    monkeypatch.setattr(TemporalRunManager, 'recover', AsyncMock())
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url='http://127.0.0.1:8787',
                        temporal_enabled=temporal, modal_token_id='test-id', modal_token_secret='test-secret')
    app = create_app(settings)

    async def close(*args):
        assert app.state.manager.closing

    connection = SimpleNamespace(__aexit__=AsyncMock(side_effect=close))
    factory = AsyncMock(return_value=connection)
    lookup = AsyncMock(return_value=SimpleNamespace(app_id='ap-test'))
    monkeypatch.setattr(modal.Client, 'from_credentials', aio(factory))
    monkeypatch.setattr(modal.App, 'lookup', aio(lookup))
    monkeypatch.setattr(modal.Workspace, 'from_context', lambda **kw:
        SimpleNamespace(billing=SimpleNamespace(report=aio(AsyncMock(return_value=[])))))
    with TestClient(app, base_url=settings.public_url, client=('127.0.0.1', 50000)) as http:
        session = http.get('/api/session').json()
        response = http.put('/api/settings/sandboxes', json={'provider': 'modal', 'revision': 0, 'values': {}},
                            headers={'Origin': settings.public_url, 'X-CSRF-Token': session['csrf']})
        assert response.status_code == 200

        async def use_connections():
            assert await app.state.manager.client() is connection
            await billing_sources.modal_costs(settings, date(2026, 10, 1), date(2026, 10, 2),
                                             clients=app.state.spend.infrastructure.modal_clients)

        http.portal.call(use_connections)
        factory.assert_awaited_once_with('test-id', 'test-secret')
        assert all(call.kwargs['client'] is connection for call in lookup.await_args_list)
        connection.__aexit__.assert_not_awaited()
    connection.__aexit__.assert_awaited_once()


def aio(function):
    return SimpleNamespace(aio=function)


async def test_snapshot_refresh_includes_runtime_fixes_without_touching_user_files():
    written = {}
    async def write(text, path):
        written[path] = text
    await refresh_sandbox_files(SimpleNamespace(filesystem=SimpleNamespace(write_text=aio(write))))
    assert '/opt/workspace-runner/hermes_compat.py' in written
    assert '/opt/workspace-runner/hermes-steering.patch' in written
    assert '/opt/workspace-runner/hermes-stop-reason.patch' in written
    assert 'apply_hermes_patches()' in written['/opt/workspace-runner/agent.py']
    assert all(path.startswith('/opt/workspace-runner/') for path in written)


class Lines:
    def __init__(self, lines):
        self.lines = lines

    async def __aiter__(self):
        for line in self.lines:
            yield line


class FakeSandbox:
    object_id = "sb-test-only"

    def __init__(self, completed=True):
        self.terminated = False
        self.spec = None
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("result.md", "Test result")
        self.archive = buffer.getvalue()
        self.completed = completed
        self.filesystem = SimpleNamespace(write_text=aio(self.write), stat=aio(self.stat), read_bytes=aio(self.read))
        self.terminate = aio(self.terminate_sandbox)
        self.wait = aio(self.wait_sandbox)
        self.exec = aio(self.execute)

    async def write(self, text, path):
        assert path == "/tmp/task.json"
        self.spec = json.loads(text)

    async def stat(self, path):
        return SimpleNamespace(size=len(self.archive))

    async def read(self, path):
        return self.archive

    async def terminate_sandbox(self):
        self.terminated = True

    async def wait_sandbox(self, *, raise_on_termination=True):
        assert self.terminated
        assert raise_on_termination is False
        return 0

    async def execute(self, *command, timeout=None, bufsize=-1):
        assert command[0] == "/opt/hermes-env/bin/python"
        async def wait():
            return 0 if self.completed else 1
        lines = [
            "Third-party diagnostic output is not copied to the UI.\n",
            "WORKSPACE_EVENT " + json.dumps({"kind": "tool", "message": "Ran test suite"}) + "\n",
            "WORKSPACE_EVENT " + json.dumps({"kind": "final", "message": "Tests passed", "completed": self.completed}) + "\n",
        ]
        # Modal's default is arbitrary chunks; adjacent writes can arrive in
        # one chunk. Respect the requested framing in the fake as the SDK does.
        return SimpleNamespace(stdout=Lines(lines if bufsize == 1 else ["".join(lines)]),
                               stderr=Lines([]), wait=aio(wait))


@pytest.fixture
def runner(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url="https://workspace.example", workspace_password="a-valid-test-password",
                        modal_token_id="test", modal_token_secret="modal-secret-only", litellm_api_key="model-secret", litellm_api_base="https://model.example/v1", agent_model="test-model")
    manager = RunManager(Store(tmp_path), settings)
    manager.image = lambda: "fake image"
    async def client():
        return "fake client"
    manager.client = client
    async def lookup(*args, **kwargs):
        return "fake app"
    monkeypatch.setattr("app.runner.modal.App.lookup", aio(lookup))
    return manager


@pytest.mark.parametrize("completed,expected_status", [(True, "completed"), (False, "failed")])
async def test_cloud_lifecycle_collects_result_and_cleans_up(runner, monkeypatch, completed, expected_status):
    sandbox = FakeSandbox(completed=completed)
    async def create(**kwargs):
        assert kwargs["timeout"] == 86400  # Modal's machine lifetime; no overall turn cap
        assert kwargs["cpu"] == 2 and kwargs["memory"] == 4096
        return sandbox
    monkeypatch.setattr("app.runner.modal.Sandbox.create", aio(create))
    run = runner.store.create_run("Run tests", "", "modal", [])
    await runner.execute(run)
    result = runner.store.run(run["id"])
    assert result["status"] == expected_status
    assert result["sandbox_id"] == sandbox.object_id
    assert result["token_hash"] == ""
    assert sandbox.terminated
    assert "model-secret" not in json.dumps(sandbox.spec)
    assert sandbox.spec['timeout'] is None and sandbox.spec['max_iterations'] == 0
    assert (runner.settings.data_dir / "artifacts" / f"{run['id']}.zip").exists()
    assert any(row["kind"] == "artifact" for row in runner.store.events(run["id"]))
    assert any(row["message"] == "Ran test suite" for row in runner.store.events(run["id"]))
    assert not any("could not be decoded" in row["message"] for row in runner.store.events(run["id"]))


async def test_cancel_during_provisioning_cannot_orphan_sandbox(runner, monkeypatch):
    sandbox = FakeSandbox()
    started, release = asyncio.Event(), asyncio.Event()
    async def create(**kwargs):
        started.set()
        await release.wait()
        return sandbox
    monkeypatch.setattr("app.runner.modal.Sandbox.create", aio(create))
    run = runner.store.create_run("Cancel before boot", "", "modal", [])
    job = asyncio.create_task(runner.execute(run))
    await started.wait()
    await runner.cancel(run["id"])
    assert runner.store.run(run["id"])["token_hash"] == ""
    release.set()
    await job
    assert sandbox.terminated and sandbox.spec is None
    assert runner.store.run(run["id"])["status"] == "cancelled"


async def test_active_stop_revokes_capability_and_saves_without_waiting_for_ui_lock(runner):
    from unittest.mock import AsyncMock

    run = runner.store.create_run('Stop active work', '', 'modal', [])
    runner.store.update_run(run['id'], status='running', token_hash='active-capability')
    sandbox = FakeSandbox()
    async def terminate():
        row = runner.store.run(run['id'])
        assert row['status'] == 'stopping' and row['token_hash'] == ''
        await sandbox.terminate_sandbox()
    sandbox.terminate = aio(terminate)
    runner.sandboxes[run['id']] = sandbox
    capture_busy = asyncio.Lock()
    await capture_busy.acquire()
    runner.computer = SimpleNamespace(locks={run['id']: capture_busy}, save_captures=AsyncMock())
    try:
        await asyncio.wait_for(runner.cancel(run['id']), 1)
        assert sandbox.terminated
        runner.computer.save_captures.assert_awaited_once_with(sandbox, run['id'], releasing=True)
    finally:
        capture_busy.release()


async def test_shutdown_during_provisioning_cleans_up_after_creation(runner, monkeypatch):
    sandbox = FakeSandbox()
    started, release = asyncio.Event(), asyncio.Event()
    async def create(**kwargs):
        started.set()
        await release.wait()
        return sandbox
    monkeypatch.setattr("app.runner.modal.Sandbox.create", aio(create))
    run = runner.store.create_run("Shutdown during boot", "", "modal", [])
    job = asyncio.create_task(runner.execute(run))
    await started.wait()
    job.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert sandbox.terminated
    assert runner.store.run(run["id"])["status"] == "interrupted"
    assert runner.store.run(run["id"])["token_hash"] == ""


async def test_slack_stop_during_context_fetch_settles_and_can_continue(runner):
    started, release = asyncio.Event(), asyncio.Event()
    async def prepare(run_id):
        started.set()
        await release.wait()
    runner.prepare_context = prepare
    run = runner.store.create_run("Stop before context returns", "", "modal", [], chat_enabled=True)
    runner.submit(run)
    await started.wait()
    # Slack reserves cancellation in its receipt transaction before cleanup.
    runner.store.update_run(run['id'], status='stopping', token_hash='')
    runner.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run['id'],))
    await runner.cancel(run['id'])
    job = runner.jobs[run['id']]
    release.set()
    await job
    assert runner.store.run(run['id'])['status'] == 'cancelled'
    message, submit = runner.store.enqueue_message(run['id'], 'Continue now', 'followup')
    assert submit and message['status'] == 'queued'
