"""OS-scoped role status and planned quiescence, without an HTTP admin route."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import socket
import stat
import sys
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
        self.directory_fd: int | None = None
        self.lock_fd: int | None = None

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
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._claim_path()
            self._remove_orphan()
            listener.bind(str(path))
            node = path.lstat()
            self.inode = node.st_dev, node.st_ino
            os.chmod(path, 0o600)
            self._record_socket()
            # Own pathname cleanup under our lock, including on Python 3.13+
            # where asyncio otherwise removes the socket when closing.
            if sys.version_info >= (3, 13):
                self.server = await asyncio.start_unix_server(
                    self.handle, sock=listener, limit=MAX_BYTES, cleanup_socket=False,
                )
            else:
                self.server = await asyncio.start_unix_server(self.handle, sock=listener, limit=MAX_BYTES)
        except BaseException:
            listener.close()
            try:
                self._unlink()
            finally:
                self._release_path()
            raise
        return self

    def _claim_path(self) -> None:
        # A persistent lock inode serializes startup, orphan recovery and final
        # cleanup across processes. Never unlink it: a waiter may hold its fd.
        self.directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.lock_fd = os.open(
            self.path.name + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600, dir_fd=self.directory_fd,
        )
        node = os.fstat(self.lock_fd)
        if (
            not stat.S_ISREG(node.st_mode) or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) & 0o077 or node.st_nlink != 1
        ):
            raise ValueError("The control lock requires a private, owner-owned regular file.")
        fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._check_claim()

    def _check_claim(self) -> None:
        assert self.directory_fd is not None and self.lock_fd is not None
        parent, opened = self.path.parent.lstat(), os.fstat(self.directory_fd)
        lock = os.stat(self.path.name + ".lock", dir_fd=self.directory_fd, follow_symlinks=False)
        held = os.fstat(self.lock_fd)
        if (
            not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
            or (parent.st_dev, parent.st_ino) != (opened.st_dev, opened.st_ino)
            or not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.geteuid()
            or stat.S_IMODE(lock.st_mode) & 0o077 or lock.st_nlink != 1
            or (lock.st_dev, lock.st_ino) != (held.st_dev, held.st_ino)
        ):
            raise ValueError("The control directory or lock changed during ownership.")

    def _socket_node(self) -> os.stat_result:
        node = os.stat(self.path.name, dir_fd=self.directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(node.st_mode) or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) & 0o077 or node.st_nlink != 1
        ):
            raise ValueError("The control path must be a private, owner-owned socket.")
        return node

    @staticmethod
    def _socket_record(node: os.stat_result) -> bytes:
        # Include ctime so an unrelated later socket cannot inherit a claim
        # merely because the filesystem reused an old inode number.
        return f"moyai-role-control-v1:{node.st_dev}:{node.st_ino}:{node.st_ctime_ns}\n".encode()

    def _record_socket(self) -> None:
        self._check_claim()
        assert self.lock_fd is not None
        node = self._socket_node()
        if (node.st_dev, node.st_ino) != self.inode:
            raise ValueError("The control socket changed before recording ownership.")
        record = self._socket_record(node)
        if os.pwrite(self.lock_fd, record, 0) != len(record):
            raise OSError("The control socket ownership record was incomplete.")
        os.ftruncate(self.lock_fd, len(record))
        os.fsync(self.lock_fd)

    def _remove_orphan(self) -> None:
        self._check_claim()
        try:
            before = self._socket_node()
        except FileNotFoundError:
            return
        assert self.lock_fd is not None
        record = self._socket_record(before)
        if os.pread(self.lock_fd, 256, 0) != record:
            # Refusal alone cannot distinguish an orphan from a legacy process
            # paused between bind and listen that does not hold our lock.
            raise FileExistsError("The control socket has no matching ownership record.")
        # Only a refused connect qualifies for cleanup; permission errors,
        # timeouts and unknown errors retain the path. Never send a command.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(.25)
            try:
                probe.connect(str(self.path))
            except ConnectionRefusedError:
                pass
            else:
                raise FileExistsError("The control socket already has a live listener.")
        self._check_claim()
        after = self._socket_node()
        if self._socket_record(after) != record or os.pread(self.lock_fd, 256, 0) != record:
            raise ValueError("The control socket changed during orphan inspection.")
        os.unlink(self.path.name, dir_fd=self.directory_fd)

    def _release_path(self) -> None:
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
        if self.directory_fd is not None:
            os.close(self.directory_fd)
            self.directory_fd = None

    def _unlink(self) -> None:
        if self.inode is None:
            return
        self._check_claim()
        try:
            node = self._socket_node()
        except (FileNotFoundError, ValueError):
            return
        if (node.st_dev, node.st_ino) == self.inode:
            os.unlink(self.path.name, dir_fd=self.directory_fd)

    async def _close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.clients:
            await asyncio.wait(self.clients)
        self._unlink()

    async def __aexit__(self, *_args) -> None:
        # A cancelled exit must not release ownership while a client is still
        # handling a control request. SIGKILL releases the OS lock automatically.
        closing = asyncio.create_task(self._close())
        cancelled = None
        try:
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError as error:
                    cancelled = error
            closing.result()
        finally:
            self._release_path()
        if cancelled is not None:
            raise cancelled


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
