"""Durable links between an ordinary conversation and its delegated sessions."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class DelegatedSession(Base):
    __tablename__ = "moyai_delegated_sessions"
    __table_args__ = (UniqueConstraint("parent_turn_id", "tool_call_id"),)

    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    parent_session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    parent_turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
    initial_turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), unique=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    tool_call_id: Mapped[str] = mapped_column(String(250))
    fingerprint: Mapped[str] = mapped_column(String(64))
    grants: Mapped[str] = mapped_column(Text)
    depth: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    avatar: Mapped[int] = mapped_column(Integer)
    report: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class DelegatedTurn(Base):
    """Only the initial assignment and its deferred-answer continuations."""

    __tablename__ = "moyai_delegated_turns"
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_delegated_sessions.session_id"), index=True)
