"""Cooperative shutdown at an operation boundary, retaining in-flight ownership."""

from __future__ import annotations

import asyncio


async def pause(seconds: float, stop: asyncio.Event | None = None, *, wake: asyncio.Event | None = None) -> None:
    if wake is not None:
        waiting = [asyncio.create_task(event.wait()) for event in (wake, stop) if event is not None]
        try:
            await asyncio.wait(waiting, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waiting:
                task.cancel()
            await asyncio.gather(*waiting, return_exceptions=True)
        return
    if stop is None:
        await asyncio.sleep(seconds)
        return
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


async def finish[T](task: asyncio.Task[T]) -> T:
    """Drain an owned task before propagating cancellation of its caller."""
    cancelled = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as error:
            if task.cancelled():
                raise
            cancelled = error
        except BaseException as error:
            if cancelled is not None:
                raise cancelled from error
            raise
    if cancelled is not None:
        raise cancelled
    return result
