"""Memory-maintenance and broker settlement boundaries on real local PostgreSQL."""

import asyncio
import json
import os
from types import SimpleNamespace

import anyio
import httpx2
import pytest
import pytest_asyncio
import test_product_inference as inference_fixtures
import test_product_memory as memory_fixtures
from fastapi import FastAPI
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select, text, update
from test_inference_usage import WireStream, payload
from test_product_memory_store import memory_execution

from moyai.inference_usage import InferenceActivity, InferenceUsageTransport, UsageCapture
from moyai.models import Session, Turn
from moyai.product_drain_models import ProductDrainState
from moyai.product_inference import InferenceAdmissionError, ProductInferenceRequest
from moyai.product_inference_routes import broker_request, product_inference_routes
from moyai.product_memory import ProductMemoryService
from moyai.product_memory_review import WORKFLOW_PREFIX, ProductMemoryReviewService

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
product_database = inference_fixtures.product_database
admitted_product = inference_fixtures.admitted_product
inference = inference_fixtures.inference


@pytest_asyncio.fixture
async def maintenance(inference, monkeypatch):
    ctx = inference
    product = ctx.product
    async with product.source.begin() as db:
        for statement in memory_fixtures.MEMORY_SCHEMA:
            await db.execute(text(statement))
    product.adapter.settings.agent_model = "test"
    product.adapter.settings.memory_review_idle_seconds = 0
    memory = ProductMemoryService(product.database, product.adapter)
    review = ProductMemoryReviewService(memory, {
        "test": FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart('{"memories":[]}')])),
    })

    async def budget(payload):
        return SimpleNamespace(input_budget=8000)

    monkeypatch.setattr(review.context_budget, "measure", budget)
    async with product.database.sessions.begin() as db:
        await db.execute(update(ProductDrainState).where(ProductDrainState.id == 1).values(paused=False, generation=1))
    session, turn, scope, _ = await memory_execution(product)
    product.database.product_memory_review = review
    assert await product.database.save_result(
        scope, conversation='{"messages":[]}', output="Original answer", workspace=None, usage={},
    )
    await product.database.settle(turn.id)
    prepared = await review.prepare(turn.id)
    ctx.service.memory_review = review
    ctx.review, ctx.review_scope = review, prepared.scope
    ctx.review_turn, ctx.review_session = turn, session
    ctx.logical_review = f"{product.settings.temporal_namespace}/{WORKFLOW_PREFIX}{turn.id}/{turn.id}/model-0"
    return ctx


async def test_completed_turn_review_uses_original_grant_and_existing_paid_ledger_barrier(maintenance):
    ctx = maintenance
    reviewed = SimpleNamespace(**{**vars(ctx), "scope": ctx.review_scope})
    await inference_fixtures.perform(reviewed, logical=ctx.logical_review)
    row, = await inference_fixtures.requests(ctx)
    assert row.turn_id == ctx.review_turn.id and row.actor_id == ctx.review_scope.actor_id
    assert row.authority_version == ctx.review_scope.product_grant.authority_version
    assert row.status == "completed"
    async with ctx.product.database.sessions() as db:
        assert (await db.get(Turn, ctx.review_turn.id)).status == "completed"
        assert (await db.get(Session, ctx.review_session.id)).active_turn is None
    with pytest.raises(InferenceAdmissionError, match="paid outcome"):
        await inference_fixtures.perform(reviewed, logical=ctx.logical_review, attempt=2)
    assert len(ctx.calls) == 1


async def test_foreground_reclaims_review_slot_and_interrupted_paid_review_cannot_repeat(maintenance):
    ctx = maintenance
    await ctx.service.close()
    ctx.service.settings.max_concurrent_model_requests = 1
    await ctx.service.start(recovery=False)
    entered = asyncio.Event()
    reviewed = SimpleNamespace(**{**vars(ctx), "scope": ctx.review_scope})

    async def held_provider(request):
        entered.set()
        await asyncio.Future()

    background = ctx.service.slots.run_maintenance(
        inference_fixtures.perform(reviewed, logical=ctx.logical_review, handler=held_provider)
    )
    await asyncio.wait_for(entered.wait(), 3)
    await asyncio.wait_for(inference_fixtures.perform(ctx, logical="foreground-owns-next-slot"), 5)
    assert background.cancelled()
    async with ctx.product.database.sessions() as db:
        rows = list(await db.scalars(select(ProductInferenceRequest)))
        review_row = next(row for row in rows if row.turn_id == ctx.review_turn.id)
        foreground = next(row for row in rows if row.turn_id == ctx.turn.id)
        assert review_row.status == "unknown" and foreground.status == "completed"
    with pytest.raises(InferenceAdmissionError, match="paid outcome"):
        await inference_fixtures.perform(reviewed, logical=ctx.logical_review, attempt=2)
    assert len(ctx.calls) == 2 and ctx.service.slots.active == 0


