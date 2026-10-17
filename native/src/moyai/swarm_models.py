"""Product state for native teams; conversations and execution remain SDK-owned."""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class SwarmMission(Base):
    __tablename__ = "moyai_swarm_missions"
    __table_args__ = (UniqueConstraint("owner_id", "request_key"),)
    id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    auth_version: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"))
    goal: Mapped[str] = mapped_column(Text)
    connections: Mapped[str] = mapped_column(Text)
    grants_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="active")
    generation: Mapped[int] = mapped_column(Integer, default=1)
    dispatched_generation: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    board_seq: Mapped[int] = mapped_column(Integer, default=0)
    round: Mapped[int] = mapped_column(Integer, default=1)
    leader_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    completion_turn_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_turns.id"), nullable=True)
    completion_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    budget_seconds: Mapped[int] = mapped_column(Integer, default=0)
    ends_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    next_round_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SwarmMember(Base):
    __tablename__ = "moyai_swarm_members"
    __table_args__ = (UniqueConstraint("mission_id", "position"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("moyai_swarm_missions.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(200))
    seen_seq: Mapped[int] = mapped_column(Integer, default=0)
    assignment: Mapped[str] = mapped_column(Text, default="")


class SwarmPost(Base):
    __tablename__ = "moyai_swarm_posts"
    __table_args__ = (
        UniqueConstraint("mission_id", "sequence"),
        UniqueConstraint("mission_id", "identity"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("moyai_swarm_missions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    identity: Mapped[str] = mapped_column(String(200))
    author_session_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_sessions.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class SwarmTurn(Base):
    """Server-authored provenance and the mission generation admitted for a turn."""

    __tablename__ = "moyai_swarm_turns"
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("moyai_swarm_missions.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    generated: Mapped[bool] = mapped_column(Boolean)
    reported: Mapped[bool] = mapped_column(Boolean, default=False)


class SwarmOperation(Base):
    __tablename__ = "moyai_swarm_operations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mission_id: Mapped[str] = mapped_column(ForeignKey("moyai_swarm_missions.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(Text)
