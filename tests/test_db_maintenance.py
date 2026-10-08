import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Store
from app.db_maintenance import (
    backup_database,
    database_stats,
    health_summary,
    prune_backups,
    restore_backup,
    verify_database,
)
from app.persistence import restore_checkpoint


def make_store(path):
    settings = Settings(_env_file=None, data_dir=path)
    store = Store(settings.data_dir, default_model="")
    store.create_run("Backup probe", "", "demo", [])
    return store


def test_verify_database_accepts_healthy_and_rejects_missing(tmp_path):
    missing = verify_database(tmp_path / "workspace.db")
    assert not missing["ok"]
    assert missing["errors"]

    make_store(tmp_path / "data")
    healthy = verify_database(tmp_path / "data" / "workspace.db")
    assert healthy["ok"]
    assert healthy["integrity"] == "ok"
    assert "runs" in healthy["tables"]


def test_verify_database_rejects_corrupt_file(tmp_path):
    target = tmp_path / "workspace.db"
    target.write_bytes(b"not a sqlite database at all")
    checked = verify_database(target)
    assert not checked["ok"]
    assert checked["errors"]


def test_backup_database_verifies_rotates_and_sets_permissions(tmp_path):
    make_store(tmp_path / "data")
    backup_dir = tmp_path / "backups"
    first = backup_database(tmp_path / "data", backup_dir, keep=2)
    assert first["path"].endswith(".db")
    assert (tmp_path / "backups" / first["backup"]).stat().st_mode & 0o777 == 0o600
    # Sidecar manifest is written next to the backup.
    sidecar = (tmp_path / "backups" / first["backup"]).with_suffix(".json")
    assert json.loads(sidecar.read_text())["backup"] == first["backup"]

    time.sleep(1.05)  # distinct UTC-second timestamps sort chronologically
    second = backup_database(tmp_path / "data", backup_dir, keep=2)
    assert second["backup"] != first["backup"]
    time.sleep(1.05)
    third = backup_database(tmp_path / "data", backup_dir, keep=2)
    remaining = sorted(p.name for p in backup_dir.iterdir() if p.suffix == ".db")
    assert len(remaining) == 2
    assert first["backup"] not in remaining
    assert third["backup"] in remaining


def test_backup_dir_contains_only_db_and_manifest(tmp_path):
    make_store(tmp_path / "data")
    backup_dir = tmp_path / "backups"
    manifest = backup_database(tmp_path / "data", backup_dir, keep=7)
    names = sorted(p.name for p in backup_dir.iterdir())
    assert names == sorted([manifest["backup"], Path(manifest["backup"]).with_suffix(".json").name])
    assert not list(backup_dir.glob("*.next.db*"))
    assert not list(backup_dir.glob("*-shm"))
    assert not [p for p in backup_dir.glob("*-wal") if p.stat().st_size != 0]


