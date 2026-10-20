"""Filesystem/process regressions; the real application restart is rehearsed separately."""

from __future__ import annotations

import asyncio
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from moyai.product_role_control import RoleControl


@pytest.fixture
def path():
    # Keep AF_UNIX names within macOS's 104-byte limit.
    with tempfile.TemporaryDirectory(prefix="moyai-control-") as directory:
        yield Path(directory) / "role.sock"


def orphan(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
    return path.lstat().st_ino


async def test_recovers_refused_socket_and_keeps_stable_lock_inode(path):
    orphan(path)
    async with RoleControl(path, None):
        assert stat.S_ISSOCK(path.lstat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        lock_inode = path.with_name(path.name + ".lock").stat().st_ino
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(str(path))
    assert not path.exists()
    async with RoleControl(path, None):
        assert path.with_name(path.name + ".lock").stat().st_ino == lock_inode


async def test_preserves_legacy_live_listener_without_lock(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        path.chmod(0o600)
        listener.listen()
        inode = path.stat().st_ino
        with pytest.raises(FileExistsError, match="live listener"):
            async with RoleControl(path, None):
                pytest.fail("A legacy listener must survive.")
        assert path.stat().st_ino == inode
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(str(path))


@pytest.mark.parametrize("kind", ["regular", "symlink", "directory", "fifo"])
async def test_preserves_non_socket_paths(path, kind):
    if kind == "regular":
        path.write_text("keep")
    elif kind == "symlink":
        target = path.with_name("target")
        orphan(target)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = path.lstat()
    with pytest.raises(ValueError, match="owner-owned socket"):
        async with RoleControl(path, None):
            pytest.fail("An unrelated path must survive.")
    assert path.lstat() == before


async def test_preserves_foreign_socket(path, monkeypatch):
    orphan(path)
    real_stat = os.stat

    def observed(name, *args, **kwargs):
        node = real_stat(name, *args, **kwargs)
        if name == path.name:
            values = list(node)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return node

    monkeypatch.setattr(os, "stat", observed)
    with pytest.raises(ValueError, match="owner-owned socket"):
        async with RoleControl(path, None):
            pytest.fail("Foreign ownership must fail closed.")
    assert path.exists()


@pytest.mark.parametrize("failure", [TimeoutError, PermissionError, FileNotFoundError])
async def test_ambiguous_connect_error_preserves_socket(path, monkeypatch, failure):
    inode = orphan(path)

    def fail(*args):
        raise failure("ambiguous probe")

    monkeypatch.setattr(socket.socket, "connect", fail)
    with pytest.raises(failure):
        async with RoleControl(path, None):
            pytest.fail("Only connection refusal permits recovery.")
    assert path.stat().st_ino == inode


async def test_changed_inode_during_probe_is_never_removed(path, monkeypatch):
    orphan(path)
    replacement_path = path.with_name("replacement")
    orphan(replacement_path)
    replacement_inode = replacement_path.stat().st_ino

    def replace(*args):
        replacement_path.replace(path)
        raise ConnectionRefusedError()

    monkeypatch.setattr(socket.socket, "connect", replace)
    with pytest.raises(ValueError, match="changed"):
        async with RoleControl(path, None):
            pytest.fail("A newly installed socket must survive.")
    assert path.stat().st_ino == replacement_inode


@pytest.mark.parametrize("kind", ["symlink", "regular", "socket"])
async def test_cleanup_preserves_replaced_path(path, kind):
    async with RoleControl(path, None):
        path.unlink()
        if kind == "symlink":
            path.symlink_to(path.with_name("absent"))
        elif kind == "regular":
            path.write_text("keep")
        else:
            orphan(path)
        replacement = path.lstat()
    assert path.lstat() == replacement


@pytest.mark.parametrize("kind", ["symlink", "shared", "hardlink", "fifo"])
async def test_rejects_unsafe_lock_file_without_removing_orphan(path, kind):
    inode = orphan(path)
    lock = path.with_name(path.name + ".lock")
    if kind == "symlink":
        lock.symlink_to(path.with_name("absent"))
    elif kind == "shared":
        lock.touch(mode=0o644)
    elif kind == "hardlink":
        target = path.with_name("lock-source")
        target.touch(mode=0o600)
        os.link(target, lock)
    else:
        os.mkfifo(lock, 0o600)
    with pytest.raises((ValueError, OSError)):
        async with RoleControl(path, None):
            pytest.fail("An unsafe lock cannot authorize recovery.")
    assert path.stat().st_ino == inode


async def test_changed_lock_inode_during_probe_preserves_orphan(path, monkeypatch):
    inode = orphan(path)

    def replace(*args):
        replacement = path.with_name("new-lock")
        replacement.touch(mode=0o600)
        replacement.replace(path.with_name(path.name + ".lock"))
        raise ConnectionRefusedError()

    monkeypatch.setattr(socket.socket, "connect", replace)
    with pytest.raises(ValueError, match="lock changed"):
        async with RoleControl(path, None):
            pytest.fail("Replaced locks cannot authorize recovery.")
    assert path.stat().st_ino == inode


async def test_startup_failure_releases_lock_and_owned_socket(path, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("server failed")

    with monkeypatch.context() as patch:
        patch.setattr(asyncio, "start_unix_server", fail)
        with pytest.raises(RuntimeError, match="server failed"):
            async with RoleControl(path, None):
                pytest.fail("Startup must fail.")
    assert not path.exists()
    async with RoleControl(path, None):
        assert path.exists()


CHILD = """
import asyncio,sys
from pathlib import Path
from moyai.product_role_control import RoleControl
async def main():
    if len(sys.argv)>2:
        original=asyncio.start_unix_server
        async def hold(*args,**kwargs):
            print("BOUND",flush=True)
            await asyncio.Event().wait()
        asyncio.start_unix_server=hold
    try:
        async with RoleControl(Path(sys.argv[1]),None):
            print("READY",flush=True)
            await asyncio.Event().wait()
    except BlockingIOError:
        print("LOCKED",flush=True)
asyncio.run(main())
"""


def child(path, *, before_listen=False):
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, str(path), *(["hold"] if before_listen else [])],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def line(process):
    assert select.select([process.stdout], [], [], 10)[0], "Child did not reach its boundary."
    return process.stdout.readline().strip()


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=10)


@pytest.mark.parametrize("before_listen", [False, True])
async def test_process_lock_protects_startup_and_sigkill_releases_it(path, before_listen):
    first = child(path, before_listen=before_listen)
    try:
        assert line(first) == ("BOUND" if before_listen else "READY")
        inode = path.stat().st_ino
        competitor = child(path)
        try:
            assert line(competitor) == "LOCKED"
            assert competitor.wait(timeout=10) == 0
            assert path.stat().st_ino == inode
        finally:
            stop(competitor)
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=10)
        assert path.stat().st_ino == inode
        # The lock is kernel-released even though __aexit__ never ran.
        async with RoleControl(path, None):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(str(path))
    finally:
        stop(first)


@pytest.mark.parametrize("cancel", [False, True])
async def test_lock_held_until_existing_clients_finish(path, cancel):
    release = asyncio.Event()
    control = RoleControl(path, None)
    await control.__aenter__()
    waiter = asyncio.create_task(release.wait())
    control.clients.add(waiter)
    closing = asyncio.create_task(control.__aexit__())
    try:
        await asyncio.sleep(0)
        if cancel:
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        with pytest.raises(BlockingIOError):
            async with RoleControl(path, None):
                pytest.fail("Cleanup still owns the lifetime lock.")
    finally:
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
    async with RoleControl(path, None):
        assert path.exists()
