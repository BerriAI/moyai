"""Personal conversation identity and immutable background-check provenance."""

from typing import Literal

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
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


class DotSlackPreference(Base):
    """Explicit notification consent; the existing Delivery table owns sends."""

    __tablename__ = "moyai_dot_slack_preferences"

    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient: Mapped[str | None] = mapped_column(Text, nullable=True)
    destination_key: Mapped[str | None] = mapped_column(String(201), nullable=True, unique=True)
    started_ts: Mapped[str | None] = mapped_column(String(40), nullable=True)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
    slack_app: Mapped[str | None] = mapped_column(String(8), nullable=True)


class DotSlackConversation(Base):
    """One private conversation per Slack root; the personal Dot stays stable."""

    __tablename__ = "moyai_dot_slack_conversations"

    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    dot_session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), index=True)
    notification_turn_id: Mapped[str | None] = mapped_column(
        ForeignKey("moyai_dot_notifications.turn_id"), nullable=True, unique=True,
    )


class DotDiscoveryTurn(Base):
    __tablename__ = "moyai_dot_discovery_turns"

    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_dot_attention_turns.turn_id"), primary_key=True)
    # Encrypted, host-validated observations and item revisions. Recorded once
    # before judgment, so retries cannot acquire authority over newer edits.
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources: Mapped[str] = mapped_column(Text)


class DotItem(Base):
    __tablename__ = "moyai_dot_items"
    __table_args__ = (UniqueConstraint("owner_id", "source_key", name="dot_item_source"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    connection_id: Mapped[str] = mapped_column(ForeignKey("moyai_connections.id"))
    account_key: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(64))
    source_version: Mapped[str] = mapped_column(Text)  # encrypted
    content: Mapped[str] = mapped_column(Text)  # encrypted title, summary and evidence
    state: Mapped[Literal["open", "resolved", "dismissed", "snoozed"]] = mapped_column(String(20), default="open")
    owner_controlled: Mapped[bool] = mapped_column(Boolean, default=False)
    owner_revision: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    snoozed_until: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    last_seen_at: Mapped[str] = mapped_column(String(40), default=now)
