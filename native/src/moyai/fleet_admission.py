"""Keep a sealed service generation sealed across process restarts."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from app.db import database

from .fleet_registry import SCHEMA, FleetAdmissionError, FleetRegistry

if TYPE_CHECKING:
    from .product_lifecycle import ProductRoleLifecycle

BUILD_REVISION_FILE = Path("/app/build-revision")


class FleetAdmission:
    def __init__(self, lifecycle: ProductRoleLifecycle):
        self.life = lifecycle
        self.schema = SCHEMA
        if os.environ.get("MOYAI_FLEET_REGISTRY_SCHEMA", SCHEMA) != SCHEMA:
            raise FleetAdmissionError("fleet_admission_registry_cannot_be_redirected")
        self.service_id = os.environ.get("RENDER_SERVICE_ID", "")
        self.service_name = os.environ.get("RENDER_SERVICE_NAME", "")
        self.instance_id = os.environ.get("RENDER_INSTANCE_ID", "")
        if self.service_id:
            product = lifecycle.runtime.product
            assert product is not None
            try:
                actual_commit = BUILD_REVISION_FILE.read_text().strip()
            except OSError:
                raise FleetAdmissionError("fleet_image_revision_unavailable") from None
            git_commit = os.environ.get("RENDER_GIT_COMMIT", "")
            if (
                not re.fullmatch("[a-f0-9]{40}", actual_commit)
                or actual_commit != product.settings.moyai_build_sha
                or git_commit not in {"", actual_commit}
            ):
                raise FleetAdmissionError("fleet_image_revision_changed")
        elif getattr(getattr(lifecycle.runtime, "product_config", None), "local_rehearsal", False):
            # The supported rehearsal mode already requires loopback data and
            # service endpoints and rejects Render identities. These explicit
            # synthetic provider IDs let the actual application exercise this
            # same registry protocol; they never qualify a production preflight.
            self.service_id = os.environ.get("MOYAI_LOCAL_FLEET_SERVICE_ID", "")
            self.instance_id = os.environ.get("MOYAI_LOCAL_FLEET_INSTANCE_ID", "")
            self.service_name = os.environ.get("MOYAI_LOCAL_FLEET_SERVICE_NAME", "")
        self.generation = os.environ.get("MOYAI_RELEASE_GENERATION", "")
        required = os.environ.get("MOYAI_FLEET_REQUIRED", "false")
        if required not in {"true", "false"}:
            raise FleetAdmissionError("fleet_admission_required_invalid")
        self.required = required == "true"
        self.registered = False
        self.current: dict | None = None

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
        context = product.store.connect() if starting else nullcontext(product.store.database.owner)
        with context as connection:
            if starting:
                connection.check_owner()
            registry = FleetRegistry(connection.raw if starting else connection, schema=self.schema)
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

    def require_drain(self, request_id: str) -> None:
        if self.current is not None and (
            not self.registered or not self.sealed or request_id != self.request_id
        ):
            raise FleetAdmissionError("fleet_admission_requires_durable_role_seal")
