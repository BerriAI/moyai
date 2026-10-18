"""Native role lifetimes using the product's existing ownership protocols."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from app.db import database as source_io
from starlette.responses import JSONResponse

from .models import now
from .product_quiesce import ProductQuiescence

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)


class ReceiptMaintenance:
    """Recover both ledgers under one canonical broker-maintenance owner."""

    def __init__(self, *services: Any):
        self.services = services

    def start(self) -> None:
        for service in self.services:
            service.start()

    async def close(self) -> None:
        errors = []
        for service in reversed(self.services):
            try:
                await service.close()
            except BaseException as error:  # noqa: BLE001 - drain all services, then re-raise
                errors.append(error)
        if errors:
            raise errors[0]


class _CoordinatorClient:
    """The existing handoff protocol needs settings and a Temporal connection."""

    def __init__(self, runtime: Runtime):
        self.runtime, self.settings = runtime, runtime.product_settings

    async def connect_temporal(self):
        return await self.runtime.connect()


class ProductRoleLifecycle:
    def __init__(self, runtime: Runtime):
        if runtime.product is None or runtime.product_config is None:
            raise ValueError("Product roles require canonical resources.")
        if runtime.product_config.role not in {"api", "coordinator", "broker", "worker"}:
            raise ValueError("Use the standalone preview lifecycle for local combined execution.")
        self.runtime = runtime
        self.role = runtime.product_config.role
        self.ready = False
        self.closing = False
        self.failed = False
        self.handoff: Any = None
        self.broker: Any = None
        self.worker: Any = None
        self.watch_task: asyncio.Task[None] | None = None
        self.retire: Any = None
        self.quiescence = ProductQuiescence(self)
        self.admission: Any = None
        self.admission_busy = False

    @property
    def healthy(self) -> bool:
        if not self.ready or self.failed or self.closing or self.quiescence.requested:
            return False
        if self.handoff is not None and not self.handoff.healthy:
            return False
        return self.broker is None or not self.broker.draining

    def headers(self) -> dict[str, str]:
        product = self.runtime.product
        assert product is not None
        owner = product.store.database.owner_pid if product.store is not None else ""
        headers = {
            "X-Moyai-Build": product.settings.moyai_build_sha,
            "X-Moyai-Owner": str(owner), "X-Moyai-Role": self.role,
            "X-Moyai-Rolling-Release": "1", "X-Moyai-Core": "pydantic-v1",
            "Cache-Control": "no-store",
        }
        if self.role == "api":
            headers["X-Moyai-API-Release"] = "1"
        if self.role == "coordinator":
            headers["X-Moyai-Coordinator-State"] = self.handoff.status if self.handoff else "starting"
        if self.role == "broker":
            headers["X-Moyai-Broker-Draining"] = str(not self.healthy).lower()
        return headers

    @asynccontextmanager
    async def coordinator_background(self):
        # CoordinatorHandoff sets its owner ContextVar before entering here and
        # retains the singleton until every task and Slack receipt has drained.
        from .worker import dispatcher, reconcile_workflows
        tasks: list[asyncio.Task[Any]] = []
        slack = self.runtime.product_slack
        try:
            if slack is not None:
                slack.start()
            tasks = [
                asyncio.create_task(
                    dispatcher(self.runtime, stop=self.quiescence.background_stop), name="native-dispatch",
                ),
                asyncio.create_task(
                    reconcile_workflows(self.runtime, stop=self.quiescence.background_stop),
                    name="native-reconciliation",
                ),
            ]
            yield
        finally:
            if not self.quiescence.requested:
                for task in tasks:
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            if slack is not None:
                await slack.close()
            if self.quiescence.requested:
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

    async def start_broker(self) -> None:
        from app.broker_ownership import BrokerMaintenance, BrokerRequests, BrokerWork
        from app.spend import Spend

        from .product_connections import CommittedSourceWrites

        product, inference = self.runtime.product, self.runtime.product_inference
        assert product is not None
        if inference is None or inference.remote or inference.recovery is None:
            raise RuntimeError("The broker requires its admitted inference service.")
        # These canonical constructors may read PostgreSQL. Never run them on
        # the event loop, and do not construct a legacy execution manager.
        work = await source_io(BrokerWork, product.store)
        spend = await source_io(
            Spend, product.store, product.settings, product.security, CommittedSourceWrites(),
        )
        maintenance = BrokerMaintenance(
            product.store, None, ReceiptMaintenance(spend.recovery, inference.recovery),
        )
        self.broker = BrokerRequests(work, inference.slots, maintenance)
        await self.broker.start()

    async def close_broker(self) -> None:
        if self.broker is not None:
            try:
                await self.broker.close()
            finally:
                await self.broker.drain()

    async def watch(self) -> None:
        product = self.runtime.product
        assert product is not None
        while True:
            try:
                await source_io(product.store.database.check_owner)
                if self.admission is not None:
                    from app.database import DatabaseBusyError
                    try:
                        await self.admission.refresh()
                    except DatabaseBusyError:
                        # Hot adoption may wait for the canonical writer/pool.
                        # Contention is not loss of this process's owner. The
                        # controller cannot seal/promote an unregistered member.
                        if not self.admission_busy:
                            log.warning("Fleet admission is waiting for database capacity.")
                        self.admission_busy = True
                    else:
                        self.admission_busy = False
                        if self.admission.sealed and not self.quiescence.requested:
                            self.quiescence.begin(self.quiescence.identity(), self.admission.request_id)
                if self.broker is not None and self.broker.failed:
                    raise RuntimeError("Broker ownership ended.")
                if self.handoff is not None and not self.handoff.healthy and (
                    not self.quiescence.requested or self.handoff.status == "failed"
                ):
                    raise RuntimeError("Coordinator ownership ended.")
                if self.worker is not None and not self.worker.is_running and not self.quiescence.requested:
                    raise RuntimeError("Native worker polling ended.")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - every ownership failure must retire the process
                self.failed = True
                log.error("Canonical runtime ownership failed; this process must drain and retire.")
                if self.retire is not None:
                    self.retire()
                return
            await asyncio.sleep(.5)

    async def close_watch(self) -> None:
        if self.watch_task is not None:
            self.watch_task.cancel()
            await asyncio.gather(self.watch_task, return_exceptions=True)
            self.watch_task = None

    @asynccontextmanager
    async def lifespan(self):
        product = self.runtime.product
        assert product is not None
        async with AsyncExitStack() as stack:
            from .fleet_admission import FleetAdmission
            self.admission = FleetAdmission(self)
            await self.admission.start()
            async with asyncio.timeout(product.settings.temporal_startup_timeout_seconds):
                await self.runtime.connect()
            if self.role == "worker":
                from .worker import make_worker
                inference = self.runtime.product_inference
                self.runtime.settings.validate_worker(brokered=bool(inference and inference.remote))
                self.worker = await stack.enter_async_context(make_worker(self.runtime))
                # Plugins may await owner registration before native polling.
                # A live startup is not an ownership failure or a ready worker.
                async with asyncio.timeout(product.settings.temporal_startup_timeout_seconds):
                    while not self.worker.is_running:
                        await asyncio.sleep(.01)
            elif self.role == "broker":
                # Register cleanup before startup so partial acquisition drains.
                stack.push_async_callback(self.close_broker)
                await self.start_broker()
            elif self.role == "coordinator":
                from app.coordinator_handoff import CoordinatorHandoff
                self.handoff = CoordinatorHandoff(product.store, _CoordinatorClient(self.runtime))
                await stack.enter_async_context(self.handoff.lifespan(self.coordinator_background))
            self.ready = True
            self.watch_task = asyncio.create_task(self.watch(), name="native-role-owner")
            stack.push_async_callback(self.close_watch)
            try:
                yield
            finally:
                self.closing, self.ready = True, False
                await self.quiescence.close()


class ProductRoleMiddleware:
    """Role routing and health identity, before any native request handler."""

    def __init__(self, app: Any, lifecycle: ProductRoleLifecycle):
        self.app, self.lifecycle = app, lifecycle

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        if scope["type"] == "http":
            # Include the broker ownership/database gate in broker preparation.
            # This is server state, never a timestamp accepted from a header.
            scope.setdefault("state", {})["moyai_received_at"] = now()
        life, path = self.lifecycle, scope.get("path", "")
        health = path == "/health"
        allowed = health or (
            life.role == "api" and not path.startswith(("/broker/", "/hooks/"))
            or life.role == "coordinator" and path.startswith("/hooks/")
            or life.role == "broker" and path.startswith("/broker/native/")
        )
        if not allowed or not life.healthy:
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            return await JSONResponse(
                {"detail": "This runtime role is unavailable for this request."},
                status_code=503 if not life.healthy else 404, headers=life.headers(),
            )(scope, receive, send)
        if health and scope["type"] == "http":
            product = life.runtime.product
            assert product is not None
            try:
                await source_io(product.store.database.check_owner)
            except Exception:  # noqa: BLE001 - health never exposes connection/configuration details
                return await JSONResponse(
                    {"detail": "Canonical runtime ownership is unavailable."},
                    status_code=503, headers=life.headers(),
                )(scope, receive, send)
            # Existing release probes expect this exact body. Core identity is
            # carried separately, alongside the canonical owner and role.
            return await JSONResponse({"status": "ok"}, headers=life.headers())(scope, receive, send)
        if life.role == "broker":
            from app.broker_ownership import BrokerOwnershipMiddleware
            downstream = BrokerOwnershipMiddleware(self.app, life.broker)
        else:
            downstream = self.app
        task = asyncio.current_task()
        assert task is not None
        life.quiescence.requests.add(task)
        scope.setdefault("state", {})["moyai_reconnect"] = life.quiescence.reconnect
        try:
            await downstream(scope, receive, send)
        finally:
            life.quiescence.requests.discard(task)
            life.quiescence.requests_changed.set()
