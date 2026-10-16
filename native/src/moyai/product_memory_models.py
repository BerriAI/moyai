"""Native receipts/barriers only; canonical encrypted memory content stays in the source."""

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, now


class ProductMemoryOperation(Base):
    __tablename__ = "moyai_product_memory_operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"))
    owner_id: Mapped[str] = mapped_column(String(512))
    turn_id: Mapped[str] = mapped_column(ForeignKey("moyai_turns.id"))
    authority_version: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    memory_id: Mapped[str] = mapped_column(String(32))
    source_revision: Mapped[int] = mapped_column(Integer)
    version: Mapped[str | None] = mapped_column(String(120), nullable=True)
    existed: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[str] = mapped_column(String(40), default=now)


class ProductMemoryBarrier(Base):
    __tablename__ = "moyai_product_memory_barriers"

    owner_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)
