"""Personal conversation identity and immutable background-check provenance."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class PersonalDot(Base):
    __tablename__ = "moyai_personal_dots"

    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), unique=True)
    checkpoint_cursor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class DotAttentionTurn(Base):
    __tablename__ = "moyai_dot_attention_turns"

    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    automation_id: Mapped[str] = mapped_column(ForeignKey("moyai_product_automation_bindings.id"))
    generation: Mapped[int] = mapped_column(Integer)
    destination_session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"))
    grants_key: Mapped[str] = mapped_column(String(64))
    # Set only when the accepted output is published. The complete typed decision
    # and connector evidence remain in the turn's encrypted result/conversation.
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)


class DotNotification(Base):
    __tablename__ = "moyai_dot_notifications"
    __table_args__ = (UniqueConstraint("session_id", "fingerprint", name="dot_notification_evidence"),)

    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_dot_attention_turns.turn_id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("moyai_messages.id"), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
