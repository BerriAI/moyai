"""Render control plane; agent execution uses the configured sandbox provider.

Initial deployments run a maintenance app. After the old web service is stopped,
set RENDER_MIGRATION_STAGE=false to import its final Modal Volume checkpoint.
An existing Render database is never overwritten by bootstrap.
"""
import asyncio
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import sqlite3
import time
from urllib.parse import urlparse

import modal
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


maintenance = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
LISTENER_GRACE_SECONDS = 30
API_GRACEFUL_SHUTDOWN_SECONDS = 240
BROKER_GRACEFUL_SHUTDOWN_SECONDS = 240


class RenderOverlapServer(uvicorn.Server):
    """Keep accepting briefly while Render's private routing converges."""

    def __init__(self, config, *, listener_grace_seconds=LISTENER_GRACE_SECONDS):
        super().__init__(config)
        self.listener_grace_seconds = listener_grace_seconds
        self.listener_grace_deadline = None
        self.http_drain_deadline = None

    def handle_exit(self, sig, frame):
        # Measure from the first signal, not from when the event loop gets to
        # shutdown. Repeated SIGTERM must never extend the platform's deadline.
        if sig == signal.SIGTERM and self.started and not self.should_exit:
            self.listener_grace_deadline = time.monotonic() + self.listener_grace_seconds
            if self.config.timeout_graceful_shutdown is not None:
                self.http_drain_deadline = self.listener_grace_deadline + self.config.timeout_graceful_shutdown
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets=None):
        if self.listener_grace_deadline is not None and not self.force_exit:
            logging.getLogger("uvicorn.error").info(
                "Keeping listener open for up to %s seconds during routing handoff",
                self.listener_grace_seconds,
            )
            while not self.force_exit:
                remaining = self.listener_grace_deadline - time.monotonic()
                if remaining <= 0:
                    break
                # Keep serving requests and observing Uvicorn's second-SIGINT
                # force-exit flag; neither sleep nor signal handling blocks I/O.
                await asyncio.sleep(min(0.1, remaining))
        if self.http_drain_deadline is not None:
            self.config.timeout_graceful_shutdown = max(0, min(
                self.config.timeout_graceful_shutdown,
                self.http_drain_deadline - time.monotonic(),
            ))
        await super().shutdown(sockets=sockets)


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
    # Render connects from an in-container proxy on 127.0.0.1 and appends
    # "<client>, <Cloudflare edge>, <Render proxy>" to X-Forwarded-For.
    os.environ.setdefault("TRUSTED_PROXY_HOPS", "3")
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
    role = os.environ.get("MOYAI_RUNTIME_ROLE")
    config = uvicorn.Config(
        maintenance if stage == "true" else "app.main:app", host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")), workers=1,
        timeout_graceful_shutdown=(API_GRACEFUL_SHUTDOWN_SECONDS if role == "api" else
                                   BROKER_GRACEFUL_SHUTDOWN_SECONDS if role == "broker" else 20),
        # The app resolves the client from X-Forwarded-For itself (TRUSTED_PROXY_HOPS).
        proxy_headers=False,
    )
    server = RenderOverlapServer(config) if role in {"api", "broker", "coordinator"} else uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
