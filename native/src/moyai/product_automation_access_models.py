"""Mutable owner-issued grants, bounded to one persisted native human turn."""

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductAutomationChatGrant(Base):
    __tablename__ = "moyai_product_automation_chat_grants"

    # The canonical definition may still be paused and unmigrated.
    automation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    permissions: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
