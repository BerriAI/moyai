from __future__ import annotations

import argparse
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def serve(log_path: Path, response_path: Path, host: str, port: int, cert: Path, key: Path) -> None:
    responses = json.loads(response_path.read_text())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            body = b"ok" if self.path == "/healthz" else b"not found"
            self.send_response(200 if self.path == "/healthz" else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            path = self.path.split("?", 1)[0].rstrip("/")
            with log_path.open("a") as log:
                log.write(json.dumps({"path": path, **body}, ensure_ascii=False) + "\n")
            raw = json.dumps(responses.get(path, responses["default"])).encode()
            self.send_response(200)
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
    serve(args.log_path, args.response_path, args.host, args.port, args.cert, args.key)


if __name__ == "__main__":
    main()
