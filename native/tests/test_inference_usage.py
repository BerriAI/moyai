from __future__ import annotations

import asyncio
import gzip
import json
import zlib
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import httpx2 as httpx
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import ExecuteActivityInput, Replayer

from moyai import inference_usage
from moyai.agent import inference_models, make_agent
from moyai.contracts import RunScope
from moyai.execution import AuthorizedActivity
from moyai.inference_usage import (
    INFERENCE_ACTIVITY,
    MAX_CAPTURE_BYTES,
    InferenceActivity,
    InferenceUsage,
    InferenceUsageTransport,
    UsageCapture,
    money,
)
from moyai.models import Event
from moyai.performance import PREFIX
from moyai.storage import EncryptedPayloadCodec
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

SCOPE = RunScope("session", "turn", "actor", "route-alias")


@pytest.fixture(autouse=True)
def accounting_result_deadline(monkeypatch):
    # Content/receipt assertions should not depend on a loaded CI runner
    # committing to PostgreSQL within the production best-effort deadline.
    # The contention test below sets its own short deadline and verifies that
    # database stalls cannot cause another paid model request.
    monkeypatch.setattr(inference_usage, "WRITE_TIMEOUT_SECONDS", 10.0)


class WireStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, size: int = 17, fail: bool = False):
        self.body, self.size, self.fail = body, size, fail
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), self.size):
            yield self.body[offset:offset + self.size]
        if self.fail:
            raise httpx.ReadError("synthetic transport interruption")

    async def aclose(self):
        self.closed = True


@contextmanager
def bound(runtime, *, scope=SCOPE, attempt="workflow/activity/1"):
    context = InferenceActivity(runtime.database, scope, attempt)
    token = INFERENCE_ACTIVITY.set(context)
    try:
        yield context
    finally:
        INFERENCE_ACTIVITY.reset(token)


def payload(*, cost=None, identifier="response-one"):
    usage = {
        "input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
        "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
        "output_tokens_details": {"reasoning_tokens": 4},
    }
    if cost is not None:
        usage["x_litellm_response_cost"] = cost
    return {
        "id": identifier, "object": "response", "created_at": 1_792_051_200,
        "model": "provider-model", "status": "completed", "error": None,
        "output": [{
            "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "Private response content.", "annotations": []}],
        }],
        "usage": usage,
    }


def event(value):
    return b"data: " + json.dumps(value).encode() + b"\n\n"


def sse(*, cost=None):
    terminal = payload(cost=cost)
    created = {**terminal, "status": "in_progress", "usage": None, "output": []}
    return b"".join([
        event({"type": "response.created", "sequence_number": 0, "response": created}),
        event({
            "type": "response.output_item.added", "sequence_number": 1, "output_index": 0,
            "item": {**terminal["output"][0], "content": []},
        }),
        event({
            "type": "response.content_part.added", "sequence_number": 2,
            "output_index": 0, "content_index": 0, "item_id": "msg_1",
            "part": {"type": "output_text", "text": "", "annotations": []},
        }),
        event({
            "type": "response.output_text.delta", "sequence_number": 3,
            "output_index": 0, "content_index": 0, "item_id": "msg_1", "delta": "Private response content.",
        }),
        event({"type": "response.completed", "sequence_number": 4, "response": terminal}),
        b"data: [DONE]\n\n",
    ])


async def rows(runtime):
    async with runtime.database.sessions() as db:
        return list((await db.scalars(select(InferenceUsage).order_by(InferenceUsage.created_at))).all())


def transport(body, *, headers=None, status=200, size=17, fail=False):
    stream = WireStream(body, size=size, fail=fail)
    async def handler(request):
        return httpx.Response(status, headers=headers or {"content-type": "application/json"}, stream=stream)
    return InferenceUsageTransport(httpx.MockTransport(handler)), stream


async def request(runtime, body, *, headers=None, attempt="workflow/activity/1", scope=SCOPE, **kwargs):
    adapter, stream = transport(body, headers=headers, **kwargs)
    with bound(runtime, scope=scope, attempt=attempt) as context:
        async with httpx.AsyncClient(transport=adapter) as client:
            response = await client.post(
                "https://gateway.invalid/v1/responses",
                json={"model": "route-alias", "input": "Private prompt content."},
                headers={"Authorization": "Bearer synthetic-never-persist"},
            )
        return response, context, stream


