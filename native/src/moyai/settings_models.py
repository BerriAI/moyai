"""Additive Settings records; existing native execution tables are unchanged.

Import this module before Database.start(), which creates new tables under the
native schema lock. No ALTER TABLE or legacy database access is required.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, new_id, now


class SettingSkill(Base):
    __tablename__ = "moyai_setting_skills"
    __table_args__ = (UniqueConstraint("scope_key", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    scope_key: Mapped[str] = mapped_column(String(512), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(1024))
    content: Mapped[str] = mapped_column(Text)  # encrypted Markdown
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SettingSecret(Base):
    __tablename__ = "moyai_setting_secrets"
    __table_args__ = (UniqueConstraint("scope_key", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    scope_key: Mapped[str] = mapped_column(String(512), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(1000), default="")
    value: Mapped[str] = mapped_column(Text)  # encrypted, never returned by the API
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SettingAutomation(Base):
    __tablename__ = "moyai_setting_automations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    owner_auth_version: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"))
    name: Mapped[str] = mapped_column(String(150))
    prompt: Mapped[str] = mapped_column(Text)  # encrypted
    model: Mapped[str] = mapped_column(String(200))
    connections: Mapped[str] = mapped_column(Text, default="[]")
    interval_minutes: Mapped[int] = mapped_column(Integer)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(30), default="provisioning")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class SettingAutomationRun(Base):
    """One durable admission receipt per Temporal occurrence, not a run engine."""

    __tablename__ = "moyai_setting_automation_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    automation_id: Mapped[str] = mapped_column(ForeignKey("moyai_setting_automations.id"), index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_turns.id"), nullable=True)
    request: Mapped[str] = mapped_column(Text)  # encrypted immutable admission snapshot
    status: Mapped[str] = mapped_column(String(30), default="admitting")
    created_at: Mapped[str] = mapped_column(String(40), default=now)
