"""Native publication intent/receipts; never fabricate canonical run records."""

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductGitOperation(Base):
    __tablename__ = "moyai_product_git_operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    connection_id: Mapped[str] = mapped_column(String(36))
    connection_revision: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(80))
    fingerprint: Mapped[str] = mapped_column(String(64))
    original_effect_id: Mapped[str] = mapped_column(ForeignKey("moyai_effects.id"))
    document: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
