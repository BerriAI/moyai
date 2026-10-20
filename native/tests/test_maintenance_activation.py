"""Local activation transport boundaries; full application proof is separate."""

from __future__ import annotations

import asyncio
import json
import os
import socket

import pytest

from moyai import maintenance_activation as activation


@pytest.fixture
def staged(tmp_path, monkeypatch):
    # Unix socket paths have a much smaller limit than ordinary file paths.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="mwa-") as directory:
        path = Path(directory).resolve() / "control.sock"
        monkeypatch.setenv("MOYAI_MAINTENANCE_SOCKET", str(path))
        monkeypatch.setenv("MOYAI_BUILD_SHA", "a" * 40)
        monkeypatch.setenv("MOYAI_RUNTIME_ROLE", "coordinator")
        monkeypatch.setenv("RENDER_SERVICE_ID", "srv-local")
        monkeypatch.setenv("MOYAI_MAINTENANCE_TOKEN", "c" * 32)
        monkeypatch.setenv("RENDER_MIGRATION_STAGE", "true")
        stopped = []
        control = activation.StagedActivation(lambda: stopped.append(True))
        calls = []
        monkeypatch.setattr(activation, "verify_guard", lambda guard: calls.append(guard))
        durable = []
        def accept(receipt):
            activation.verify_guard(receipt.guard)
            durable.append(receipt)
        monkeypatch.setattr(activation, "accept_durable", accept)
        monkeypatch.setattr(activation, "durable_receipt", lambda: durable[0] if durable else None)
        body = {
            "command": "activate", "expected": control.identity.model_dump(),
            "request_id": "maintenance:example",
            "guard": {"product_schema": "product", "native_schema": "native",
                      "generation": 3, "request_fingerprint": "b" * 64},
        }
        yield control, body, stopped, calls


async def test_acceptance_survives_the_staging_listener_and_fresh_shell(staged):
    control, body, stopped, calls = staged
    async with control.lifespan(None):
        status = await activation.request({"command": "status"})
        assert status["identity"] == body["expected"]
        reply = await activation.request(body)
        assert reply["state"] == "accepted"
    assert stopped == [True] and len(calls) == 1
    assert not control.path.exists()
    assert os.environ["RENDER_MIGRATION_STAGE"] == "true"
    receipt = activation.apply_accepted_activation()
    assert receipt is not None and receipt.request_id == body["request_id"]
    assert os.environ["RENDER_MIGRATION_STAGE"] == "false"


@pytest.mark.parametrize("fault", ["instance", "build", "role", "service", "token", "unicode", "extra"])
async def test_wrong_identity_and_invalid_commands_do_not_activate(staged, fault):
    control, body, stopped, calls = staged
    if fault in {"instance", "build", "role", "service", "token"}:
        body["expected"][fault] = {"instance": "f" * 32, "build": "f" * 40,
                                    "role": "worker", "service": "srv-other", "token": "f" * 32}[fault]
    elif fault == "unicode":
        body["request_id"] = "bad\r\nheader"
    else:
        body["unchecked"] = True
    async with control.lifespan(None):
        reply = await activation.request(body)
        assert reply["ok"] is False
    assert not stopped and not calls and activation.accepted_receipt() is None


async def test_changed_pause_or_schema_does_not_persist_acceptance(staged, monkeypatch):
    control, body, stopped, _ = staged
    def reject(_guard):
        raise ValueError("Changed pause.")
    monkeypatch.setattr(activation, "verify_guard", reject)
    async with control.lifespan(None):
        assert (await activation.request(body))["ok"] is False
    assert not stopped and activation.accepted_receipt() is None


async def test_identical_request_is_idempotent_and_another_request_is_rejected(staged):
    control, body, _, calls = staged
    async with control.lifespan(None):
        replies = await asyncio.gather(activation.request(body), activation.request(body))
        assert replies[0] == replies[1] and replies[0]["ok"] is True
        assert (await activation.request({**body, "request_id": "maintenance:different"}))["ok"] is False
    assert len(calls) == 1


async def test_lost_reply_does_not_cancel_accepted_activation(staged):
    control, body, stopped, _ = staged
    async with control.lifespan(None):
        _, writer = await asyncio.open_unix_connection(str(control.path))
        writer.write(json.dumps(body).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        async with asyncio.timeout(2):
            while not stopped:
                await asyncio.sleep(.01)
    assert activation.accepted_receipt() is not None


async def test_empty_filesystem_restart_recovers_durable_activation(staged):
    control, body, _, _ = staged
    async with control.lifespan(None):
        assert (await activation.request(body))["ok"] is True
    (control.path.parent / "activated.json").unlink()
    recovered = activation.apply_accepted_activation()
    assert recovered is not None and recovered.request_id == body["request_id"]
    assert os.environ["RENDER_MIGRATION_STAGE"] == "false"


async def test_cancelled_committed_write_still_requests_normal_startup(staged, monkeypatch):
    import threading
    control, body, stopped, _ = staged
    entered, release = threading.Event(), threading.Event()
    def accept(_receipt):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(activation, "accept_durable", accept)
    async with control.lifespan(None):
        client = asyncio.create_task(activation.request(body))
        assert await asyncio.to_thread(entered.wait, 2)
        task = next(iter(control.clients))
        task.cancel()
        release.set()
        await asyncio.gather(task, client, return_exceptions=True)
    assert stopped and control.receipt is not None


async def test_container_restart_cannot_use_another_builds_receipt(staged, monkeypatch):
    control, body, _, _ = staged
    async with control.lifespan(None):
        assert (await activation.request(body))["ok"] is True
    monkeypatch.setenv("MOYAI_BUILD_SHA", "e" * 40)
    with pytest.raises(ValueError, match="different container"):
        activation.apply_accepted_activation()
    assert os.environ["RENDER_MIGRATION_STAGE"] == "true"


async def test_unrecorded_socket_is_not_removed(staged):
    control, _, _, _ = staged
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(control.path))
        with pytest.raises(FileNotFoundError):
            async with control.lifespan(None):
                pytest.fail("An unrecorded socket was adopted.")
        assert control.path.exists()


async def test_live_recorded_socket_is_not_removed(staged):
    control, _, _, _ = staged
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(control.path))
        listener.listen()
        node = control.path.lstat()
        activation.write_private(control.path.parent / "socket.json", json.dumps({
            "device": node.st_dev, "inode": node.st_ino,
        }))
        with pytest.raises(ValueError, match="live control socket"):
            async with control.lifespan(None):
                pytest.fail("A live socket was adopted.")
        assert control.path.exists()


async def test_symlinked_receipt_is_not_read(staged, tmp_path):
    control, body, _, _ = staged
    target = tmp_path / "receipt.json"
    target.write_text(json.dumps({
        "identity": body["expected"], "request_id": body["request_id"], "guard": body["guard"],
    }))
    (control.path.parent / "activated.json").symlink_to(target)
    with pytest.raises(OSError):
        activation.accepted_receipt()