def signed_review(ctx, body=None):
    request = httpx2.Request(
        "POST", "https://gateway.invalid/v1/responses",
        json=body or {"model": "test", "input": "private review input"},
    )
    activity = InferenceActivity(
        ctx.product.database, ctx.review_scope, "review-proxy/1",
        logical_identity=ctx.logical_review, requests=1,
    )
    return broker_request(ctx.service, request, UsageCapture(activity, "e" * 64, request))


async def test_signed_review_broker_uses_original_completed_scope_without_stream_or_tools(maintenance, monkeypatch):
    from moyai import product_inference_routes as routes

    ctx = maintenance
    calls = []

    async def upstream(request):
        calls.append(request)
        return httpx2.Response(200, stream=WireStream(json.dumps(payload(cost="0.07")).encode()), headers={
            "content-type": "application/json", "x-litellm-call-id": "review-broker-receipt",
        })

    monkeypatch.setattr(routes, "InferenceUsageTransport", lambda **kwargs: InferenceUsageTransport(
        httpx2.MockTransport(upstream), **kwargs,
    ))
    app = FastAPI()
    app.include_router(product_inference_routes(ctx.service))
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        for body in ({"model": "test", "stream": True}, {"model": "test", "tools": [{"type": "function"}]}):
            signed = signed_review(ctx, body)
            response = await client.post(str(signed.url), headers=signed.headers, content=signed.content)
            assert response.status_code == 409
        assert not calls
        signed = signed_review(ctx)
        response = await client.post(str(signed.url), headers=signed.headers, content=signed.content)
        assert response.status_code == 200
        repeat = await client.post(str(signed.url), headers=signed.headers, content=signed.content)
        assert repeat.status_code == 409
    row, = await inference_fixtures.requests(ctx)
    assert row.turn_id == ctx.review_turn.id and row.status == "completed"
    assert row.cost_usd == "0.07" and row.gateway_id == "review-broker-receipt"
    assert len(calls) == 1 and ctx.service.slots.active == 0


async def test_remote_review_keeps_reclaimable_slot_through_response_body(maintenance, monkeypatch):
    from moyai import product_inference_routes as routes

    ctx = maintenance
    await ctx.service.close()
    ctx.service.settings.max_concurrent_model_requests = 1
    await ctx.service.start(recovery=False)
    body_started = asyncio.Event()

    class DelayedBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            body_started.set()
            await asyncio.Future()
            yield b""  # pragma: no cover - cancelled by foreground slot reclamation

    async def upstream(request):
        return httpx2.Response(200, stream=DelayedBody(), headers={"content-type": "application/json"})

    monkeypatch.setattr(routes, "InferenceUsageTransport", lambda **kwargs: InferenceUsageTransport(
        httpx2.MockTransport(upstream), **kwargs,
    ))
    app = FastAPI()
    app.include_router(product_inference_routes(ctx.service))
    signed = signed_review(ctx)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        pending = asyncio.create_task(client.post(str(signed.url), headers=signed.headers, content=signed.content))
        await asyncio.wait_for(body_started.wait(), 3)
        await asyncio.wait_for(inference_fixtures.perform(ctx, logical="foreground-during-review-body"), 5)
        response = await asyncio.wait_for(pending, 3)
        assert response.status_code == 503 and response.headers["x-moyai-memory-preempted"] == "1"
    async with ctx.product.database.sessions() as db:
        rows = list(await db.scalars(select(ProductInferenceRequest)))
        assert next(row for row in rows if row.turn_id == ctx.review_turn.id).status == "unknown"
        assert next(row for row in rows if row.turn_id == ctx.turn.id).status == "completed"
    assert ctx.service.slots.active == 0


async def test_completed_broker_response_settles_under_asgi_level_cancellation(inference, monkeypatch):
    ctx = inference
    entered, release, done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scope_ready = asyncio.get_running_loop().create_future()
    observe = ctx.service.observe

    async def held_receipt(capture):
        if capture.finished:
            entered.set()
            await release.wait()
        await observe(capture)

    async def upstream(request):
        return httpx2.Response(200, stream=WireStream(json.dumps(payload(cost="0.05")).encode()), headers={
            "content-type": "application/json", "x-litellm-call-id": "confirmed-before-disconnect",
        })

    async def caller():
        with anyio.CancelScope() as scope:
            scope_ready.set_result(scope)
            await inference_fixtures.perform(ctx, handler=upstream)
        done.set()

    monkeypatch.setattr(ctx.service, "observe", held_receipt)
    async with anyio.create_task_group() as group:
        group.start_soon(caller)
        scope = await scope_ready
        await asyncio.wait_for(entered.wait(), 3)
        scope.cancel()
        # Another event-loop task still advances while cleanup owns its write.
        await asyncio.sleep(0.03)
        assert not done.is_set()
        release.set()
        await asyncio.wait_for(done.wait(), 3)
    row, = await inference_fixtures.requests(ctx)
    assert row.status == "completed" and row.gateway_id == "confirmed-before-disconnect"
    assert row.cost_usd == "0.05" and ctx.service.slots.active == 0