@pytest.mark.parametrize("value", [None, True, False, "-1", "NaN", "Infinity", "1e100000", "1e-100000", {}, []])
def test_money_rejects_untrusted_or_unbounded_values(value):
    assert money(value) is None


@pytest.mark.parametrize(("value", "expected"), [(0, "0"), ("0.0000", "0"), ("0.012300", "0.0123"), ("1e-6", "0.000001")])
def test_money_preserves_reported_precision_and_actual_zero(value, expected):
    assert money(value) == expected


async def test_nonstream_actual_header_and_only_billing_fields_are_persisted(runtime):
    body = json.dumps(payload()).encode()
    response, context, stream = await request(runtime, body, headers={
        "content-type": "application/json", "x-litellm-response-cost": "0.012300",
        "x-litellm-call-id": "gateway-one",
    })
    assert response.content == body and stream.closed
    row, = await rows(runtime)
    assert (row.session_id, row.turn_id, row.actor_id) == ("session", "turn", "actor")
    assert (row.model, row.response_model) == ("route-alias", "provider-model")
    assert (row.provider_request_id, row.response_id) == ("gateway-one", "response-one")
    assert row.provider_timestamp == "2026-10-15T08:00:00+00:00"
    assert (row.cost_usd, row.cost_status, row.cost_source) == ("0.0123", "actual", "response_header")
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (11, 7, 18)
    assert (row.cache_read_tokens, row.cache_write_tokens, row.reasoning_tokens) == (3, 2, 4)
    assert row.status == "completed"
    persisted = repr({column.name: getattr(row, column.name) for column in InferenceUsage.__table__.columns})
    assert not any(secret in persisted for secret in (
        "Private prompt", "Private response", "synthetic-never-persist", "Authorization",
    ))
    await context.latest.persist()
    assert len(await rows(runtime)) == 1


@pytest.mark.parametrize("cost", [None, "0", "0.000000123400"])
async def test_stream_final_usage_prices_without_trusting_early_header(runtime, cost):
    body = sse(cost=cost)
    response, _, _ = await request(runtime, body, headers={
        "content-type": "text/event-stream", "x-litellm-response-cost": "999",
    })
    assert response.content == body
    row, = await rows(runtime)
    assert row.cost_usd == money(cost)
    assert row.cost_status == ("missing" if cost is None else "actual")
    assert row.cost_source == (None if cost is None else "response_usage")
    assert row.input_tokens == 11 and row.status == "completed"


async def test_nonterminal_stream_cost_is_not_an_actual_receipt(runtime):
    body = event({"type": "response.created", "response": payload(cost="42")})
    await request(runtime, body, headers={"content-type": "text/event-stream"})
    row, = await rows(runtime)
    assert row.cost_usd is None and row.cost_status == "missing" and row.status == "interrupted"
    assert row.response_id == "response-one" and row.input_tokens is None


async def test_stream_request_with_missing_content_type_still_distrusts_header(runtime):
    adapter, _ = transport(sse(), headers={"x-litellm-response-cost": "0"})
    with bound(runtime):
        async with httpx.AsyncClient(transport=adapter) as client:
            await client.post("https://gateway.invalid/v1/responses", json={"model": "route", "stream": True})
    row, = await rows(runtime)
    assert row.input_tokens == 11 and row.cost_usd is None and row.cost_status == "missing"


@pytest.mark.parametrize("location", ["response", "usage", "cost", "event"])
async def test_legacy_final_reported_cost_extensions_in_responses(runtime, location):
    value = payload()
    final = {"type": "response.completed", "response": value}
    if location == "event":
        final["x_litellm_response_cost"] = "0.009"
    elif location == "response":
        value["x_litellm_response_cost"] = "0.009"
    else:
        value["usage"]["cost" if location == "cost" else "x_litellm_response_cost"] = "0.009"
    await request(runtime, event(final), headers={"content-type": "text/event-stream"})
    row, = await rows(runtime)
    assert row.cost_usd == "0.009" and row.cost_source == "response_usage"


async def test_parallel_model_activities_keep_actor_and_turn_attribution(runtime):
    scopes = [RunScope(f"session-{i}", f"turn-{i}", f"actor-{i}", "route-alias") for i in range(2)]
    await asyncio.gather(*[
        request(runtime, json.dumps(payload(cost=f"0.{i + 1}")).encode(), scope=scope, attempt=f"workflow-{i}")
        for i, scope in enumerate(scopes)
    ])
    recorded = {row.actor_id: row for row in await rows(runtime)}
    for i, scope in enumerate(scopes):
        assert recorded[scope.actor_id].session_id == scope.session_id
        assert recorded[scope.actor_id].turn_id == scope.turn_id
        assert recorded[scope.actor_id].cost_usd == f"0.{i + 1}"
    assert INFERENCE_ACTIVITY.get() is None


