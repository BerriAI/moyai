"""Additive Slack product binding; no runtime imports or schema side effects."""

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductSlackBinding(Base):
    __tablename__ = "moyai_product_slack_bindings"

    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    source_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    requester_id: Mapped[str] = mapped_column(String(512))
    team_id: Mapped[str] = mapped_column(String(100))
    channel_id: Mapped[str] = mapped_column(String(100))
    thread_ts: Mapped[str] = mapped_column(String(50))
    started_ts: Mapped[str] = mapped_column(String(50))
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    connection_authority: Mapped[str] = mapped_column(String(64))
    delivery_after: Mapped[str] = mapped_column(String(40), default=now)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
