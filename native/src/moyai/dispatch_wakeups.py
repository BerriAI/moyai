"""Committed PostgreSQL notifications are hints; durable rows remain authoritative."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from types import TracebackType
from typing import Self

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .settings import Settings

logger = logging.getLogger(__name__)
FALLBACK_SECONDS = 0.5


def channel(settings: Settings) -> str:
    # A database can host several isolated product/native schemas and queues.
    scope = f"{settings.database_schema}:{settings.temporal_queue}"
    return "moyai_dispatch_" + hashlib.sha256(scope.encode()).hexdigest()[:32]


async def notify_dispatch(db: AsyncSession, settings: Settings) -> None:
    if db.get_bind().dialect.name == "postgresql":
        # PostgreSQL delivers only after the containing transaction commits,
        # and discards this notification if it rolls back.
        await db.execute(text("SELECT pg_notify(:channel, '')"), {"channel": channel(settings)})


class DispatchWakeups:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.event = asyncio.Event()
        self.ready = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def _notified(self, _connection: asyncpg.Connection, _pid: int, _channel: str, _payload: str) -> None:
        self.event.set()

    async def listen(self) -> None:
        url = self.settings.postgres_url
        assert url is not None
        while True:
            connection: asyncpg.Connection | None = None
            disconnected = asyncio.Event()
            try:
                connection = await asyncpg.connect(
                    url, timeout=5, command_timeout=5,
                    server_settings={"application_name": channel(self.settings)},
                )
                assert connection is not None
                connection.add_termination_listener(lambda _, signal=disconnected: signal.set())
                await connection.add_listener(channel(self.settings), self._notified)
                self.ready.set()
                self.event.set()  # Reconcile anything committed before LISTEN became active.
                await disconnected.wait()
            except asyncio.CancelledError:
                raise
            except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError):
                logger.warning("Dispatch notifications are unavailable; durable polling remains active.")
            finally:
                self.ready.clear()
                if connection is not None:
                    try:
                        await connection.close(timeout=5)
                    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, TimeoutError):
                        logger.warning("Closing a dispatch listener failed; terminating its connection.")
                    finally:
                        connection.terminate()
            await asyncio.sleep(FALLBACK_SECONDS)

    async def wait(self) -> None:
        with suppress(TimeoutError):
            async with asyncio.timeout(FALLBACK_SECONDS):
                await self.event.wait()

    async def __aenter__(self) -> Self:
        if self.settings.postgres_url:
            self.task = asyncio.create_task(self.listen(), name="native-dispatch-listener")
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None,
        exc: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
