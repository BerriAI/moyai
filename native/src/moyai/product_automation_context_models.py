"""Immutable execution selections and event cursors; no copied definitions."""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductAutomationExecutionContext(Base):
    __tablename__ = "moyai_product_automation_execution_contexts"

    automation_id: Mapped[str] = mapped_column(ForeignKey("moyai_product_automation_bindings.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    authority_version: Mapped[str] = mapped_column(String(64))
    connection_ids: Mapped[str] = mapped_column(Text, default="[]")
    workspace_context: Mapped[str] = mapped_column(Text, default="{}")
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductAutomationEventCursor(Base):
    __tablename__ = "moyai_product_automation_event_cursors"

    automation_id: Mapped[str] = mapped_column(ForeignKey("moyai_product_automation_bindings.id"), primary_key=True)
    engine: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
