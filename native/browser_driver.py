"""Guest-local supervisor. Playwright runs only in its disposable worker."""
from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]

SOCKET_NAME = "browser.sock"
PID_NAME = "browser-driver.pid"
MAX_FRAME = 2 * 1024 * 1024
IDLE_SECONDS = 900


class DriverUnavailable(ConnectionError):
    """The connection failed before any request bytes were sent."""


@dataclass(frozen=True)
class Process:
    pid: int
    command: tuple[str, ...]


def remaining(deadline: float) -> float:
    value = deadline - time.time()
    if not math.isfinite(value) or value <= 0:
        raise TimeoutError("The browser deadline expired.")
    return value


def object_frame(frame: bytes) -> dict[str, Json]:
    value: Json = json.loads(frame)
    if not isinstance(value, dict):
        raise TypeError("Expected one browser protocol object.")
    return value


def read_frame(connection: socket.socket, deadline: float) -> bytes:
    buffer = bytearray()
    while len(buffer) <= MAX_FRAME:
        connection.settimeout(remaining(deadline))
        chunk = connection.recv(min(65536, MAX_FRAME + 1 - len(buffer)))
        if not chunk:
            raise ConnectionError("The browser connection closed.")
        buffer.extend(chunk)
        if len(buffer) > MAX_FRAME:
            raise ValueError("The browser protocol frame is too large.")
        if b"\n" in buffer:
            if buffer[-1:] != b"\n" or buffer.count(b"\n") != 1:
                raise ValueError("Unexpected browser protocol data.")
            return bytes(buffer)
    raise ValueError("The browser protocol frame is too large.")


def command_line(pid: int) -> tuple[str, ...]:
    try:
        return tuple(os.fsdecode(part) for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part)
    except (FileNotFoundError, ProcessLookupError):
        return ()


def terminate(process: Process) -> None:
    current = command_line(process.pid)
    if current and current != process.command:
        return  # The PID was reused by an unrelated process.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def managed(command: tuple[str, ...], state: Path, directory: Path) -> bool:
    return (
        len(command) >= 4
        and command[0] == sys.executable
        and command[2:4] == ("serve", str(state))
        and Path(command[1]).parent == directory
        and bool(re.fullmatch(
            r"(?:browser(?:-driver)?-[a-f0-9]{64}|browser_driver|browser_guest)\.py",
            Path(command[1]).name,
        ))
    )