def test_backup_refuses_missing_and_corrupt_source(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_database(tmp_path / "empty", tmp_path / "backups")
    corrupt_dir = tmp_path / "corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "workspace.db").write_bytes(b"garbage")
    with pytest.raises(RuntimeError, match="corrupt"):
        backup_database(corrupt_dir, tmp_path / "backups")


def test_prune_backups_keeps_newest_and_ignores_other_files(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "notes.txt").write_text("operator notes stay")
    for stamp in ("20240101-000001", "20240102-000001", "20240103-000001"):
        (backup_dir / f"workspace-{stamp}.db").write_bytes(b"x")
    deleted = prune_backups(backup_dir, keep=2)
    assert deleted == ["workspace-20240101-000001.db"]
    assert (backup_dir / "notes.txt").exists()


def test_restore_backup_round_trip_and_refuses_corrupt(tmp_path):
    make_store(tmp_path / "data")
    manifest = backup_database(tmp_path / "data", tmp_path / "backups")
    result = restore_backup(tmp_path / "backups" / manifest["backup"], tmp_path / "restored")
    assert result["restored"].endswith("workspace.db")
    restored = Store(tmp_path / "restored")
    assert restored.rows("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 1

    bad = tmp_path / "bad.db"
    bad.write_bytes(b"corrupt")
    with pytest.raises(RuntimeError, match="corrupt"):
        restore_backup(bad, tmp_path / "restored2")


def test_database_stats_never_raises_and_reports_rows(tmp_path):
    make_store(tmp_path / "data")
    stats = database_stats(tmp_path / "data")
    assert stats["database_bytes"] and stats["database_bytes"] > 0
    assert stats["rows"]["runs"] == 1
    assert stats["disk_free_bytes"] and stats["disk_free_bytes"] > 0
    empty_stats = database_stats(tmp_path / "missing-dir")
    assert empty_stats["rows"]["runs"] is None


def test_health_summary_flags_corrupt_database(tmp_path):
    make_store(tmp_path / "data")
    healthy = health_summary(tmp_path / "data")
    assert healthy["ok"]
    assert healthy["database"]["ok"] is True
    (tmp_path / "data" / "workspace.db").write_bytes(b"corrupt")
    sick = health_summary(tmp_path / "data")
    assert not sick["ok"]
    assert "database integrity check failed" in sick["reasons"]


def test_restore_checkpoint_refuses_corrupt_source(tmp_path):
    checkpoint_dir = tmp_path / "volume"
    checkpoint_dir.mkdir()
    (checkpoint_dir / "workspace.db").write_bytes(b"corrupt checkpoint")
    settings = Settings(_env_file=None, data_dir=tmp_path / "local", checkpoint_dir=checkpoint_dir)
    with pytest.raises(RuntimeError, match="integrity"):
        restore_checkpoint(settings)
    assert not (tmp_path / "local" / "workspace.db").exists()


def test_restore_checkpoint_accepts_valid_source(tmp_path):
    source_dir = tmp_path / "volume"
    source_settings = Settings(_env_file=None, data_dir=source_dir)
    Store(source_settings.data_dir, default_model="").create_run("Checkpointed", "", "demo", [])
    settings = Settings(_env_file=None, data_dir=tmp_path / "local", checkpoint_dir=source_dir)
    restore_checkpoint(settings)
    assert Store(tmp_path / "local").rows("SELECT COUNT(*) AS n FROM runs")[0]["n"] == 1


def test_health_probe_stays_compatible_and_detailed_needs_admin(tmp_path):
    from app.main import create_app
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        public_url="https://workspace.example",
        workspace_password="valid-long-test-password",
    )
    Store(settings.data_dir, default_model="")
    with TestClient(create_app(settings), base_url="https://workspace.example") as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/api/health/detailed").status_code == 401


def test_detailed_health_and_backup_api_as_admin(tmp_path):
    from app.main import create_app
    backup_dir = tmp_path / "backups"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        backup_dir=backup_dir,
        public_url="https://workspace.example",
        workspace_password="valid-long-test-password",
    )
    Store(settings.data_dir, default_model="")
    with TestClient(create_app(settings), base_url="https://workspace.example") as client:
        client.headers.update({"Origin": settings.public_url})
        login = client.post("/api/login", json={"password": "valid-long-test-password"})
        assert login.status_code == 200
        client.headers.update({"X-CSRF-Token": client.get("/api/session").json()["csrf"]})
        detailed = client.get("/api/health/detailed")
        assert detailed.status_code == 200
        body = detailed.json()
        assert body["ok"] is True
        assert body["database"]["ok"] is True
        created = client.post("/api/admin/backup", json={})
        assert created.status_code == 201
        assert created.json()["backup"].endswith(".db")
        assert len(list(backup_dir.glob("workspace-*.db"))) == 1


def test_backup_api_requires_configuration(tmp_path):
    from app.main import create_app
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        public_url="https://workspace.example",
        workspace_password="valid-long-test-password",
    )
    Store(settings.data_dir, default_model="")
    with TestClient(create_app(settings), base_url="https://workspace.example") as client:
        client.headers.update({"Origin": settings.public_url})
        client.post("/api/login", json={"password": "valid-long-test-password"})
        client.headers.update({"X-CSRF-Token": client.get("/api/session").json()["csrf"]})
        response = client.post("/api/admin/backup", json={})
        assert response.status_code == 409


def test_backup_cli_verify_only_and_round_trip(tmp_path):
    import subprocess
    import sys
    make_store(tmp_path / "data")
    backup_dir = tmp_path / "backups"
    ok = subprocess.run(
        [sys.executable, "scripts/db_backup.py", "--data-dir", str(tmp_path / "data"),
         "--backup-dir", str(backup_dir), "--keep", "3"],
        capture_output=True, text=True, cwd=".",
    )
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    verify = subprocess.run(
        [sys.executable, "scripts/db_backup.py", "--data-dir", str(tmp_path / "data"), "--verify-only"],
        capture_output=True, text=True, cwd=".",
    )
    assert verify.returncode == 0
