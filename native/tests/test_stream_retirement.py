"""Regression for a cancelled SDK subscription that exits normally."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Request
from pydantic_ai.messages import PartStartEvent, TextPart

from moyai.api import create_app


@pytest.mark.parametrize("reason", ["reconnect", "consumer_cancelled"])
async def test_api_does_not_reopen_cancelled_sdk_subscription(runtime, active, actor, monkeypatch, reason):
    session, turn, _ = active
    waiting, closed, reconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()
    subscriptions = []

    class Subscription:
        offset = 72

        async def __aenter__(self):
            subscriptions.append(self)
            return self

        async def __aexit__(self, *_):
            closed.set()

        def __aiter__(self):
            return self.events()

        async def events(self):
            self.offset = 73
            yield PartStartEvent(index=0, part=TextPart("saved"))
            waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Workflow Streams ends the iterator when its poll is cancelled.
                return

    async def connect():
        return SimpleNamespace(get_workflow_handle=lambda _: object())

    monkeypatch.setattr(runtime, "connect", connect)
    monkeypatch.setattr(runtime.durability, "stream_agent_events", lambda *a, **kw: Subscription())
    app = create_app(runtime, run_dispatcher=False)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/sessions/{session_id}/events")

    async def receive():
        await asyncio.Event().wait()

    request = Request({"type": "http", "headers": [], "state": {"moyai_reconnect": reconnect}}, receive)
    response = await endpoint(request, session.id, turn.id, 0, actor)
    stream = response.body_iterator
    assert await anext(stream) == "event: connected\ndata: {}\n\n"
    assert "id: 73" in await anext(stream)
    consumer = asyncio.create_task(anext(stream))
    await waiting.wait()
    if reason == "reconnect":
        reconnect.set()
        assert await asyncio.wait_for(consumer, 1) == "retry: 250\n\n"
        consumer = asyncio.create_task(anext(stream))
        expected = StopAsyncIteration
    else:
        consumer.cancel()
        expected = asyncio.CancelledError
    with pytest.raises(expected):
        await asyncio.wait_for(consumer, 1)
    assert closed.is_set()
    assert len(subscriptions) == 1
