"""Process-local release drain; never a substitute for fleet/Temporal readiness."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC
from typing import TYPE_CHECKING
from uuid import uuid4

from app.db import database

from .background_stop import finish

if TYPE_CHECKING:
    from .product_lifecycle import ProductRoleLifecycle


class ProductQuiescence:
    def __init__(self, lifecycle: ProductRoleLifecycle):
        self.life = lifecycle
        self.instance = uuid4().hex
        self.state = "serving"
        self.request_id = ""
        self.task: asyncio.Task[None] | None = None
        self.reconnect = asyncio.Event()
        self.requests: set[asyncio.Task] = set()
        self.requests_changed = asyncio.Event()
        self.background_stop = asyncio.Event()

    @property
    def requested(self) -> bool:
        return self.task is not None

    def identity(self) -> dict[str, str | int]:
        product = self.life.runtime.product
        assert product is not None
        owner = product.authority
        if owner is None:
            raise ValueError("Canonical process identity is unavailable.")
        return {
            "protocol": "moyai-role-drain-v1", "instance": self.instance,
            "process_pid": os.getpid(), "build": product.settings.moyai_build_sha,
            "schema": owner.schema, "role": owner.role, "owner_pid": owner.owner_pid,
            "owner_started": owner.owner_started.astimezone(UTC).isoformat(),
            "cluster_policy": owner.cluster_policy, "role_policy": owner.role_policy,
        }

    async def check_owner(self) -> None:
        product = self.life.runtime.product
        assert product is not None
        await database(product.store.database.check_owner)

    async def status(self) -> dict:
        await self.check_owner()
        return {
            "identity": self.identity(), "state": "failed" if self.life.failed else self.state,
            "request_id": self.request_id, "active_requests": len(self.requests),
            "ready": self.life.healthy,
            "worker_drain_required": self.life.role == "worker",
            "generation_admission": (
                {
                    "registered": self.life.admission.registered,
                    "generation": self.life.admission.current["generation"],
                    "state": self.life.admission.current["state"],
                }
                if self.life.admission is not None and self.life.admission.current is not None else None
            ),
        }

    def begin(self, expected: dict, request_id: str) -> None:
        if expected != self.identity():
            raise ValueError("The expected process identity changed.")
        if not request_id or len(request_id) > 200:
            raise ValueError("A bounded release request identity is required.")
        if self.requested:
            if request_id != self.request_id:
                raise ValueError("Another quiescence request already owns this process.")
            return
        admission = self.life.admission
        if admission is not None:
            admission.require_drain(request_id)
        if self.life.role == "worker" and (admission is None or not admission.sealed):
            raise ValueError("Worker retirement requires Temporal deployment drainage.")
        if not self.life.healthy:
            raise ValueError("Only a ready process can begin planned quiescence.")
        # Admission and task registration run on the same loop without an await.
        self.request_id, self.state = request_id, "quiescing"
        self.reconnect.set()
        self.background_stop.set()
        if self.life.broker is not None:
            self.life.broker.closing = True
        slack = getattr(self.life.runtime, "product_slack", None)
        if self.life.role == "coordinator" and slack is not None:
            slack.stop_admitting()
        self.task = asyncio.create_task(self._drain(), name="native-role-quiescence")

    async def _drain(self) -> None:
        try:
            while self.requests:
                self.requests_changed.clear()
                await self.requests_changed.wait()
            if self.life.broker is not None:
                await self.life.broker.quiesce()
            if self.life.handoff is not None:
                self.life.handoff.stop.set()
                await finish(self.life.handoff.task)
                # Standbys can acknowledge durable HTTP intake without owning
                # the coordinator's background context. Their accepted tasks
                # must also finish before this process reports drained.
                slack = getattr(self.life.runtime, "product_slack", None)
                if slack is not None:
                    await slack.close()
            if self.life.worker is not None:
                await self.life.worker.shutdown()
            await self.check_owner()
        except BaseException:
            # A failed drain is not permission to retire or start it again.
            self.state = "failed"
            raise
        else:
            self.state = "drained"

    async def close(self) -> None:
        if self.task is not None:
            await finish(self.task)
