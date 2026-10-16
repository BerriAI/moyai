"""Consolidated accounting observations; calculation remains in product_billing.

Source identities are deliberately namespaced. Two observations are not one
charge merely because they have the same ID, amount, or timestamp. The existing
reconciler, rather than this storage layer, decides which charges overlap.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class BillingRecord(Base):
    __tablename__ = "moyai_billing_records"

    source: Mapped[str] = mapped_column(String(16), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    identities: Mapped[list[str]] = mapped_column(ARRAY(Text).with_variant(JSON, "sqlite"))
    record: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))

    __table_args__ = (
        Index("ix_moyai_billing_records_source_time", "source", "created_at"),
        Index("ix_moyai_billing_records_identities", "identities", postgresql_using="gin"),
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
