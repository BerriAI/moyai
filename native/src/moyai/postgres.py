"""Keep libpq URI options intact when SQLAlchemy opens an asyncpg connection."""

from __future__ import annotations

from typing import Any

import asyncpg
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_engine(url: str | URL, **options: Any) -> AsyncEngine:
    parsed = make_url(url)
    if parsed.drivername not in {"postgres", "postgresql", "postgresql+asyncpg"}:
        return create_async_engine(url, **options)
    dsn = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    connect_args = options.pop("connect_args", {})

    async def connect() -> asyncpg.Connection:
        return await asyncpg.connect(dsn, **connect_args)

    return create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"), async_creator=connect, **options,
    )
