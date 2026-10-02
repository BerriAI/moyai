import asyncio
import sqlite3

import pytest

from app.config import Settings
from app.db import Store
from app.persistence import Checkpoints, restore_checkpoint


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
