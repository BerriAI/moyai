from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

GITHUB_HOSTS = frozenset({"api.github.com", "api.githubcopilot.com"})


class GitHubStub:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []
        self._lock = threading.Lock()
        self._base_url = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def api_url(self) -> str:
        return self._base_url

    @property
    def mcp_url(self) -> str:
        return self._base_url + "/mcp/"

    def start(self) -> None:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                return None

            def _respond(self, status: int, payload: dict[str, Any] | None = None) -> None:
                body = json.dumps(payload).encode() if payload is not None else b""
                self.send_response(status)
                if payload is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _handle(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    payload = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    payload = {}
                operation = str(payload.get("method", "")) if isinstance(payload, dict) else ""
                record = {"method": self.command, "path": self.path, "operation": operation}
                with stub._lock:
                    stub.requests.append(record)
                if self.path == "/app/installations/201/access_tokens" and self.command == "POST":
                    self._respond(201, {
                        "token": "eval-github-installation-token",
                        "expires_at": "2099-01-01T00:00:00Z",
                    })
                    return
                if self.path == "/mcp/" and self.command == "POST":
                    if operation == "initialize":
                        self._respond(200, {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "result": {
                                "protocolVersion": "2025-03-26",
                                "capabilities": {"tools": {}},
                                "serverInfo": {"name": "local-github-stub", "version": "1"},
                            },
                        })
                        return
                    if operation == "tools/list":
                        self._respond(200, {
                            "jsonrpc": "2.0",
                            "id": payload["id"],
                            "result": {"tools": []},
                        })
                        return
                    if operation in {"server/discover", "notifications/initialized"}:
                        self._respond(202)
                        return
                self._respond(404, {"error": "not found"})

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def do_PUT(self) -> None:
                self._handle()

            def do_PATCH(self) -> None:
                self._handle()

            def do_DELETE(self) -> None:
                self._handle()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._base_url = f"http://127.0.0.1:{server.server_port}"
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join()
            self._thread = None


@contextmanager
def block_github_dns():
    original = socket.getaddrinfo

    def guarded(host: str | bytes, *args: Any, **kwargs: Any) -> Any:
        hostname = host.decode() if isinstance(host, bytes) else host
        if hostname.rstrip(".").lower() in GITHUB_HOSTS:
            raise OSError(f"GitHub host {hostname} must use the local evaluation stub.")
        return original(host, *args, **kwargs)

    with patch("socket.getaddrinfo", guarded):
        yield