async def test_each_paid_retry_counts_once_but_reobserved_receipt_never_double_bills(runtime):
    for attempt, receipt in (("attempt-1", "call-1"), ("attempt-2", "call-2"), ("attempt-3", "call-2")):
        _, context, _ = await request(runtime, json.dumps(payload(cost="0.5")).encode(), attempt=attempt, headers={
            "content-type": "application/json", "x-litellm-call-id": receipt,
        })
        await context.latest.persist()
    recorded = await rows(runtime)
    assert len(recorded) == 3
    assert sum(Decimal(row.cost_usd) for row in recorded if row.cost_usd is not None) == Decimal(1)
    assert recorded[-1].coverage_error == "duplicate_provider_receipt"
    assert recorded[-1].cost_status == "missing" and recorded[-1].status == "completed"


async def test_unpriced_receipt_does_not_prevent_later_actual_observation(runtime):
    headers = {"content-type": "application/json", "x-litellm-call-id": "same-receipt"}
    await request(runtime, json.dumps(payload()).encode(), headers=headers, attempt="attempt-1")
    await request(runtime, json.dumps(payload(cost="0.01")).encode(), headers=headers, attempt="attempt-2")
    first, second = await rows(runtime)
    assert first.cost_status == "missing" and first.receipt_key is None
    assert second.cost_status == "actual" and second.cost_usd == "0.01"


async def test_provider_error_and_retry_are_separate_attempts(runtime):
    async def disconnected(request):
        raise httpx.ConnectError("synthetic unavailable gateway", request=request)
    with bound(runtime, attempt="attempt-1"):
        async with httpx.AsyncClient(transport=InferenceUsageTransport(httpx.MockTransport(disconnected))) as client:
            with pytest.raises(httpx.ConnectError):
                await client.post("https://gateway.invalid/v1/responses", json={"model": "route-alias"})
    await request(runtime, json.dumps(payload(cost="0.3")).encode(), attempt="attempt-2")
    first, second = await rows(runtime)
    assert first.status == "failed" and first.cost_status == "missing"
    assert second.status == "completed" and second.cost_usd == "0.3"
    assert first.request_key != second.request_key


async def test_interrupted_stream_keeps_observed_response_identity(runtime):
    with pytest.raises(httpx.ReadError):
        await request(runtime, event({"type": "response.created", "response": payload()}),
                      headers={"content-type": "text/event-stream"}, fail=True)
    row, = await rows(runtime)
    assert row.response_id == "response-one" and row.status == "interrupted" and row.cost_usd is None


async def test_cancellation_is_preserved_and_keeps_missing_coverage(runtime):
    async def cancelled(request):
        raise asyncio.CancelledError()
    with bound(runtime):
        async with httpx.AsyncClient(transport=InferenceUsageTransport(httpx.MockTransport(cancelled))) as client:
            with pytest.raises(asyncio.CancelledError):
                await client.post("https://gateway.invalid/v1/responses", json={"model": "route-alias"})
    row, = await rows(runtime)
    assert row.status == "interrupted" and row.cost_usd is None


async def test_malformed_cost_number_cannot_break_delivery(runtime):
    body = b'{"id":"response-one","usage":{"cost":1e9999999999999999999999999999999999}}'
    response, _, _ = await request(runtime, body)
    assert response.content == body
    row, = await rows(runtime)
    assert row.cost_usd is None and row.coverage_error == "invalid_usage_body"


