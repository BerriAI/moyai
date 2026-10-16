"""Durable native review metadata; private input snapshots remain encrypted."""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductMemoryReview(Base):
    __tablename__ = "moyai_product_memory_reviews"

    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    owner_id: Mapped[str] = mapped_column(String(512))
    authority_version: Mapped[str] = mapped_column(String(64))
    preferences_revision: Mapped[int] = mapped_column(Integer)
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(200), unique=True)
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    saved_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
