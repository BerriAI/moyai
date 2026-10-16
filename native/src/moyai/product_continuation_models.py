"""Explicit product continuation records; importing this module performs no I/O."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now

CONTINUATION_TABLES = frozenset({
    "moyai_product_continuations",
    "moyai_product_workspace_restorations",
    "moyai_product_execution_contexts",
})


class ProductContinuation(Base):
    __tablename__ = "moyai_product_continuations"
    __table_args__ = (UniqueConstraint("native_session_id"),)

    source_schema: Mapped[str] = mapped_column(String(63), primary_key=True)
    source_session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    native_session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"))
    source_owner_id: Mapped[str] = mapped_column(String(512))
    initiated_by: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    history: Mapped[str] = mapped_column(Text)  # Native-sealed public-message seed; never legacy checkpoints.
    history_count: Mapped[int] = mapped_column(Integer)
    imported_count: Mapped[int] = mapped_column(Integer)
    recent_history: Mapped[bool] = mapped_column(Boolean, default=False)
    workspace: Mapped[str] = mapped_column(Text)  # Native-sealed, exact source file/snapshot references.
    workspace_fingerprint: Mapped[str] = mapped_column(String(64))
    first_turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"))
    provenance: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductWorkspaceRestoration(Base):
    __tablename__ = "moyai_product_workspace_restorations"

    session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(250), primary_key=True)
    workspace_scope: Mapped[str] = mapped_column(String(64))
    manifest_fingerprint: Mapped[str] = mapped_column(String(64))
    files: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(BigInteger)
    completed_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductExecutionContext(Base):
    """A canonical repository/environment selection pinned at native admission."""

    __tablename__ = "moyai_product_execution_contexts"

    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    descriptor: Mapped[str] = mapped_column(Text)  # Native-sealed reference descriptor, not a copied recipe/key.
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductContinuationCapture(Base):
    """Verified recovery bytes retained before an old computer is terminated.

    Added separately from the v5 inventory so its original receipt stays stable.
    """

    __tablename__ = "moyai_product_continuation_captures"

    native_session_id: Mapped[str] = mapped_column(ForeignKey("moyai_sessions.id"), primary_key=True)
    source_session_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    descriptor: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40), default=now)
