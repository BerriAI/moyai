"""Role routing/drain regressions; not a substitute for four-process app proof."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from moyai.product_lifecycle import ProductRoleLifecycle, ProductRoleMiddleware, ReceiptMaintenance
from moyai.settings import Settings


def lifecycle(role, *, owner=None):
    def check():
        if owner:
            owner()
    product = SimpleNamespace(
        store=SimpleNamespace(database=SimpleNamespace(owner_pid=123, check_owner=check)),
        settings=SimpleNamespace(moyai_build_sha="synthetic-build"),
    )
    runtime = SimpleNamespace(product=product, product_config=SimpleNamespace(role=role))
    life = ProductRoleLifecycle(runtime)
    life.ready = True
    return life


@pytest.mark.parametrize(("role", "allowed", "forbidden"), [
    ("api", "/api/example", "/broker/native/v1/responses"),
    ("coordinator", "/hooks/example", "/api/example"),
    ("broker", "/broker/native/v1/responses", "/api/example"),
    ("worker", None, "/api/example"),
])
async def test_role_routing_and_release_probe_identity(role, allowed, forbidden):
    app, life = FastAPI(), lifecycle(role)
    app.add_middleware(ProductRoleMiddleware, lifecycle=life)
    calls = []

    @app.get("/{path:path}")
    async def endpoint(path: str):
        calls.append(path)
        return {"handler": True}

    # Isolate role routing from the separately tested canonical broker ownership
    # wrapper, whose real PostgreSQL owner/slots are part of assembled proof.
    if role == "broker":
        life.broker = SimpleNamespace(draining=False, tasks=set(), check=lambda: None)
    if role == "coordinator":
        life.handoff = SimpleNamespace(status="standby", healthy=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/health")
        assert response.json() == {"status": "ok"}
        assert response.headers["x-moyai-role"] == role
        assert response.headers["x-moyai-core"] == "pydantic-v1"
        assert response.headers["x-moyai-owner"] == "123"
        assert not calls  # Health is not the native UI's catch-all route.
        assert (await client.get(forbidden)).status_code == 404
        assert not calls
        if allowed:
            assert (await client.get(allowed)).status_code == 200
        life.ready = False
        assert (await client.get(allowed or "/health")).status_code == 503


async def test_lost_owner_cannot_report_ready_or_disclose_connection_errors():
    def lost():
        raise RuntimeError("private-connection-string")
    app, life = FastAPI(), lifecycle("api", owner=lost)
    app.add_middleware(ProductRoleMiddleware, lifecycle=life)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/health")
    assert response.status_code == 503
    assert "private-connection-string" not in response.text
    retired = []
    life.retire = lambda: retired.append(True)
    await life.watch()
    assert life.failed and not life.healthy and retired == [True]


async def test_adoption_pool_contention_does_not_retire_a_valid_owner():
    from app.database import DatabaseBusyError

    life = lifecycle("api")
    attempted = asyncio.Event()
    retired = []

    async def busy():
        attempted.set()
        raise DatabaseBusyError("Local contention.")

    life.admission = SimpleNamespace(refresh=busy)
    life.retire = lambda: retired.append(True)
    watching = asyncio.create_task(life.watch())
    await attempted.wait()
    await asyncio.sleep(0)
    assert life.admission_busy and not life.failed and retired == []
    watching.cancel()
    await asyncio.gather(watching, return_exceptions=True)


async def test_receipt_services_all_drain_before_cancellation_propagates():
    completed = []

    class Service:
        def __init__(self, name, cancel=False):
            self.name, self.cancel = name, cancel

        def start(self):
            completed.append("start-" + self.name)

        async def close(self):
            completed.append("close-" + self.name)
            if self.cancel:
                raise asyncio.CancelledError()

    group = ReceiptMaintenance(Service("legacy"), Service("native", cancel=True))
    group.start()
    with pytest.raises(asyncio.CancelledError):
        await group.close()
    assert completed == ["start-legacy", "start-native", "close-native", "close-legacy"]


def test_only_explicit_product_broker_workers_can_omit_the_gateway_key(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, gateway_key="")
    with pytest.raises(ValueError, match="LITELLM_API_KEY"):
        settings.validate_worker(brokered=True)
    settings.product_mode = "existing"
    with pytest.raises(ValueError, match="LITELLM_API_KEY"):
        settings.validate_worker()
    settings.validate_worker(brokered=True)
    settings.gateway_url = "http://untrusted.invalid/v1"
    with pytest.raises(ValueError, match="HTTPS"):
        settings.validate_worker(brokered=True)


@pytest.mark.parametrize("starts_polling", [True, False])
async def test_worker_health_waits_for_native_polling_and_times_out_cleanly(monkeypatch, starts_polling):
    import moyai.worker as worker_module
    from moyai.fleet_admission import FleetAdmission

    life = lifecycle("worker")
    life.ready = False
    life.runtime.product.settings.temporal_startup_timeout_seconds = .15
    worker = SimpleNamespace(is_running=False)
    entered, ready, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []

    @asynccontextmanager
    async def waiting_worker(_runtime):
        entered.set()
        try:
            yield worker
        finally:
            closed.append(True)

    async def connect():
        return None

    life.runtime.connect = connect
    life.runtime.product_inference = None
    life.runtime.settings = SimpleNamespace(validate_worker=lambda **_: None)
    # This fixture deliberately has no canonical PostgreSQL connection. Fleet
    # admission and sealing are exercised against PostgreSQL separately.
    async def refresh(self):
        return True
    monkeypatch.setattr(FleetAdmission, "start", refresh)
    monkeypatch.setattr(FleetAdmission, "refresh", refresh)
    monkeypatch.setattr(worker_module, "make_worker", waiting_worker)

    async def serve():
        async with life.lifespan():
            ready.set()
            await finish.wait()

    task = asyncio.create_task(serve())
    await entered.wait()
    assert not ready.is_set() and not life.healthy and life.watch_task is None
    if starts_polling:
        worker.is_running = True
        await asyncio.wait_for(ready.wait(), 1)
        assert life.healthy and life.watch_task is not None
        finish.set()
        await task
    else:
        with pytest.raises(TimeoutError):
            await task
        assert not ready.is_set() and not life.ready and life.watch_task is None
    assert closed == [True]
