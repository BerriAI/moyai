import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.db import Store
from app.config import Settings
from render_start import configure_environment, import_checkpoint, maintenance


@pytest.fixture(autouse=True)
def restore_render_environment(monkeypatch):
    # configure_environment writes these process-wide values. Register them with
    # monkeypatch so startup tests do not change later tests' Settings defaults.
    import os
    for key in ('PUBLIC_URL', 'TRUST_MODAL_PROXY', 'CHECKPOINT_DIR', 'MODAL_VOLUME_NAME', 'DATA_DIR'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
        if not os.environ[key]:
            os.environ.pop(key)


class Volume:
    def __init__(self, db, entries):
        self.db, self.entries, self.reads = db, entries, []
        self.read_file = SimpleNamespace(aio=self.read)
        self.listdir = SimpleNamespace(aio=self.list)

    async def read(self, path):
        self.reads.append(path)
        yield self.db if path == "workspace.db" else b"archive bytes"

    async def list(self, *args, **kwargs):
        return [SimpleNamespace(path=path) for path in self.entries]


def snapshot(tmp_path, status="idle"):
    store = Store(tmp_path / "source")
    run = store.create_run("preserved conversation", "", "modal", [], chat_enabled=True)
    store.update_run(run["id"], status=status, snapshot_id="im-saved-workspace")
    store.execute("UPDATE messages SET status='completed'")
    store.execute("INSERT INTO connections VALUES('linear','encrypted-original-credential','Team','now')")
    backup = tmp_path / "backup.db"
    with store.connect() as src, sqlite3.connect(backup) as dest:
        src.backup(dest)
    return backup.read_bytes(), run["id"]


async def test_import_preserves_sessions_credentials_and_archives_once(tmp_path):
    data, run_id = snapshot(tmp_path)
    volume = Volume(data, [f"artifacts/{run_id}.zip", "../escape.zip", "artifacts/not-a-run.zip"])
    target = tmp_path / "target"
    assert await import_checkpoint(target, volume)
    store = Store(target)
    assert store.run(run_id)["snapshot_id"] == "im-saved-workspace"
    assert store.messages(run_id)[0]["content"] == "preserved conversation"
    assert store.rows("SELECT encrypted FROM connections")[0]["encrypted"] == "encrypted-original-credential"
    assert (target / "artifacts" / f"{run_id}.zip").read_bytes() == b"archive bytes"
    assert not await import_checkpoint(target, volume)
    assert volume.reads == ["workspace.db", f"artifacts/{run_id}.zip"]
    assert (target / "workspace.db").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("broken", ["active", "corrupt", "download"])
async def test_failed_import_never_publishes_partial_database(tmp_path, broken):
    data, run_id = snapshot(tmp_path, "running" if broken == "active" else "idle")
    volume = Volume(b"not sqlite" if broken == "corrupt" else data, [f"artifacts/{run_id}.zip"])
    if broken == "download":
        async def failing(path):
            if path != "workspace.db":
                raise RuntimeError("Transfer failed")
            yield data
        volume.read_file.aio = failing
    target = tmp_path / "target"
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        await import_checkpoint(target, volume)
    assert not (target / "workspace.db").exists()
    assert not (target / ".modal-import").exists()


def test_render_uses_its_own_origin_and_disk_not_modal_proxy(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://moyai.example/")
    monkeypatch.setenv("PUBLIC_URL", "https://old.example")
    monkeypatch.setenv("TRUST_MODAL_PROXY", "true")
    monkeypatch.setenv("CHECKPOINT_DIR", "/checkpoints")
    monkeypatch.setenv("MODAL_VOLUME_NAME", "old-volume")
    configure_environment()
    import os
    assert os.environ["PUBLIC_URL"] == "https://moyai.example"
    assert os.environ["TRUST_MODAL_PROXY"] == "false"
    assert "CHECKPOINT_DIR" not in os.environ and "MODAL_VOLUME_NAME" not in os.environ


def test_staging_cannot_accept_work_or_slack_events():
    from fastapi.testclient import TestClient
    with TestClient(maintenance) as client:
        assert client.get("/health").json()["mode"] == "migration_staging"
        assert client.post("/hooks/slack/events", json={"type":"event_callback"}).status_code == 503
        assert client.post("/api/runs", json={"prompt":"Do work"}).status_code == 503


@pytest.mark.parametrize('render_origin', ['', 'https://old.onrender.com'])
def test_explicit_custom_origin_supports_private_render_service(monkeypatch, render_origin):
    import os
    from docker_start import server_command
    from docker_healthcheck import health_request
    monkeypatch.setenv('RENDER_SERVICE_ID', 'srv-test')
    monkeypatch.setenv('RENDER_EXTERNAL_URL', render_origin)
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://moyai.example/')
    monkeypatch.delenv('PORT', raising=False)
    assert Settings(_env_file=None).public_url == 'https://moyai.example/'
    configure_environment()
    assert os.environ['PUBLIC_URL'] == 'https://moyai.example'
    assert server_command()[-1].endswith('render_start.py')
    request = health_request()
    assert request.full_url == 'http://127.0.0.1:10000/health'
    assert request.get_header('Host') == 'moyai.example'


@pytest.mark.parametrize('url', ['http://moyai.example', 'https://user:pass@moyai.example',
                                  'https://moyai.example/path', 'https://moyai.example?bad=1'])
def test_invalid_custom_origins_never_start(monkeypatch, url):
    monkeypatch.setenv('MOYAI_PUBLIC_URL', url)
    with pytest.raises(RuntimeError):
        configure_environment()


@pytest.mark.parametrize('role,seconds', [('api', 270), ('coordinator', 20), ('broker', 20), ('worker', 20)])
def test_render_api_shutdown_budget_leaves_time_for_cleanup(monkeypatch, role, seconds):
    import render_start
    monkeypatch.setenv('MOYAI_PUBLIC_URL', 'https://moyai.example')
    monkeypatch.setenv('MOYAI_RUNTIME_ROLE', role)
    monkeypatch.setenv('RENDER_MIGRATION_STAGE', 'true')
    calls = []
    monkeypatch.setattr(render_start.uvicorn, 'run', lambda *args, **kwargs: calls.append(kwargs))
    render_start.main()
    assert calls[0]['timeout_graceful_shutdown'] == seconds
    assert calls[0]['workers'] == 1
