import asyncio
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.db import Store
from app.persistence import Checkpoints, restore_checkpoint
from test_workspace import workspace


@pytest.mark.skipif(not hasattr(os, 'geteuid'), reason='POSIX database ownership')
@pytest.mark.parametrize('operation', ['create', 'reopen', 'query'])
def test_database_rejects_other_uid_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                              operation: str) -> None:
    store = Store(tmp_path) if operation != 'create' else None
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}

    def different_user() -> int:
        return tmp_path.stat().st_uid + 1

    monkeypatch.setattr(os, 'geteuid', different_user)

    def forbidden_open(*args: object, **kwargs: object) -> None:
        pytest.fail('A different UID must be rejected before SQLite can create sidecars')

    monkeypatch.setattr(sqlite3, 'connect', forbidden_open)
    with pytest.raises(PermissionError, match='run maintenance as the database owner'):
        if operation == 'query':
            assert store is not None
            store.rows('SELECT id FROM runs')
        else:
            Store(tmp_path)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before


async def test_checkpoint_restores_committed_database_and_results(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "local", checkpoint_dir=tmp_path / "volume")
    store = Store(settings.data_dir)
    run = store.create_run("Durable task", "", "demo", [])
    archives = settings.data_dir / "artifacts"
    archives.mkdir()
    (archives / f"{run['id']}.zip").write_bytes(b"immutable result archive")
    media = archives / (run['id'] + '-captures')
    media.mkdir()
    (media / 'flow.webm').write_bytes(b'saved recording')
    calls = []
    async def commit():
        calls.append(True)
    checkpoints = Checkpoints(store, settings, commit=commit)
    await checkpoints.flush()
    settings.data_dir = tmp_path / "new-container"
    restore_checkpoint(settings)
    restored = Store(settings.data_dir)
    assert restored.run(run["id"])["prompt"] == "Durable task"
    assert (settings.data_dir / "artifacts" / f"{run['id']}.zip").read_bytes() == b"immutable result archive"
    with restored.connect() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (settings.data_dir / 'artifacts' / (run['id'] + '-captures') / 'flow.webm').read_bytes() == b'saved recording'
    assert calls == [True]


async def test_writes_during_commit_are_not_lost_and_failed_commits_retry(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "local", checkpoint_dir=tmp_path / "volume")
    store = Store(settings.data_dir)
    run = store.create_run("Durable task", "", "demo", [])
    started, release = asyncio.Event(), asyncio.Event()
    attempts = 0
    async def commit():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Simulated volume failure")
        started.set()
        await release.wait()
    checkpoints = Checkpoints(store, settings, commit=commit)
    with pytest.raises(RuntimeError):
        await checkpoints.flush()
    assert checkpoints.saved_generation == -1
    pending = asyncio.create_task(checkpoints.flush())
    await started.wait()
    store.update_run(run["id"], status="completed", summary="Saved after snapshot")
    release.set()
    await pending
    assert checkpoints.saved_generation != store.generation
    await checkpoints.flush()
    with sqlite3.connect(settings.checkpoint_dir / "workspace.db") as conn:
        assert conn.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()[0] == "completed"
    assert attempts == 3


async def test_computer_input_acknowledges_before_cloud_commit_and_keeps_activity(workspace, tmp_path):
    app, client = workspace
    hub, store = app.state.computer, app.state.store
    checkpoints = app.state.automations.checkpoints
    original_settings = checkpoints.settings
    checkpoints.settings = original_settings.model_copy(update={'checkpoint_dir': tmp_path / 'checkpoint'})
    entered, release = asyncio.Event(), asyncio.Event()
    commits, failing = [], False
    async def commit():
        commits.append(True)
        entered.set()
        await release.wait()
        if failing:
            raise RuntimeError('Simulated volume failure')
    checkpoints.commit = commit
    run = store.create_run('Responsive input', '', 'modal', [], chat_enabled=True)
    url = f"/api/runs/{run['id']}/computer"
    hub.sandbox = AsyncMock(return_value=SimpleNamespace(computer_request=AsyncMock(return_value={})))
    hub.execute = AsyncMock(return_value=[])
    pending = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=app.state.settings.public_url,
                                    cookies=client.cookies, headers=client.headers) as browser:
            response = await asyncio.wait_for(browser.post(url, json={'action': 'input', 'args': {'events': []}}), 1)
            assert response.status_code == 200 and not commits
            touched = hub.touched(run['id'])
            assert touched > 0 and checkpoints.saved_generation != store.generation
            pending = asyncio.create_task(checkpoints.flush())
            await asyncio.wait_for(entered.wait(), 1)
            assert not pending.done()
            release.set()
            await pending
            with sqlite3.connect(checkpoints.settings.checkpoint_dir / 'workspace.db') as conn:
                assert conn.execute('SELECT touched FROM computer_activity WHERE run_id=?', (run['id'],)).fetchone()[0] == touched

            failing = True
            assert (await browser.post(url, json={'action': 'input', 'args': {'events': []}})).status_code == 200
            assert len(commits) == 1 and checkpoints.saved_generation != store.generation
            for action in ('claim', 'release', 'screenshot', 'record_stop'):
                result = await browser.post(url, json={'action': action})
                assert result.status_code == 503 and 'persistence' in result.json()['detail']
            result = await browser.patch('/api/organization', json={'name': 'Still durable'})
            assert result.status_code == 503 and len(commits) == 6
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        checkpoints.settings = original_settings


def test_modal_proxy_accepts_only_configured_public_host(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings = Settings(_env_file=None, data_dir=tmp_path, public_url="https://workspace.example",
                        workspace_password="valid-long-test-password", trust_modal_proxy=True)
    with TestClient(create_app(settings), base_url="http://172.20.0.2:8787") as client:
        assert client.get("/health").status_code == 400
        assert client.get("/health", headers={"X-Forwarded-Host": "attacker.example"}).status_code == 400
        assert client.get("/health", headers={"X-Forwarded-Host": "workspace.example"}).json() == {"status": "ok"}
        assert client.get("/api/runs", headers={"X-Forwarded-Host": "workspace.example"}).status_code == 401
