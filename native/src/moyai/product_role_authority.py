"""Apply canonical process ownership to native ORM and Harness transactions.

The source Store owns admission and the non-reconnecting PostgreSQL owner
session. These adapters do not elect owners or publish a second role policy.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker


class ProductOwnershipLost(RuntimeError):
    """The original process or its admitted runtime contract was revoked."""


CONNECTION_BINDING = """
    WITH held AS MATERIALIZED (
        SELECT pid,classid,objid,objsubid,mode,granted FROM pg_locks
        WHERE locktype='advisory'
        AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
        AND pid IN (pg_backend_pid(),:pid)
    ), markers AS (
        SELECT * FROM held WHERE pid=pg_backend_pid() AND classid=726948
    )
    SELECT pg_backend_pid() AS connection_pid,
        (SELECT count(*) FROM markers) AS marker_count,
        NOT EXISTS(SELECT 1 FROM markers WHERE objid!=:pid OR objsubid!=2
            OR mode!='ShareLock' OR NOT granted) AS marker_valid,
        NOT EXISTS(SELECT 1 FROM held WHERE pid=:pid AND classid=726948) AS parent_unmarked,
        NOT EXISTS(SELECT 1 FROM held WHERE pid=pg_backend_pid()
            AND classid IN (726943,726944,726946)) AS connection_has_no_role,
        EXISTS(SELECT 1 FROM held WHERE pid=pg_backend_pid() AND classid=726940) AS already_fenced,
        EXISTS(SELECT 1 FROM held l JOIN pg_stat_activity a ON a.pid=l.pid
            WHERE l.pid=:pid AND a.backend_start=:started AND l.classid=726940
            AND l.objid=CAST(CAST(:schema AS regnamespace) AS oid)
            AND l.objsubid=2 AND l.mode='ShareLock' AND l.granted) AS parent_valid
