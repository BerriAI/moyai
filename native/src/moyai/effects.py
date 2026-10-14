"""Fence possibly completed external writes without replacing the SDK's tool ledger."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from temporalio.exceptions import ActivityError, ApplicationError

from .connections import read_only_tool
from .contracts import RunScope
from .database import Database
from .models import Effect, now

COMPUTER_EFFECTS = {"shell", "run_command", "start_command"}
SERVICE_PREFIXES = ("github_", "linear_", "notion_", "slack_")


def requires_receipt(tool: ToolDefinition) -> bool:
    return tool.name in COMPUTER_EFFECTS or (
        tool.name.startswith(SERVICE_PREFIXES) and not read_only_tool(tool)
    )


def effect_id(ctx: RunContext[RunScope], call: ToolCallPart) -> str:
    return hashlib.sha256(f"{ctx.deps.turn_id}:{ctx.run_id}:{call.tool_call_id}".encode()).hexdigest()


class EffectReceipts(AbstractCapability[RunScope]):
    id = "moyai_effects"

    def __init__(self, database: Database):
        self.database = database

    @durable_operation("reserve")
    async def reserve(self, scope: RunScope, identity: str, name: str, args: dict[str, Any]) -> None:
        await self.database.authorize_scope(scope)
        serialized = json.dumps(args, sort_keys=True, default=str)
        fingerprint = hashlib.sha256(f"{name}:{serialized}".encode()).hexdigest()
        try:
            async with self.database.sessions.begin() as db:
                previous = await db.get(Effect, identity)
                if previous:
                    if previous.fingerprint != fingerprint:
                        raise ValueError("An effect identity cannot change its operation.")
                    return
                if await db.scalar(select(Effect.id).where(
                    Effect.session_id == scope.session_id, Effect.state == "pending"
                )):
                    raise PermissionError("An earlier action needs reconciliation before another write.")
                db.add(Effect(
                    id=identity, session_id=scope.session_id, turn_id=scope.turn_id,
                    actor_id=scope.actor_id, tool_name=name,
                    arguments=self.database.security.seal(serialized), fingerprint=fingerprint,
                ))
        except IntegrityError as error:
            raise PermissionError("Another action is awaiting a confirmed outcome.") from error

    @durable_operation("confirm")
    async def confirm(self, identity: str) -> None:
        # A received result is factual evidence even if Stop/revocation arrived meanwhile.
        async with self.database.sessions.begin() as db:
            await db.execute(update(Effect).where(
                Effect.id == identity, Effect.state == "pending"
            ).values(state="confirmed", settled_at=now()))

    async def prepare_tools(
        self, ctx: RunContext[RunScope], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [
            replace(tool, sequential=True) if requires_receipt(tool) else tool
            for tool in tool_defs
        ]

    async def before_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any],
    ) -> dict[str, Any]:
        if requires_receipt(tool_def):
            # A deferred native tool has not dispatched yet. ApprovalRequired is
            # enforced by the SDK before this hook once the tool is marked below.
            await self.reserve(ctx.deps, effect_id(ctx, call), call.tool_name, args)
        return args

    async def after_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], result: Any,
    ) -> Any:
        if requires_receipt(tool_def):
            await self.confirm(effect_id(ctx, call))
        return result

    async def on_tool_execute_error(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], error: Exception,
    ) -> Any:
        cause = error.cause if isinstance(error, ActivityError) else None
        if (
            requires_receipt(tool_def) and isinstance(cause, ApplicationError)
            and cause.type in {"MoyaiGuidanceChanged", "MoyaiPermissionDenied", "MoyaiMissingExecutionScope"}
        ):
            # Our inbound interceptor refused before calling the tool body. This
            # outcome is known, so it must not leave a false uncertainty barrier.
            await self.confirm(effect_id(ctx, call))
        raise error
