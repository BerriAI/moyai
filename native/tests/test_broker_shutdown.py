"""Deterministic shutdown scheduling; live broker proof is recorded separately."""

from __future__ import annotations

import asyncio
import os

import httpx2 as httpx
import pytest
import pytest_asyncio
import test_product_inference as inference_fixtures
import test_product_memory_broker as memory_fixtures
from fastapi import FastAPI
from test_inference_usage import WireStream

from moyai.inference_usage import INFERENCE_ACTIVITY, InferenceActivity, UsageCapture
from moyai.product_inference import InferenceAdmissionError
from moyai.product_inference_routes import broker_request, product_inference_routes

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
admitted_product = inference_fixtures.admitted_product
inference = inference_fixtures.inference
maintenance = memory_fixtures.maintenance


@pytest_asyncio.fixture(params=["foreground", "maintenance"])
async def broker(maintenance, request):
    ctx = maintenance
    ctx.kind = request.param
    scope = ctx.review_scope if ctx.kind == "maintenance" else ctx.scope
    logical = ctx.logical_review if ctx.kind == "maintenance" else "shutdown-foreground"
    activity = InferenceActivity(
        ctx.product.database, scope, logical + "/1", logical_identity=logical, requests=1,
    )
    original = httpx.Request("POST", "https://gateway.invalid/v1/responses", json={"model": "test"})
    ctx.signed = broker_request(ctx.service, original, UsageCapture(activity, "e" * 64, original))
    app = FastAPI()
    app.include_router(product_inference_routes(ctx.service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False)) as client:
        ctx.caller = client
        yield ctx


async def relay(ctx):
    signed = ctx.signed
    return await ctx.caller.post(str(signed.url), headers=signed.headers, content=signed.content)


async def test_injected_send_barrier_preserves_known_unsent_shutdown_refusal(broker, monkeypatch):
    ctx = broker
    entered, release = asyncio.Event(), asyncio.Event()
    owner = ctx.service.upstream
    factory = ctx.service.upstream_client
    activities = []

    async def before_send(request):
        # Injected scheduling barrier: the default broker has no request hook.
        # This tests the borrowed-client lifetime contract, not a naturally
        # observed production interleaving between client construction and send.
        activities.append(INFERENCE_ACTIVITY.get())
        entered.set()
        await release.wait()

    def client():
        value = factory()
        value.event_hooks["request"].append(before_send)
        return value

    monkeypatch.setattr(ctx.service, "upstream_client", client)
    pending = asyncio.create_task(relay(ctx))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not ctx.service.active and not await inference_fixtures.requests(ctx)
        await ctx.service.close()
        assert owner.closed
        release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 409
        assert response.headers["x-moyai-inference-denied"] == "1"
        assert isinstance(activities[0].admission_error, InferenceAdmissionError)
        assert activities[0].latest is None
        assert not await inference_fixtures.requests(ctx)
        assert INFERENCE_ACTIVITY.get() is None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_closed_pool_client_creation_is_an_admission_refusal(broker):
    ctx = broker
    # Direct owner closure exercises the factory's defensive closed-resource
    # check. Ordinary service.close() first sets started=False, tested below.
    await ctx.service.upstream.aclose()
    response = await relay(ctx)
    assert response.status_code == 409
    assert response.headers["x-moyai-inference-denied"] == "1"
    assert not await inference_fixtures.requests(ctx)


async def test_request_after_service_shutdown_is_denied_before_scheduling(broker):
    ctx = broker
    await ctx.service.close()
    response = await relay(ctx)
    assert response.status_code == 409
    assert response.headers["x-moyai-inference-denied"] == "1"
    assert not await inference_fixtures.requests(ctx)


async def test_shutdown_during_existing_authorization_boundary_is_denied(broker, monkeypatch):
    ctx = broker
    authorizer = ctx.service.memory_review if ctx.kind == "maintenance" else ctx.service.database
    authorize = authorizer.authorize_scope
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_authorization(scope, **kwargs):
        # The route already awaits this database-backed authority boundary.
        # Hold it before client construction; no send hook is installed.
        if "db" not in kwargs:
            entered.set()
            await release.wait()
        return await authorize(scope, **kwargs)

    monkeypatch.setattr(authorizer, "authorize_scope", held_authorization)
    pending = asyncio.create_task(relay(ctx))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await ctx.service.close()
        release.set()
        response = await asyncio.wait_for(pending, 5)
        assert response.status_code == 409
        assert response.headers["x-moyai-inference-denied"] == "1"
        assert not await inference_fixtures.requests(ctx)
        assert INFERENCE_ACTIVITY.get() is None
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_unrelated_runtime_failure_stays_unknown_and_cannot_replay(broker):
    ctx = broker
    calls = []

    async def failed(request):
        calls.append(request)
        raise RuntimeError("Unclassified upstream failure.")

    await inference_fixtures.install_broker_upstream(ctx, failed)
    response = await relay(ctx)
    assert response.status_code == 502
    assert "x-moyai-inference-denied" not in response.headers
    row, = await inference_fixtures.requests(ctx)
    assert row.status == "unknown" and ctx.service.slots.active == 0
    repeat = await relay(ctx)
    assert repeat.status_code == 409 and repeat.headers["x-moyai-inference-denied"] == "1"
    assert len(calls) == 1
    assert not ctx.service.upstream.closed and INFERENCE_ACTIVITY.get() is None


async def test_closed_client_factory_does_not_mark_ambient_activity(inference):
    ctx = inference
    activity = InferenceActivity(ctx.product.database, ctx.scope, "unrelated-caller")
    await ctx.service.upstream.aclose()
    token = INFERENCE_ACTIVITY.set(activity)
    try:
        with pytest.raises(InferenceAdmissionError):
            ctx.service.upstream_client()
        assert activity.admission_error is None
    finally:
        INFERENCE_ACTIVITY.reset(token)


async def test_oversized_maintenance_reply_is_not_an_unsent_denial(maintenance, monkeypatch):
    from moyai import product_inference_routes as routes

    ctx = maintenance

    async def oversized(request):
        return httpx.Response(200, stream=WireStream(b"x" * 65))

    await inference_fixtures.install_broker_upstream(ctx, oversized)
    monkeypatch.setattr(routes, "MAX_MEMORY_REVIEW_RESPONSE_BYTES", 64)
    signed = memory_fixtures.signed_review(ctx, {"model": "test"})
    app = FastAPI()
    app.include_router(product_inference_routes(ctx.service))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
        response = await client.post(str(signed.url), headers=signed.headers, content=signed.content)
    assert response.status_code == 502 and "x-moyai-inference-denied" not in response.headers
    row, = await inference_fixtures.requests(ctx)
    assert row.status == "unknown" and ctx.service.slots.active == 0
