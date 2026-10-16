"""Content-free timing evidence, written only at API/activity/transport boundaries.

The existing event store gives each turn four versioned, atomic first observations.
No workflow commands or schema changes are needed. Missing evidence stays missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import Event, now

if TYPE_CHECKING:
    from .contracts import RunScope
    from .database import Database

logger = logging.getLogger(__name__)
PREFIX = "performance.v1."
KINDS = ("received", "worker", "dispatch", "computer")


def evidence(session_id: str, turn_id: str, kind: str, stamp: str, **details: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("Unknown performance observation.")
    return {
        "session_id": session_id, "turn_id": turn_id, "kind": PREFIX + kind,
        "identity": f"{PREFIX}{kind}:{turn_id}", "created_at": stamp,
        "content": json.dumps(details, separators=(",", ":")),
    }


async def record(database: Database, values: dict[str, Any]) -> None:
    """Bounded best effort; an observation must never retry or fail an effect.

    Earliest *observed* time wins even when an earlier request settles last.
    Computer completion may update its own initial pending observation.
    """
    try:
        async with asyncio.timeout(1):
            async with database.sessions.begin() as db:
                insert = postgres_insert if database.settings.postgres_url else sqlite_insert
                statement = insert(Event).values(**values)
                await db.execute(statement.on_conflict_do_update(
                    index_elements=[Event.identity],
                    set_={"created_at": statement.excluded.created_at, "content": statement.excluded.content},
                    where=Event.created_at >= statement.excluded.created_at,
                ))
    except Exception:  # noqa: BLE001 - telemetry failures cannot change a provider/tool outcome
        # Neither exception strings nor request contents belong in this log.
        logger.warning("Native performance observation could not be saved.")


def record_dispatch(database: Database, values: dict[str, Any]) -> None:
    """Do not await telemetry between dispatch observation and transport entry.

    Runtime shutdown drains these bounded, one-second writes. Cancellation of a
    paid request cannot cancel its observation or turn telemetry into a retry.
    """
    if len(database.performance_tasks) >= 128:
        logger.warning("Native performance observation capacity reached.")
        return
    task = asyncio.create_task(record(database, values))
    database.performance_tasks.add(task)
    task.add_done_callback(database.performance_tasks.discard)


@dataclass
class ComputerTiming:
    mode: str
    status: str = "ready"


@asynccontextmanager
async def computer_timing(
    database: Database, scope: RunScope, mode: str, *,
    preparation: bool = False, ensure: bool = False,
):
    """First acquisition attempt, including failed/cancelled/incomplete attempts."""
    stamp = now()
    if ensure:
        # Recovery precedes SDK attachment in another activity. Carry its
        # start/mode through the first ensure; a provider poll alone is not ready.
        try:
            async with asyncio.timeout(1):
                async with database.sessions() as db:
                    previous = await db.scalar(select(Event).where(
                        Event.identity == f"{PREFIX}computer:{scope.turn_id}",
                    ))
                    details = json.loads(previous.content) if previous else {}
                    if previous and details.get("status") == "prepared":
                        stamp, mode = previous.created_at, details["mode"]
        except Exception:  # noqa: BLE001 - missing telemetry cannot block workspace attachment
            logger.warning("Native computer preparation timing could not be read.")
    timing = ComputerTiming(mode)
    await record(database, evidence(
        scope.session_id, scope.turn_id, "computer", stamp, mode=mode, status="pending",
    ))
    try:
        yield timing
    except BaseException:
        timing.status = "failed"
        raise
    finally:
        finished = now()
        await record(database, evidence(
            scope.session_id, scope.turn_id, "computer", stamp,
            mode=timing.mode, status="prepared" if preparation and timing.status == "ready" else timing.status,
            finished_at=finished,
        ))