def ensure(state: Path, helper: Path, deadline: float, env: Mapping[str, str]) -> Process:
    program = Path(__file__).resolve()
    command = (sys.executable, str(program), "serve", str(state), str(helper))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid_file = state / PID_NAME
    try:
        pid = int(pid_file.read_text())
        current = command_line(pid)
        if current == command:
            return Process(pid, command)
        if managed(current, state, program.parent):
            terminate(Process(pid, current))
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass
    (state / SOCKET_NAME).unlink(missing_ok=True)
    child = subprocess.Popen(
        command, env=env, start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    process = Process(child.pid, command)
    try:
        pid_file.write_text(str(child.pid))
        ready_by = time.monotonic() + min(3, remaining(deadline))
        while time.monotonic() < ready_by:
            if (state / SOCKET_NAME).exists():
                return process
            if child.poll() is not None:
                break
            time.sleep(.01)
        raise DriverUnavailable("The browser supervisor did not start.")
    except BaseException:
        terminate(process)
        raise


def exchange(state: Path, frame: bytes, deadline: float) -> dict[str, Json]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(min(2, remaining(deadline)))
        try:
            connection.connect(str(state / SOCKET_NAME))
        except OSError as error:
            raise DriverUnavailable("The browser supervisor is unavailable.") from error
        connection.settimeout(remaining(deadline))
        connection.sendall(frame)
        response = object_frame(read_frame(connection, deadline))
    if response.get("error"):
        raise RuntimeError("The browser operation could not be confirmed.")
    result = response.get("result")
    if not isinstance(result, dict):
        raise TypeError("Invalid browser result.")
    return result


def call(
    state: Path, helper: Path, command: dict[str, Json], deadline: float,
    env: Mapping[str, str], *, diagnostics: bool = False,
) -> dict[str, Json]:
    remaining(deadline)
    frame = json.dumps({"command": command, "deadline": deadline, "diagnostics": diagnostics}).encode() + b"\n"
    if len(frame) > MAX_FRAME:
        raise ValueError("The browser request is too large.")
    for attempt in range(2):
        process = ensure(state, helper, deadline, env)
        try:
            return exchange(state, frame, deadline)
        except DriverUnavailable:
            terminate(process)
            if attempt:
                raise
        except BaseException:
            # Retire the whole private worker group before releasing command.lock.
            # Never replay an action after sendall, even if its reply was lost.
            terminate(process)
            raise
    raise DriverUnavailable("The browser supervisor is unavailable.")


def relay(
    connection: socket.socket, worker: subprocess.Popen[bytes], frame: bytes, deadline: float,
) -> bool:
    assert worker.stdin is not None and worker.stdout is not None
    pending = memoryview(frame)
    output = bytearray()
    expires = time.monotonic() + remaining(deadline)
    with selectors.DefaultSelector() as poll:
        poll.register(connection, selectors.EVENT_READ, "caller")
        poll.register(worker.stdout, selectors.EVENT_READ, "result")
        poll.register(worker.stdin, selectors.EVENT_WRITE, "request")
        while len(output) <= MAX_FRAME:
            timeout = expires - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("The browser worker exceeded its deadline.")
            for key, _ in poll.select(timeout):
                if key.data == "caller":
                    raise ConnectionError("The browser caller disconnected or sent extra data.")
                if key.data == "request":
                    written = os.write(worker.stdin.fileno(), pending[:65536])
                    pending = pending[written:]
                    if not pending:
                        poll.unregister(worker.stdin)
                if key.data == "result":
                    chunk = os.read(worker.stdout.fileno(), 65536)
                    if not chunk:
                        raise ConnectionError("The browser worker exited without a result.")
                    output.extend(chunk)
                    if b"\n" in output:
                        if output[-1:] != b"\n" or output.count(b"\n") != 1 or len(output) > MAX_FRAME:
                            raise ValueError("Invalid browser worker frame.")
                        response = object_frame(bytes(output))
                        connection.settimeout(remaining(deadline))
                        connection.sendall(output)
                        return not bool(response.get("error"))
    raise ValueError("The browser result is too large.")


def serve(state: Path, worker_command: tuple[str, ...], *, idle_seconds: float = IDLE_SECONDS) -> None:
    if os.getpgrp() != os.getpid():
        raise RuntimeError("The browser supervisor must own a separate process group.")

    def stop(_signal: int = 0, _frame: object = None) -> None:
        # No Playwright call or Python unwinding is involved in the deadline path.
        # Chromium and the VNC processes belong to other process groups.
        os.killpg(os.getpid(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / SOCKET_NAME
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(1)
            server.settimeout(idle_seconds)
            worker = subprocess.Popen(
                worker_command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            assert worker.stdin is not None and worker.stdout is not None
            os.set_blocking(worker.stdin.fileno(), False)
            os.set_blocking(worker.stdout.fileno(), False)
            while True:
                connection, _ = server.accept()
                with connection:
                    frame = read_frame(connection, time.time() + 5)
                    message = object_frame(frame)
                    deadline = message.get("deadline")
                    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
                        raise TypeError("Invalid browser deadline.")
                    remaining(deadline)
                    if not relay(connection, worker, frame, deadline):
                        return
    except (OSError, ValueError, TypeError, RuntimeError):
        return
    finally:
        stop()


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "serve":
        raise SystemExit("Usage: browser_driver.py serve STATE BROWSER_HELPER")
    serve(Path(sys.argv[2]), (sys.executable, sys.argv[3], "worker"))