async def test_large_content_frame_does_not_reduce_accounting_coverage(runtime):
    body = event({"type": "response.output_text.delta", "delta": "x" * (MAX_CAPTURE_BYTES * 2)})
    body += sse(cost="0.4")
    response, context, _ = await request(runtime, body, headers={"content-type": "text/event-stream"}, size=8191)
    assert response.content == body and context.latest.buffer == b"" and context.latest.event == b""
    row, = await rows(runtime)
    assert row.cost_usd == "0.4" and row.coverage_error is None


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("compressed", [True, False])
@pytest.mark.parametrize("usage_first", [True, False])
async def test_large_terminal_payload_preserves_exact_cost_and_tokens(runtime, streaming, compressed, usage_first):
    final = payload(cost="0.1234567890123456789")
    final["output"] = [{
        "text": 'Private output with escaped \\" braces {} and fake "usage":{"cost":"999"} 😀' * 12_000,
        "nested": [{"usage": {"cost": "999", "input_tokens": 999}}],
    }]
    if usage_first:
        final = {"usage": final.pop("usage"), **final}
    body = (
        event({"type": "response.completed", "response": final}) + b"data: [DONE]\n\n"
        if streaming else json.dumps(final).encode()
    )
    wire = gzip.compress(body) if compressed else body
    headers = {"content-type": "text/event-stream" if streaming else "application/json"}
    if compressed:
        headers["content-encoding"] = "gzip"
    response, context, _ = await request(runtime, wire, headers=headers, size=8191)
    assert response.content == body
    row, = await rows(runtime)
    assert row.cost_usd == "0.1234567890123456789"
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (11, 7, 18)
    assert row.coverage_error is None and row.status == "completed"
    assert context.latest.projection.retained < MAX_CAPTURE_BYTES


async def test_stream_crlf_and_multiline_data_with_byte_split_transport(runtime):
    value = {"type": "response.completed", "response": payload(cost="0.025")}
    lines = json.dumps(value, indent=2, ensure_ascii=False).splitlines()
    body = b"event: response.completed\r\n" + b"".join(
        b"data: " + line.encode() + b"\r\n" for line in lines
    ) + b"\r\ndata: [DONE]\r\n\r\n"
    response, _, _ = await request(runtime, body, headers={"content-type": "text/event-stream"}, size=1)
    assert response.content == body
    row, = await rows(runtime)
    assert row.cost_usd == "0.025" and row.coverage_error is None


@pytest.mark.parametrize("empty_event", [b"data:\n\n", b"data: \r\n\r\n"])
async def test_empty_stream_events_do_not_create_false_missing_coverage(runtime, empty_event):
    body = empty_event + sse(cost="0.025")
    response, _, _ = await request(runtime, body, headers={"content-type": "text/event-stream"}, size=1)
    assert response.content == body
    row, = await rows(runtime)
    assert row.cost_usd == "0.025" and row.coverage_error is None


async def test_large_chat_choices_are_not_mistaken_for_final_usage_only_chunk(runtime):
    body = event({"choices": [{"delta": {"content": "x" * MAX_CAPTURE_BYTES * 2}}],
                  "usage": {"cost": "999", "prompt_tokens": 999}})
    body += event({"choices": [], "usage": {"cost": "0.25", "prompt_tokens": 7,
                                          "completion_tokens": 3, "total_tokens": 10}})
    body += b"data: [DONE]\n\n"
    _, _, _ = await request(runtime, body, headers={"content-type": "text/event-stream"}, size=1021)
    row, = await rows(runtime)
    assert (row.cost_usd, row.input_tokens, row.total_tokens) == ("0.25", 7, 10)
    assert row.coverage_error is None


async def test_gzip_observation_does_not_rewrite_transport_bytes(runtime):
    body = sse(cost="0.004")
    compressed = gzip.compress(body)
    response, _, _ = await request(runtime, compressed, headers={
        "content-type": "text/event-stream", "content-encoding": "gzip",
    })
    assert response.content == body and response.headers["content-encoding"] == "gzip"
    row, = await rows(runtime)
    assert row.cost_usd == "0.004" and row.input_tokens == 11


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_compressed_capture_drains_decoded_tail_when_input_ends(runtime, encoding, streaming):
    value = {
        "id": "response-one", "output": "x" * (16220 if streaming else 16267),
        "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18, "cost": "0.123456789"},
    }
    body = (
        b"".join(
            b"data: " + json.dumps({"type": kind, "response": value}, separators=(",", ":")).encode() + b"\n\n"
            for kind in ("response.created", "response.completed")
        ) if streaming else json.dumps(value, separators=(",", ":")).encode()
    )
    # The complete JSON is decodable although the compression trailer was cut.
    # With bounded output, zlib holds the closing braces after consuming every
    # input byte. HTTPX flushes those bytes to the client; capture must keep them.
    wire = gzip.compress(body, mtime=0)[:-9] if encoding == "gzip" else zlib.compress(body)[:-5]
    response, _, _ = await request(runtime, wire, size=len(wire), headers={
        "content-type": "text/event-stream" if streaming else "application/json",
        "content-encoding": encoding,
    })
    assert response.content == body
    row, = await rows(runtime)
    assert (row.cost_usd, row.input_tokens, row.output_tokens, row.total_tokens) == ("0.123456789", 11, 7, 18)
    assert row.cost_status == "actual" and row.coverage_error is None


