from __future__ import annotations

import argparse
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def proxy_ready() -> bool:
    # Both Harbor adapters retry immediately. Bound each readiness wait below
    # their five-second exec timeout so a cold proxy gets time to initialize.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:4000/health/liveliness", timeout=0.3) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    return False


def serve(log_path: Path, response_path: Path, host: str, port: int, cert: Path, key: Path) -> None:
    response = json.loads(response_path.read_text())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            ready = self.path == "/healthz" and proxy_ready()
            body = b"ok" if ready else b"not ready"
            self.send_response(200 if ready else 503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            accepted = self.path == "/v1/chat/completions" and body.get("model") == "deepseek-flash"
            result = {**response, "id": f"fixture-{uuid.uuid4()}"}
            with log_path.open("a") as log:
                log.write(json.dumps({
                    "schema_version": 2, "model": body.get("model"), "path": self.path,
                    "response_id": result["id"], "status": 200 if accepted else 400,
                    "content": "OK" if accepted else None,
                }) + "\n")
            raw = json.dumps(result if accepted else {"error": "Unknown fixture route or model"}).encode()
            self.send_response(200 if accepted else 400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer((host, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-path", type=Path, required=True)
    parser.add_argument("--response-path", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=443)
    parser.add_argument("--cert", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    args.log_path.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    credentials = json.loads(Path("/eval/credentials.json").read_text())
    environment = {
        **os.environ,
        **credentials,
        "LITELLM_ROUTE_RECEIPTS": str(args.log_path.with_name("litellm_routes.jsonl")),
    }
    with args.log_path.with_name("litellm_proxy.log").open("a") as proxy_log:
        proxy = subprocess.Popen(
            ["litellm", "--config", "/eval/proxy.yaml", "--host", "127.0.0.1", "--port", "4000"],
            cwd="/eval", env=environment, stdout=proxy_log, stderr=subprocess.STDOUT,
        )
        try:
            serve(args.log_path, args.response_path, args.host, args.port, args.cert, args.key)
        finally:
            proxy.terminate()
            proxy.wait(timeout=10)


if __name__ == "__main__":
    main()
