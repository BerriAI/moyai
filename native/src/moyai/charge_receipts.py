"""Apply authenticated gateway charges to the existing consolidated ledger."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from sqlalchemy import String, bindparam, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .billing_storage_models import BillingRecord, BillingStorageState
from .models import now
from .product_inference import ProductInferenceRequest

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
TokenCount = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
MAX_RECEIPTS_PER_REQUEST = 1024


class ChargeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    event_id: str = Field(min_length=1, max_length=4096)
    request_id: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    gateway_scope: Digest
    key_hash: Digest
    currency: Literal["USD"]
    amount_usd: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,24})?$")
    model: str = Field(min_length=1, max_length=200)
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    total_tokens: TokenCount | None = None
    cache_read_tokens: TokenCount | None = None
    cache_write_tokens: TokenCount | None = None
    reasoning_tokens: TokenCount | None = None

    def identity(self) -> str:
        return hashlib.sha256(
            f"{self.gateway_scope}/{self.key_hash}/{self.event_id}".encode(),
        ).hexdigest()

    def fingerprint(self) -> str:
        value = self.model_dump()
        value["amount_usd"] = self.amount_usd.rstrip("0").rstrip(".") if "." in self.amount_usd else self.amount_usd
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ChargeReceiptBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    events: tuple[ChargeReceipt, ...] = Field(min_length=1, max_length=32)


class AppliedReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    receipt: ChargeReceipt
    fingerprint: Digest


APPLIED_RECEIPTS = TypeAdapter(dict[Digest, AppliedReceipt])
JSON_RECEIPTS = TypeAdapter(dict[str, JsonValue])

ReceiptError = Literal[
    "storage_not_consolidated", "unknown_request", "scope_mismatch", "not_dispatched",
    "missing_ledger", "receipt_conflict",
    "invalid_ledger_receipts", "receipt_limit", "unsafe_durability",
]


class ReceiptResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    event_id: str
    acknowledged: bool
    duplicate: bool = False
    error: ReceiptError | None = None


class ChargeReceiptIngestor:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def accept(self, receipt: ChargeReceipt) -> ReceiptResult:
        try:
            async with self.sessions.begin() as db:
                await db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
                await db.execute(text("SET LOCAL synchronous_commit = on"))
                result = await self._apply(db, receipt)
            return result
        except IntegrityError:
            return self._reject(receipt, "receipt_conflict")

    async def _apply(self, db: AsyncSession, receipt: ChargeReceipt) -> ReceiptResult:
        identity = receipt.identity()
        lock = int.from_bytes(hashlib.sha256(("charge/" + identity).encode()).digest()[:8], "big", signed=True)
        durable = await db.scalar(text(
            "SELECT current_setting('fsync') = 'on' FROM pg_advisory_xact_lock(:lock)"
        ), {"lock": lock})
        if not durable:
            return self._reject(receipt, "unsafe_durability")
        state = await db.get(BillingStorageState, 1)
        if state is None or state.phase != "consolidated":
            return self._reject(receipt, "storage_not_consolidated")
        admission = await db.get(ProductInferenceRequest, receipt.request_id, with_for_update=True)
        if admission is None:
            return self._reject(receipt, "unknown_request")
        if (admission.gateway_scope, admission.key_hash) != (receipt.gateway_scope, receipt.key_hash):
            return self._reject(receipt, "scope_mismatch")
        if admission.status == "not_dispatched":
            return self._reject(receipt, "not_dispatched")

        fingerprint = receipt.fingerprint()
        primary = await db.get(BillingRecord, ("native", admission.request_key), with_for_update=True)
        if primary is None:
            return self._reject(receipt, "missing_ledger")
        if any(getattr(primary, field) != getattr(admission, field) for field in (
            "request_key", "actor_id", "session_id", "turn_id", "model",
        )):
            return self._reject(receipt, "scope_mismatch")
        previous = list((await db.scalars(select(BillingRecord).where(
            BillingRecord.billing_receipts.op("?")(bindparam("identity", type_=String)),
        ).limit(2), {"identity": identity})).all())
        if len(previous) > 1 or previous and previous[0] is not primary:
            return self._reject(receipt, "receipt_conflict")
        try:
            saved = APPLIED_RECEIPTS.validate_python(primary.billing_receipts or {})
        except ValidationError:
            return self._reject(receipt, "invalid_ledger_receipts")
        if any(
            key != entry.receipt.identity() or entry.fingerprint != entry.receipt.fingerprint()
            or (entry.receipt.request_id, entry.receipt.gateway_scope, entry.receipt.key_hash)
            != (admission.id, admission.gateway_scope, admission.key_hash)
            for key, entry in saved.items()
        ):
            return self._reject(receipt, "invalid_ledger_receipts")
        if identity in saved:
            if saved[identity].fingerprint != fingerprint or primary.billing_request_id != admission.id:
                return self._reject(receipt, "receipt_conflict")
            return ReceiptResult(event_id=receipt.event_id, acknowledged=True, duplicate=True)
        if len(saved) >= MAX_RECEIPTS_PER_REQUEST:
            return self._reject(receipt, "receipt_limit")
        updated = {**saved, identity: AppliedReceipt(receipt=receipt, fingerprint=fingerprint)}
        primary.billing_receipts = JSON_RECEIPTS.validate_python(APPLIED_RECEIPTS.dump_python(updated, mode="json"))
        primary.billing_request_id = admission.id
        primary.billing_purpose = admission.purpose
        if not saved:
            primary.receipt_key = identity
            primary.cost_receipt_id = receipt.event_id
        primary.gateway_scope = receipt.gateway_scope
        primary.key_hash = receipt.key_hash
        with localcontext() as context:
            context.prec = 80
            primary.cost_usd = format(sum(
                (Decimal(value.receipt.amount_usd) for value in updated.values()), Decimal(0),
            ), "f")
        primary.cost_status = "actual"
        primary.cost_source = "gateway_receipt"
        models = {value.receipt.model for value in updated.values()}
        primary.response_model = next(iter(models)) if len(models) == 1 else None
        primary.provider_timestamp = now()
        for field in (
            "input_tokens", "output_tokens", "total_tokens",
            "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        ):
            values = [getattr(value.receipt, field) for value in updated.values()]
            total = sum(values) if all(value is not None for value in values) else None
            setattr(primary, field, total if total is None or total < 2**63 else None)
        admission.cost_usd = primary.cost_usd
        admission.cost_source = "gateway_receipt"
        admission.receipt_key = primary.receipt_key
        primary_event = primary.cost_receipt_id or ""
        admission.cost_receipt_id = (
            primary_event if len(primary_event) <= 200
            else "sha256:" + hashlib.sha256(primary_event.encode()).hexdigest()
        )
        admission.recovery_error = None
        admission.next_attempt_at = None
        return ReceiptResult(event_id=receipt.event_id, acknowledged=True)

    @staticmethod
    def _reject(receipt: ChargeReceipt, error: ReceiptError) -> ReceiptResult:
        return ReceiptResult(event_id=receipt.event_id, acknowledged=False, error=error)
