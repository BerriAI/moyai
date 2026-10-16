"""Observe new gateway requests without changing the native SDK's execution.

Only explicitly reported LiteLLM USD amounts are actual cost. In particular,
streaming response headers precede generation and are never a final price.
The decimal/usage rules mirror app.spend, without importing its legacy state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import zlib
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import anyio
import httpx2 as httpx
from pydantic_ai.messages import ModelResponse
from sqlalchemy import BigInteger, String, case, func
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from .contracts import RunScope
from .models import Base, now

if TYPE_CHECKING:
    from .database import Database
    from .product_inference import Admission

log = logging.getLogger(__name__)
MAX_CAPTURE_BYTES = 256 * 1024
WRITE_TIMEOUT_SECONDS = 1.0


class InferenceUsage(Base):
    """Content-free, per-HTTP-attempt ledger; no historical price backfill.

    IDs are not foreign keys: deleting a conversation must not erase spend.
    Consumers sum Decimal(cost_usd) and separately report missing/pending rows.
    A unique gateway receipt prevents counting one reported charge twice.
    """

    __tablename__ = "moyai_inference_usage"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    turn_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(512), index=True)
    model: Mapped[str] = mapped_column(String(200))
    response_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider: Mapped[str] = mapped_column(String(30), default="litellm")
    provider_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    response_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_scope: Mapped[str] = mapped_column(String(64))
    receipt_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), index=True)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    provider_timestamp: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_usd: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_status: Mapped[str] = mapped_column(String(20), default="missing")
    cost_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    coverage_error: Mapped[str | None] = mapped_column(String(50), nullable=True)


def money(value: object) -> str | None:
    """Legacy finite/nonnegative USD validation, with bounded canonical output."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        if len(str(value)) > 100:
            return None
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            return None
        if number == 0:
            return "0"
        exponent = number.as_tuple().exponent
        if number.adjusted() > 30 or not isinstance(exponent, int) or exponent < -60:
            return None
        result = format(number, "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    except (InvalidOperation, ValueError):
        return None


def _identity(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= 200 and value.isprintable():
        return value
    return None


def _tokens(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _timestamp(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


@dataclass
class InferenceActivity:
    database: Database
    scope: RunScope
    identity: str
    requests: int = 0
    latest: UsageCapture | None = None
    logical_identity: str | None = None
    admission_error: Exception | None = None

    def capture(self, request: httpx.Request) -> UsageCapture:
        self.requests += 1
        key = hashlib.sha256(f"{self.identity}/{self.requests}".encode()).hexdigest()
        self.latest = UsageCapture(self, key, request)
        return self.latest

    async def result(self, response: object) -> None:
        # SDK usage is a fallback for oversized/encoded wire payloads. Never
        # attribute an aggregate result to several physical request attempts.
        if self.requests == 1 and self.latest and isinstance(response, ModelResponse):
            self.latest.native_usage(response)
            await self.latest.persist()


INFERENCE_ACTIVITY: ContextVar[InferenceActivity | None] = ContextVar("moyai_inference_activity", default=None)


class UsageCapture:
    """Bounded, transient parsing; only allowlisted billing fields reach SQL."""

    def __init__(self, activity: InferenceActivity, key: str, request: httpx.Request):
        self.activity = activity
        scope = activity.scope
        # Hash only the gateway endpoint, never a key, user-info, or query string.
        base = request.url.path.rstrip("/")
        for suffix in ("/responses/compact", "/responses", "/chat/completions"):
            if base.endswith(suffix):
                base = base.removesuffix(suffix).removesuffix("/v1")
                break
        endpoint = f"{request.url.scheme}://{request.url.host}:{request.url.port}{base}"
        self.values: dict[str, Any] = {
            "id": key, "request_key": key, "session_id": scope.session_id,
            "turn_id": scope.turn_id, "actor_id": scope.actor_id, "model": scope.model,
            "provider": "litellm", "provider_scope": hashlib.sha256(endpoint.encode()).hexdigest(),
            "created_at": now(), "cost_status": "missing", "status": "pending",
        }
        # The SDK's JSON request is already materialized. Read only the route
        # alias; never retain or write input, metadata, headers, or credentials.
        self.request_streaming: bool | None = None
        try:
            if len(request.content) <= 8 * 1024 * 1024:
                body = json.loads(request.content)
                if isinstance(body, dict):
                    self.request_streaming = body.get("stream", False) is True
                    if model := _identity(body.get("model")):
                        self.values["model"] = model
        except (ValueError, httpx.RequestNotRead, RecursionError):
            pass
        self.streaming = False
        self.buffer = b""
        self.event = b""
        self.drop_line = False
        self.drop_event = False
        self.disabled = False
        self.finished = False
        self._finish_task: asyncio.Task[None] | None = None
        self.exhausted = False
        self.terminal = False
        self.decoder = None
        self.admission: Admission | None = None
        self.gateway_call_id: str | None = None

    def headers(self, response: httpx.Response) -> None:
        self.streaming = (
            self.request_streaming is True
            or "text/event-stream" in response.headers.get("content-type", "").lower()
        )
        self.values["status"] = "pending" if response.is_success else "failed"
        self.gateway_call_id = _identity(response.headers.get("x-litellm-call-id"))
        identifier = self.gateway_call_id or _identity(response.headers.get("x-request-id"))
        self.values["provider_request_id"] = identifier
        if identifier:
            self.values["receipt_key"] = hashlib.sha256(
                f"{self.values['provider_scope']}/{identifier}".encode()
            ).hexdigest()
        if (
            self.request_streaming is False and not self.streaming
            and (cost := money(response.headers.get("x-litellm-response-cost"))) is not None
        ):
            self.values.update(cost_usd=cost, cost_status="actual", cost_source="response_header")
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding in {"gzip", "deflate"}:
            self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
        elif encoding != "identity":
            self.disabled = True
            self.values["coverage_error"] = "unsupported_content_encoding"

    def consume(self, value: object, *, final: bool) -> None:
        if not isinstance(value, dict):
            return
        payload = value.get("response", value)
        if not isinstance(payload, dict):
            return
        for source, target in (("id", "response_id"), ("model", "response_model")):
            if identifier := _identity(payload.get(source)):
                self.values[target] = identifier
        if timestamp := _timestamp(payload.get("created_at", payload.get("created"))):
            self.values["provider_timestamp"] = timestamp
        if not final:
            return
        usage = payload.get("usage")
        if isinstance(usage, dict):
            details = usage.get("input_tokens_details", usage.get("prompt_tokens_details"))
            details = details if isinstance(details, dict) else {}
            output = usage.get("output_tokens_details", usage.get("completion_tokens_details"))
            output = output if isinstance(output, dict) else {}
            fields = {
                "input_tokens": usage.get("input_tokens", usage.get("prompt_tokens")),
                "output_tokens": usage.get("output_tokens", usage.get("completion_tokens")),
                "total_tokens": usage.get("total_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens", details.get("cached_tokens")),
                "cache_write_tokens": usage.get("cache_creation_input_tokens", details.get(
                    "cache_write_tokens", details.get("cache_creation_tokens")
                )),
                "reasoning_tokens": usage.get("reasoning_tokens", output.get("reasoning_tokens")),
            }
            self.values.update({key: count for key, raw in fields.items() if (count := _tokens(raw)) is not None})
        # Match the legacy optional LiteLLM extension, including Responses'
        # nested terminal response. Never use SDK price estimates.
        for source, field in (
            (value, "x_litellm_response_cost"), (payload, "x_litellm_response_cost"),
            (usage, "x_litellm_response_cost"), (usage, "cost"),
        ):
            if isinstance(source, dict) and (cost := money(source.get(field))) is not None:
                self.values.update(cost_usd=cost, cost_status="actual", cost_source="response_usage")

    def _event(self) -> None:
        if self.drop_event:
            self.drop_event = False
        elif self.event == b"[DONE]":
            self.terminal = True
        elif self.event:
            try:
                value = json.loads(self.event, parse_float=Decimal)
                if isinstance(value, dict):
                    kind = value.get("type")
                    final = kind in {"response.completed", "response.failed", "response.incomplete"}
                    # Chat Completions' final usage-only chunk is also a receipt.
                    final = final or (
                        kind is None and value.get("choices") == [] and isinstance(value.get("usage"), dict)
                    )
                    self.consume(value, final=final)
                    if kind in {"response.completed", "response.failed", "response.incomplete"}:
                        self.terminal = True
                        self.values["status"] = {
                            "response.completed": "completed", "response.failed": "failed",
                            "response.incomplete": "interrupted",
                        }[kind]
            except (ValueError, UnicodeDecodeError, RecursionError, InvalidOperation, OverflowError):
                self.values["coverage_error"] = "invalid_usage_frame"
        self.event = b""

    def _line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        if not line:
            self._event()
        elif line.startswith(b"data:") and not self.drop_event:
            data = line[5:].removeprefix(b" ")
            if len(self.event) + len(data) + 1 > MAX_CAPTURE_BYTES:
                self.event, self.drop_event = b"", True
                self.values["coverage_error"] = "oversized_usage_frame"
            else:
                self.event += (b"\n" if self.event else b"") + data

    def feed(self, chunk: bytes) -> None:
        if self.disabled:
            return
        try:
            if self.decoder:
                chunk = self.decoder.decompress(chunk, MAX_CAPTURE_BYTES + 1)
                if self.decoder.unconsumed_tail or len(chunk) > MAX_CAPTURE_BYTES:
                    self.disabled = True
                    self.decoder = None
                    self.values["coverage_error"] = "oversized_compressed_chunk"
                    return
            # A transport may return a very large chunk. Bound our own buffer
            # independently of the SDK's buffering and preserve every input byte.
            for offset in range(0, len(chunk), 16 * 1024):
                piece = chunk[offset:offset + 16 * 1024]
                if not self.streaming:
                    if len(self.buffer) + len(piece) > MAX_CAPTURE_BYTES:
                        self.buffer, self.disabled = b"", True
                        self.values["coverage_error"] = "oversized_usage_body"
                        return
                    self.buffer += piece
                    continue
                self.buffer += piece
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    if not self.drop_line:
                        self._line(line)
                    self.drop_line = False
                if len(self.buffer) > MAX_CAPTURE_BYTES:
                    self.buffer, self.event = b"", b""
                    self.drop_line = self.drop_event = True
                    self.values["coverage_error"] = "oversized_usage_frame"
        except (ValueError, OverflowError, zlib.error):
            self.disabled = True
            self.buffer = self.event = b""
            self.values["coverage_error"] = "invalid_usage_encoding"

    def native_usage(self, response: ModelResponse) -> None:
        if response_id := _identity(response.provider_response_id):
            self.values.setdefault("response_id", response_id)
        usage = response.usage
        fields = {
            "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens, "cache_write_tokens": usage.cache_write_tokens,
            "reasoning_tokens": usage.details.get("reasoning_tokens"),
        }
        # SDK defaults are zero for each omitted field. Only the wire parser
        # can prove an actual zero; keep unknown per-field coverage nullable.
        for key, raw in fields.items():
            if (count := _tokens(raw)) is not None and count > 0:
                self.values.setdefault(key, count)
        if (
            "total_tokens" not in self.values
            and all(key in self.values for key in ("input_tokens", "output_tokens"))
            and (total := _tokens(self.values["input_tokens"] + self.values["output_tokens"])) is not None
        ):
            self.values["total_tokens"] = total

    async def finish(self, *, exhausted: bool) -> None:
        if self.finished:
            if self._finish_task is not None:
                from .product_inference import drain
                await drain(self._finish_task)
            return
        self.finished = True
        self.exhausted = exhausted
        if not self.disabled:
            if self.streaming:
                self._line(self.buffer)
                self._event()
            elif exhausted:
                try:
                    self.consume(json.loads(self.buffer, parse_float=Decimal), final=True)
                except (ValueError, UnicodeDecodeError, RecursionError, InvalidOperation, OverflowError):
                    self.values["coverage_error"] = "invalid_usage_body"
        self.buffer = self.event = b""
        self.decoder = None
        if self.values["status"] == "pending":
            self.values["status"] = "completed" if (self.terminal or (exhausted and not self.streaming)) else "interrupted"
        self.values["finished_at"] = now()
        async def settle() -> None:
            with anyio.CancelScope(shield=True):
                await self.persist()
        self._finish_task = asyncio.create_task(settle())
        from .product_inference import drain
        await drain(self._finish_task)

    async def persist(self) -> None:
        """Analytics failure cannot fail/retry a successful provider request.

        The pre-dispatch row retains missing coverage if final persistence fails.
        If both writes fail, emit a safe operational warning; there is no claim
        of durable accounting while the database itself is unavailable.
        """
        try:
            if self.admission is not None:
                async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                    await self.admission.service.observe(self)
            else:
                await self._write()
        except IntegrityError:
            # A different HTTP attempt reported a receipt already consumed by
            # another row. Retain this attempt as missing; never double bill it.
            self.values.update(
                receipt_key=None, cost_usd=None, cost_status="missing", cost_source=None,
                coverage_error="duplicate_provider_receipt",
            )
            try:
                if self.admission is not None:
                    async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                        await self.admission.service.observe(self)
                else:
                    await self._write()
            except Exception:  # noqa: BLE001 - analytics must not trigger a paid inference retry
                self._write_failed()
        except Exception:  # noqa: BLE001 - analytics must not trigger a paid inference retry
            self._write_failed()

    def _write_failed(self) -> None:
        log.warning(
            "Native inference usage write failed; accounting coverage is incomplete (request %s).",
            self.values["request_key"],
        )

    async def _write(self) -> None:
        database = self.activity.database
        insert = postgres_insert if database.settings.postgres_url else sqlite_insert
        values = {
            **self.values,
            # An unpriced observation must not reserve the sole priced receipt
            # if another attempt subsequently observes its final amount.
            "receipt_key": self.values.get("receipt_key") if self.values.get("cost_usd") is not None else None,
        }
        statement = insert(InferenceUsage).values(**values)
        stable = {"id", "request_key", "session_id", "turn_id", "actor_id", "model", "provider", "created_at"}
        updates = {key: getattr(statement.excluded, key) for key in values if key not in stable}
        # Repeated completion/cleanup writes must not erase an actual receipt.
        for key in ("cost_usd", "cost_source", "receipt_key"):
            if key in updates:
                updates[key] = func.coalesce(getattr(InferenceUsage, key), updates[key])
        if "cost_status" in updates:
            updates["cost_status"] = case(
                (InferenceUsage.cost_usd.is_not(None), "actual"), else_=statement.excluded.cost_status,
            )
        async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
            async with database.sessions.begin() as db:
                await db.execute(statement.on_conflict_do_update(index_elements=["request_key"], set_=updates))


class ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, capture: UsageCapture):
        self.stream, self.capture = stream, capture

    async def __aiter__(self) -> AsyncIterator[bytes]:
        exhausted = False
        try:
            async for chunk in self.stream:
                self.capture.feed(chunk)
                yield chunk
            exhausted = True
        finally:
            await self.capture.finish(exhausted=exhausted)

    async def aclose(self) -> None:
        try:
            await self.stream.aclose()
        finally:
            try:
                await self.capture.finish(exhausted=False)
            finally:
                if self.capture.admission is not None:
                    from .product_inference import drain
                    await drain(asyncio.create_task(self.capture.admission.close()))


class InferenceUsageTransport(httpx.AsyncBaseTransport):
    """Public HTTPX extension: observe, never rewrite/replay the wire stream."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, *, require_admission: bool = False):
        self.transport = transport if transport is not None else httpx.AsyncHTTPTransport()
        self.require_admission = require_admission

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        context = INFERENCE_ACTIVITY.get()
        generation = request.method == "POST" and request.url.path.rstrip("/").endswith(
            ("/responses", "/responses/compact", "/chat/completions")
        )
        required = self.require_admission or (
            context is not None and context.database.settings.product_mode == "existing"
        )
        if not generation:
            if required:
                from .product_inference import InferenceAdmissionError
                error = InferenceAdmissionError("This native model operation has no product admission route.")
                if context is not None:
                    context.admission_error = error
                raise error
            return await self.transport.handle_async_request(request)
        if context is None:
            if self.require_admission:
                from .product_inference import InferenceAdmissionError
                raise InferenceAdmissionError("Product model dispatch requires an authorized native activity.")
            return await self.transport.handle_async_request(request)
        capture = context.capture(request)
        service = getattr(context.database, "product_inference", None)
        if service is None and required:
            from .product_inference import InferenceAdmissionError
            context.admission_error = InferenceAdmissionError("Product inference admission is not configured.")
            raise context.admission_error
        if service is not None:
            if service.remote:
                # Only the broker writes the ledger for this physical request.
                # The SDK sees the unchanged response stream; no nested agent.
                try:
                    proxied = service.proxy_request(request, capture)
                except Exception as error:
                    context.admission_error = error
                    raise
                context.latest = None
                response = await self.transport.handle_async_request(proxied)
                if response.headers.get("x-moyai-inference-denied") == "1":
                    from .product_inference import InferenceAdmissionError
                    context.admission_error = InferenceAdmissionError("Product broker admission refused.")
                return response
            try:
                await service.admit(request, capture)
            except Exception as error:
                # OpenAI wraps transport exceptions. Preserve safe admission
                # classification for the Temporal inbound interceptor.
                context.admission_error = error
                raise
        else:
            await capture.persist()
        try:
            response = await self.transport.handle_async_request(request)
        except BaseException as error:
            if capture.admission is not None:
                capture.admission.unsent = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
            capture.values["status"] = "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
            if capture.admission is not None and capture.admission.unsent:
                capture.values.update(status="not_dispatched", coverage_error="not_dispatched")
            try:
                await capture.finish(exhausted=False)
            finally:
                if capture.admission is not None:
                    from .product_inference import drain
                    await drain(asyncio.create_task(capture.admission.close()))
            raise
        capture.headers(response)
        assert isinstance(response.stream, httpx.AsyncByteStream)
        response.stream = ObservedStream(response.stream, capture)
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()
