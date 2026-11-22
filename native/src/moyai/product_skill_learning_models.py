"""Native learning controls, durable work and provenance; lesson text stays encrypted."""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class SkillLearningPreference(Base):
    __tablename__ = "moyai_skill_learning_preferences"

    scope_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SkillLearningJob(Base):
    __tablename__ = "moyai_skill_learning_jobs"

    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    scope_key: Mapped[str] = mapped_column(String(512), index=True)
    authority_version: Mapped[str] = mapped_column(String(64))
    preferences_revision: Mapped[int] = mapped_column(Integer)
    input_fingerprint: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str] = mapped_column(String(200), unique=True)
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    saved_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class LearnedSkill(Base):
    __tablename__ = "moyai_learned_skills"
    __table_args__ = (UniqueConstraint("scope_key", "topic_hash", name="learned_skill_topic"),)

    skill_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(512), index=True)
    topic_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    source_turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"))
    evidence: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
