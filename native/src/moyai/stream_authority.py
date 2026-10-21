"""Fresh authority for each SSE release, without redundant native checkouts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request

from .models import User

if TYPE_CHECKING:
    from .database import Database
    from .product_identity import ProductIdentityAdapter


async def authorize_stream_viewer(
    database: Database,
    identity: ProductIdentityAdapter | None,
    request: Request,
    user: User,
    session_id: str,
) -> None:
    # Request-local Slack audience results must not survive a stream release.
    cast(Any, user).slack_read_cache = {}
    # Canonical authority owns its own readers and also checks native controls.
    # Finish before holding a native connection, including with a size-one pool.
    if identity is not None:
        await identity.require_request_current(request)
    await database.prefetch_session_audiences(user, [session_id])
    # A new session per check keeps native controls and ACLs fresh. Reuse its
    # connection for the checks, never its identity map across releases.
    async with database.sessions() as db:
        fresh = await db.get(User, user.id)
        if fresh is None or not fresh.enabled or fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your access changed.")
        await database.allowed_session(db, session_id, user)
