"""Native request provenance; credential values stay in the canonical vault."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductCredentialRequest(Base):
    __tablename__ = "moyai_product_credential_requests"
    __table_args__ = (
        UniqueConstraint("session_id", "actor_id", "turn_id", "request_key"),
        UniqueConstraint("turn_id", "tool_call_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    authority_version: Mapped[str] = mapped_column(String(64))
    tool_call_id: Mapped[str] = mapped_column(String(200))
    request_key: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64))
    capability: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    secret_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    secret_revision: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    resolution_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
