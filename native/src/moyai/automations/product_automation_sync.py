"""Reconcile durable product automation intent with confirmed Temporal schedules.

The command module owns definition changes and occurrence admission. This module
owns best-effort dispatch, remote-operation serialization and recovery. It uses
that module's canonical row reader and writer fence so synchronization never
introduces a second transaction or lock protocol for the same product state.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.pool import NullPool
from temporalio.api.common.v1 import Payload
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from ..models import now
from ..postgres import create_engine
from ..product_config import CORE_IDENTITY
from ..product_identity import ProductGrant
from .product_automation_models import ProductAutomationBinding as Binding

if TYPE_CHECKING:
    from .product_automations import ProductAutomationService

RPC_TIMEOUT = timedelta(seconds=3)
SYNC_CONCURRENCY = 2


class ProductAutomationSync:
    """Internal schedule interface; construction performs no I/O or activation.

    The injected Temporal client is also the deterministic test adapter seam.
    Keep SDK workflow/activity declarations in product_automations for replay.
    """

    def __init__(self, service: ProductAutomationService):
        self.service = service
        self.slots = asyncio.Semaphore(SYNC_CONCURRENCY)

    async def request_sync(self, identity: str) -> bool:
        from .product_automations import ProductAutomationSyncWorkflow

        try:
            client = await self.service.connect()
            await client.start_workflow(
                ProductAutomationSyncWorkflow.run, identity,
                id=f"moyai-product-automation-sync-{identity}", task_queue=self.service.queue,
                start_signal="changed", rpc_timeout=RPC_TIMEOUT,
            )
            return True
        except (RPCError, WorkflowAlreadyStartedError, TimeoutError, OSError):
            return False

    async def dispatch_pending(self, *, limit: int = 20) -> int:
        """Call from the existing coordinator dispatcher; this adds no timer."""
        async with self.service.database.sessions() as db:
            identities = list(await db.scalars(select(Binding.id).where(
                Binding.sync_state == "pending",
            ).order_by(Binding.updated_at).limit(min(limit, 100))))
        count = sum([await self.request_sync(identity) for identity in identities])
        return count

    @asynccontextmanager
    async def _sync_lock(self, identity: str):
        # Like workspace restoration, hold only the advisory transaction on a
        # dedicated connection. A normal pool slot here would starve the short
        # data/authority transactions when the native pool has one connection.
        async with self.slots:
            engine = create_engine(self.service.database.engine.url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    acquired = await connection.scalar(text(
                        "SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"
                    ), {"key": f"moyai-product-automation-sync:{self.service.schema}:{identity}"})
                    if not acquired:
                        raise RuntimeError("Another automation synchronization is in progress.")
                    yield
            finally:
                await engine.dispose()

    async def _pause_remote(self, client: Client, identity: str) -> None:
        handle = client.get_schedule_handle(identity)
        try:
            await handle.pause(note="Canonical native migration fence", rpc_timeout=RPC_TIMEOUT)
            description = await handle.describe(rpc_timeout=RPC_TIMEOUT)
            if not description.schedule.state.paused:
                raise RuntimeError("Schedule pause is not confirmed.")
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise

    async def reconcile(self, identity: str) -> dict[str, Any]:
        from .product_automations import (
            ProductAutomationWorkflow,
            _definition,
            definition_digest,
            legacy_schedule_id,
            native_schedule_id,
            once_id,
        )

        client = await self.service.connect()
        async with self._sync_lock(identity):
            for _ in range(3):
                async with self.service.database.sessions() as db:
                    row = await self.service._row(db, identity)
                    binding = await db.get(Binding, identity)
                    if binding is None:
                        return {"status": "unbound"}
                    definition = _definition(row)
                    generation, desired = binding.generation, binding.state
                    original = ProductGrant(binding.owner_id, binding.authority_version)
                    old_ids = list((await db.execute(text(
                        f"SELECT schedule_id FROM {self.service._table('automation_schedules')} WHERE automation_id=:id"
                    ), {"id": identity})).scalars())
                    old_ids += [legacy_schedule_id(identity, trigger.id) for trigger in definition.triggers if trigger.schedule]
                    previous_ids = json.loads(binding.schedule_ids)
                    consumed_once = {
                        trigger.id for trigger in definition.triggers
                        if trigger.schedule and trigger.schedule.frequency == "once"
                        and await db.scalar(text(
                            f"SELECT 1 FROM {self.service._table('automation_runs')} WHERE occurrence=:key"
                        ), {"key": once_id(identity, trigger)})
                    }
                    valid = (
                        row["owner_id"] == binding.owner_id
                        and bool(row["paused"]) and row["revision"] == binding.source_revision
                        and definition.harness == CORE_IDENTITY
                        and definition_digest(definition) == binding.definition_digest
                    )
                authorized = True
                try:
                    await self.service.identity.require_current(original)
                except HTTPException:
                    authorized = False
                if not valid or not authorized:
                    for schedule_id in previous_ids:
                        await self._pause_remote(client, schedule_id)
                    async with self.service.database.sessions.begin() as db:
                        await db.execute(update(Binding).where(
                            Binding.id == identity, Binding.generation == generation,
                        ).values(sync_state="blocked", last_error="Definition or original authority changed.", updated_at=now()))
                    return {"status": "blocked"}
                # Both the durable canonical fence and remote pause confirmation
                # precede any native unpause. A stale old coordinator still sees
                # paused/native-harness at its own admission transaction.
                for schedule_id in sorted(set(old_ids)):
                    await self._pause_remote(client, schedule_id)
                planned_ids = [
                    native_schedule_id(identity, trigger.id) for trigger in definition.triggers
                    if trigger.schedule and desired != "deleted"
                ]
                # Track intent BEFORE remote creation. A later generation can
                # delete a schedule even if creation succeeded but its ack was
                # lost, or an update removed its trigger during the RPC.
                async with self.service.database.sessions.begin() as db:
                    tracked = await db.scalar(select(Binding).where(Binding.id == identity).with_for_update())
                    assert tracked is not None
                    tracked.schedule_ids = json.dumps(sorted(set(json.loads(tracked.schedule_ids)) | set(planned_ids)))
                desired_ids = []
                for trigger in definition.triggers:
                    if trigger.schedule is None:
                        continue
                    schedule_id = native_schedule_id(identity, trigger.id)
                    if desired == "deleted":
                        continue
                    desired_ids.append(schedule_id)
                    once = trigger.schedule.frequency == "once"
                    used = trigger.id in consumed_once
                    schedule = Schedule(
                        action=ScheduleActionStartWorkflow(
                            ProductAutomationWorkflow.run,
                            args=[identity, generation, row["revision"], trigger.id],
                            id=f"moyai-native-automation-tick-{identity}-{trigger.id}",
                            task_queue=self.service.queue, execution_timeout=timedelta(minutes=20),
                        ),
                        spec=trigger.schedule.spec(),
                        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.ALLOW_ALL, catchup_window=timedelta(minutes=15)),
                        state=ScheduleState(
                            paused=desired != "active" or used, limited_actions=once and not used,
                            remaining_actions=1 if once and not used else 0,
                        ),
                    )
                    try:
                        await client.create_schedule(schedule_id, schedule, rpc_timeout=RPC_TIMEOUT)
                    except ScheduleAlreadyRunningError:
                        await client.get_schedule_handle(schedule_id).update(
                            lambda _, schedule=schedule: ScheduleUpdate(schedule), rpc_timeout=RPC_TIMEOUT,
                        )
                for stale in sorted(set(previous_ids) - set(desired_ids)):
                    try:
                        await client.get_schedule_handle(stale).delete(rpc_timeout=RPC_TIMEOUT)
                    except RPCError as error:
                        if error.status != RPCStatusCode.NOT_FOUND:
                            raise
                # Read back actual state. Only this ack can mark the intent ready.
                for schedule_id in desired_ids:
                    description = await client.get_schedule_handle(schedule_id).describe(rpc_timeout=RPC_TIMEOUT)
                    used = any(schedule_id == native_schedule_id(identity, key) for key in consumed_once)
                    if description.schedule.state.paused != (desired != "active" or used):
                        raise RuntimeError("Native schedule state is not confirmed.")
                    remote = description.schedule.action
                    if (
                        not isinstance(remote, ScheduleActionStartWorkflow)
                        or remote.workflow != "MoyaiProductAutomationV1" or remote.task_queue != self.service.queue
                    ):
                        raise RuntimeError("Native schedule action is not confirmed.")
                    arguments = list(remote.args)
                    if arguments and isinstance(arguments[0], Payload):
                        arguments = await client.data_converter.decode(arguments)
                    expected_trigger = next(
                        trigger.id for trigger in definition.triggers
                        if schedule_id == native_schedule_id(identity, trigger.id)
                    )
                    if arguments != [identity, generation, row["revision"], expected_trigger]:
                        raise RuntimeError("Native schedule generation is not confirmed.")
                async with self.service.database.sessions.begin() as db:
                    await self.service._writer(db)
                    current = await db.get(Binding, identity)
                    fresh = await self.service._row(db, identity)
                    if current and current.generation == generation and fresh["revision"] == row["revision"]:
                        current.schedule_ids = json.dumps(desired_ids)
                        current.synced_generation, current.sync_state = generation, "ready"
                        current.last_error, current.updated_at = "", now()
                        return {"status": "ready", "generation": generation}
            return {"status": "pending"}
