"""Optional native agent tracing, with replay-safe Temporal context propagation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, MutableMapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import UUID

import logfire
import requests
from logfire.types import ExceptionCallbackHelper
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace import Span as SDKSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import SecretStr
from pydantic_ai.durable_exec.temporal import LogfirePlugin
from pydantic_ai.exceptions import ModelRetry, ToolFailed, ToolFailedError, ToolRetryError
from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponsePart,
    RetryPromptPart,
    TextContent,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.instrumented import InstrumentationSettings
from temporalio import workflow

from .presentation import private_tool
from .settings import Settings
from .trace_content import redact_content, sensitive_field

if TYPE_CHECKING:
    from fastapi import FastAPI

TRACE_CONTEXT = TraceContextTextMapPropagator()
TOOL_CONTENT_ATTRIBUTES = {
    "gen_ai.tool.call.arguments", "gen_ai.tool.call.result",
    "tool_arguments", "tool_response",
}


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


def _record_handled_tool_exception(helper: ExceptionCallbackHelper) -> None:
    """Keep handled Pydantic AI failures visible through Temporal's tracer.

    Temporal 1.34's replay-safe span treats every recorded non-workflow failure
    as a workflow-task failure and suppresses its end. These exceptions are
    consumed by Pydantic AI as tool responses; they do not fail a workflow task.
    Use the OTel SDK's event serializer via Logfire's public callback, without
    changing Temporal's failure types, tracer, span lifetime or replay guard.
    """
    if workflow.in_workflow() and isinstance(helper.exception, (ToolFailed, ModelRetry, ToolFailedError, ToolRetryError)):
        SDKSpan.record_exception(
            helper.span, helper.exception, attributes=helper.event_attributes, escaped=True,
        )
        helper.no_record_exception()


class _ContentSafeBatchSpanProcessor(BatchSpanProcessor):
    """Apply the content policy to every export, including framework exceptions."""

    def __init__(self, *args: Any, include_content: bool, secrets: list[str], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._include_content = include_content
        self._secrets = secrets

    def on_end(self, span: ReadableSpan) -> None:
        source = span.attributes or {}
        capture = self._include_content and not private_tool(str(source.get("gen_ai.tool.name", "")))
        hidden = {"gen_ai.system_instructions", "gen_ai.tool.deferral.metadata"}
        if not capture:
            hidden |= TOOL_CONTENT_ATTRIBUTES | {"exception.message", "exception.stacktrace"}
        attributes = {
            key: value if key == "logfire.json_schema" else redact_content(value, self._secrets)
            for key, value in source.items() if key not in hidden
        }
        if span.instrumentation_scope and span.instrumentation_scope.name == "opentelemetry.instrumentation.fastapi":
            # Route templates, methods and status suffice for request diagnostics.
            # Raw URLs can contain file names, login codes or signed query values.
            attributes = {
                key: value for key, value in attributes.items()
                if key not in {"http.url", "http.target", "url.full", "url.path", "url.query"}
                and not key.startswith(("http.request.header.", "http.response.header."))
            }
            attributes["logfire.msg"] = span.name
        events = [
            Event(event.name, attributes={
                key: redact_content(value, self._secrets)
                for key, value in (event.attributes or {}).items() if key not in hidden
            }, timestamp=event.timestamp)
            for event in span.events
        ]
        # Some receivers retain attributes but drop events. Keep the diagnostic
        # on its existing span as well, without creating another error span.
        for event in events:
            if event.name == "exception":
                for key in ("exception.type", "exception.message"):
                    if event.attributes and key in event.attributes:
                        attributes[key] = event.attributes[key]
        if span.status.status_code == StatusCode.ERROR and "exception.type" in attributes:
            attributes.setdefault("error.type", attributes["exception.type"])
        description = redact_content(span.status.description, self._secrets) if capture else None
        super().on_end(ReadableSpan(
            name=span.name, context=span.context, parent=span.parent, resource=span.resource,
            attributes=attributes, events=events, links=span.links, kind=span.kind,
            status=Status(span.status.status_code, description), start_time=span.start_time, end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        ))


def _request_context(span: Span, scope: dict[str, Any]) -> None:
    """Correlate separate reconnect requests without reviving an old trace."""
    path = scope.get("path", "").split("/")
    if len(path) >= 4 and path[1:3] == ["api", "sessions"]:
        try:
            session_id = str(UUID(path[3]))
        except ValueError:
            return
        import json

        span.set_attribute("metadata", json.dumps({"moyai_session_id": session_id}))


def _keep_session_id(match: logfire.ScrubMatch) -> str | None:
    """Allow only Moyai's UUID metadata, not session cookies or arbitrary values."""
    if match.path == ("attributes", "metadata", "moyai_session_id") and isinstance(match.value, str):
        try:
            if str(UUID(match.value)) == match.value:
                return match.value
        except ValueError:
            pass
    return None


