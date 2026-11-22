from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import get_tracer_provider
from pydantic import SecretStr, ValidationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai import tracing
from moyai.inference_usage import InferenceUsageTransport
from moyai.settings import Settings
from moyai.storage import encrypted_converter
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

PARENT = "00-12345678901234567890123456789012-1234567890123456-01"


@pytest.mark.parametrize("endpoint", [
    "https://gateway.example/v1/traces",
    "https://gateway.example/lens-ingest/v1/traces",
    "http://127.0.0.1:4318/v1/traces",
    "http://lens.lens.svc.cluster.local:4318/v1/traces",
])
def test_trace_configuration_preserves_complete_endpoint(endpoint):
    settings = Settings(_env_file=None, trace_endpoint=endpoint, trace_api_key="synthetic")
    assert settings.trace_endpoint == endpoint
    assert "synthetic" not in repr(settings)


@pytest.mark.parametrize("endpoint,key", [
    ("https://gateway.example/v1/traces", ""),
    ("", "synthetic"),
    ("http://public.example/v1/traces", "synthetic"),
    ("https://user:password@gateway.example/v1/traces", "synthetic"),
    ("https://gateway.example/v1/traces?token=secret", "synthetic"),
    ("https://gateway.example/v1/traces#fragment", "synthetic"),
    ("https://gateway.example/incorrect", "synthetic"),
])
def test_invalid_or_incomplete_tracing_configuration_is_rejected(endpoint, key):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trace_endpoint=endpoint, trace_api_key=key)


def test_trace_forwarding_does_not_forward_baggage_or_invalid_parents():
    assert tracing.trace_headers({"traceparent": PARENT, "baggage": "private=secret"}) == {
        "traceparent": PARENT,
    }
    for value in ("invalid", PARENT.replace("1234567890123456-01", "0000000000000000-01"), "x" * 10000):
        assert tracing.trace_headers({"traceparent": value}) == {}


async def test_real_inference_transport_propagates_current_span_and_keeps_broker_parent():
    seen = []

    async def wire(request):
        seen.append(dict(request.headers))
        return httpx2.Response(200, json={})

    provider = TracerProvider()
    try:
        async with httpx2.AsyncClient(transport=InferenceUsageTransport(httpx2.MockTransport(wire))) as client:
            with provider.get_tracer("test").start_as_current_span("model") as span:
                await client.post("https://gateway.invalid/v1/responses", json={})
                await client.post("https://gateway.invalid/v1/responses", json={}, headers={"traceparent": PARENT})
            assert seen[0]["traceparent"].split("-")[1] == f"{span.get_span_context().trace_id:032x}"
            assert seen[1]["traceparent"] == PARENT
            assert "baggage" not in seen[0]
    finally:
        provider.shutdown()


async def test_native_temporal_trace_is_content_free_and_replay_emits_no_duplicates(
    runtime, active, monkeypatch,
):
    """Actual Temporal/agent path; fixture model supplements the real gateway proof."""
    _, turn, _ = active
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda **kwargs: exporter)
    global_provider = get_tracer_provider()
    runtime.tracing = tracing.LensTracing(runtime.settings.model_copy(update={
        "trace_endpoint": "http://127.0.0.1:4318/v1/traces",
        "trace_api_key": SecretStr("synthetic-export-key"),
    }))
    runtime.agent.instrument = runtime.tracing.instrumentation
    converter = encrypted_converter(runtime.security, runtime.objects)
    async with await WorkflowEnvironment.start_local(
        plugins=runtime.temporal_plugins(), data_converter=converter,
    ) as environment:
        runtime.client = environment.client
        async with make_worker(runtime):
            handle = await runtime.client.start_workflow(
                TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
            )
            result = await asyncio.wait_for(handle.result(), timeout=45)
            history = await handle.fetch_history()
        assert result["status"] == "completed"
        assert runtime.tracing.logfire.force_flush()
        spans = exporter.get_finished_spans()
        assert any(span.attributes.get("gen_ai.agent.name") == "moyai" for span in spans)
        attributes = json.dumps([dict(span.attributes or {}) for span in spans])
        assert "Perform the task." not in attributes
        assert "synthetic-export-key" not in attributes
        assert not any(span.instrumentation_scope.name == "temporalio" for span in spans)
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=[TurnWorkflow],
            plugins=runtime.temporal_plugins(), data_converter=converter,
        ).replay_workflow(history)
        assert runtime.tracing.logfire.force_flush()
        assert len(exporter.get_finished_spans()) == len(spans)
    assert get_tracer_provider() is global_provider
