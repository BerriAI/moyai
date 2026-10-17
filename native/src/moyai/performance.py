"""Content-free timing evidence, written only at API/activity/transport boundaries.

The existing event store gives each turn four versioned, atomic first observations.
No workflow commands or schema changes are needed. Missing evidence stays missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from itertools import islice
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
MAX_PENDING = 4096
BATCH_SIZE = 32


def evidence(session_id: str, turn_id: str, kind: str, stamp: str, **details: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("Unknown performance observation.")
    return {
        "session_id": session_id, "turn_id": turn_id, "kind": PREFIX + kind,
        "identity": f"{PREFIX}{kind}:{turn_id}", "created_at": stamp,
        "content": json.dumps(details, separators=(",", ":")),
    }


async def write(db: Any, values: list[dict[str, Any]], *, postgres: bool) -> None:
    insert = postgres_insert if postgres else sqlite_insert
    statement = insert(Event).values(values)
    await db.execute(statement.on_conflict_do_update(
        index_elements=[Event.identity],
        set_={"created_at": statement.excluded.created_at, "content": statement.excluded.content},
        where=Event.created_at >= statement.excluded.created_at,
    ))


def admission(db: Any, turn: Any, stamp: str, *, source: str, surface: str) -> None:
    """Commit coverage with the turn, including non-HTTP and control admissions."""
    db.add(Event(**evidence(
        turn.session_id, turn.id, "received", stamp,
        source=source, surface=surface, coverage_version=2,
    )))


async def record(database: Database, values: dict[str, Any]) -> bool:
    """Bounded best effort; an observation must never retry or fail an effect.

    Earliest *observed* time wins even when an earlier request settles last.
    Computer completion may update its own initial pending observation.
    """
    try:
        async with asyncio.timeout(1):
            async with database.sessions.begin() as db:
                await write(db, [values], postgres=bool(database.settings.postgres_url))
        return True
    except Exception:  # noqa: BLE001 - telemetry failures cannot change a provider/tool outcome
        # Neither exception strings nor request contents belong in this log.
        logger.warning("Native performance observation could not be saved.")
        return False


class DispatchRecorder:
    """One bounded writer per process; coalesce repeated attempts before SQL.

    The database remains the cross-process arbiter of the earliest observation.
    The cache only avoids redundant writes. Neither enqueueing nor retries await
    anything on the outbound transport path.
    """

    def __init__(self, database: Database):
        self.database = database
        self.pending: dict[str, dict[str, Any]] = {}
        self.saved: OrderedDict[str, str] = OrderedDict()

    def enqueue(self, values: dict[str, Any]) -> None:
        identity, stamp = values["identity"], values["created_at"]
        if identity in self.saved and self.saved[identity] <= stamp:
            self.saved.move_to_end(identity)
            return
        previous = self.pending.get(identity)
        if previous and previous["created_at"] <= stamp:
            return
        if previous is None and len(self.pending) >= MAX_PENDING:
            logger.warning("Native performance observation capacity reached.")
            return
        self.pending[identity] = values
        if not any(not task.done() for task in self.database.performance_tasks):
            task = asyncio.create_task(self.flush())
            self.database.performance_tasks.add(task)
            task.add_done_callback(self.database.performance_tasks.discard)

    async def flush(self) -> None:
        from sqlalchemy.exc import IntegrityError

        while self.pending:
            batch = list(islice(self.pending.values(), BATCH_SIZE))
            for attempt in range(3):
                try:
                    async with asyncio.timeout(1):
                        async with self.database.sessions.begin() as db:
                            await write(db, batch, postgres=bool(self.database.settings.postgres_url))
                    break
                except IntegrityError:
                    # A deleted turn must not discard observations for other turns.
                    for values in batch:
                        if await record(self.database, values):
                            self.remember(values)
                        elif self.pending.get(values["identity"]) is values:
                            del self.pending[values["identity"]]
                    break
                except Exception:  # noqa: BLE001 - telemetry cannot retry inference
                    if attempt == 2:
                        logger.warning(
                            "Native performance observations could not be saved (%s pending).",
                            len(self.pending),
                        )
                        self.pending.clear()
                        return
                    await asyncio.sleep(.05 * (attempt + 1))
            else:
                continue
            for values in batch:
                # Failed per-row writes were removed above and must not be cached.
                if self.pending.get(values["identity"]) is values:
                    self.remember(values)

    def remember(self, values: dict[str, Any]) -> None:
        identity, stamp = values["identity"], values["created_at"]
        self.saved[identity] = min(self.saved.get(identity, stamp), stamp)
        self.saved.move_to_end(identity)
        if len(self.saved) > MAX_PENDING:
            self.saved.popitem(last=False)
        if self.pending.get(identity) is values:
            del self.pending[identity]


def record_dispatch(database: Database, values: dict[str, Any]) -> None:
    """Do not await telemetry between dispatch observation and transport entry.

    Runtime shutdown drains the bounded writer. Cancellation of a paid request
    cannot cancel its observation or turn telemetry into an inference retry.
    """
    database.dispatch_timings.enqueue(values)


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
