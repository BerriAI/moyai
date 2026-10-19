"""Consolidated accounting observations; calculation remains in product_billing.

Source identities are deliberately namespaced. Two observations are not one
charge merely because they have the same ID, amount, or timestamp. The existing
reconciler, rather than this storage layer, decides which charges overlap.
"""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue
from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class BillingRecord(Base):
    __tablename__ = "moyai_billing_records"

    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Native request identity and metadata. Legacy rows leave these null.
    request_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    response_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    response_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    provider_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    provider_timestamp: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reasoning_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    coverage_error: Mapped[str | None] = mapped_column(String(50), nullable=True)
    billing_request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    billing_receipts: Mapped[dict[str, JsonValue] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True,
    )
    billing_purpose: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Historical metadata remains available to the unchanged recovery policy.
    legacy_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    key_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    gateway_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gateway_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_receipt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_next_attempt_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_recovery_attempts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cost_recovery_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_moyai_billing_records_source_time", "source", "created_at"),
        Index("ix_moyai_billing_records_gateway", "source", "gateway_id"),
        Index("ix_moyai_billing_records_provider", "source", "provider_request_id"),
        Index("ix_moyai_billing_records_actor_time", "actor_id", "created_at"),
        Index("ix_moyai_billing_records_session", "session_id"),
        Index("ix_moyai_billing_records_turn", "turn_id"),
        Index("ix_moyai_billing_records_billing_request", "billing_request_id"),
        Index("ix_moyai_billing_records_receipts", "billing_receipts", postgresql_using="gin"),
        Index("ix_moyai_billing_records_legacy_run", "legacy_run_id"),
        Index("ix_moyai_billing_records_legacy_recovery", "key_hash", "gateway_scope", "cost_next_attempt_at"),
        Index(
            "ix_moyai_billing_records_legacy_receipt", "gateway_scope", "key_hash", "cost_receipt_id",
            unique=True, postgresql_where=text("source='legacy' AND cost_receipt_id<>''"),
            sqlite_where=text("source='legacy' AND cost_receipt_id<>''"),
        ),
        CheckConstraint("source IN ('legacy','native')", name="moyai_billing_source"),
    )


class BillingStorageState(Base):
    __tablename__ = "moyai_billing_storage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    source_schema: Mapped[str] = mapped_column(Text)
    native_schema: Mapped[str] = mapped_column(Text)
    phase: Mapped[str] = mapped_column(String(20))
    legacy_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    native_cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    legacy_done: Mapped[bool] = mapped_column(Boolean)
    native_done: Mapped[bool] = mapped_column(Boolean)
    view_manifest: Mapped[dict[str, Any] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True,
    )


LEGACY_COLUMNS = {
    "id": "source_id", "run_id": "legacy_run_id", "user_id": "actor_id", "cost": "cost_usd",
    "prompt_tokens": "input_tokens", "completion_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens", "cache_creation_input_tokens": "cache_write_tokens",
}


def storage_column(source: str, field: str) -> str:
    if field == "id":
        return "source_id"
    return LEGACY_COLUMNS.get(field, field) if source == "legacy" else field
