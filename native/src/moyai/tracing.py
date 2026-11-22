"""Optional native agent tracing, with replay-safe Temporal context propagation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, MutableMapping
from typing import Any

import logfire
import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic_ai.durable_exec.temporal import LogfirePlugin
from pydantic_ai.models.instrumented import InstrumentationSettings

from .settings import Settings

TRACE_CONTEXT = TraceContextTextMapPropagator()


def trace_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Forward only a valid W3C parent, never baggage or arbitrary headers."""
    parent = headers.get("traceparent", "")
    if len(parent) != 55:
        return {}
    context = TRACE_CONTEXT.extract({"traceparent": parent})
    if not get_current_span(context).get_span_context().is_valid:
        return {}
    return {"traceparent": parent}


def inject_traceparent(headers: MutableMapping[str, str]) -> None:
    """Connect model calls to their native activity without replacing a broker parent."""
    if trace_headers(headers):
        return
    TRACE_CONTEXT.inject(headers)


class _NoRedirectSession(requests.Session):
    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        kwargs["allow_redirects"] = False
        return super().send(request, **kwargs)


class LensTracing:
    def __init__(self, settings: Settings):
        exporter = OTLPSpanExporter(
            endpoint=settings.trace_endpoint,
            headers={"Authorization": "Bearer " + settings.trace_api_key.get_secret_value()},
            timeout=3,
            session=_NoRedirectSession(),
        )
        self.logfire = logfire.configure(
            local=True, send_to_logfire=False, console=False, metrics=False,
            service_name="moyai", inspect_arguments=False,
            additional_span_processors=[BatchSpanProcessor(
                exporter, max_queue_size=2048, max_export_batch_size=128,
                schedule_delay_millis=1000, export_timeout_millis=3000,
            )],
        )
        # Keep context propagation across activities while showing the agent's
        # work instead of the SDK's control-plane polling and bookkeeping.
        self.logfire.suppress_scopes("temporalio")
        # Gateway model spans already contain the model inputs and outputs.
        # Do not copy private memory, capability inputs or credential tool data
        # into the agent's telemetry, nor change another agent's global settings.
        self.instrumentation = InstrumentationSettings(
            tracer_provider=self.logfire.config.get_tracer_provider(),
            include_content=False, include_binary_content=False,
            include_model_request_parameters=False,
        )
        self.plugin = LogfirePlugin(setup_logfire=lambda: self.logfire, metrics=False)

    async def close(self) -> None:
        await asyncio.to_thread(self.logfire.shutdown, timeout_millis=5000)
