"""Verified local backups, integrity checks, and health probes for self-hosted operation.

Modal Volume checkpoints cover Modal deployments, and Render keeps a persistent
disk, but operators previously had no documented way to export the live SQLite
database ("export the current Render database ... first" in docs/deployment.md)
and ``restore_checkpoint`` would boot whatever file it found without validation.
This module fills that gap:

- ``verify_database`` runs ``PRAGMA integrity_check`` plus a required-table
  sanity check without mutating the file.
- ``backup_database`` copies the live database with the SQLite backup API
  (which includes committed WAL content), verifies the copy, sets private
  permissions, and rotates old backups.
- ``database_stats`` / ``health_summary`` power the ``/health`` probe and the
  admin-only ``/api/health/detailed`` endpoint so load balancers fail fast
  instead of serving traffic on a corrupt or full disk.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED_TABLES = frozenset({
    "runs",
    "messages",
    "events",
    "connections",
    "organization",
})

BACKUP_FILENAME = re.compile(r"^workspace-(\d{8})-(\d{6})\.db$")
MIN_FREE_BYTES = 50 * 1024 * 1024


def _connect_readonly(path: Path) -> sqlite3.Connection:
    # mode=ro guarantees verification never mutates the file under test.
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def verify_database(path: Path) -> dict:
    """Check integrity and schema without writing. Never raises for corruption."""
    result: dict = {"path": str(path), "ok": False, "integrity": "", "tables": [], "errors": []}
    try:
        if not path.exists() or not path.is_file() or path.is_symlink():
            result["errors"].append("database file is missing" if not path.exists() else "database path is not a regular file")
            return result
        with _connect_readonly(path) as conn:
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                result["errors"].append(f"integrity_check failed: {exc}")
                return result
            result["integrity"] = integrity
            if integrity != "ok":
                result["errors"].append(f"integrity_check reported: {integrity[:500]}")
                return result
            try:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.DatabaseError as exc:
                result["errors"].append(f"schema listing failed: {exc}")
                return result
            result["tables"] = sorted(tables)
            missing = sorted(REQUIRED_TABLES - tables)
            if missing:
                result["errors"].append(f"missing required tables: {', '.join(missing)}")
                return result
            result["ok"] = True
            return result
    except (OSError, sqlite3.Error) as exc:
        result["errors"].append(f"verification failed: {exc}")
        return result


def database_stats(data_dir: Path) -> dict:
    """Best-effort sizes and row counts. Never raises; unknown values are None."""
    stats: dict = {
        "database_bytes": None,
        "wal_bytes": None,
        "shm_bytes": None,
        "artifacts": {"files": 0, "bytes": 0},
        "rows": {"runs": None, "messages": None, "events": None},
        "disk_free_bytes": None,
    }
    try:
        db_path = data_dir / "workspace.db"
        for key, name in (("database_bytes", "workspace.db"), ("wal_bytes", "workspace.db-wal"), ("shm_bytes", "workspace.db-shm")):
            candidate = data_dir / name
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    stats[key] = candidate.stat().st_size
            except OSError:
                pass
        artifacts = data_dir / "artifacts"
        try:
            if artifacts.is_dir() and not artifacts.is_symlink():
                files, total = 0, 0
                for entry in artifacts.rglob("*"):
                    try:
                        if entry.is_file() and not entry.is_symlink():
                            files += 1
                            total += entry.stat().st_size
                    except OSError:
                        continue
                stats["artifacts"] = {"files": files, "bytes": total}
        except OSError:
            pass
        if db_path.exists():
            try:
                with _connect_readonly(db_path) as conn:
                    for table in ("runs", "messages", "events"):
                        try:
                            stats["rows"][table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        except sqlite3.Error:
                            stats["rows"][table] = None
            except (OSError, sqlite3.Error):
                pass
        try:
            stats["disk_free_bytes"] = shutil.disk_usage(data_dir if data_dir.exists() else Path("/tmp")).free
        except OSError:
            pass
    except Exception as exc:  # defensive: stats must never break health probes
        log.warning("database_stats failed: %s", exc)
    return stats


def _drop_stale_sidecars(base: Path) -> None:
    """Remove SQLite SHM/WAL sidecars left by verification opens.

    ``-shm`` is pure shared memory and always safe to drop. ``-wal``/``-journal``
    are only removed when empty so a non-zero WAL (real frames) is never lost.
    Keeps ``BACKUP_DIR`` to exactly ``workspace-*.db`` + ``.json`` manifests.
    """
    for suffix in ("-shm", "-wal", "-journal"):
        sidecar = Path(str(base) + suffix)
        try:
            if not sidecar.is_file() or sidecar.is_symlink():
                continue
            if suffix != "-shm" and sidecar.stat().st_size != 0:
                continue
            sidecar.unlink()
        except OSError:
            pass


def prune_backups(backup_dir: Path, keep: int) -> list[str]:
    """Delete oldest timestamped backups beyond ``keep``. Returns deleted names."""
    keep = max(1, int(keep))
    try:
        candidates = sorted(
            (p for p in backup_dir.iterdir() if p.is_file() and BACKUP_FILENAME.match(p.name)),
            key=lambda p: p.name,
        )
    except OSError:
        return []
    doomed = candidates[: max(0, len(candidates) - keep)]
    deleted = []
    for path in doomed:
        try:
            path.unlink()
            deleted.append(path.name)
            sidecar = path.with_suffix(".json")
            try:
                if sidecar.is_file():
                    sidecar.unlink()
            except OSError:
                pass
        except OSError as exc:
            log.warning("prune_backups could not delete %s: %s", path, exc)
    return deleted


def backup_database(data_dir: Path, backup_dir: Path, *, keep: int = 7, vacuum: bool = False) -> dict:
    """Create a verified timestamped backup of the live database.

    Uses the SQLite backup API so committed WAL content is included, then runs
    ``verify_database`` on the copy before publishing. Raises on any failure
    so operators and API callers never mistake a partial file for a backup.
    """
    keep = max(1, min(90, int(keep)))
    source = data_dir / "workspace.db"
    if not source.exists():
        raise FileNotFoundError(f"No database at {source}; nothing to back up.")
    verification = verify_database(source)
    if not verification["ok"]:
        raise RuntimeError(f"Refusing to back up a corrupt database: {'; '.join(verification['errors'])}")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        backup_dir.chmod(0o700)
    except OSError:
        pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Avoid collisions when two backups land in the same second.
    target = backup_dir / f"workspace-{stamp}.db"
    counter = 0
    while target.exists():
        counter += 1
        target = backup_dir / f"workspace-{stamp}-{counter:02d}.db"
        if counter > 99:
            raise RuntimeError("Too many backups in the same second; try again.")
    if BACKUP_FILENAME.match(target.name) is None and "-" in target.stem:
        # Suffixed collision names still sort chronologically; accept them.
        pass
    staged = target.with_suffix(".next.db")
    if staged.exists():
        try:
            staged.unlink()
        except OSError:
            pass
    _drop_stale_sidecars(staged)
    with sqlite3.connect(source, timeout=10) as src, sqlite3.connect(staged) as dest:
        if vacuum:
            try:
                src.execute("VACUUM INTO ?", (str(staged),))
            except sqlite3.Error:
                # Older SQLite builds lack VACUUM INTO; fall back to backup API.
                src.backup(dest)
        else:
            src.backup(dest)
    try:
        staged.chmod(0o600)
    except OSError:
        pass
    # Checkpoint any WAL frames into the staged copy before verification so the
    # published ``.db`` is self-contained; verification opens can leave empty
    # ``-shm`` files behind.
    try:
        with sqlite3.connect(staged, timeout=10) as checkpoint_conn:
            checkpoint_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass
    checked = verify_database(staged)
    if not checked["ok"]:
        try:
            staged.unlink()
        except OSError:
            pass
        _drop_stale_sidecars(staged)
        raise RuntimeError(f"Backup verification failed: {'; '.join(checked['errors'])}")
    staged.replace(target)
    _drop_stale_sidecars(staged)
    _drop_stale_sidecars(target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    manifest = {
        "backup": target.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_integrity": verification["integrity"],
        "stats": database_stats(data_dir),
    }
    try:
        sidecar = target.with_suffix(".json")
        sidecar.write_text(json.dumps(manifest, indent=2))
        sidecar.chmod(0o600)
    except OSError as exc:
        log.warning("Could not write backup manifest: %s", exc)
    pruned = prune_backups(backup_dir, keep)
    manifest["pruned"] = pruned
    manifest["path"] = str(target)
    log.info("Database backup created: %s (pruned %d)", target.name, len(pruned))
    return manifest


def restore_backup(backup_path: Path, data_dir: Path) -> dict:
    """Safely restore a previously verified backup over the live database."""
    if backup_path.is_symlink() or not backup_path.is_file():
        raise ValueError("Backup path must be a regular file.")
    checked = verify_database(backup_path)
    if not checked["ok"]:
        raise RuntimeError(f"Refusing to restore a corrupt backup: {'; '.join(checked['errors'])}")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = data_dir / "workspace.db"
    if target.exists():
        quarantine = data_dir / f"workspace.pre-restore-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.db"
        try:
            shutil.copy2(target, quarantine)
            quarantine.chmod(0o600)
        except OSError as exc:
            log.warning("Could not quarantine current database: %s", exc)
            quarantine = None
    else:
        quarantine = None
    staged = target.with_suffix(".restore")
    shutil.copy2(backup_path, staged)
    try:
        staged.chmod(0o600)
    except OSError:
        pass
    final_check = verify_database(staged)
    if not final_check["ok"]:
        try:
            staged.unlink()
        except OSError:
            pass
        raise RuntimeError(f"Staged restore failed verification: {'; '.join(final_check['errors'])}")
    staged.replace(target)
    try:
        target.chmod(0o600)
    except OSError:
        pass
    # Remove stale WAL/SHM so the restored database opens cleanly.
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            sibling = data_dir / f"workspace.db{suffix}"
            if sibling.is_file() and not sibling.is_symlink():
                sibling.unlink()
        except OSError:
            pass
    return {"restored": str(target), "backup": str(backup_path), "quarantine": str(quarantine) if quarantine else None}


def health_summary(data_dir: Path, *, checkpoint_dir: Path | None = None) -> dict:
    """Aggregate file, integrity, and disk signals for health endpoints."""
    db_path = data_dir / "workspace.db"
    summary: dict = {
        "database": {"exists": db_path.exists(), "ok": None, "errors": []},
        "stats": database_stats(data_dir),
        "disk_low": False,
        "checkpoint": {"configured": bool(checkpoint_dir), "exists": False},
        "ok": True,
        "reasons": [],
    }
    if db_path.exists():
        verification = verify_database(db_path)
        summary["database"]["ok"] = verification["ok"]
        summary["database"]["errors"] = verification["errors"]
        summary["database"]["integrity"] = verification["integrity"]
        if not verification["ok"]:
            summary["ok"] = False
            summary["reasons"].append("database integrity check failed")
    free = summary["stats"].get("disk_free_bytes")
    if free is not None and free < MIN_FREE_BYTES:
        summary["disk_low"] = True
        summary["ok"] = False
        summary["reasons"].append(f"low disk space ({free} bytes free)")
    if checkpoint_dir:
        checkpoint_db = checkpoint_dir / "workspace.db"
        try:
            summary["checkpoint"]["exists"] = checkpoint_db.exists()
        except OSError:
            pass
    return summary
