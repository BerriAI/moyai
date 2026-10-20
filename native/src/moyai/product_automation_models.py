"""Native execution bindings; canonical automation definitions/history stay in the product."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductAutomationBinding(Base):
    __tablename__ = "moyai_product_automation_bindings"

    # IDs/revisions refer to the canonical source, never a copied definition.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    authority_version: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(20), default="paused")
    sync_state: Mapped[str] = mapped_column(String(20), default="pending")
    definition_digest: Mapped[str] = mapped_column(String(64))
    schedule_ids: Mapped[str] = mapped_column(Text, default="[]")
    synced_generation: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductAutomationOccurrence(Base):
    __tablename__ = "moyai_product_automation_occurrences"
    __table_args__ = (
        Index("moyai_product_automation_history", "automation_id", "created_at"),
    )

    occurrence: Mapped[str] = mapped_column(String(512), primary_key=True)
    automation_id: Mapped[str] = mapped_column(ForeignKey("moyai_product_automation_bindings.id"))
    generation: Mapped[int] = mapped_column(Integer)
    source_revision: Mapped[int] = mapped_column(Integer)
    trigger_id: Mapped[str] = mapped_column(String(40))
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_sessions.id"), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_turns.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductAutomationOperation(Base):
    __tablename__ = "moyai_product_automation_operations"

    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), primary_key=True)
    # The service supplies bounded ASCII keys (tool turn or HTTP operation scope).
    operation_scope: Mapped[str] = mapped_column(String(80), primary_key=True)
    request_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    # Paused/unmigrated canonical definitions can have mutation receipts too.
    automation_id: Mapped[str] = mapped_column(String(32))
    source_revision: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(Text, default="{}")


class ProductAutomationTurn(Base):
    """Automation provenance follows a turn and its resumptions, never the whole chat."""

    __tablename__ = "moyai_product_automation_turns"
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    origin_turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