async def test_large_wire_payload_keeps_reported_usage_ahead_of_sdk_fallback(runtime):
    body = json.dumps({**payload(), "output": "x" * (MAX_CAPTURE_BYTES + 1)}).encode()
    _, context, _ = await request(runtime, body)
    await context.result(ModelResponse(parts=[TextPart("Ignored content")], usage=RequestUsage(
        input_tokens=19, output_tokens=6, cache_read_tokens=4,
    )))
    row, = await rows(runtime)
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (11, 7, 18)
    assert row.cost_usd is None and row.coverage_error is None


async def test_sdk_default_zeros_do_not_fill_unreported_token_fields(runtime):
    _, context, _ = await request(runtime, b'{"id":"response-one","usage":{"input_tokens":5}}')
    await context.result(ModelResponse(parts=[TextPart("content")], usage=RequestUsage(input_tokens=5)))
    row, = await rows(runtime)
    assert row.input_tokens == 5
    assert row.output_tokens is None and row.total_tokens is None and row.cache_read_tokens is None
    assert row.cost_usd is None


async def test_no_scope_or_retrieval_never_creates_inference_rows(runtime):
    adapter, _ = transport(json.dumps(payload()).encode())
    async with httpx.AsyncClient(transport=adapter) as client:
        await client.post("https://gateway.invalid/v1/responses", json={"model": "route-alias"})
    with bound(runtime):
        adapter, _ = transport(json.dumps(payload()).encode())
        async with httpx.AsyncClient(transport=adapter) as client:
            await client.get("https://gateway.invalid/v1/responses/existing")
    assert await rows(runtime) == []


async def test_analytics_failure_preserves_pending_missing_coverage_and_response(runtime, monkeypatch, caplog):
    real_write = UsageCapture._write
    async def fail_final(capture):
        if capture.finished:
            raise RuntimeError("sensitive fake database exception must not be logged")
        await real_write(capture)
    monkeypatch.setattr(UsageCapture, "_write", fail_final)
    response, _, _ = await request(runtime, json.dumps(payload(cost="0.1")).encode())
    assert response.status_code == 200
    row, = await rows(runtime)
    assert row.status == "pending" and row.cost_status == "missing" and row.cost_usd is None
    assert "coverage is incomplete" in caplog.text
    assert "sensitive fake" not in caplog.text


async def test_database_contention_is_bounded_and_does_not_trigger_model_retry(runtime, monkeypatch):
    real_sessions = runtime.database.sessions
    class WaitingSession:
        async def __aenter__(self):
            await asyncio.sleep(10)
        async def __aexit__(self, *args):
            pass
    class WaitingSessions:
        def begin(self):
            return WaitingSession()
    monkeypatch.setattr(runtime.database, "sessions", WaitingSessions())
    monkeypatch.setattr(inference_usage, "WRITE_TIMEOUT_SECONDS", 0.01)
    async with asyncio.timeout(0.5):
        response, _, _ = await request(runtime, json.dumps(payload(cost="0.1")).encode())
    assert response.status_code == 200
    monkeypatch.setattr(runtime.database, "sessions", real_sessions)


@pytest.mark.parametrize("streaming", [False, True])
async def test_pinned_native_sdk_consumes_unchanged_response_and_records_cost(runtime, streaming):
    body = sse(cost="0.001234") if streaming else json.dumps(payload(cost="0.001234")).encode()
    adapter, wire = transport(body, headers={
        "content-type": "text/event-stream" if streaming else "application/json",
        "x-litellm-call-id": "native-sdk-request",
        "x-litellm-response-cost": "0",
    })
    async with httpx.AsyncClient(transport=adapter) as http_client:
        client = AsyncOpenAI(api_key="local-fixture-only", base_url="https://gateway.invalid/v1",
                             max_retries=0, http_client=http_client)
        model = OpenAIResponsesModel("route-alias", provider=LiteLLMProvider(openai_client=client))
        with bound(runtime) as context:
            messages = [ModelRequest(parts=[UserPromptPart("Private prompt content.")])]
            if streaming:
                async with model.request_stream(messages, None, ModelRequestParameters()) as streamed:
                    async for _ in streamed:
                        pass
                    result = streamed.get()
            else:
                result = await model.request(messages, None, ModelRequestParameters())
            await context.result(result)
    assert result.text == "Private response content." and wire.closed
    assert result.usage.input_tokens == 11
    row, = await rows(runtime)
    assert row.cost_usd == "0.001234" and row.provider_request_id == "native-sdk-request"
    assert row.model == "route-alias" and row.status == "completed"


