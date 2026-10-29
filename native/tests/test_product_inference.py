"""Disposable PostgreSQL policy tests; real app/provider proof is separate."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import httpx2
import pytest
import pytest_asyncio
import test_product_authority as authority
from app.model_slots import PostgresModelSlots
from fastapi import FastAPI
from openai import AsyncOpenAI
from pydantic_ai.messages import BinaryContent, ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.litellm import LiteLLMProvider
from sqlalchemy import delete, select, text, update
from starlette.responses import Response
from temporalio.exceptions import ApplicationError
from temporalio.worker import ExecuteActivityInput
from test_inference_usage import WireStream, payload

from moyai.analytics import ReportRange
from moyai.execution import AuthorizedActivity
from moyai.inference_usage import (
    INFERENCE_ACTIVITY,
    InferenceActivity,
    InferenceUsage,
    InferenceUsageTransport,
)
from moyai.models import Event, Session, Turn
from moyai.performance import PREFIX
from moyai.product_billing import (
    BillingReader,
    GatewayKeyScope,
    IdentityMappings,
    LegacyBillingRules,
    RecoveryPolicy,
    read_product_billing,
)
from moyai.product_inference import InferenceAdmissionError, ProductInferenceRequest, ProductInferenceService
from moyai.product_inference_routes import product_inference_routes
from moyai.product_inference_transport import BrokerUpstream

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.")
product_database = authority.product_database
admitted_product = authority.admitted_product
ACCESS_HEADERS = {
    "CF-Access-Client-Id": "synthetic-broker.access",
    "CF-Access-Client-Secret": "synthetic-access-secret",
}


@pytest_asyncio.fixture
async def inference(admitted_product):
    product = admitted_product
    session, turn, prepared = await authority.active_turn(product)
    settings = product.adapter.settings.model_copy(update={
        "agent_model": "test", "litellm_api_base": "https://gateway.invalid/v1",
        "litellm_api_key": "synthetic-inference-key", "max_agent_iterations": 0,
        "max_concurrent_model_requests": 2,
    })

    @contextmanager
    def connect():
        # Source ownership is managed by the parent process. This fixture tests
        # actual PG slot locks without constructing the entire legacy product.
        yield SimpleNamespace(check_owner=lambda: None)

    slot_database = SimpleNamespace(
        schema="moyai", connect=connect, check_owner=lambda: None,
        pool=SimpleNamespace(conninfo=product.settings.postgres_url),
    )
    service = ProductInferenceService(product.database, settings, SimpleNamespace(database=slot_database))
    product.database.product_inference = service
    await service.start(recovery=False)
    context = SimpleNamespace(
        product=product, service=service, session=session, turn=turn, scope=prepared.scope,
        calls=[], slot_database=slot_database,
    )
    yield context
    await service.close()


async def perform(ctx, *, logical="logical-one", attempt=1, cost="0.120000001", handler=None, headers=None):
    async def upstream(request):
        ctx.calls.append(dict(request.headers))
        if handler is not None:
            return await handler(request)
        return httpx2.Response(
            200, stream=WireStream(json.dumps(payload(cost=cost)).encode()),
            headers={"content-type": "application/json"},
        )

    activity = InferenceActivity(
        ctx.product.database, ctx.scope, f"{logical}/{attempt}", logical_identity=logical,
    )
    token = INFERENCE_ACTIVITY.set(activity)
    try:
        async with httpx2.AsyncClient(transport=InferenceUsageTransport(
            httpx2.MockTransport(upstream), require_admission=True,
        )) as client:
            return await client.post(
                "https://gateway.invalid/v1/responses",
                headers=headers or {"authorization": "Bearer synthetic-inference-key"},
                json={"model": "test", "input": "private prompt must not persist"},
            )
    finally:
        INFERENCE_ACTIVITY.reset(token)


async def requests(ctx):
    async with ctx.product.database.sessions() as db:
        return list((await db.scalars(select(ProductInferenceRequest).order_by(
            ProductInferenceRequest.created_at,
        ))).all())


async def install_broker_upstream(ctx, handler):
    await ctx.service.upstream.aclose()
    ctx.service.upstream = BrokerUpstream(
        ctx.service.slots.capacity, transport=httpx2.MockTransport(handler),
    )


async def test_admission_captures_exact_scope_and_physical_identity_without_legacy_run(inference):
    ctx = inference
    await perform(ctx)
    row, = await requests(ctx)
    assert row.actor_id == ctx.product.user_id and len(row.actor_id) > 200
    assert (row.session_id, row.turn_id) == (ctx.session.id, ctx.turn.id)
    assert (row.gateway_scope, row.key_hash) == ctx.service.scope()
    assert row.status == "completed" and row.cost_usd == "0.120000001"
    assert ctx.calls[0]["x-litellm-call-id"] == row.id
    assert json.loads(ctx.calls[0]["x-litellm-spend-logs-metadata"]) == {"moyai_request_id": row.id}
    async with ctx.product.database.sessions() as db:
        usage = await db.get(InferenceUsage, row.request_key)
        assert usage.cost_status == "actual" and usage.cost_usd == row.cost_usd
        assert await db.scalar(text("SELECT to_regclass('moyai.runs')")) is None
        assert await db.scalar(text("SELECT to_regclass('moyai.model_requests')")) is None
    serialized = repr({c.name: getattr(row, c.name) for c in row.__table__.columns})
    assert "private prompt" not in serialized and "synthetic-inference-key" not in serialized
    assert not ctx.service.active and ctx.service.slots.active == 0


async def test_same_logical_activity_retry_never_dispatches_again(inference):
    await perform(inference)
    with pytest.raises(InferenceAdmissionError, match="already dispatched"):
        await perform(inference, attempt=2)
    assert len(inference.calls) == len(await requests(inference)) == 1


async def test_workflow_retry_with_new_run_id_cannot_repeat_paid_activity(inference, monkeypatch):
    info = SimpleNamespace(
        activity_type="agent__moyai__model_request", workflow_namespace="local",
        workflow_id="same-turn-workflow", workflow_run_id="first-run", activity_id="model-one", attempt=1,
    )
    monkeypatch.setattr("moyai.execution.activity.info", lambda: info)
    calls = []
    async def upstream(request):
        calls.append(request)
        return httpx2.Response(
            200, stream=WireStream(json.dumps(payload(cost="0.25")).encode()),
            headers={"content-type": "application/json"},
        )
    class Next:
        async def execute_activity(self, _):
            async with httpx2.AsyncClient(transport=InferenceUsageTransport(
                httpx2.MockTransport(upstream), require_admission=True,
            )) as client:
                return await client.post(
                    "https://gateway.invalid/v1/responses",
                    headers={"authorization": "Bearer synthetic-inference-key"},
                    json={"model": "test", "input": "synthetic native workflow request"},
                )
    interceptor = AuthorizedActivity(Next(), inference.product.database)
    value = ExecuteActivityInput(fn=lambda: None, args=[inference.scope], executor=None, headers={})
    await interceptor.execute_activity(value)
    info.workflow_run_id = "retry-or-reset-run"
    with pytest.raises(ApplicationError) as denied:
        await interceptor.execute_activity(value)
    assert denied.value.non_retryable and denied.value.type == "MoyaiInferenceAdmissionDenied"
    assert len(calls) == len(await requests(inference)) == 1
    # Continue-As-New reuses activity IDs for genuinely new work. A new logical
    # agent segment must be admitted, while a reset of that segment must not.
    inference.scope.history_segment = 1
    info.workflow_run_id = "continued-execution"
    await interceptor.execute_activity(value)
    assert len(calls) == len(await requests(inference)) == 2
    info.workflow_run_id = "reset-of-continued-execution"
    with pytest.raises(ApplicationError) as denied:
        await interceptor.execute_activity(value)
    assert denied.value.type == "MoyaiInferenceAdmissionDenied"
    assert len(calls) == len(await requests(inference)) == 2


async def test_distinct_sdk_calls_are_distinct_paid_requests(inference):
    await perform(inference, logical="one")
    await perform(inference, logical="two")
    rows = await requests(inference)
    assert len(rows) == 2 and rows[0].id != rows[1].id
    assert all(row.cost_usd == "0.120000001" for row in rows)


async def test_limit_serializes_concurrent_physical_requests(inference):
    inference.service.settings.max_agent_iterations = 1
    results = await asyncio.gather(
        *(perform(inference, logical=f"parallel-{index}") for index in range(7)), return_exceptions=True,
    )
    assert sum(isinstance(result, InferenceAdmissionError) for result in results) == 4
    assert len(inference.calls) == len(await requests(inference)) == 3
    assert inference.service.slots.active == 0


async def test_revoked_product_grant_and_stop_prevent_wire(inference):
    await authority.demote(inference.product)
    with pytest.raises(Exception, match="authority changed"):
        await perform(inference)
    assert not inference.calls and not await requests(inference)


async def test_stopped_session_prevents_wire(inference):
    async with inference.product.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == inference.session.id).values(status="stopped"))
    with pytest.raises(InferenceAdmissionError, match="authority changed"):
        await perform(inference)
    assert not inference.calls and not await requests(inference)


async def test_scope_rotation_refuses_old_client_key_without_admission(inference):
    inference.service.settings.litellm_api_key = "rotated-synthetic-key"
    with pytest.raises(InferenceAdmissionError, match="credential scope"):
        await perform(inference)
    assert not inference.calls and not await requests(inference)


async def test_changed_capacity_requires_restart_before_wire(inference):
    inference.service.settings.max_concurrent_model_requests = 1
    with pytest.raises(InferenceAdmissionError, match="capacity policy changed"):
        await perform(inference)
    assert not inference.calls and not await requests(inference)


async def test_http_rejection_is_recorded_but_never_blindly_replayed(inference):
    async def rejected(_):
        return httpx2.Response(
            429, stream=WireStream(b'{"error":{"message":"synthetic limit"}}'),
            headers={"content-type": "application/json"},
        )
    response = await perform(inference, handler=rejected)
    assert response.status_code == 429
    assert (await requests(inference))[0].status == "failed"
    with pytest.raises(InferenceAdmissionError, match="already dispatched"):
        await perform(inference, attempt=2)


async def test_product_transport_cannot_bypass_scope_or_route(inference):
    calls = []
    async def upstream(request):
        calls.append(request)
        return httpx2.Response(200)
    async with httpx2.AsyncClient(transport=InferenceUsageTransport(
        httpx2.MockTransport(upstream), require_admission=True,
    )) as client:
        for method, route in (("POST", "responses"), ("POST", "completions"), ("GET", "responses/previous")):
            with pytest.raises(InferenceAdmissionError):
                await client.request(method, "https://gateway.invalid/v1/" + route)
    assert not calls and not await requests(inference)


async def test_known_preconnect_failure_can_retry_with_distinct_physical_identity(inference):
    async def fail(_):
        raise httpx2.ConnectError("synthetic connection failure")

    with pytest.raises(httpx2.ConnectError):
        await perform(inference, handler=fail)
    await perform(inference, attempt=2)
    first, second = await requests(inference)
    assert first.status == "not_dispatched" and first.cost_usd is None
    assert second.status == "completed" and first.id != second.id
    async with inference.product.database.sessions() as db:
        usage = await db.get(InferenceUsage, first.request_key)
        assert usage.status == "not_dispatched" and usage.cost_usd is None


async def test_unknown_write_failure_blocks_retry_and_keeps_missing_coverage(inference):
    async def fail(_):
        raise httpx2.WriteError("unknown remote outcome")

    with pytest.raises(httpx2.WriteError):
        await perform(inference, handler=fail)
    with pytest.raises(InferenceAdmissionError, match="already dispatched"):
        await perform(inference, attempt=2)
    row, = await requests(inference)
    assert row.status == "unknown" and row.cost_usd is None
    assert len(inference.calls) == 1


async def test_cancellation_retains_unknown_request_and_releases_slot(inference):
    entered = asyncio.Event()
    async def pending(_):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(perform(inference, handler=pending))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row, = await requests(inference)
    assert row.status == "unknown" and inference.service.slots.active == 0
    with pytest.raises(InferenceAdmissionError, match="already dispatched"):
        await perform(inference, attempt=2)


@pytest.mark.parametrize(("cleanup_lock_seconds", "cleanup_timeout", "cleared"), [
    (0, None, True),
    (1.2, None, True),
    (1.2, 0.05, False),
])
async def test_cancel_after_admission_commit_keeps_accounting_safe(
    inference, monkeypatch, cleanup_lock_seconds, cleanup_timeout, cleared,
):
    if cleanup_timeout is not None:
        monkeypatch.setattr("moyai.product_inference.UNSENT_CLEANUP_TIMEOUT_SECONDS", cleanup_timeout)
    original = inference.product.database.sessions.begin
    calls = 0
    locked = asyncio.Event()
    blockers = []

    async def hold_admission_row():
        async with inference.product.database.sessions() as db, db.begin():
            await db.scalar(select(ProductInferenceRequest).with_for_update())
            locked.set()
            await asyncio.sleep(cleanup_lock_seconds)

    @asynccontextmanager
    async def interrupted_ack():
        nonlocal calls
        calls += 1
        current = calls
        async with original() as db:
            yield db
        if current == 1:
            if cleanup_lock_seconds:
                blockers.append(asyncio.create_task(hold_admission_row()))
                await asyncio.wait_for(locked.wait(), 5)
            raise asyncio.CancelledError

    monkeypatch.setattr(inference.product.database.sessions, "begin", interrupted_ack)
    try:
        with pytest.raises(asyncio.CancelledError):
            await perform(inference)
    finally:
        await asyncio.gather(*blockers)
    assert not inference.calls
    row, = await requests(inference)
    assert row.status == ("not_dispatched" if cleared else "dispatching")
    assert row.cost_usd is None
    async with inference.product.database.sessions() as db:
        usage = await db.get(InferenceUsage, row.request_key)
        assert usage.status == ("not_dispatched" if cleared else "pending")
        assert usage.cost_usd is None
    assert inference.service.slots.active == 0
    if cleared:
        assert row.next_attempt_at is None
        await perform(inference, attempt=2)
        assert len(inference.calls) == 1
    else:
        with pytest.raises(InferenceAdmissionError, match="already dispatched"):
            await perform(inference, attempt=2)
        assert not inference.calls


async def test_legacy_and_native_slots_share_postgres_capacity(inference):
    ctx = inference
    old = PostgresModelSlots(2, ctx.slot_database)
    one, two = await old.acquire(), await old.acquire()
    task = asyncio.create_task(perform(ctx))
    try:
        await asyncio.sleep(.1)
        assert not ctx.calls
        await old.release_async(one)
        await asyncio.wait_for(task, 5)
        assert len(ctx.calls) == 1
    finally:
        await old.release_async(two)
        old.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_final_accounting_failure_does_not_repeat_paid_request(inference, monkeypatch):
    async def unavailable(_):
        raise RuntimeError("synthetic analytics outage")
    monkeypatch.setattr(inference.service, "observe", unavailable)
    result = await perform(inference)
    assert result.status_code == 200
    row, = await requests(inference)
    assert row.status == "dispatching" and row.cost_usd is None
    with pytest.raises(InferenceAdmissionError, match="already dispatched"):
        await perform(inference, attempt=2)
    assert len(inference.calls) == 1
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=spend_receipt(row)),
    )) as client:
        await inference.service.recovery.process(client, row)
    recovered, = await requests(inference)
    assert recovered.cost_usd == "0.313000001" and recovered.status == "unknown"
    async with inference.product.database.sessions() as db:
        usage = await db.get(InferenceUsage, row.request_key)
        assert usage.cost_status == "actual" and usage.coverage_error == "response_outcome_unknown"


async def test_orphan_recovery_requires_exact_owner_death_not_age(inference, monkeypatch):
    async def unavailable(_):
        raise RuntimeError("synthetic final accounting outage")
    monkeypatch.setattr(inference.service, "observe", unavailable)
    await perform(inference)
    async with inference.product.database.sessions.begin() as db:
        await db.execute(update(ProductInferenceRequest).values(created_at="2000-01-01T00:00:00+00:00"))
    recovery = inference.service.recovery
    assert await recovery.recover_orphans() == 0
    assert (await requests(inference))[0].status == "dispatching"
    await asyncio.to_thread(inference.service.slots.close)
    assert await recovery.recover_orphans() == 1
    row, = await requests(inference)
    assert row.status == "unknown"
    async with inference.product.database.sessions() as db:
        usage = await db.get(InferenceUsage, row.request_key)
        assert usage.coverage_error == "request_owner_lost"


@pytest.mark.parametrize(("header", "priced"), [("x-request-id", 2), ("x-litellm-call-id", 1)])
async def test_only_proven_gateway_call_identity_deduplicates_cost(inference, header, priced):
    async def upstream(_):
        return httpx2.Response(
            200, stream=WireStream(json.dumps(payload(cost="0.125")).encode()),
            headers={"content-type": "application/json", header: "same-id"},
        )
    await perform(inference, logical="first", handler=upstream)
    await perform(inference, logical="second", handler=upstream)
    assert sum(row.cost_usd is not None for row in await requests(inference)) == priced


def spend_receipt(row, *, cost="0.313000001", **changes):
    item = {
        "request_id": "gateway-receipt", "api_key": row.key_hash,
        "litellm_call_id": row.id, "metadata": {"moyai_request_id": row.id},
        "spend": cost, "status": "success", **changes,
    }
    return {"total_is_capped": False, "total": 1, "page_size": 2, "total_pages": 1, "page": 1, "data": [item]}


async def test_exact_recovery_reuses_product_receipt_and_never_calls_inference(inference):
    await perform(inference, cost=None)
    row, = await requests(inference)
    async def handler(request):
        assert request.method == "GET" and request.url.path == "/spend/logs/v2"
        assert request.url.params["request_id"] == row.id
        assert request.url.params["api_key"] == row.key_hash
        return httpx.Response(200, json=spend_receipt(row))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await inference.service.recovery.process(client, row)
        await inference.service.recovery.process(client, row)
    settled, = await requests(inference)
    assert settled.cost_usd == "0.313000001" and settled.cost_source == "gateway_recovery"
    assert settled.cost_receipt_id == "gateway-receipt" and len(inference.calls) == 1


async def test_repaired_receipt_permission_recovers_previously_parked_charge(inference):
    await perform(inference, cost=None)
    row, = await requests(inference)
    async with inference.product.database.sessions.begin() as db:
        saved = await db.get(ProductInferenceRequest, row.id)
        saved.recovery_error = "receipt_access_denied"
        saved.recovery_attempts = 1
        saved.next_attempt_at = None
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=spend_receipt(row)),
    )) as client:
        assert await inference.service.recovery.poll(client) == 1
        assert await inference.service.recovery.poll(client) == 0
    settled, = await requests(inference)
    assert settled.cost_usd == "0.313000001"
    assert settled.recovery_error is None and settled.next_attempt_at is None
    assert len(inference.calls) == 1


@pytest.mark.parametrize("prepared", [False, True])
async def test_failed_product_preparation_does_not_make_recorded_costs_partial(inference, prepared):
    from moyai.analytics_service import report
    from moyai.contracts import NewMessage, NewSession

    ctx = inference
    await perform(ctx)
    session = await ctx.product.database.create_session(ctx.product.actor, NewSession())
    failed = await ctx.product.database.submit(
        ctx.product.actor, session.id, NewMessage(prompt="Never dispatched", request_key="preflight"),
    )
    if prepared:
        assert (await ctx.product.database.claim_next()).id == failed.id
        await ctx.product.database.prepare(failed.id)
    await ctx.product.database.fail(failed.id, "Synthetic pre-dispatch preparation failure.")
    async with ctx.product.database.sessions.begin() as db:
        paid = await db.get(Turn, ctx.turn.id)
        paid.usage = json.dumps({"requests": 1, "input_tokens": 11, "output_tokens": 7})
    runtime = SimpleNamespace(database=ctx.product.database, security=ctx.product.database.security)
    value = await report(runtime, ctx.product.actor, None, None, None, None, None, 100, 0, "json")
    assert value["summary"]["pricing_status"] == "priced"
    assert value["summary"]["cost_usd_exact"] == "0.120000001"
    assert value["coverage"]["undispatched_failed_turns"] == 1
    assert value["coverage"]["unknown_request_turns"] == 0
    assert value["summary"]["unpriced_turns"] == 0
    assert next(row for row in value["rows"] if row["id"] == failed.id)["pricing_status"] == "empty"


@pytest.mark.parametrize("missing_usage_row", [False, True])
async def test_failed_product_turn_with_observed_attempt_remains_unpriced(inference, missing_usage_row):
    from moyai.analytics_service import report

    ctx = inference
    await perform(ctx, cost=None)
    async with ctx.product.database.sessions.begin() as db:
        turn = await db.get(Turn, ctx.turn.id)
        turn.status, turn.preparation, turn.usage = "failed", None, "{}"
        if missing_usage_row:
            await db.execute(delete(InferenceUsage).where(InferenceUsage.turn_id == turn.id))
    runtime = SimpleNamespace(database=ctx.product.database, security=ctx.product.database.security)
    value = await report(runtime, ctx.product.actor, None, None, None, None, None, 100, 0, "json")
    assert value["summary"]["pricing_status"] == "unpriced"
    assert value["coverage"]["undispatched_failed_turns"] == 0
    if not missing_usage_row:
        assert value["summary"]["unpriced_requests"] == 1
    else:
        assert value["coverage"]["unknown_request_turns"] == 1


async def test_proven_unsent_product_failure_does_not_invent_missing_cost(inference):
    from moyai.analytics_service import report

    ctx = inference

    async def never_connected(_):
        raise httpx2.ConnectError("Synthetic pre-connect failure.")

    with pytest.raises(httpx2.ConnectError):
        await perform(ctx, handler=never_connected)
    await ctx.product.database.fail(ctx.turn.id, "Failed before connecting to the model.")
    runtime = SimpleNamespace(database=ctx.product.database, security=ctx.product.database.security)
    value = await report(runtime, ctx.product.actor, None, None, None, None, None, 100, 0, "json")
    assert value["summary"]["requests"] == value["summary"]["unpriced_requests"] == 0
    assert value["summary"]["pricing_status"] == "empty"
    assert value["coverage"]["undispatched_failed_turns"] == 1
    assert value["coverage"]["unknown_request_turns"] == 0


async def test_long_responses_receipt_keeps_exact_cost_and_scoped_identity(inference):
    await perform(inference, cost=None)
    row, = await requests(inference)
    receipt_id = "resp_" + "a" * 200
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=spend_receipt(row, request_id=receipt_id)),
    )) as client:
        await inference.service.recovery.process(client, row)
        await inference.service.recovery.process(client, row)
    saved, = await requests(inference)
    assert saved.cost_usd == "0.313000001"
    assert saved.cost_receipt_id == "sha256:" + hashlib.sha256(receipt_id.encode()).hexdigest()
    assert saved.receipt_key == hashlib.sha256(
        f"{saved.gateway_scope}/{saved.key_hash}/{receipt_id}".encode()
    ).hexdigest()
    assert len(inference.calls) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_receipt_access_denial_is_backed_off_without_replaying_inference(inference, status):
    await perform(inference, cost=None)
    row, = await requests(inference)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status),
    )) as client:
        await inference.service.recovery.process(client, row)
        saved, = await requests(inference)
        assert saved.recovery_error == "receipt_access_denied"
        assert saved.next_attempt_at is not None
        assert saved.recovery_attempts == 1 and saved.cost_usd is None
        assert await inference.service.recovery.poll(client) == 0
    assert len(inference.calls) == 1


@pytest.mark.parametrize(("changes", "error"), [
    ({"api_key": "wrong-scope"}, "scope_mismatch"),
    ({"litellm_call_id": "different-request"}, "conflicting_identifiers"),
    ({"spend": "0"}, "unverified_zero"),
])
async def test_recovery_rejects_unproven_cost(inference, changes, error):
    await perform(inference, cost=None)
    row, = await requests(inference)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=spend_receipt(row, **changes)),
    )) as client:
        await inference.service.recovery.process(client, row)
    row, = await requests(inference)
    assert row.cost_usd is None and row.recovery_error == error and row.next_attempt_at is None


async def test_key_rotation_during_recovery_does_not_assign_receipt(inference):
    await perform(inference, cost=None)
    row, = await requests(inference)
    def handler(_):
        inference.service.settings.litellm_api_key = "rotated-during-lookup"
        return httpx.Response(200, json=spend_receipt(row))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await inference.service.recovery.process(client, row)
    assert (await requests(inference))[0].cost_usd is None


@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("image_size", [0, 7 * 1024 * 1024])
async def test_broker_relay_has_one_scoped_ledger_and_blocks_paid_replay(inference, protected, image_size):
    ctx = inference
    ctx.service.settings = ctx.service.settings.model_copy(update={
        "public_url": "https://workspace.invalid",
        "cloudflare_access_client_id": ACCESS_HEADERS["CF-Access-Client-Id"] if protected else "",
        "cloudflare_access_client_secret": ACCESS_HEADERS["CF-Access-Client-Secret"] if protected else "",
    })
    image = b"x" * image_size
    calls = []
    async def upstream(request):
        assert request.headers["authorization"] == "Bearer synthetic-inference-key"
        assert all(name not in request.headers for name in ACCESS_HEADERS)
        if image:
            assert len(request.content) > 8 * 1024 * 1024
            body = json.loads(request.content)
            image_url = body["input"][0]["content"][1]["image_url"]
            assert base64.b64decode(image_url.split(",", 1)[1]) == image
        calls.append(request)
        return httpx2.Response(
            200, stream=WireStream(json.dumps(payload(cost="0.43")).encode()),
            headers={"content-type": "application/json", "x-litellm-call-id": "exact-call-id"},
        )
    await install_broker_upstream(ctx, upstream)
    app = FastAPI()
    app.include_router(product_inference_routes(ctx.service))
    broker_calls = []

    @app.middleware("http")
    async def access_gate(request, call_next):
        broker_calls.append(request)
        assert str(request.url) == "https://workspace.invalid/broker/native/v1/responses"
        if protected and any(request.headers.get(name) != value for name, value in ACCESS_HEADERS.items()):
            return Response("Synthetic Cloudflare Access refusal", status_code=401)
        if not protected:
            assert all(name not in request.headers for name in ACCESS_HEADERS)
        return await call_next(request)

    worker_database = SimpleNamespace(
        settings=ctx.product.settings, product_identity=ctx.product.adapter,
    )
    worker = ProductInferenceService(worker_database, ctx.service.settings.model_copy(update={
        "moyai_separate_broker": True, "moyai_runtime_role": "worker",
        "litellm_api_key": "",
    }), None)
    worker_database.product_inference = worker
    await worker.start()
    try:
        for attempt, expected in ((1, 200), (2, 409)):
            activity = InferenceActivity(
                worker_database, ctx.scope, f"proxy/{attempt}", logical_identity="proxy",
                foreground=True,
            )
            token = INFERENCE_ACTIVITY.set(activity)
            try:
                async with httpx2.AsyncClient(transport=InferenceUsageTransport(
                    httpx2.ASGITransport(app=app), require_admission=True,
                )) as client:
                    if attempt == 1:
                        sdk = AsyncOpenAI(
                            base_url="https://gateway.invalid/v1", api_key="not-configured",
                            max_retries=0, http_client=client,
                        )
                        model = OpenAIResponsesModel("test", provider=LiteLLMProvider(openai_client=sdk))
                        result = await model.request(
                            [ModelRequest(parts=[UserPromptPart(
                                ["private input", BinaryContent(data=image, media_type="image/png")]
                                if image else "private input",
                            )])],
                            None, ModelRequestParameters(),
                        )
                        assert result.usage.input_tokens == 11
                    else:
                        response = await client.post(
                            "https://gateway.invalid/v1/responses",
                            headers={"authorization": "Bearer not-configured"},
                            json={"model": "test", "input": "private input"},
                        )
                        assert response.status_code == expected
                assert activity.latest is None
                assert bool(activity.admission_error) == (attempt == 2)
            finally:
                INFERENCE_ACTIVITY.reset(token)
        row, = await requests(ctx)
        assert row.cost_usd == "0.43" and row.status == "completed"
        assert len(calls) == 1 and row.id == calls[0].headers["x-litellm-call-id"]
        assert row.gateway_id == "exact-call-id"
        assert len(broker_calls) == 2
        if ctx.product.database.performance_tasks:
            await asyncio.gather(*ctx.product.database.performance_tasks)
        async with ctx.product.database.sessions() as db:
            timing, = list(await db.scalars(select(Event).where(Event.kind == PREFIX + "dispatch")))
        details = json.loads(timing.content)
        assert timing.turn_id == ctx.turn.id and details["route"] == "broker"
        assert details["attempt_id"] == row.request_key
        assert details["broker_received_at"] <= timing.created_at
    finally:
        await worker.close()


@pytest.mark.parametrize("broker_url", ["", "https://broker.invalid"])
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("route", ["responses", "responses/compact", "chat/completions"])
async def test_broker_request_uses_only_configured_access_credentials(inference, protected, route, broker_url):
    from moyai import product_inference_routes as routes
    from moyai.inference_usage import UsageCapture

    service = inference.service
    service.settings = service.settings.model_copy(update={
        "public_url": "https://workspace.invalid/",
        "broker_url": broker_url,
        "cloudflare_access_client_id": ACCESS_HEADERS["CF-Access-Client-Id"] if protected else "",
        "cloudflare_access_client_secret": ACCESS_HEADERS["CF-Access-Client-Secret"] if protected else "",
    })
    activity = InferenceActivity(
        inference.product.database, inference.scope, "signed/1", logical_identity="signed", requests=1,
    )
    original = httpx2.Request(
        "POST", "https://gateway.invalid/v1/" + route, json={"model": "test"},
        headers={
            "Authorization": "Bearer synthetic-inference-key", "Accept": "text/event-stream",
            **dict.fromkeys(ACCESS_HEADERS, "untrusted-incoming-value"),
        },
    )
    signed = routes.broker_request(service, original, UsageCapture(activity, "f" * 64, original))
    assert str(signed.url) == (broker_url or "https://workspace.invalid") + "/broker/native/v1/" + route
    assert signed.content == original.content and signed.headers["accept"] == "text/event-stream"
    assert signed.headers["accept-encoding"] == "identity"
    assert signed.headers["authorization"] != original.headers["authorization"]
    for name, value in ACCESS_HEADERS.items():
        assert signed.headers.get(name) == (value if protected else None)
        assert original.headers[name] == "untrusted-incoming-value"
    scope, identity, logical, ordinal, foreground = routes._verify(
        service, signed.headers["authorization"], route, original.content,
    )
    assert (scope.actor_id, scope.turn_id) == (inference.scope.actor_id, inference.scope.turn_id)
    assert (identity, logical, ordinal) == ("signed/1", "signed", 1)
    assert foreground is False
    assert all(not grant.credential for grant in scope.grants)
    envelope = json.dumps(routes._signer(service).loads(signed.headers["authorization"].removeprefix("Bearer ")))
    assert all(value not in envelope for value in ACCESS_HEADERS.values())


async def test_broker_rejects_missing_signature_and_altered_payload(inference, monkeypatch):
    from itsdangerous.timed import TimestampSigner

    from moyai.inference_usage import UsageCapture
    from moyai.product_inference_routes import broker_request

    inference.service.settings = inference.service.settings.model_copy(update={
        "cloudflare_access_client_id": ACCESS_HEADERS["CF-Access-Client-Id"],
        "cloudflare_access_client_secret": ACCESS_HEADERS["CF-Access-Client-Secret"],
    })
    app = FastAPI()
    app.include_router(product_inference_routes(inference.service))
    activity = InferenceActivity(
        inference.product.database, inference.scope, "signed/1", logical_identity="signed", requests=1,
    )
    original = httpx2.Request("POST", "https://gateway.invalid/v1/responses", json={"model": "test"})
    stamp = TimestampSigner("synthetic").get_timestamp()
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: stamp)
    signed = broker_request(inference.service, original, UsageCapture(activity, "f" * 64, original))
    assert "synthetic-inference-key" not in signed.headers["authorization"]
    assert all(signed.headers[name] == value for name, value in ACCESS_HEADERS.items())
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as client:
        unsigned = await client.post(str(signed.url), headers=ACCESS_HEADERS, content=original.content)
        altered = await client.post(str(signed.url), headers=signed.headers, content=b'{"model":"other"}')
        wrong_route = await client.post(str(signed.url) + "/compact", headers=signed.headers, content=original.content)
        monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: stamp + 302)
        expired = await client.post(str(signed.url), headers=signed.headers, content=original.content)
    assert unsigned.status_code == altered.status_code == wrong_route.status_code == expired.status_code == 403
    assert not await requests(inference)


async def test_admitted_native_billing_joins_canonical_actor_and_exact_source_receipt(inference):
    ctx = inference
    await perform(ctx)
    row, = await requests(ctx)
    async def no_wire(_):
        raise httpx2.ConnectError("synthetic known-unsent reservation")
    with pytest.raises(httpx2.ConnectError):
        await perform(ctx, logical="unsent-reservation", handler=no_wire)
    # A separate broker observation of this same physical request is not a
    # second charge. These isolated source rows test the real read projection.
    async with ctx.product.source.begin() as db:
        await db.execute(text("""CREATE TABLE moyai.model_requests (
            id TEXT PRIMARY KEY,model TEXT,created_at TEXT,status TEXT,
            run_id TEXT,user_id TEXT,cost TEXT,key_hash TEXT,gateway_scope TEXT)"""))
        await db.execute(text("CREATE TABLE moyai.run_roots(run_id TEXT,root_id TEXT)"))
        await db.execute(text("""INSERT INTO moyai.model_requests VALUES (
            :id,'test',:created,'completed','source-session',:actor,:cost,:key,:gateway)"""), {
            "id": row.id, "created": row.created_at, "actor": row.actor_id, "cost": row.cost_usd,
            "key": row.key_hash, "gateway": row.gateway_scope,
        })
    clock = datetime.now(UTC)
    period = ReportRange(clock - timedelta(days=1), clock + timedelta(days=1), "custom")
    async with ctx.product.source.connect() as old, ctx.product.database.engine.connect() as new:
        projection = await read_product_billing(
            BillingReader(old, schema="moyai"), BillingReader(new, schema=ctx.product.schema), period,
            rules=LegacyBillingRules.from_product(), mappings=IdentityMappings("v", "test"),
            policy=RecoveryPolicy(True, GatewayKeyScope(*ctx.service.scope())), clock=clock,
        )
    charge, = projection.charges
    assert charge.actor_id == "legacy:" + row.actor_id
    assert str(charge.amount) == row.cost_usd
    assert len(charge.observations) == 2
    assert {item.source.engine for item in charge.observations} == {"legacy", "native"}