class _ContentInstrumentation(InstrumentationSettings):
    """Capture conversation and tool content while excluding private context."""

    def __init__(self, *, secrets: list[str], **kwargs: Any):
        super().__init__(**kwargs)
        self._secrets = secrets

    def system_instructions_attributes(self, instructions: str | None) -> dict[str, str]:
        return {}

    def messages_to_otel_messages(self, messages: list[ModelMessage]):
        visible: list[ModelMessage] = []
        for message in messages:
            if isinstance(message, ModelRequest):
                parts: list[ModelRequestPart] = []
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        content = [part.content] if isinstance(part.content, str) else part.content
                        texts = []
                        for item in content:
                            if isinstance(item, TextContent):
                                if isinstance(item.metadata, str) and item.metadata.startswith(
                                    ("pydantic-ai-harness.memory.v1:", "moyai.private-context:")
                                ):
                                    continue
                                item = item.content
                            if isinstance(item, str):
                                texts.append(str(redact_content(item, self._secrets)))
                        parts.append(replace(part, content=texts))
                    elif isinstance(part, BaseToolReturnPart):
                        parts.append(replace(part, content=(
                            None if private_tool(part.tool_name) else redact_content(part.content, self._secrets)
                        )))
                    elif isinstance(part, RetryPromptPart):
                        parts.append(replace(part, content=(
                            "[private tool error omitted]" if private_tool(part.tool_name)
                            else redact_content(part.content, self._secrets)
                        )))
                visible.append(replace(message, parts=parts))
            else:
                response_parts: list[ModelResponsePart] = []
                for part in message.parts:
                    if isinstance(part, TextPart):
                        response_parts.append(replace(part, content=str(redact_content(part.content, self._secrets))))
                    elif isinstance(part, BaseToolCallPart):
                        response_parts.append(replace(part, args=(
                            None if private_tool(part.tool_name) else redact_content(part.args, self._secrets)
                        )))
                visible.append(replace(message, parts=response_parts))
        return super().messages_to_otel_messages(visible)


class LensTracing:
    def __init__(self, settings: Settings):
        secrets = [
            value.get_secret_value()
            for name in type(settings).model_fields
            if isinstance(value := getattr(settings, name), SecretStr) and value.get_secret_value()
        ]

        def scrub(match: logfire.ScrubMatch) -> Any:
            # Logfire otherwise removes an entire tool result containing words
            # such as "authentication". Scrub credential values, not that prose.
            if (
                settings.trace_include_content and len(match.path) >= 2
                and match.path[:1] == ("attributes",)
                and match.path[1] in TOOL_CONTENT_ATTRIBUTES | {"final_result"}
                and not (len(match.path) > 2 and sensitive_field(str(match.path[-1])))
            ):
                return redact_content(match.value, secrets)
            return _keep_session_id(match)

        exporter = OTLPSpanExporter(
            endpoint=settings.trace_endpoint,
            headers={"Authorization": "Bearer " + settings.trace_api_key.get_secret_value()},
            timeout=3,
            session=_NoRedirectSession(),
        )
        self.logfire = logfire.configure(
            local=True, send_to_logfire=False, console=False, metrics=False,
            service_name="moyai", inspect_arguments=False,
            advanced=logfire.AdvancedOptions(exception_callback=_record_handled_tool_exception),
            scrubbing=logfire.ScrubbingOptions(callback=scrub),
            additional_span_processors=[_ContentSafeBatchSpanProcessor(
                exporter, include_content=settings.trace_include_content, secrets=secrets,
                max_queue_size=2048, max_export_batch_size=128,
                schedule_delay_millis=1000, export_timeout_millis=3000,
            )],
        )
        options: dict[str, Any] = {}
        instrumentation_type = InstrumentationSettings
        if settings.trace_include_content:
            instrumentation_type = _ContentInstrumentation
            options["secrets"] = secrets
        self.instrumentation = instrumentation_type(
            tracer_provider=self.logfire.config.get_tracer_provider(),
            version=6,
            include_content=settings.trace_include_content, include_binary_content=False,
            include_model_request_parameters=False,
            **options,
        )
        self.plugin = LogfirePlugin(setup_logfire=lambda: self.logfire, metrics=False)

    def instrument_app(self, app: FastAPI) -> None:
        """Install the official middleware before FastAPI builds its stack."""
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=self.logfire.config.get_tracer_provider(),
            server_request_hook=_request_context, exclude_spans=["send", "receive"],
            excluded_urls=r"/(?:health|healthz|readyz|favicon\.ico)(?:\?|$),/assets/",
            # Nonempty impossible patterns also override environment-wide capture.
            http_capture_headers_server_request=["^$"],
            http_capture_headers_server_response=["^$"],
        )

    async def close(self) -> None:
        await asyncio.to_thread(self.logfire.shutdown, timeout_millis=5000)
