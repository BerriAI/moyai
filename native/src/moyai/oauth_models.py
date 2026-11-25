"""Personal OAuth secrets stay separate from admitted, durable connection grants."""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base


class OAuthCredential(Base):
    __tablename__ = "moyai_oauth_credentials"
    connection_id: Mapped[str] = mapped_column(ForeignKey("moyai_connections.id"), primary_key=True)
    encrypted: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    authorization_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class OAuthAuthorization(Base):
    __tablename__ = "moyai_oauth_authorizations"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("moyai_users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    session_hash: Mapped[str] = mapped_column(String(64))
    auth_version: Mapped[int] = mapped_column(Integer)
    connection_id: Mapped[str] = mapped_column(ForeignKey("moyai_connections.id"))
    connection_revision: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[int] = mapped_column(Integer)
    encrypted: Mapped[str] = mapped_column(Text)
