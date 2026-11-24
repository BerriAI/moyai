"""Shared publication authority and refusal transitions, before provider writes."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from app.connector_errors import ConnectorError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ToolFailed
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import Grant, RunScope
from .models import Effect, Turn, User, now
from .product_github_models import ProductGitOperation as Operation

if TYPE_CHECKING:
    from .product_connections import ProductConnections


class PublicationJournal(ABC):
    _requester_name = "publication"
    _write_provider = "GitHub"
    _result_keys: tuple[str, ...] = ("result",)
    _replaced_error = "This publication attempt was refused or replaced. It will not dispatch a write."
    _refused_error = (
        "No repository write was attempted. After correcting the reported condition, "
        "retry with the same request_key and unchanged inputs in a new tool call."
    )
    _uncertain_error = (
        "This operation may already have attempted writes. Inspect publication_status "
        "with the original operation and request_key; do not start a replacement publication."
    )

    def __init__(self, service: ProductConnections, scope: RunScope, grant: Grant):
        self.service, self.scope, self.grant = service, scope, grant
        self.database = service.database
        self.target = grant.policy["product"]["repository_id"]

    async def authorize(self, db: AsyncSession) -> None:
        await self.service.product.identity.authorize_admission(db, self.scope.product_grant)
        actor, turn = await db.get(User, self.scope.actor_id), await db.get(Turn, self.scope.turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise ToolFailed(f"The {self._requester_name} requester is unavailable.")
        await self.database._check_product_turn(db, turn, self.scope.product_grant)
        cast(Any, actor).product_grant = self.scope.product_grant
        session = await self.database.execution_session(db, self.scope.session_id, actor)
        if session.active_turn != turn.id or turn.status != "running" or turn.actor_id != actor.id:
            raise ToolFailed(f"This {self._requester_name} was stopped or superseded.")
        current = await self.database.scope_grants(db, self.scope, session)
        if self.grant not in current or self.grant.read_only:
            raise ToolFailed(f"Current {self._write_provider} write access is required.")

    def document(self, row: Operation) -> dict:
        return json.loads(self.database.security.unseal(row.document))

    def require_attempt(self, current: Operation, row: Operation) -> None:
        if current.original_effect_id != row.original_effect_id or self.document(current).get("preflight_error"):
            raise ToolFailed(self._replaced_error)

    @abstractmethod
    async def operation(self, ctx: RunContext[RunScope], kind: str, request: Any) -> Operation:
        """Reserve an operation with the provider's frozen request identity."""

    @asynccontextmanager
    async def attempt(self, ctx: RunContext[RunScope], kind: str, request: Any) -> AsyncIterator[Operation]:
        row = await self.operation(ctx, kind, request)
        try:
            yield row
        except (ToolFailed, ConnectorError) as error:
            if await self.refuse_preflight(row, str(error)):
                raise ToolFailed(f"{error} {self._refused_error}") from None
            raise ToolFailed(f"{error} {self._uncertain_error}") from None

    async def refuse_preflight(self, row: Operation, reason: str) -> bool:
        # Share step's lock: a refusal must never reclassify a dispatched write.
        async with self.database.sessions.begin() as db:
            current = await db.get(Operation, row.id, with_for_update=True)
            if current is None or current.original_effect_id != row.original_effect_id:
                return False
            document = self.document(current)
            if document["attempts"] or document["steps"] or any(document.get(key) for key in self._result_keys):
                return False
            effect = await db.get(Effect, current.original_effect_id, with_for_update=True)
            if effect is None or effect.state not in {"pending", "not_dispatched"}:
                return False
            document["preflight_error"] = reason
            current.document, current.updated_at = self.database.security.seal(document), now()
            effect.state, effect.settled_at = "not_dispatched", now()
            return True
