"""Keep a sealed service generation sealed across process restarts."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from app.db import database

from .fleet_registry import SCHEMA, FleetAdmissionError, FleetRegistry

if TYPE_CHECKING:
    from .product_lifecycle import ProductRoleLifecycle

BUILD_REVISION_FILE = Path("/app/build-revision")


def service_identity(source_sha: str, *, local_rehearsal: bool) -> tuple[str, str, str]:
    """Validate the image before either early authorization or role startup."""
    if os.environ.get("MOYAI_FLEET_REGISTRY_SCHEMA", SCHEMA) != SCHEMA:
        raise FleetAdmissionError("fleet_admission_registry_cannot_be_redirected")
    service_id = os.environ.get("RENDER_SERVICE_ID", "")
    service_name = os.environ.get("RENDER_SERVICE_NAME", "")
    instance_id = os.environ.get("RENDER_INSTANCE_ID", "")
    if service_id:
        try:
            actual_commit = BUILD_REVISION_FILE.read_text().strip()
        except OSError:
            raise FleetAdmissionError("fleet_image_revision_unavailable") from None
        git_commit = os.environ.get("RENDER_GIT_COMMIT", "")
        if (
            not re.fullmatch("[a-f0-9]{40}", actual_commit) or actual_commit != source_sha
            or git_commit not in {"", actual_commit}
        ):
            raise FleetAdmissionError("fleet_image_revision_changed")
    elif local_rehearsal:
        # Only the explicit loopback rehearsal mode permits synthetic provider
        # identities; these never qualify a production provider preflight.
        service_id = os.environ.get("MOYAI_LOCAL_FLEET_SERVICE_ID", "")
        instance_id = os.environ.get("MOYAI_LOCAL_FLEET_INSTANCE_ID", "")
        service_name = os.environ.get("MOYAI_LOCAL_FLEET_SERVICE_NAME", "")
    return service_id, instance_id, service_name


class FleetAdmission:
    def __init__(self, lifecycle: ProductRoleLifecycle):
        self.life = lifecycle
        self.schema = SCHEMA
        product = lifecycle.runtime.product
        assert product is not None
        self.service_id, self.instance_id, self.service_name = service_identity(
            product.settings.moyai_build_sha,
            local_rehearsal=getattr(getattr(lifecycle.runtime, "product_config", None), "local_rehearsal", False),
        )
        self.generation = os.environ.get("MOYAI_RELEASE_GENERATION", "")
        required = os.environ.get("MOYAI_FLEET_REQUIRED", "false")
        if required not in {"true", "false"}:
            raise FleetAdmissionError("fleet_admission_required_invalid")
        self.required = required == "true"
        self.bridge = bool(getattr(getattr(lifecycle.runtime, "settings", None), "worker_adoption_bridge", False))
        self.required = self.required or self.bridge
        self.registered = False
        self.current: dict | None = None
        self._startup_lock: int | None = None

    @property
    def sealed(self) -> bool:
        return self.current is not None and self.current["state"] in {"draining", "retired"}

    @property
    def request_id(self) -> str:
        return self.current["drain_request"] if self.current is not None else ""

    def _observe(self, starting=False) -> dict | None:
        runtime = self.life.runtime
        product, config = runtime.product, runtime.product_config
        assert product is not None and config is not None and config.native_schema is not None
        # Startup reads hold the fleet row through their transaction, even when
        # disabled. An activation must take that row exclusively and inventory
        # canonical owners after the lock, so a concurrent unmanaged start is
        # either inventoried first or sees the newly enabled registry.
        # Registry locks and pool waits must not occupy the canonical owner
        # session used by health/leadership checks. Keep every observation on
        # its own pooled transaction, including unmanaged and hot-adoption reads.
        with product.store.connect() as connection:
            connection.check_owner()
            registry = FleetRegistry(connection.raw, schema=self.schema)
            scope = registry.scope(product.settings.moyai_database_schema, lock=starting)
            if scope is None or not scope["enabled"]:
                if self.required or self.registered:
                    raise FleetAdmissionError("fleet_admission_not_enabled")
                return None
            role = registry.role(fleet=scope["fleet"], service_id=self.service_id,
                                 service_name=self.service_name, generation=self.generation)
            if role is None:
                raise FleetAdmissionError("fleet_admission_service_unregistered")
            registry.validate(
                scope, role, identity=self.life.quiescence.identity(),
                native_schema=config.native_schema, queue=runtime.settings.temporal_queue,
                deployment=runtime.settings.worker_deployment, build=runtime.settings.worker_build_id,
                generation=self.generation,
                bridge=self.bridge, lock=starting,
            )
            if role["service_id"] is not None and role["service_id"] != self.service_id:
                raise FleetAdmissionError("fleet_admission_service_changed")
            return role

    def _register(self) -> dict:
        runtime = self.life.runtime
        product, config = runtime.product, runtime.product_config
        assert product is not None and config is not None and config.native_schema is not None
        # A pooled canonical transaction keeps compatibility/ownership fences.
        # The role's shared row lock is held until this outer transaction commits.
        with product.store.connect() as connection:
            connection.begin_write()
            role = FleetRegistry(connection.raw, schema=self.schema).register(
                source_schema=product.settings.moyai_database_schema,
                service_id=self.service_id, instance_id=self.instance_id, service_name=self.service_name,
                generation=self.generation, identity=self.life.quiescence.identity(),
                native_schema=config.native_schema, queue=runtime.settings.temporal_queue,
                deployment=runtime.settings.worker_deployment, build=runtime.settings.worker_build_id,
                bridge=self.bridge,
            )
            connection.check_owner(lock_compatibility=True)
            return role

    async def refresh(self, *, starting=False) -> bool:
        role = await database(self._observe, starting)
        self.current = role
        if role is None:
            return True
        if role["service_id"] is None:
            if not starting:
                raise FleetAdmissionError("fleet_admission_service_binding_lost")
            return False
        if not self.registered:
            # A service sealed before this registration can never start. A
            # previously registered owner may finish its admitted operations.
            self.current = await database(self._register)
            self.registered = True
            self.generation = self.current["generation"]
        return True

    async def start(self) -> None:
        from app.database import DatabaseBusyError

        product = self.life.runtime.product
        assert product is not None
        async with asyncio.timeout(product.settings.temporal_startup_timeout_seconds):
            while True:
                try:
                    if await self.refresh(starting=True):
                        return
                except DatabaseBusyError:
                    # No role polling has started. Wait within the existing
                    # startup budget without pretending registration succeeded.
                    pass
                await asyncio.sleep(.1)

    def _lock_startup(self) -> bool:
        product = self.life.runtime.product
        assert product is not None and self.current is not None
        if self._startup_lock is not None:
            raise FleetAdmissionError("fleet_startup_already_locked")
        owner = product.store.database
        owner.check_owner()
        key = FleetRegistry(owner.owner, schema=self.schema).startup_key(
            fleet=self.current["fleet"], generation=self.current["generation"], role=self.life.role,
        )
        # This is a nonblocking ownership operation, not a registry read or a
        # transaction. Losing this session releases the gate AND fences normal
        # application writes through the existing canonical ownership checks.
        acquired = owner.owner.execute("SELECT pg_try_advisory_lock_shared(%s)", (key,)).fetchone()[0]
        if acquired:
            self._startup_lock = key
        return acquired

    def _unlock_startup(self) -> None:
        if self._startup_lock is None:
            return
        product = self.life.runtime.product
        assert product is not None
        key, self._startup_lock = self._startup_lock, None
        owner = product.store.database.owner
        if (
            not owner.closed and not owner.broken
            and not owner.execute("SELECT pg_advisory_unlock_shared(%s)", (key,)).fetchone()[0]
        ):
            raise FleetAdmissionError("fleet_startup_lock_lost")

    async def complete_startup(self) -> None:
        if self._startup_lock is not None:
            def complete():
                product = self.life.runtime.product
                assert product is not None
                try:
                    product.store.database.check_owner()
                finally:
                    self._unlock_startup()
            await database(complete)

    @asynccontextmanager
    async def startup(self):
        """Order role startup against sealing, including a lost registration reply."""
        try:
            await self.start()
            if self.current is not None:
                product = self.life.runtime.product
                assert product is not None
                async with asyncio.timeout(product.settings.temporal_startup_timeout_seconds):
                    while not await database(self._lock_startup):
                        await asyncio.sleep(.1)
                    # Registration may have committed before a seal and its
                    # acknowledgement arrived afterward. Reread under the gate;
                    # a registration receipt alone cannot authorize startup.
                    await self.refresh(starting=True)
                if self.sealed:
                    # This exact captured owner never started role work. Persist
                    # that completion before exiting so retirement can reconcile
                    # it without incorrectly treating owner disappearance as proof.
                    await self.finish_drain(self.request_id)
                    raise FleetAdmissionError("fleet_admission_sealed_before_start")
            yield
        finally:
            # database() drains acquisition before propagating cancellation;
            # the lock remains discoverable here even if its reply was lost.
            if self._startup_lock is not None:
                await database(self._unlock_startup)

    def require_drain(self, request_id: str) -> None:
        if self.current is not None and (
            not self.registered or not self.sealed or request_id != self.request_id
        ):
            raise FleetAdmissionError("fleet_admission_requires_durable_role_seal")

    async def finish_drain(self, request_id: str) -> None:
        if self.current is None:
            return
        self.require_drain(request_id)
        product = self.life.runtime.product
        assert product is not None

        def save():
            assert self.current is not None
            with product.store.connect() as connection:
                connection.begin_write()
                FleetRegistry(connection.raw, schema=self.schema).record_drained(
                    fleet=self.current["fleet"], generation=self.current["generation"], role=self.life.role,
                    service_id=self.service_id, instance_id=self.instance_id,
                    identity=self.life.quiescence.identity(), request_id=request_id,
                )
                connection.check_owner(lock_compatibility=True)

        await database(save)
