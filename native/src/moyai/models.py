"""Application records. SDK conversations/steps/plans remain native SDK data."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "moyai_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="member")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auth_version: Mapped[int] = mapped_column(Integer, default=1)
    slack_user_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    preferences: Mapped[str] = mapped_column(Text, default="{}")


class LoginAttempt(Base):
    __tablename__ = "moyai_login_attempts"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    window: Mapped[int] = mapped_column(Integer, index=True)
    count: Mapped[int] = mapped_column(Integer, default=1)


class Session(Base):
    __tablename__ = "moyai_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    surface: Mapped[str] = mapped_column(String(20), default="web")
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="idle")
    active_turn: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_grants: Mapped[str | None] = mapped_column(String(64), nullable=True)
    conversation: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    save_warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now, index=True)


class SessionPreference(Base):
    __tablename__ = "moyai_session_preferences"
    user_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    folder: Mapped[str] = mapped_column(String(200), default="")


class Turn(Base):
    __tablename__ = "moyai_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "actor_id", "request_key", name="turn_request_identity"),
        Index("turn_dispatch", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    request_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="queued")
    mode: Mapped[str] = mapped_column(String(20), default="when_idle")
    parent_turn: Mapped[str | None] = mapped_column(String(36), nullable=True)
    connections: Mapped[str] = mapped_column(Text, default="[]")
    deferred_results: Mapped[str | None] = mapped_column(Text, nullable=True)
    preparation: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    usage: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_id: Mapped[str] = mapped_column(String(100), unique=True)
    enqueue_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    accepted_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    finished_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Message(Base):
    __tablename__ = "moyai_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
    author_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_users.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    identity: Mapped[str] = mapped_column(String(200), unique=True)


class Event(Base):
    __tablename__ = "moyai_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
    kind: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    identity: Mapped[str] = mapped_column(String(250), unique=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Upload(Base):
    __tablename__ = "moyai_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("moyai_sessions.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    media_type: Mapped[str] = mapped_column(String(100))
    digest: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class TurnImage(Base):
    __tablename__ = "moyai_turn_images"
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    upload_id: Mapped[str] = mapped_column(ForeignKey("moyai_uploads.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)


class ComputerLease(Base):
    __tablename__ = "moyai_computer_leases"
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    expires_at: Mapped[int] = mapped_column(Integer)


class ComputerOperation(Base):
    __tablename__ = "moyai_computer_operations"
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    identity: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[int] = mapped_column(Integer)


class Connection(Base):
    __tablename__ = "moyai_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(150))
    credential: Mapped[str] = mapped_column(Text)
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    policy: Mapped[str] = mapped_column(Text, default="{}")


class Artifact(Base):
    __tablename__ = "moyai_artifacts"
    __table_args__ = (UniqueConstraint("turn_id", "name", "digest"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    media_type: Mapped[str] = mapped_column(String(150))
    digest: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class SlackThread(Base):
    __tablename__ = "moyai_slack_threads"
    __table_args__ = (UniqueConstraint("team_id", "channel_id", "thread_ts"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(String(100))
    channel_id: Mapped[str] = mapped_column(String(100))
    thread_ts: Mapped[str] = mapped_column(String(50))
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), unique=True)


class SlackInbox(Base):
    __tablename__ = "moyai_slack_inbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Delivery(Base):
    __tablename__ = "moyai_deliveries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity: Mapped[str] = mapped_column(String(250), unique=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
    destination: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    receipt: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class Effect(Base):
    __tablename__ = "moyai_effects"
    __table_args__ = (
        Index(
            "moyai_one_unsettled_effect", "session_id", unique=True,
            sqlite_where=text("state = 'pending'"), postgresql_where=text("state = 'pending'"),
        ),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    tool_name: Mapped[str] = mapped_column(String(250))
    arguments: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="pending")
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    settled_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

