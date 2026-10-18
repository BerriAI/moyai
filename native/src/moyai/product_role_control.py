"""OS-scoped role status and planned quiescence, without an HTTP admin route."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import stat
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from .product_quiesce import ProductQuiescence

MAX_BYTES = 16384


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: str
    expected: dict[str, str | int] = Field(default_factory=dict)
    request_id: str = Field(default="", max_length=200)


class RoleControl:
    def __init__(self, path: Path, quiescence: ProductQuiescence):
        self.path, self.quiescence = path, quiescence
        self.server: asyncio.AbstractServer | None = None
        self.inode: tuple[int, int] | None = None
        self.clients: set[asyncio.Task] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.clients.add(task)
        try:
            try:
                async with asyncio.timeout(5):
                    raw = await reader.readline()
                    if not raw.endswith(b"\n") or len(raw) > MAX_BYTES:
                        raise ValueError("Invalid control request.")
                    request = ControlRequest.model_validate_json(raw)
                    if request.command not in {"status", "quiesce"}:
                        raise ValueError("Unsupported control command.")
                    await self.quiescence.check_owner()
                    if request.command == "quiesce":
                        self.quiescence.begin(request.expected, request.request_id)
                    result = {"ok": True, **await self.quiescence.status()}
            except (ValueError, ValidationError, TimeoutError):
                result = {"ok": False, "error": "Invalid, conflicting, or unavailable role control request."}
            except Exception:  # noqa: BLE001 - never disclose a provider/DB connection string
                result = {"ok": False, "error": "Canonical role control is unavailable."}
            writer.write(json.dumps(result).encode() + b"\n")
            async with asyncio.timeout(5):
                await writer.drain()
        except (ConnectionError, TimeoutError):
            pass
        finally:
            # Quiescence belongs to the process, not the possibly lost reply.
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.clients.discard(task)

    async def __aenter__(self) -> Self:
        path = self.path
        if not path.is_absolute():
            raise ValueError("The control socket must have an absolute path.")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise ValueError("The control socket requires an owner-only directory.")
        # Never remove a stale path or replace another live process's socket.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            os.chmod(path, 0o600)
            node = path.lstat()
            self.inode = node.st_dev, node.st_ino
            self.server = await asyncio.start_unix_server(self.handle, sock=listener, limit=MAX_BYTES)
        except BaseException:
            listener.close()
            self._unlink()
            raise
        return self

    def _unlink(self) -> None:
        if self.inode is None:
            return
        try:
            node = self.path.lstat()
        except FileNotFoundError:
            return
        if (node.st_dev, node.st_ino) == self.inode:
            self.path.unlink()

    async def __aexit__(self, *_args) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.clients:
            await asyncio.wait(self.clients)
        self._unlink()


async def request(path: Path, body: dict) -> dict:
    async with asyncio.timeout(10):
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_BYTES)
        try:
            writer.write(json.dumps(body).encode() + b"\n")
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "quiesce"])
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--expected", type=Path, help="Saved status JSON for this exact process.")
    parser.add_argument("--request-id", default="")
    args = parser.parse_args(argv)
    body = {"command": args.command}
    if args.command == "quiesce":
        if args.expected is None or not args.request_id:
            parser.error("Quiescence requires --expected and --request-id.")
        body.update(expected=json.loads(args.expected.read_text())["identity"], request_id=args.request_id)
    try:
        result = asyncio.run(request(args.socket, body))
    except (OSError, ValueError, TimeoutError):
        parser.exit(1, "Role control reply unavailable; inspect status before further action.\n")
    print(json.dumps(result))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
