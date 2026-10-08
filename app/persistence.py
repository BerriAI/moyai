"""Single-writer SQLite checkpoints for Modal Server deployments.

SQLite stays on the container's local filesystem. Only complete snapshots and
immutable result archives live on the Modal Volume. Never run multiple writers.
"""
import asyncio
import logging
import shutil
import sqlite3
from pathlib import Path

import modal

log = logging.getLogger(__name__)


def restore_checkpoint(settings):
    if not settings.checkpoint_dir:
        return
    source = settings.checkpoint_dir / "workspace.db"
    target = settings.data_dir / "workspace.db"
    if source.exists() and not target.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("Checkpoint is not a regular file; refusing to restore.")
        staged = target.with_suffix(".restore")
        shutil.copy2(source, staged)
        try:
            staged.chmod(0o600)
        except OSError:
            pass
        # Never boot a corrupt checkpoint: verify the staged copy before
        # publishing it. A corrupt source aborts startup instead of serving
        # bad data.
        from .db_maintenance import verify_database
        checked = verify_database(staged)
        if not checked["ok"]:
            try:
                staged.unlink()
            except OSError:
                pass
            log.error("Checkpoint restore refused: %s", "; ".join(checked["errors"]))
            raise RuntimeError(f"Checkpoint failed integrity validation: {'; '.join(checked['errors'])}")
        staged.replace(target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        log.info("Restored checkpoint database (%s).", target)
        archives = settings.checkpoint_dir / "artifacts"
        if archives.exists():
            shutil.copytree(archives, settings.data_dir / "artifacts", dirs_exist_ok=True)


class Checkpoints:
    def __init__(self, store, settings, commit=None):
        self.store, self.settings = store, settings
        self.lock = asyncio.Lock()
        self.saved_generation = -1
        self.commit = commit
        if settings.checkpoint_dir and commit is None:
            if not settings.modal_volume_name:
                raise ValueError("MODAL_VOLUME_NAME is required for checkpoints")
            self.commit = modal.Volume.from_name(settings.modal_volume_name).commit.aio

    async def flush(self):
        directory = self.settings.checkpoint_dir
        if not directory:
            return
        async with self.lock:
            generation = self.store.generation
            if generation == self.saved_generation:
                return
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = directory / "workspace.next.db"
            # backup() includes committed WAL content and produces a complete DB.
            with self.store.connect() as source, sqlite3.connect(temporary) as target:
                source.backup(target)
            temporary.chmod(0o600)
            temporary.replace(directory / "workspace.db")
            archives = self.settings.data_dir / "artifacts"
            if archives.exists():
                target_dir = directory / "artifacts"
                target_dir.mkdir(exist_ok=True, mode=0o700)
                for source in archives.glob("*.zip"):
                    target = target_dir / source.name
                    if not target.exists() or (source.stat().st_mtime_ns, source.stat().st_size) != (target.stat().st_mtime_ns, target.stat().st_size):
                        staged = target.with_suffix(".tmp")
                        shutil.copy2(source, staged)
                        staged.replace(target)
                for source_dir in archives.glob('*-captures'):
                    if not source_dir.is_dir() or source_dir.is_symlink():
                        continue
                    dest = target_dir / source_dir.name
                    dest.mkdir(exist_ok=True, mode=0o700)
                    for source in source_dir.iterdir():
                        target = dest / source.name
                        if source.suffix not in {'.png', '.webm'} or source.is_symlink() or target.exists():
                            continue
                        staged = target.with_suffix('.next')
                        shutil.copy2(source, staged)
                        staged.replace(target)
            await self.commit()
            # Writes during the commit await remain dirty for the next flush.
            self.saved_generation = generation

    async def watch(self):
        while True:
            await asyncio.sleep(2)
            try:
                await self.flush()
            except Exception:
                log.error("Cloud checkpoint failed; durable writes will retry before acknowledgement.")
