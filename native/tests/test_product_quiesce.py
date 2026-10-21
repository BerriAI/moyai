"""Protocol regressions; actual application rehearsals remain separately required."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_product_lifecycle import lifecycle

from moyai.product_lifecycle import ProductRoleMiddleware
from moyai.product_role_authority import ProductRoleAuthority
from moyai.product_role_control import RoleControl, request


def role(name="api", owner=None):
    life = lifecycle(name, owner=owner)
    life.runtime.product.authority = ProductRoleAuthority(
        "test_release", name, 123, datetime(2026, 10, 17, tzinfo=UTC), "a" * 64, "b" * 64,
    )
    return life


async def test_quiescence_finishes_accepted_http_and_refuses_new_admission():
    life, app = role(), FastAPI()
    app.add_middleware(ProductRoleMiddleware, lifecycle=life)
    accepted, release = asyncio.Event(), asyncio.Event()
    effects = []

    @app.post("/api/save")
    async def save():
        accepted.set()
        await release.wait()
        effects.append("saved")
        return {"saved": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://local") as client:
        pending = asyncio.create_task(client.post("/api/save"))
        await accepted.wait()
        q = life.quiescence
        q.begin(q.identity(), "release-1")
        assert not life.healthy and q.reconnect.is_set()
        assert (await client.post("/api/save")).status_code == 503
        assert (await q.status())["active_requests"] == 1
        waiter = asyncio.create_task(q.close())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        assert not waiter.done() and not pending.cancelled() and q.state == "quiescing"
        release.set()
        assert (await pending).status_code == 200
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert (await q.status())["state"] == "drained" and effects == ["saved"]
        q.begin(q.identity(), "release-1")  # Lost acknowledgments can be reconciled.
        with pytest.raises(ValueError, match="Another"):
            q.begin(q.identity(), "release-2")


async def test_socket_is_private_rejects_stale_identity_and_survives_lost_reply():
    # Unix socket paths are limited to 104 bytes on macOS.
    with tempfile.TemporaryDirectory(prefix="moyai-drain-") as directory:
        path = Path(directory) / "control.sock"
        life = role()
        q = life.quiescence
        async with RoleControl(path, q):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            status = await request(path, {"command": "status"})
            stale = {**status["identity"], "owner_started": "2020-01-01T00:00:00Z"}
            assert not (await request(path, {
                "command": "quiesce", "expected": stale, "request_id": "stale",
            }))["ok"]
            assert not q.requested
            _reader, writer = await asyncio.open_unix_connection(str(path))
            writer.write(json.dumps({
                "command": "quiesce", "expected": status["identity"], "request_id": "live",
            }).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            async with asyncio.timeout(2):
                while not q.requested:
                    await asyncio.sleep(.01)
            await q.close()
            observed = await request(path, {"command": "status"})
            assert observed["state"] == "drained" and observed["request_id"] == "live"
            assert observed["identity"] == status["identity"]
        assert not path.exists()


async def test_socket_never_overwrites_a_live_socket_or_shared_directory():
    with tempfile.TemporaryDirectory(prefix="moyai-drain-") as directory:
        path = Path(directory) / "control.sock"
        async with RoleControl(path, role().quiescence):
            inode = path.stat().st_ino
            with pytest.raises(OSError):
                async with RoleControl(path, role().quiescence):
                    pytest.fail("An existing controller must not be replaced.")
            assert path.stat().st_ino == inode
        os.chmod(directory, 0o755)
        with pytest.raises(ValueError, match="owner-only"):
            async with RoleControl(path, role().quiescence):
                pytest.fail("An insecure directory must be rejected.")


async def test_worker_and_wrong_process_cannot_claim_drained():
    worker = role("worker").quiescence
    with pytest.raises(ValueError, match="durable fleet seal"):
        worker.begin(worker.identity(), "worker")
    first, replacement = role().quiescence, role().quiescence
    with pytest.raises(ValueError, match="identity"):
        replacement.begin(first.identity(), "restarted")
    assert not replacement.requested and not worker.requested


async def test_quiescence_failure_never_reports_drained():
    def failed():
        raise RuntimeError("private-database-string")

    life = role(owner=failed)
    life.quiescence.begin(life.quiescence.identity(), "will-fail")
    with pytest.raises(RuntimeError):
        await life.quiescence.close()
    assert life.quiescence.state == "failed"
    with tempfile.TemporaryDirectory(prefix="moyai-drain-") as directory:
        path = Path(directory) / "control.sock"
        async with RoleControl(path, life.quiescence):
            status = await request(path, {"command": "status"})
            assert not status["ok"] and "private" not in json.dumps(status)


async def test_broker_planned_drain_finishes_request_and_followup_cleanup():
    from app.broker_ownership import BrokerRequests

    entered, release, cleanup_entered, cleanup_release = (asyncio.Event() for _ in range(4))
    events = []
    slots = SimpleNamespace(_maintenance=set(), check=lambda: None)

    async def cleanup():
        cleanup_entered.set()
        await cleanup_release.wait()
        events.append("receipt")

    async def accepted():
        entered.set()
        await release.wait()
        followup = asyncio.create_task(cleanup())
        slots._maintenance.add(followup)
        followup.add_done_callback(slots._maintenance.discard)
        events.append("response")

    class Maintenance:
        task = None
        owned = True

        async def close(self):
            events.append("release-leader")
            self.owned = False

    broker = BrokerRequests(
        SimpleNamespace(database=SimpleNamespace(check_owner=lambda: None)), slots, Maintenance(),
    )
    accepted_task = asyncio.create_task(accepted())
    broker.tasks.add(accepted_task)
    accepted_task.add_done_callback(broker.tasks.discard)
    await entered.wait()
    draining = asyncio.create_task(broker.quiesce())
    await asyncio.sleep(0)
    assert broker.draining and not accepted_task.cancelled() and not draining.done()
    release.set()
    await cleanup_entered.wait()
    await asyncio.sleep(0)
    assert events == ["response"] and not draining.done()
    cleanup_release.set()
    await draining
    assert events == ["response", "receipt", "release-leader"]


async def test_coordinator_keeps_singleton_until_accepted_background_operation_finishes():
    from app.coordinator_handoff import CoordinatorHandoff, coordinator_owner

    life = role("coordinator")
    active, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lock = {"held": False}
    events = []

    def acquire():
        if lock["held"]:
            return False
        lock["held"] = True
        return True

    def unlock():
        assert events == ["settled"]
        lock["held"] = False
        events.append("unlocked")

    db = life.runtime.product.store.database
    db.schema, db.acquire_coordinator, db.release_coordinator = "test_release", acquire, unlock
    life.runtime.product.store.rows = lambda *_: []

    async def connect():
        pass

    manager = SimpleNamespace(
        settings=SimpleNamespace(temporal_startup_timeout_seconds=2), connect_temporal=connect,
    )
    life.handoff = CoordinatorHandoff(life.runtime.product.store, manager)

    @asynccontextmanager
    async def background():
        assert coordinator_owner.get() == ("test_release", 123)
        active.set()
        try:
            yield
        finally:
            closing.set()
            await release.wait()
            assert coordinator_owner.get() == ("test_release", 123) and lock["held"]
            events.append("settled")

    retired = []
    life.retire = lambda: retired.append(True)
    async with life.handoff.lifespan(background):
        await active.wait()
        life.watch_task = asyncio.create_task(life.watch())
        life.quiescence.begin(life.quiescence.identity(), "coordinator-release")
        await closing.wait()
        assert life.handoff.status == "draining" and lock["held"]
        assert not acquire() and life.quiescence.state == "quiescing"
        release.set()
        await life.quiescence.close()
        # A planned, completed handoff is not an ownership-loss crash.
        await asyncio.sleep(.55)
        await life.close_watch()
        assert not life.failed and retired == []
    assert events == ["settled", "unlocked"] and life.quiescence.state == "drained"
