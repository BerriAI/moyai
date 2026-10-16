"""Render role listeners with a maintenance boundary and graceful routing handoff."""
import asyncio
import logging
import os
import signal
import time
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


maintenance = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
LISTENER_GRACE_SECONDS = 30
API_GRACEFUL_SHUTDOWN_SECONDS = 240
BROKER_GRACEFUL_SHUTDOWN_SECONDS = 240


class RenderOverlapServer(uvicorn.Server):
    """Keep accepting briefly while Render's private routing converges."""

    def __init__(self, config, *, listener_grace_seconds=LISTENER_GRACE_SECONDS, failure_check=None):
        super().__init__(config)
        self.listener_grace_seconds = listener_grace_seconds
        self.listener_grace_deadline = None
        self.http_drain_deadline = None
        self.failure_check = failure_check
        self.retired = False

    async def on_tick(self, counter):
        if await super().on_tick(counter):
            return True
        if self.failure_check and self.failure_check():
            # A fenced broker cannot recover its original ownership session.
            # Use normal HTTP/lifespan cleanup before Render replaces it; never
            # reconnect underneath live requests or keep serving permanent 503s.
            logging.getLogger('uvicorn.error').error('Retiring failed broker for automatic restart after cleanup')
            self.retired = True
            self.should_exit = True
            return True
        return False

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








def main():
    from docker_start import selected_core
    selected_core()
    configure_environment()
    stage = os.environ.get("RENDER_MIGRATION_STAGE", "true").lower()
    if stage not in {"true", "false"}:
        raise RuntimeError("RENDER_MIGRATION_STAGE must be true or false.")
    target = maintenance
    if stage == "false":
        # No legacy bootstrap or app.main import in this branch. Native startup
        # verifies schemas, source fencing and canonical role ownership.
        from moyai.product_config import load_product_config
        from moyai.product_start import create_application
        product_config = load_product_config()
        assert product_config is not None
        target = create_application(product_config, host="0.0.0.0")
    role = os.environ.get("MOYAI_RUNTIME_ROLE")
    config = uvicorn.Config(
        target, host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")), workers=1,
        timeout_graceful_shutdown=(API_GRACEFUL_SHUTDOWN_SECONDS if role == "api" else
                                   BROKER_GRACEFUL_SHUTDOWN_SECONDS if role == "broker" else 20),
        # The app resolves the client from X-Forwarded-For itself (TRUSTED_PROXY_HOPS).
        proxy_headers=False,
    )
    server = (RenderOverlapServer(config)
              if role in {'api', 'broker', 'coordinator'} else uvicorn.Server(config))
    lifecycle = None
    if stage == "false":
        lifecycle = getattr(getattr(target, "state", None), "product_lifecycle", None)
        if lifecycle is not None:
            lifecycle.retire = lambda: setattr(server, "should_exit", True)
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    if getattr(server, 'retired', False) or (lifecycle is not None and lifecycle.failed):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
