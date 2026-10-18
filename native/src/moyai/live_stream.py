"""Keep native event delivery bounded by the viewer's current authority."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress

from fastapi import HTTPException


async def authorized_events(
    source: AsyncIterator[str],
    authorize: Callable[[], Awaitable[None]],
    disconnected: Callable[[], Awaitable[bool]],
    *,
    interval: float = 2,
    reconnect: asyncio.Event | None = None,
) -> AsyncIterator[str]:
    """Check even an idle stream; cancel and close its owned SDK reader on exit."""
    pending: asyncio.Task[str] | None = None
    stopping = asyncio.create_task(reconnect.wait()) if reconnect is not None else None

    async def pull() -> str:
        return await anext(source)

    try:
        await authorize()
        while not await disconnected():
            if reconnect is not None and reconnect.is_set():
                # No id, finished, refresh or revocation event: EventSource
                # reconnects with the last delivered ID and keeps saved UI state.
                yield "retry: 250\n\n"
                return
            if pending is None:
                pending = asyncio.create_task(pull())
            waiting = {pending, stopping} if stopping is not None else {pending}
            done, _ = await asyncio.wait(waiting, timeout=interval, return_when=asyncio.FIRST_COMPLETED)
            if stopping is not None and stopping in done:
                yield "retry: 250\n\n"
                return
            # Recheck after every wait, including idle intervals, and immediately
            # before delivery. The preceding event's check need not be repeated.
            await authorize()
            if not done:
                yield ": keepalive\n\n"
                continue
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            yield item
    except HTTPException as error:
        if error.status_code in {401, 403, 404}:
            yield 'event: access_revoked\ndata: {}\n\n'
        else:
            yield 'event: refresh\ndata: {}\n\n'
    finally:
        if stopping is not None:
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                await pending
        close = getattr(source, "aclose", None)
        if close is not None:
            await close()
