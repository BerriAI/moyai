"""Versioned maintenance intent and safe-boundary receipts, never agent state."""

from __future__ import annotations

import hashlib

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now

BOOTSTRAP_FINGERPRINT = hashlib.sha256(b"bootstrap").hexdigest()


class ProductDrainState(Base):
    __tablename__ = "moyai_product_drain_state"
    __table_args__ = (CheckConstraint("id = 1", name="moyai_product_drain_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, default=0)
    paused: Mapped[bool] = mapped_column(Boolean, default=True)
    request_key: Mapped[str] = mapped_column(String(200), default="bootstrap")
    request_fingerprint: Mapped[str] = mapped_column(String(64), default=BOOTSTRAP_FINGERPRINT)
    requested_at: Mapped[str] = mapped_column(String(40), default=now)
    owner_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    owner_started: Mapped[str | None] = mapped_column(String(40), nullable=True)
    source_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProductDrainWorker(Base):
    __tablename__ = "moyai_product_drain_workers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_pid: Mapped[int] = mapped_column(Integer)
    owner_started: Mapped[str] = mapped_column(String(40))
    requested_drain: Mapped[bool] = mapped_column(Boolean)
    source_policy: Mapped[str] = mapped_column(String(64))
    worker_policy: Mapped[str] = mapped_column(String(64))
    registered_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductDrainReceipt(Base):
    __tablename__ = "moyai_product_drain_receipts"
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(200))
    workflow_run_id: Mapped[str] = mapped_column(String(36))
    boundary: Mapped[str] = mapped_column(String(80))
    phase: Mapped[str] = mapped_column(String(16))
    parked_at: Mapped[str] = mapped_column(String(40), default=now)
    resumed_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resumed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