"""


@dataclass(frozen=True)
class ProductRoleAuthority:
    schema: str
    role: str
    owner_pid: int
    owner_started: datetime
    cluster_policy: str
    role_policy: str

    def __post_init__(self) -> None:
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema)
            or self.role not in {"api", "coordinator", "broker", "worker"}
            or self.owner_pid <= 0
            or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in (
                self.cluster_policy, self.role_policy,
            ))
        ):
            raise ValueError("Native role authority requires a verified canonical owner and contract.")

    @classmethod
    def from_source(cls, database: Any) -> ProductRoleAuthority:
        """Capture only after Store.configure_runtime succeeds, off the event loop."""
        database.check_owner()
        row = database.owner.execute(
            "SELECT backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
        ).fetchone()
        return cls(
            database.schema, database.runtime_role, database.owner_pid, row[0],
            database.policy, getattr(database, database.runtime_role + "_policy"),
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "pid": self.owner_pid, "started": self.owner_started,
            "cluster": self.cluster_policy, "role": self.role_policy,
        }

    def _queries(self, *, final: bool) -> list[tuple[str, bool]]:
        from app.coordinator_handoff import coordinator_owner

        leader = coordinator_owner.get()
        if leader is not None and leader != (self.schema, self.owner_pid):
            raise ProductOwnershipLost("Coordinator authority belongs to a different process.")
        owner = """
            SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
            WHERE l.locktype='advisory' AND l.pid=:pid AND a.backend_start=:started
            AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database())
            AND l.classid=726940 AND l.objid=CAST(CAST(:schema AS regnamespace) AS oid)
            AND l.objsubid=2 AND l.granted
        """
        policy = f"""
            SELECT 1 FROM "{self.schema}".runtime_policy p
            JOIN "{self.schema}".runtime_{self.role}_policy r
              ON r.id=p.id AND r.cluster_fingerprint=p.fingerprint
            WHERE p.id=1 AND p.fingerprint=:cluster AND r.fingerprint=:role
        """
        queries = [("SELECT pg_stat_clear_snapshot()", False), (owner, True)]
        if leader is not None:
            queries.append(("""
                SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=:pid
                AND database=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND classid=726943 AND objid=CAST(CAST(:schema AS regnamespace) AS oid)
                AND objsubid=2 AND granted
            """, True))
        # Lock policy rows only at commit, after any canonical writer lock the
        # operation needs. Locking them before admission would invert the
        # canonical writer -> policy order during rolling role revocation.
        queries.append((policy + (" FOR SHARE OF p,r" if final else ""), True))
        return queries

    def check(self, connection: Connection, *, final: bool = False) -> None:
        if not final:
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            binding = connection.execute(text(CONNECTION_BINDING), self.parameters).mappings().one()
            if self._must_bind(binding):
                connection.execute(text("SELECT pg_advisory_lock_shared(726948,:pid)"), self.parameters)
            connection.execute(text("""
                SELECT pg_advisory_xact_lock_shared(
                    726940, CAST(CAST(:schema AS regnamespace) AS oid)::int
                )
            """), self.parameters)
        for sql, required in self._queries(final=final):
            result = connection.execute(text(sql), self.parameters)
            if required and result.scalar_one_or_none() != 1:
                raise ProductOwnershipLost("Canonical runtime ownership or role compatibility was lost.")

    def _must_bind(self, binding: Any) -> bool:
        # Mark only a subordinate connection, once, before its transaction
        # fence. Never hide a process owner or silently move a pooled connection
        # to another authority. asyncpg's pool reset may remove session locks;
        # an absent marker is rebound only on an otherwise unfenced connection.
        count = binding["marker_count"]
        if (
            binding["connection_pid"] == self.owner_pid
            or count not in (0, 1)
            or not all(binding[name] for name in (
                "marker_valid", "parent_unmarked", "connection_has_no_role", "parent_valid",
            ))
            or (count == 0 and binding["already_fenced"])
        ):
            raise ProductOwnershipLost("The native database connection has incompatible ownership.")
        return count == 0

    def _asyncpg_query(self, sql: str) -> tuple[str, list[Any]]:
        values: list[Any] = []

        def parameter(match: re.Match[str]) -> str:
            values.append(self.parameters[match[1]])
            return f"${len(values)}"

        return re.sub(r"(?<!:):([a-z]+)", parameter, sql), values

    def sessions(self, engine: AsyncEngine) -> async_sessionmaker:
        """A runtime-local session factory; no global Session event handlers."""
        factory = sessionmaker(expire_on_commit=False)

        @event.listens_for(factory, "after_begin")
        def admit(_session: Session, _transaction: Any, connection: Connection) -> None:
            self.check(connection)

        @event.listens_for(factory, "before_commit")
        def settle(session: Session) -> None:
            # Flush before taking policy row locks. All application writes and
            # their authority checks must precede the final compatibility check.
            session.flush()
            self.check(session.connection(), final=True)

        return async_sessionmaker(engine, sync_session_class=factory, expire_on_commit=False)

    async def check_asyncpg(self, connection: Any, *, final: bool = False) -> None:
        if not final:
            await connection.execute("SET LOCAL lock_timeout = '5s'")
            query, values = self._asyncpg_query(CONNECTION_BINDING)
            binding = await connection.fetchrow(query, *values)
            if binding is None:
                raise ProductOwnershipLost("The native database connection has no ownership identity.")
            if self._must_bind(binding):
                await connection.fetchval("SELECT pg_advisory_lock_shared(726948,$1)", self.owner_pid)
            await connection.fetchval(
                "SELECT pg_advisory_xact_lock_shared(726940, $1::regnamespace::oid::int)", self.schema,
            )
        for sql, required in self._queries(final=final):
            query, values = self._asyncpg_query(sql)
            result = await connection.fetchval(query, *values)
            if required and result != 1:
                raise ProductOwnershipLost("Canonical runtime ownership or role compatibility was lost.")


class OwnedHarnessPool:
    """The public Harness pool protocol with an owned outer transaction.

    SDK savepoints remain native. If authority is revoked or the operation is
    cancelled, asyncpg rolls back the entire operation before returning its
    connection. An acquired connection can never silently commit after fencing.
    """

    def __init__(self, pool: asyncpg.Pool, authority: ProductRoleAuthority):
        self.pool, self.authority = pool, authority

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        async with self.pool.acquire() as connection, connection.transaction():
            await self.authority.check_asyncpg(connection)
            yield connection
            await self.authority.check_asyncpg(connection, final=True)
