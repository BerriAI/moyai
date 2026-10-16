"""Verified human reply identity; the resumed task keeps its original grant."""

from __future__ import annotations

from typing import Any, cast

from fastapi import HTTPException

from .models import Turn, User


async def answer_actor(runtime: Any, user: User, turn: Turn) -> User:
    if runtime.product is None or not runtime.database.product_authority_enabled:
        if user.id != turn.actor_id:
            raise HTTPException(403, "Only the original requester may answer this request.")
        return user
    identity = runtime.product.identity
    async with runtime.database.sessions.begin() as db:
        caller = await identity.authorize_admission(db, getattr(user, "product_grant", None))
        saved = await db.get(Turn, turn.id)
        if saved is None or saved.session_id != turn.session_id:
            raise HTTPException(404, "The requesting task is unavailable.")
        await runtime.database.allowed_session(db, saved.session_id, user)
        own = caller.id == saved.actor_id
        if not own and caller.kind == "slack":
            own = await identity.personal_memory_owner(db, caller.id) == saved.actor_id
        if not own and saved.actor_id.startswith("slack:") and caller.individual:
            own = await identity.personal_memory_owner(db, saved.actor_id) == caller.id
        if not own:
            raise HTTPException(403, "Only the original requester or their currently verified linked sign-in may answer.")
        original = await identity.authorize_admission(
            db, await runtime.database._saved_product_grant(db, saved),
        )
        actor = await db.get(User, saved.actor_id)
        if actor is None or not actor.enabled or actor.auth_version != original.native_auth_version:
            raise HTTPException(403, "The original task authority changed.")
        cast(Any, actor).product_grant = original.grant
        return actor


async def can_answer(runtime: Any, user: User, turn: Turn) -> bool:
    try:
        await answer_actor(runtime, user, turn)
        return True
    except HTTPException as error:
        if error.status_code not in {401, 403, 404}:
            raise
        return False
