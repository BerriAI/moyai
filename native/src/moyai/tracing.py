"""Optional native agent tracing, with replay-safe Temporal context propagation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, MutableMapping
from copy import copy
from dataclasses import replace
from typing import Any
from uuid import UUID

import logfire
import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import SecretStr
from pydantic_ai.durable_exec.temporal import LogfirePlugin
from pydantic_ai.messages import (
    BaseToolCallPart,
    BaseToolReturnPart,
    ModelMessage,
    ModelRequest,
    ModelRequestPart,
    ModelResponsePart,
    TextContent,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.instrumented import InstrumentationSettings

from .presentation import redact
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


def _keep_session_id(match: logfire.ScrubMatch) -> str | None:
    """Allow only Moyai's UUID metadata, not session cookies or arbitrary values."""
    if match.path == ("attributes", "metadata", "moyai_session_id") and isinstance(match.value, str):
        try:
            if str(UUID(match.value)) == match.value:
                return match.value
        except ValueError:
            pass
    return None


class _PromptResponseInstrumentation(InstrumentationSettings):
    """Opt in to conversation text without enabling tool or instruction capture."""

    def __init__(self, *, secrets: list[str], **kwargs: Any):
        super().__init__(**kwargs)
        self._secrets = secrets

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
                                texts.append(str(redact(item, self._secrets)))
                        parts.append(replace(part, content=texts))
                    elif isinstance(part, BaseToolReturnPart):
                        parts.append(replace(part, content=None))
                visible.append(replace(message, parts=parts))
            else:
                response_parts: list[ModelResponsePart] = []
                for part in message.parts:
                    if isinstance(part, TextPart):
                        response_parts.append(replace(part, content=str(redact(part.content, self._secrets))))
                    elif isinstance(part, BaseToolCallPart):
                        response_parts.append(replace(part, args=None))
                visible.append(replace(message, parts=response_parts))
        # The SDK's cached model serialization and agent summary both use this
        # hook. Enable text only on this sanitized copy; tool spans, exceptions,
        # deferral metadata and system instructions keep include_content=False.
        content_settings = copy(self)
        content_settings.include_content = True
        return InstrumentationSettings.messages_to_otel_messages(content_settings, visible)


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
            scrubbing=logfire.ScrubbingOptions(callback=_keep_session_id),
            additional_span_processors=[BatchSpanProcessor(
                exporter, max_queue_size=2048, max_export_batch_size=128,
                schedule_delay_millis=1000, export_timeout_millis=3000,
            )],
        )
        # Keep context propagation across activities while showing the agent's
        # work instead of the SDK's control-plane polling and bookkeeping.
        self.logfire.suppress_scopes("temporalio")
        options: dict[str, Any] = {}
        instrumentation_type = InstrumentationSettings
        if settings.trace_include_content:
            instrumentation_type = _PromptResponseInstrumentation
            options["secrets"] = [
                value.get_secret_value()
                for name in type(settings).model_fields
                if isinstance(value := getattr(settings, name), SecretStr) and value.get_secret_value()
            ]
        self.instrumentation = instrumentation_type(
            tracer_provider=self.logfire.config.get_tracer_provider(),
            include_content=False, include_binary_content=False,
            include_model_request_parameters=False,
            **options,
        )
        self.plugin = LogfirePlugin(setup_logfire=lambda: self.logfire, metrics=False)

    async def close(self) -> None:
        await asyncio.to_thread(self.logfire.shutdown, timeout_millis=5000)
