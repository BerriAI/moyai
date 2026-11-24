"""Independent durable responsibilities; execution stays in native turns."""

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, new_id, now


class Responsibility(Base):
    __tablename__ = "moyai_responsibilities"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="responsibility_request"),
        Index("moyai_responsibility_due", "status", "next_check_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    product_grant: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_key: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"))
    objective: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="waiting")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    watermark: Mapped[int] = mapped_column(Integer, default=0)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    max_evaluations: Mapped[int] = mapped_column(Integer, default=10)
    evaluations: Mapped[int] = mapped_column(Integer, default=0)
    active_turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    next_check_at: Mapped[str] = mapped_column(String(40), default=now)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class ResponsibilityAssignment(Base):
    __tablename__ = "moyai_responsibility_assignments"
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    responsibility_id: Mapped[str] = mapped_column(ForeignKey("moyai_responsibilities.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    through_sequence: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text)
    reconciled: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[str] = mapped_column(String(40))


class ResponsibilityCompletion(Base):
    __tablename__ = "moyai_responsibility_completions"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True, index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), unique=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("moyai_messages.id"))


class ResponsibilityFinding(Base):
    __tablename__ = "moyai_responsibility_findings"
    __table_args__ = (UniqueConstraint("responsibility_id", "fingerprint", name="responsibility_finding_evidence"),)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_responsibility_assignments.turn_id"), primary_key=True)
    responsibility_id: Mapped[str] = mapped_column(ForeignKey("moyai_responsibilities.id"), index=True)
    decision: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    read_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class ResponsibilityWake(Base):
    """Transactional outbox; no parent FK lock while a source/worker is settling."""

    __tablename__ = "moyai_responsibility_wakes"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    responsibility_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
