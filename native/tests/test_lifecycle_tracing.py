"""Framework integration and domain evidence; live application proof is separate."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from pydantic import SecretStr
from test_application_boundaries import sign_in
from test_computer_recovery import REFERENCE, REPLACEMENT, saved_login

from moyai import tracing
from moyai.lifecycle_tracing import task_state
from moyai.models import Session, Turn


@pytest.fixture
def exporter(runtime, monkeypatch, request):
    result = InMemorySpanExporter()
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda **kwargs: result)
    runtime.settings = runtime.settings.model_copy(update={
        "trace_endpoint": "http://127.0.0.1:4318/v1/traces", "trace_api_key": SecretStr("private-export-key"),
        "trace_include_content": getattr(request, "param", False),
    })
    runtime.configure_tracing()
    return result


def spans(runtime, exporter):
    assert runtime.tracing.logfire.force_flush()
    return exporter.get_finished_spans()


def serialized(values):
    return json.dumps([{
        "name": span.name, "attributes": dict(span.attributes or {}), "status": span.status.description,
        "events": [{"name": event.name, "attributes": dict(event.attributes or {})} for event in span.events],
    } for span in values])


@pytest.mark.parametrize("exporter", [False, True], indirect=True)
async def test_official_fastapi_spans_keep_context_and_follow_exception_content_policy(runtime, exporter, monkeypatch):
    provider = runtime.tracing
    app = FastAPI()
    provider.instrument_app(app)
    assert runtime.configure_tracing() is provider

    @app.get("/api/sessions/{session_id}/diagnostic")
    async def diagnostic(session_id: str):
        raise RuntimeError("diagnostic-failure private-export-key")

    # Global OTel header capture must not override this application's policy.
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")
    session_id = "12345678-1234-4234-8234-123456789abc"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
    ) as client:
        response = await client.get(
            f"/api/sessions/{session_id}/diagnostic?value=private-query-content",
            headers={"authorization": "Bearer private-header-content", "cookie": "private-cookie-content"},
        )
    assert response.status_code == 500
    captured = spans(runtime, exporter)
    server = [span for span in captured if span.kind == SpanKind.SERVER]
    assert len(server) == 1
    assert server[0].instrumentation_scope.name == "opentelemetry.instrumentation.fastapi"
    assert server[0].status.status_code == StatusCode.ERROR
    assert json.loads(server[0].attributes["metadata"])["moyai_session_id"] == session_id
    assert any(event.attributes.get("exception.type", "").endswith("RuntimeError") for event in server[0].events)
    assert not any("http send" in span.name or "http receive" in span.name for span in captured)
    payload = serialized(captured)
    assert "private-" not in payload
    assert ("diagnostic-failure" in payload) is runtime.settings.trace_include_content
    assert ("exception.message" in server[0].attributes) is runtime.settings.trace_include_content


@pytest.mark.parametrize("mode", ["restored", "fresh", "failed", "fenced"])
async def test_reconnect_traces_observed_loss_and_committed_recovery(runtime, actor, monkeypatch, exporter, mode):
    session, turn_id, restore, guest = await saved_login(runtime, actor, monkeypatch)
    if mode == "fresh":
        async with runtime.database.sessions.begin() as db:
            (await db.get(Session, session.id)).snapshot = None
        monkeypatch.setattr("moyai.computer_recovery.fresh_sandbox", restore)
    elif mode == "failed":
        restore.side_effect = TimeoutError("private-provider-endpoint")
    elif mode == "fenced":
        async def stopped(*_args):
            async with runtime.database.sessions.begin() as db:
                (await db.get(Session, session.id)).status = "stopped"
                (await db.get(Turn, turn_id)).status = "stopped"
            return {"available": True}
        guest.side_effect = stopped
    async with sign_in(runtime) as client:
        response = await client.post(f"/api/sessions/{session.id}/computer", json={"action": "reconnect"})
    captured = spans(runtime, exporter)
    root = next(span for span in captured if span.attributes.get("moyai.computer.operation") == "reconnect")
    health = next(span for span in captured if span.name == "moyai.computer.health")
    assert health.attributes["moyai.computer.state"] == "unavailable"
    assert health.attributes["moyai.computer.observation"] == "provider_liveness"
    assert health.parent.span_id == root.context.span_id
    assert health.context.trace_id == root.context.trace_id
    assert root.attributes["moyai.turn.id"] == turn_id
    assert json.loads(root.attributes["metadata"])["moyai_session_id"] == session.id
    recovered = [event for event in root.events if event.name == "moyai.computer.recovered"]
    if mode in {"failed", "fenced"}:
        assert response.status_code in {409, 503}
        assert not recovered
        assert root.status.status_code == StatusCode.ERROR
    else:
        assert response.status_code == 200
        assert root.attributes["moyai.recovery.mode"] == mode
        assert root.attributes["pydantic_ai.workspace.id"] == REPLACEMENT["id"]
        assert len(recovered) == 1
        assert recovered[0].attributes["moyai.computer.previous_id"] == REFERENCE["id"]
        assert recovered[0].attributes["moyai.recovery.files_restored"] is (mode == "restored")
    assert "private-provider-endpoint" not in serialized(captured)


async def test_waiting_and_late_failure_report_committed_state(runtime, actor, monkeypatch, exporter):
    session, turn_id, _, _ = await saved_login(runtime, actor, monkeypatch)
    with runtime.tracing.instrumentation.tracer.start_as_current_span("settle"):
        await task_state(runtime.database, turn_id)
    waiting = spans(runtime, exporter)[-1]
    assert waiting.attributes["moyai.task.state"] == "waiting"
    assert waiting.attributes["moyai.wait.reason"] == "browser_login"
    assert waiting.attributes["moyai.computer.keepalive_eligible"] is False
    assert waiting.status.status_code != StatusCode.ERROR

    async with runtime.database.sessions.begin() as db:
        (await db.get(Session, session.id)).status = "idle"
        (await db.get(Session, session.id)).active_turn = None
        (await db.get(Turn, turn_id)).status = "completed"
    await runtime.database.fail(turn_id, "private-late-failure")
    with runtime.tracing.instrumentation.tracer.start_as_current_span("late failure"):
        await task_state(runtime.database, turn_id)
    settled = spans(runtime, exporter)[-1]
    assert settled.attributes["moyai.task.state"] == "completed"
    assert settled.status.status_code != StatusCode.ERROR
    assert "private-late-failure" not in serialized([settled])


async def test_collector_failure_does_not_change_api_result(runtime, actor, exporter, monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExportResult

    monkeypatch.setattr(exporter, "export", lambda batch: SpanExportResult.FAILURE)
    async with sign_in(runtime) as client:
        response = await client.get("/api/me")
    assert response.status_code == 200
    assert runtime.tracing.logfire.force_flush()
