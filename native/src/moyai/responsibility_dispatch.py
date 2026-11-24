"""Deliver committed wake intents using the existing native coordinator.

Like automation synchronization, this bridges a DB commit to Signal-With-Start.
It does not decide when work is due or implement execution retries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlalchemy import String, cast, delete, literal, select, tuple_
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from .dispatch_wakeups import notify_dispatch
from .models import now
from .responsibility_models import Responsibility, ResponsibilityWake
from .responsibility_workflow import ResponsibilityRun, ResponsibilityWorkflow

if TYPE_CHECKING:
    from .database import Database
    from .runtime import Runtime

logger = logging.getLogger(__name__)


def wake_insert(db: AsyncSession):
    return postgres_insert(ResponsibilityWake) if db.get_bind().dialect.name == "postgresql" else sqlite_insert(ResponsibilityWake)


async def request_wake(db: AsyncSession, identity: str, generation: int, reason: str) -> None:
    await db.execute(wake_insert(db).values(
        id=f"{identity}:{generation}:{reason}", responsibility_id=identity, generation=generation, created_at=now(),
    ).on_conflict_do_nothing(index_elements=["id"]))


async def seed_wakes(database: Database) -> None:
    """Adopt existing v28 work once per coordinator start, without changing it."""
    async with database.sessions.begin() as db:
        await db.execute(wake_insert(db).from_select(
            ["id", "responsibility_id", "generation", "created_at"],
            select(
                Responsibility.id + literal(":") + cast(Responsibility.generation, String) + literal(":start"),
                Responsibility.id, Responsibility.generation, literal(now()),
            ).where(Responsibility.status.in_(["waiting", "running"])),
        ).on_conflict_do_nothing(index_elements=["id"]))
        await notify_dispatch(db, database.settings)


@dataclass
class ResponsibilityDispatch:
    seeded: bool = False
    after: tuple[str, str] | None = None
    through: tuple[str, str] | None = None


async def dispatch_responsibilities(runtime: Runtime, state: ResponsibilityDispatch, *, limit: int = 50) -> None:
    if not state.seeded:
        await seed_wakes(runtime.database)
        state.seeded = True
    async with runtime.database.sessions() as db:
        if state.through is None:
            end = (await db.execute(select(ResponsibilityWake.created_at, ResponsibilityWake.id).order_by(
                ResponsibilityWake.created_at.desc(), ResponsibilityWake.id.desc(),
            ).limit(1))).first()
            if end is None:
                return
            state.through = end.created_at, end.id
        query = select(ResponsibilityWake).where(
            tuple_(ResponsibilityWake.created_at, ResponsibilityWake.id) <= state.through,
        )
        if state.after is not None:
            query = query.where(tuple_(ResponsibilityWake.created_at, ResponsibilityWake.id) > state.after)
        events = list(await db.scalars(query.order_by(
            ResponsibilityWake.created_at, ResponsibilityWake.id,
        ).limit(limit)))
    groups: dict[tuple[str, int], list[str]] = {}
    for event in events:
        groups.setdefault((event.responsibility_id, event.generation), []).append(event.id)
    if not events:
        state.after = state.through = None
        return
    client = await runtime.connect()
    slots = asyncio.Semaphore(runtime.settings.temporal_dispatch_concurrency)
    attempted: set[str] = set()
    through_attempt = 0

    async def deliver(identity: str, generation: int, event_ids: list[str]) -> None:
        nonlocal through_attempt
        async with slots:
            attempted.update(event_ids)
            # Only pass the contiguous prefix that actually obtained an RPC
            # slot. Cancellation must not skip groups still waiting for one.
            while through_attempt < len(events) and events[through_attempt].id in attempted:
                event = events[through_attempt]
                state.after = event.created_at, event.id
                through_attempt += 1
            if through_attempt == len(events) and len(events) < limit:
                state.after = state.through = None
            await send(identity, generation, event_ids)

    async def send(identity: str, generation: int, event_ids: list[str]) -> None:
        request = ResponsibilityRun(identity, generation)
        try:
            await client.start_workflow(
                ResponsibilityWorkflow.run, request, id=request.workflow_id,
                task_queue=runtime.settings.temporal_queue, start_signal="changed",
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=timedelta(seconds=1),
            )
        except WorkflowAlreadyStartedError:
            # Reconcile any already accepted result without admitting work.
            # This generation remains closed until an explicit Resume.
            await runtime.database.responsibilities.tick(identity, generation=generation, closed=True)
        except (RPCError, TimeoutError):
            logger.warning("Temporal did not acknowledge a responsibility wake; its intent remains saved.")
            return
        async with runtime.database.sessions.begin() as db:
            # Ack only the selected events, never a newer completion/control.
            await db.execute(delete(ResponsibilityWake).where(ResponsibilityWake.id.in_(event_ids)))

    await asyncio.gather(*(deliver(identity, generation, ids) for (identity, generation), ids in groups.items()))