async def test_execution_authority_binds_attempt_and_resets_context(runtime, active, monkeypatch):
    _, _, prepared = active
    info = SimpleNamespace(
        activity_type="agent__moyai__model_request", workflow_namespace="default",
        workflow_id="workflow", workflow_run_id="run", activity_id="activity", attempt=2,
    )
    monkeypatch.setattr("moyai.execution.activity.info", lambda: info)
    seen = []
    class Next:
        async def execute_activity(self, input):
            seen.append(INFERENCE_ACTIVITY.get())
            return ModelResponse(parts=[TextPart("ok")])
    interceptor = AuthorizedActivity(Next(), runtime.database)
    input = ExecuteActivityInput(fn=lambda: None, args=[prepared.scope], executor=None, headers={})
    result = await interceptor.execute_activity(input)
    assert result.text == "ok"
    assert seen[0].scope == prepared.scope and seen[0].identity == "default/workflow/run/activity/2"
    assert INFERENCE_ACTIVITY.get() is None
    await runtime.database.stop(await runtime.database.user(prepared.scope.actor_id), prepared.scope.session_id)
    with pytest.raises(ApplicationError):
        await interceptor.execute_activity(input)
    assert len(seen) == 1 and INFERENCE_ACTIVITY.get() is None


async def test_native_temporal_retry_counts_paid_attempts_and_replay_does_not_bill(runtime, active, tmp_path, monkeypatch):
    """Real local Temporal + native SDK; wire fixtures are supplementary E2E."""
    _, turn, prepared = active
    calls = []
    async def handler(request):
        calls.append(json.loads(request.content)["model"])
        body = sse(cost="0.01")
        if len(calls) == 1:
            body = body.removesuffix(b"data: [DONE]\n\n")
        return httpx.Response(200, headers={
            "content-type": "text/event-stream", "x-litellm-call-id": f"paid-attempt-{len(calls)}",
        }, stream=WireStream(body, fail=len(calls) == 1))
    monkeypatch.setattr(
        "moyai.agent.InferenceUsageTransport",
        lambda **kwargs: InferenceUsageTransport(httpx.MockTransport(handler), **kwargs),
    )
    settings = runtime.settings.model_copy(update={
        "gateway_url": "https://gateway.invalid/v1", "gateway_key": SecretStr("local-fixture-only"),
        "model_catalog": "",
    })
    runtime.models = inference_models(settings)
    runtime.agent, runtime.durability = make_agent(
        settings, runtime.database, runtime.stores, runtime.objects, models=runtime.models,
        workspace=LocalWorkspace(tmp_path / "computer"), skills_directory="skills",
    )
    converter = DataConverter(payload_codec=EncryptedPayloadCodec(runtime.security, runtime.objects))
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    try:
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=settings.temporal_queue,
                )
                result = await asyncio.wait_for(handle.result(), timeout=45)
                history = await handle.fetch_history()
            assert result["status"] == "completed"
            await Replayer(interceptors=[ActivityCapacity()], workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()],
                           data_converter=converter).replay_workflow(history)
    finally:
        for model in runtime.models.values():
            await model.client.close()
    recorded = await rows(runtime)
    assert calls == ["test", "test"]
    assert len(recorded) == 2 and len({row.request_key for row in recorded}) == 2
    assert all(row.actor_id == prepared.scope.actor_id and row.turn_id == turn.id for row in recorded)
    assert all(row.cost_status == "actual" for row in recorded)
    assert sum(Decimal(row.cost_usd) for row in recorded) == Decimal("0.02")
    if runtime.database.performance_tasks:
        await asyncio.gather(*runtime.database.performance_tasks)
    async with runtime.database.sessions() as db:
        dispatch, = list(await db.scalars(select(Event).where(Event.kind == PREFIX + "dispatch")))
    assert json.loads(dispatch.content)["attempt_id"] == recorded[0].id
