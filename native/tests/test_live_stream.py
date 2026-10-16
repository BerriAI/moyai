from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from moyai.live_stream import authorized_events


@pytest.mark.asyncio
async def test_idle_stream_revocation_closes_sdk_reader():
    closed = asyncio.Event()
    revoked = False

    async def source():
        try:
            await asyncio.Event().wait()
            yield "private"
        finally:
            closed.set()

    async def authorize():
        if revoked:
            raise HTTPException(401)

    async def disconnected():
        return False

    stream = authorized_events(source(), authorize, disconnected, interval=0.01)
    assert await anext(stream) == ": keepalive\n\n"
    revoked = True
    assert "access_revoked" in await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_result_ready_after_revocation_is_not_delivered():
    revoked = False

    async def source():
        nonlocal revoked
        revoked = True
        yield "private"

    async def authorize():
        if revoked:
            raise HTTPException(403)

    async def disconnected():
        return False

    items = [item async for item in authorized_events(source(), authorize, disconnected)]
    assert items == ['event: access_revoked\ndata: {}\n\n']


@pytest.mark.asyncio
async def test_cancelled_consumer_closes_waiting_sdk_reader():
    entered, closed = asyncio.Event(), asyncio.Event()

    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield "unused"
        finally:
            closed.set()

    async def authorize():
        pass

    async def disconnected():
        return False

    stream = authorized_events(source(), authorize, disconnected)
    consumer = asyncio.create_task(anext(stream))
    await entered.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert closed.is_set()
