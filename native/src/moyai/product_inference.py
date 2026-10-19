"""Product admission around native SDK HTTP requests, never a model loop.

The product's physical-request and cluster-slot limits are retained. A committed
dispatch is a replay barrier, including when its final acknowledgement was lost.
Only proven pre-connect failures may be retried by Temporal. Receipt recovery
never performs inference. No legacy run/model_requests rows are fabricated.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import anyio
import httpx2 as httpx
import jwt
from sqlalchemy import Integer, String, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .inference_usage import InferenceUsage
from .models import Base, Session, Turn, User, now
from .product_inference_transport import BrokerUpstream

if TYPE_CHECKING:
    from app.config import Settings as ProductSettings

    from .database import Database
    from .inference_usage import UsageCapture
    from .product_memory_review import ProductMemoryReviewService

log = logging.getLogger(__name__)
UNSENT_CLEANUP_TIMEOUT_SECONDS = 10.0


class InferenceAdmissionError(RuntimeError):
    """Fixed, safe message; no request bodies, SQL values or credentials."""


class ProductInferenceRequest(Base):
    """Immutable scope captured before the first possible upstream byte."""

    __tablename__ = "moyai_product_inference_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(64), unique=True)
    logical_key: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    turn_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_id: Mapped[str] = mapped_column(String(512), index=True)
    authority_version: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(String(100), default="unknown")
    gateway_scope: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), index=True)
    gateway_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cost_receipt_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    receipt_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40))
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cost_usd: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cost_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    recovery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    recovery_error: Mapped[str | None] = mapped_column(String(50), nullable=True)
    next_attempt_at: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    owner_pid: Mapped[int] = mapped_column(Integer)
    owner_started: Mapped[str] = mapped_column(String(40))


@dataclass
class Admission:
    service: ProductInferenceService
    id: str
    permit: Any
    capture: UsageCapture
    released: bool = False
    unsent: bool = False
    monitor: asyncio.Task[None] | None = None
    owner: asyncio.Task[Any] | None = None

    async def close(self) -> None:
        if self.released:
            return
        self.released = True
        if self.owner is not None:
            self.service.active.discard(self.owner)
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        await self.service.slots.release_async(self.permit)


async def drain(task: asyncio.Task[Any]) -> Any:
    """Let accounting/slot cleanup finish even when its caller is cancelled."""
    cancelled = False
    # ASGI middleware uses level cancellation. Without an AnyIO shield every
    # await is immediately cancelled again, including SQL cleanup and this loop.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class ProductInferenceService:
    def __init__(self, database: Database, settings: ProductSettings, store: Any):
        # Store is the lazy canonical proxy. Do not touch it before start().
        self.database, self.settings, self.store = database, settings, store
        self.slots: Any = None
        self.recovery: Any = None
        self.started = False
        self.active: set[asyncio.Task[Any]] = set()
        self.memory_review: ProductMemoryReviewService | None = None
        self.upstream: BrokerUpstream | None = None

    @property
    def remote(self) -> bool:
        return self.settings.moyai_separate_broker and self.settings.moyai_runtime_role != "broker"

    @classmethod
    def from_runtime(cls, runtime: Any) -> ProductInferenceService:
        if runtime.product_identity is None or runtime.product_settings is None:
            raise ValueError("Product inference requires canonical product authority and settings.")
        return cls(runtime.database, runtime.product_settings, runtime.product_store)

    def scope(self) -> tuple[str, str]:
        # Exact existing normalization/hash semantics; never current-key backfill.
        from app.security import digest
        from app.spend import gateway_scope

        return (
            gateway_scope(self.settings.litellm_api_base),
            digest(self.settings.litellm_api_key) if self.settings.litellm_api_key else "",
        )

    async def start(self, *, recovery: bool = True) -> None:
        from app.model_slots import PostgresModelSlots

        from .product_inference_recovery import ProductInferenceRecovery

        if self.started:
            return
        if not self.database.settings.postgres_url or self.database.product_identity is None:
            raise InferenceAdmissionError("Product inference requires canonical PostgreSQL admission.")
        if self.settings.moyai_runtime_role != "standalone" and not self.settings.moyai_separate_broker:
            raise InferenceAdmissionError("Distributed native inference requires the separate product broker.")
        if self.remote:
            self.started = True
            return
        if self.settings.litellm_charge_receipts_enabled:
            from .billing_storage_models import BillingStorageState

            async with self.database.sessions() as db:
                storage = await db.get(BillingStorageState, 1)
                if storage is None or storage.phase != "consolidated":
                    raise InferenceAdmissionError("Consolidate billing storage before enabling charge receipts.")
            try:
                async with (
                    httpx.AsyncClient(timeout=5, follow_redirects=False) as client,
                    client.stream(
                        "GET",
                        self.settings.litellm_api_base.rstrip("/").removesuffix("/v1")
                        + "/billing/receipts/capabilities",
                        headers={
                            "Authorization": "Bearer " + self.settings.litellm_api_key,
                            "Accept-Encoding": "identity",
                        },
                    ) as response,
                ):
                    if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 4096:
                            raise ValueError
                        body.extend(chunk)
                capability = json.loads(body)
                if (
                    not isinstance(capability, dict) or capability.get("version") != 1
                    or capability.get("required") is not True
                    or capability.get("gateway_scope") != self.scope()[0]
                ):
                    raise ValueError
            except (httpx.HTTPError, ValueError):
                raise InferenceAdmissionError("The configured gateway does not confirm required charge receipts.") from None
        self.slots = PostgresModelSlots(self.settings.max_concurrent_model_requests, self.store.database)
        await asyncio.to_thread(self.slots.check)
        self.recovery = ProductInferenceRecovery(self)
        self.upstream = BrokerUpstream(self.slots.capacity)
        self.started = True
        if recovery:
            self.recovery.start()

    async def close(self) -> None:
        self.started = False
        if self.recovery is not None:
            await self.recovery.close()
        tasks = tuple(task for task in self.active if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.upstream is not None:
                await drain(asyncio.create_task(self.upstream.aclose()))
                self.upstream = None
        finally:
            if self.slots is not None:
                await asyncio.to_thread(self.slots.close)
                self.slots = None

    def upstream_client(self) -> httpx.AsyncClient:
        if not self.started or self.upstream is None:
            raise InferenceAdmissionError("Product inference admission is not running.")
        return self.upstream.client(self.database.settings.model_timeout_seconds)

    def validate_request(self, request: httpx.Request, capture: UsageCapture) -> tuple[str, str]:
        if not self.started or (not self.remote and self.slots is None):
            raise InferenceAdmissionError("Product inference admission is not running.")
        if not self.remote and self.slots.capacity != self.settings.max_concurrent_model_requests:
            raise InferenceAdmissionError("Model capacity policy changed; restart this broker before admitting work.")
        expected = httpx.URL(self.settings.litellm_api_base.rstrip("/") + "/")
        target = request.url
        routes = {expected.path + part for part in ("responses", "responses/compact", "chat/completions")}
        if (
            (target.scheme, target.host, target.port) != (expected.scheme, expected.host, expected.port)
            or target.path not in routes or target.query
            or (not self.remote and (
                not self.settings.litellm_api_key
                or not hmac.compare_digest(
                    request.headers.get("authorization", ""),
                    "Bearer " + self.settings.litellm_api_key,
                )
            ))
        ):
            raise InferenceAdmissionError("The inference gateway or credential scope changed before admission.")
        allowed = {self.settings.resolve_model(name) for name in self.settings.allowed_models()}
        try:
            if len(request.content) > 8 * 1024 * 1024:
                raise ValueError
            body = json.loads(request.content)
            selected = body.get("model") if isinstance(body, dict) else None
        except (ValueError, RecursionError, httpx.RequestNotRead):
            raise InferenceAdmissionError("The native inference request is invalid or exceeds its bound.") from None
        if not isinstance(selected, str) or selected != capture.values["model"] or selected not in allowed:
            raise InferenceAdmissionError("The requested inference model is not currently allowed.")
        if capture.activity.scope.surface == "memory_review" and (body.get("tools") or body.get("stream")):
            raise InferenceAdmissionError("Memory review accepts only tool-free, non-streaming model requests.")
        return self.scope()

    async def admit(self, request: httpx.Request, capture: UsageCapture) -> Admission:
        if self.remote:
            raise InferenceAdmissionError("Only the product broker can admit inference.")
        gateway, key_hash = self.validate_request(request, capture)
        activity, scope = capture.activity, capture.activity.scope
        if not activity.logical_identity or scope.product_grant is None:
            raise InferenceAdmissionError("A native activity and original product authority are required.")
        logical_key = hashlib.sha256(
            f"{activity.logical_identity}/{activity.requests}".encode()
        ).hexdigest()
        request_id = str(uuid4())  # Allocated before any cancellable admission I/O.
        permit = await self.slots.acquire()
        admission = Admission(self, request_id, permit, capture)
        try:
            owner_pid, owner_started = await asyncio.to_thread(self.slots.ownership)
            async with self.database.sessions.begin() as db:
                maintenance = scope.surface == "memory_review"
                if maintenance:
                    if self.memory_review is None:
                        raise InferenceAdmissionError("Native memory-review admission is not configured.")
                    # The maintenance authorizer starts with the source lock
                    # and preserves the job's original completed-turn grant.
                    await self.memory_review.authorize_inference(db, capture)
                # Reuse canonical account/role fencing in this exact transaction.
                turn = await db.get(Turn, scope.turn_id)
                if turn is None:
                    raise InferenceAdmissionError("The inference turn is no longer available.")
                await self.database._check_product_turn(db, turn, scope.product_grant)
                # Stop and admission serialize on the same session row.
                session = await db.scalar(select(Session).where(
                    Session.id == scope.session_id,
                ).with_for_update())
                await db.refresh(turn)
                actor = await db.get(User, scope.actor_id)
                if session is None or actor is None:
                    raise InferenceAdmissionError("Inference authority changed before dispatch.")
                if not maintenance and (
                    session.deleted or session.status != "running"
                    or session.active_turn != turn.id or turn.status != "running"
                    or turn.actor_id != scope.actor_id or turn.session_id != scope.session_id
                    or not actor.enabled
                    or (session.visibility == "private" and session.owner_id != actor.id)
                ):
                    raise InferenceAdmissionError("Inference authority changed before dispatch.")
                grants = await self.database.scope_grants(db, scope, session)
                if [(g.id, g.revision) for g in grants] != [(g.id, g.revision) for g in scope.grants]:
                    raise InferenceAdmissionError("Connection access changed before inference dispatch.")
                prior = await db.scalar(select(ProductInferenceRequest.id).where(
                    ProductInferenceRequest.logical_key == logical_key,
                    ProductInferenceRequest.status != "not_dispatched",
                ).limit(1))
                if prior is not None:
                    raise InferenceAdmissionError(
                        "This model activity already dispatched a request. Its paid outcome cannot be replayed."
                    )
                count = await db.scalar(select(func.count()).select_from(ProductInferenceRequest).where(
                    ProductInferenceRequest.turn_id == turn.id,
                ))
                if self.settings.max_agent_iterations and (count or 0) >= self.settings.max_agent_iterations * 3:
                    raise InferenceAdmissionError("This turn reached its product model request limit.")
                # A runtime settings/key change while waiting cannot bind another scope.
                if self.validate_request(request, capture) != (gateway, key_hash):
                    raise InferenceAdmissionError("The inference policy changed during admission.")
                capture.values["provider_scope"] = gateway
                db.add(ProductInferenceRequest(
                    id=request_id, request_key=capture.values["request_key"], logical_key=logical_key,
                    session_id=scope.session_id, turn_id=scope.turn_id, actor_id=scope.actor_id,
                    authority_version=scope.product_grant.authority_version,
                    model=capture.values["model"], gateway_scope=gateway, key_hash=key_hash,
                    purpose=scope.surface,
                    status="dispatching", created_at=capture.values["created_at"],
                    next_attempt_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
                    owner_pid=owner_pid, owner_started=owner_started,
                ))
                db.add(InferenceUsage(**capture.values))
            # These server-owned headers carry only the allocated request ID.
            request.headers["x-litellm-call-id"] = request_id
            request.headers["x-litellm-spend-logs-metadata"] = json.dumps({"moyai_request_id": request_id})
            if self.settings.litellm_charge_receipt_secret:
                issued = int(datetime.now(UTC).timestamp())
                request.headers["x-litellm-billing-context"] = jwt.encode({
                    "version": 1, "sub": request_id, "aud": "litellm-charge-receipts",
                    "iat": issued, "exp": issued + 300,
                    "gateway_scope": gateway, "key_hash": key_hash,
                    "method": request.method, "route": request.url.path,
                    "body_sha256": hashlib.sha256(request.content).hexdigest(),
                }, self.settings.litellm_charge_receipt_secret, algorithm="HS256")
            capture.admission = admission
            current = asyncio.current_task()
            assert current is not None
            self.active.add(current)
            admission.owner = current
            current.add_done_callback(self.active.discard)
            admission.monitor = asyncio.create_task(self._monitor(current))
            return admission
        except BaseException:
            # admit() never performs HTTP. Even a lost commit acknowledgement
            # here is known-unsent. Best-effort evidence may clear ONLY this
            # allocated request; if SQL is unavailable its barrier stays intact.
            async def cleanup() -> None:
                try:
                    await self.not_dispatched(request_id)
                finally:
                    await admission.close()
            await drain(asyncio.create_task(cleanup()))
            raise

    async def not_dispatched(self, request_id: str) -> None:
        try:
            # This clears a known-unsent replay barrier, not optional analytics.
            # Allow transient pool/row contention while keeping shutdown bounded.
            async with asyncio.timeout(UNSENT_CLEANUP_TIMEOUT_SECONDS):
                async with self.database.sessions.begin() as db:
                    row = await db.get(ProductInferenceRequest, request_id, with_for_update=True)
                    if row is None or row.status != "dispatching" or row.cost_usd is not None:
                        return
                    row.status, row.finished_at, row.next_attempt_at = "not_dispatched", now(), None
                    usage = await db.get(InferenceUsage, row.request_key)
                    if usage is not None:
                        usage.status, usage.finished_at = "not_dispatched", row.finished_at
                        usage.coverage_error = "not_dispatched"
        except Exception:  # noqa: BLE001 - preserve the original admission failure and unknown barrier
            log.warning("Native unsent inference cleanup was unavailable; the durable barrier remains.")

    async def _monitor(self, owner: asyncio.Task[Any]) -> None:
        try:
            while True:
                await asyncio.sleep(1)
                await asyncio.to_thread(self._check_owner)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any lost ownership must cancel external work
            log.warning("Native inference capacity ownership was lost; cancelling local request.")
            owner.cancel()

    def _check_owner(self) -> None:
        self.store.database.check_owner()
        self.slots.check()

    async def observe(self, capture: UsageCapture) -> None:
        """Commit content-free usage and admission outcome together.

        Failure is allowed to retain the earlier durable unknown request; it
        must never turn a paid response into another inference retry.
        """
        admission = capture.admission
        assert admission is not None
        values = capture.values
        async with self.database.sessions.begin() as db:
            row = await db.get(ProductInferenceRequest, admission.id, with_for_update=True)
            usage = await db.get(InferenceUsage, values["id"], with_for_update=True)
            if row is None or usage is None:
                raise InferenceAdmissionError("The admitted inference accounting record is unavailable.")
            # Preserve response-reported IDs only. Allocated ID remains separately
            # available even when the gateway omits or replaces its header.
            row.gateway_id = capture.gateway_call_id or row.gateway_id
            stable = {"id", "request_key", "session_id", "turn_id", "actor_id", "model", "provider",
                      "provider_scope", "created_at", "receipt_key", "cost_usd", "cost_status", "cost_source"}
            if row.cost_source == "gateway_receipt":
                stable.update({
                    "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
                    "cache_write_tokens", "reasoning_tokens", "response_model",
                })
            for name, value in values.items():
                if name not in stable:
                    setattr(usage, name, value)
            if values.get("coverage_error") == "duplicate_provider_receipt":
                row.recovery_error, row.next_attempt_at = "duplicate_provider_receipt", None
            cost = None if self.settings.litellm_charge_receipts_enabled else values.get("cost_usd")
            if row.cost_usd is None and cost is not None:
                receipt_key = hashlib.sha256(
                    f"{row.gateway_scope}/{row.key_hash}/{row.gateway_id or row.id}".encode()
                ).hexdigest()
                duplicate = await db.scalar(select(ProductInferenceRequest.id).where(
                    ProductInferenceRequest.receipt_key == receipt_key,
                    ProductInferenceRequest.id != row.id,
                ))
                if duplicate:
                    usage.coverage_error = row.recovery_error = "duplicate_provider_receipt"
                    row.next_attempt_at = None
                else:
                    row.cost_usd = usage.cost_usd = cost
                    row.cost_source = usage.cost_source = values.get("cost_source")
                    row.receipt_key = usage.receipt_key = receipt_key
                    usage.cost_status = "actual"
                    row.next_attempt_at = None
            if capture.finished:
                row.status = (
                    "not_dispatched" if admission.unsent else
                    values["status"] if values["status"] in {"completed", "failed"} and (
                        capture.terminal or (capture.exhausted and not capture.streaming)
                    )
                    else "unknown"
                )
                row.finished_at = values["finished_at"]
                if admission.unsent:
                    row.next_attempt_at = None

    async def recover_once(self) -> int:
        if self.recovery is None:
            raise InferenceAdmissionError("Product inference recovery is not running.")
        return await self.recovery.poll()

    def proxy_request(self, request: httpx.Request, capture: UsageCapture) -> httpx.Request:
        from .product_inference_routes import broker_request

        self.validate_request(request, capture)
        return broker_request(self, request, capture)
