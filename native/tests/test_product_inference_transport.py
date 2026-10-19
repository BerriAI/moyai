"""Real TCP/PG lifecycle regressions; synthetic gateway, not live-model E2E."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest
import test_product_inference as fixtures
from fastapi import FastAPI
from test_inference_usage import payload

from moyai.inference_usage import INFERENCE_ACTIVITY, InferenceActivity, UsageCapture
from moyai.product_inference import InferenceAdmissionError
from moyai.product_inference_routes import broker_request, product_inference_routes

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
admitted_product = fixtures.admitted_product
inference = fixtures.inference


class WireGateway:
    def __init__(self):
        self.connections = 0
        self.closed = 0
        self.requests = []
        self.writers = set()
        self.tasks = set()
        self.hold_body = asyncio.Event()
        self.hold_body.set()

    async def handle(self, reader, writer):
        self.connections += 1
        connection = self.connections
        self.writers.add(writer)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                headers = dict(
                    line.decode().lower().split(": ", 1)
                    for line in head.split(b"\r\n")[1:] if b":" in line
                )
                body = await reader.readexactly(int(headers["content-length"]))
                self.requests.append((connection, headers, json.loads(body)))
                result = json.dumps(payload(identifier=f"response-{len(self.requests)}")).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Set-Cookie: upstream-session=must-not-cross-requests; Path=/\r\n"
                    + f"Content-Length: {len(result)}\r\n\r\n".encode()
                )
                await writer.drain()
                await self.hold_body.wait()
                writer.write(result)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed += 1
            self.writers.discard(writer)
            self.tasks.discard(task)
            writer.close()
            await writer.wait_closed()


@asynccontextmanager
async def gateway(ctx):
    wire = WireGateway()
    server = await asyncio.start_server(wire.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    ctx.service.settings.litellm_api_base = f"http://127.0.0.1:{port}/v1"
    try:
        yield wire
    finally:
        server.close()
        wire.hold_body.set()
        tasks = tuple(wire.tasks)
        for writer in tuple(wire.writers):
            writer.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()


async def opened(ctx, logical, *, attempt=1):
    activity = InferenceActivity(
        ctx.product.database, ctx.scope, f"{logical}/{attempt}", logical_identity=logical,
    )
    client = ctx.service.upstream_client()
    token = INFERENCE_ACTIVITY.set(activity)
    try:
        response = await client.send(client.build_request(
            "POST", ctx.service.settings.litellm_api_base + "/responses",
            headers={"authorization": "Bearer " + ctx.service.settings.litellm_api_key},
            json={"model": "test", "input": logical},
        ), stream=True)
        return client, response, activity
    except BaseException:
        await client.aclose()
        raise
    finally:
        INFERENCE_ACTIVITY.reset(token)


async def signed_call(ctx, client, logical):
    original = httpx.Request(
        "POST", ctx.service.settings.litellm_api_base + "/responses", json={"model": "test", "input": logical},
    )
    activity = InferenceActivity(
        ctx.product.database, ctx.scope, logical + "/1", requests=1, logical_identity=logical,
    )
    signed = broker_request(ctx.service, original, UsageCapture(activity, "a" * 64, original))
    return await asyncio.wait_for(
        client.post(str(signed.url), headers=signed.headers, content=signed.content), 5,
    )


async def test_broker_reuses_socket_without_cookies_or_cached_credentials(inference):
    ctx = inference
    app = FastAPI()
    app.include_router(product_inference_routes(ctx.service))
    owner = ctx.service.upstream
    async with gateway(ctx) as wire, httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as caller:
        for index in range(3):
            ctx.service.settings.litellm_api_key = f"synthetic-key-{index}"
            response = await signed_call(ctx, caller, f"pooled-{index}")
            assert response.status_code == 200
            assert INFERENCE_ACTIVITY.get() is None
            assert not ctx.service.active and ctx.service.slots.active == 0
        assert wire.connections == 1 and wire.closed == 0
        assert [headers["authorization"] for _, headers, _ in wire.requests] == [
            f"bearer synthetic-key-{index}" for index in range(3)
        ]
        assert all("cookie" not in headers for _, headers, _ in wire.requests)
        rows = await fixtures.requests(ctx)
        assert len(rows) == 3 and len({row.key_hash for row in rows}) == 3
        assert all(row.status == "completed" for row in rows)
        denied = await signed_call(ctx, caller, "pooled-2")
        assert denied.status_code == 409 and len(wire.requests) == 3
        await ctx.service.close()
        assert owner.closed and ctx.service.upstream is None
        with pytest.raises(InferenceAdmissionError, match="not running"):
            ctx.service.upstream_client()
        assert (await signed_call(ctx, caller, "during-shutdown")).status_code == 409
        await ctx.service.start(recovery=False)
        assert ctx.service.upstream is not owner
        assert (await signed_call(ctx, caller, "after-restart")).status_code == 200
        assert wire.connections == 2


async def test_closing_one_stream_and_client_keeps_other_stream_and_pool_alive(inference):
    ctx = inference
    async with gateway(ctx) as wire:
        wire.hold_body.clear()
        first, second = await asyncio.gather(opened(ctx, "first"), opened(ctx, "second"))
        first_client, first_response, first_activity = first
        second_client, second_response, second_activity = second
        assert first_activity.latest is not second_activity.latest
        assert ctx.service.slots.active == 2 and wire.connections == 2
        await first_response.aclose()
        await first_client.aclose()
        assert ctx.service.slots.active == 1 and not ctx.service.upstream.closed
        assert not second_response.is_closed
        wire.hold_body.set()
        assert json.loads(await second_response.aread())["status"] == "completed"
        await second_response.aclose()
        await second_client.aclose()
        third_client, third_response, _ = await opened(ctx, "third")
        await third_response.aread()
        await third_response.aclose()
        await third_client.aclose()
        assert wire.connections == 2  # Third request reuses the surviving socket.
        rows = await fixtures.requests(ctx)
        assert [row.status for row in rows] == ["unknown", "completed", "completed"]
        assert ctx.service.slots.active == 0 and INFERENCE_ACTIVITY.get() is None


async def test_pool_is_bounded_even_without_product_slot_limit(inference):
    ctx = inference
    # Keep real admission unlimited while exercising a deliberately tiny pool.
    await ctx.service.close()
    ctx.service.settings.max_concurrent_model_requests = 0
    await ctx.service.start(recovery=False)
    from moyai.product_inference_transport import BrokerUpstream
    await ctx.service.upstream.aclose()
    ctx.service.upstream = BrokerUpstream(2)
    async with gateway(ctx) as wire:
        wire.hold_body.clear()
        first, second = await asyncio.gather(opened(ctx, "bounded-1"), opened(ctx, "bounded-2"))
        ctx.product.database.settings.model_timeout_seconds = 0.1
        with pytest.raises(httpx.PoolTimeout):
            await opened(ctx, "pool-wait")
        assert wire.connections == len(wire.requests) == 2
        rows = await fixtures.requests(ctx)
        assert rows[-1].status == "not_dispatched"
        wire.hold_body.set()
        for client, response, _ in (first, second):
            await response.aread()
            await response.aclose()
            await client.aclose()
        assert ctx.service.slots.active == 0
        ctx.product.database.settings.model_timeout_seconds = 5
        retry_client, retry_response, _ = await opened(ctx, "pool-wait", attempt=2)
        await retry_response.aread()
        await retry_response.aclose()
        await retry_client.aclose()
        assert wire.connections == 2


async def test_shutdown_cancels_and_settles_response_before_pool_and_slots_close(inference):
    ctx = inference
    entered = asyncio.Event()
    owner = ctx.service.upstream
    async with gateway(ctx) as wire:
        wire.hold_body.clear()

        async def active():
            client, response, _ = await opened(ctx, "shutdown")
            try:
                entered.set()
                await response.aread()
            finally:
                await response.aclose()
                await client.aclose()
                assert not owner.closed and ctx.service.slots is not None

        task = asyncio.create_task(active())
        await asyncio.wait_for(entered.wait(), 5)
        await ctx.service.close()
        assert task.cancelled() and owner.closed
        assert ctx.service.slots is None and not ctx.service.active
        assert (await fixtures.requests(ctx))[0].status == "unknown"
        await ctx.service.close()
