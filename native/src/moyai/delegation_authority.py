"""Sealed execution authority for a private delegated conversation.

Presentation stays private/web. Every turn in the child, including human
follow-ups, uses the originating policy context so its computer and connector
selection stay usable without acquiring personal capabilities from a shared
assignment. Generated turns additionally retain the exact admitted grants.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from .contracts import ExecutionContext, Grant, PreparedTurn
from .delegation_models import DelegatedSession
from .models import Session, Turn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database


@dataclass
class DelegatedAuthority:
    context: ExecutionContext
    grants: list[Grant]


async def saved_authority(
    database: Database, db: AsyncSession, link: DelegatedSession, *, depth: int = 0,
) -> DelegatedAuthority:
    value = json.loads(database.security.unseal(link.grants))
    if isinstance(value, dict):
        return TypeAdapter(DelegatedAuthority).validate_python(value)
    # Earlier revisions sealed just the grant list. Recover context from the
    # original preparation, never from today's mutable parent session settings.
    parent = await db.get(Turn, link.parent_turn_id)
    if parent is None or parent.preparation is None or depth >= 2:
        raise PermissionError("The delegated task's original authority is unavailable.")
    original = TypeAdapter(PreparedTurn).validate_json(database.security.unseal(parent.preparation))
    context = original.scope.authority
    ancestor = await db.get(DelegatedSession, link.parent_session_id)
    if ancestor is not None and original.scope.execution_context is None:
        context = (await saved_authority(database, db, ancestor, depth=depth + 1)).context
    return DelegatedAuthority(context, TypeAdapter(list[Grant]).validate_python(value))


async def session_context(database: Database, db: AsyncSession, session: Session) -> ExecutionContext:
    link = await db.get(DelegatedSession, session.id)
    if link is not None:
        return (await saved_authority(database, db, link)).context
    return ExecutionContext(session.visibility, session.surface)
