"""Render control plane; agent execution uses the configured sandbox provider.

Initial deployments run a maintenance app. After the old web service is stopped,
set RENDER_MIGRATION_STAGE=false to import its final Modal Volume checkpoint.
An existing Render database is never overwritten by bootstrap.
"""
import asyncio
import os
from pathlib import Path
import re
import shutil
import sqlite3
from urllib.parse import urlparse

import modal
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


maintenance = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@maintenance.get("/health")
def health():
    return {"status": "ok", "mode": "migration_staging"}


@maintenance.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def preparing(path: str):
    return PlainTextResponse("Moyai is being prepared on Render. Existing sessions are still on the current workspace.",
                             status_code=503, headers={"Retry-After": "60"})


def configure_environment():
    external_url = os.environ.get("MOYAI_PUBLIC_URL", "") or os.environ.get("RENDER_EXTERNAL_URL", "")
    if not external_url:
        raise RuntimeError("Set MOYAI_PUBLIC_URL for a private service, or RENDER_EXTERNAL_URL for a web service.")
    parsed = urlparse(external_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise RuntimeError("The configured public URL must be an HTTPS origin.")
    os.environ["PUBLIC_URL"] = external_url.rstrip("/")
    os.environ["TRUST_MODAL_PROXY"] = "false"
    # Render's disk is the authoritative database. No Modal Volume writer runs here.
    os.environ.pop("CHECKPOINT_DIR", None)
    os.environ.pop("MODAL_VOLUME_NAME", None)
    os.environ.setdefault("DATA_DIR", "/var/data/moyai")


async def import_checkpoint(directory: Path, volume):
    """Stage and validate a read-only copy; publish the database last."""
    target = directory / "workspace.db"
    if target.exists():
        return False
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    staged = directory / ".modal-import"
    if staged.exists():
        shutil.rmtree(staged)
    staged.mkdir(mode=0o700)

    async def download(remote, local):
        with local.open("wb") as output:
            local.chmod(0o600)
            async for chunk in volume.read_file.aio(remote):
                output.write(chunk)

    try:
        await download("workspace.db", staged / "workspace.db")
        with sqlite3.connect(staged / "workspace.db") as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("The imported checkpoint failed integrity validation.")
            ids = {row[0] for row in db.execute("SELECT id FROM runs")}
            connections = db.execute("SELECT COUNT(*) FROM connections").fetchone()[0]
            # A cutover must not silently interrupt somebody's in-flight work.
            active = db.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('idle','completed','failed','cancelled','interrupted')").fetchone()[0]
            if active:
                raise RuntimeError("The source checkpoint has active sessions. Stop the old web service cleanly before importing.")
        archives = staged / "artifacts"
        archives.mkdir(mode=0o700)
        count = 0
        for entry in await volume.listdir.aio("/", recursive=True):
            remote = entry.path.lstrip("/")
            if re.fullmatch(r"artifacts/[0-9a-f]{32}\.zip", remote) and Path(remote).stem in ids:
                await download(remote, archives / Path(remote).name)
                count += 1
        target_archives = directory / "artifacts"
        target_archives.mkdir(mode=0o700, exist_ok=True)
        for artifact in archives.iterdir():
            artifact.replace(target_archives / artifact.name)
        (staged / "workspace.db").replace(target)
        print(f"Imported {len(ids)} sessions, {connections} app connections, and {count} result archives.", flush=True)
        return True
    finally:
        shutil.rmtree(staged, ignore_errors=True)


async def bootstrap():
    from app.config import Settings
    settings = Settings(_env_file=None)
    if settings.moyai_database_url:
        return
    if (settings.data_dir / "workspace.db").exists():
        return
    source = os.environ.get("BOOTSTRAP_MODAL_VOLUME", "")
    if not source:
        raise RuntimeError("No database exists. Set BOOTSTRAP_MODAL_VOLUME for the migration; refusing an empty workspace.")
    client = await modal.Client.from_credentials.aio(settings.modal_token_id, settings.modal_token_secret)
    volume = modal.Volume.from_name(source, client=client)
    await import_checkpoint(settings.data_dir, volume)


def main():
    configure_environment()
    stage = os.environ.get("RENDER_MIGRATION_STAGE", "true").lower()
    if stage not in {"true", "false"}:
        raise RuntimeError("RENDER_MIGRATION_STAGE must be true or false.")
    if stage == "false":
        asyncio.run(bootstrap())
    uvicorn.run(maintenance if stage == "true" else "app.main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", "10000")), workers=1, timeout_graceful_shutdown=20)


if __name__ == "__main__":
    main()
