"""Check command readiness inside the native shell activity, before dispatch.

A dead computer is not evidence that an earlier command did not execute. Only
the activity that owns the pre-dispatch claim may record that outcome. Once it
enters the SDK shell, a lost result still requires reconciliation.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from functools import cached_property
from typing import Any

import anyio
from fastapi import HTTPException
from pydantic_ai import RunContext
from pydantic_ai.capabilities import WrapperCapability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool
from pydantic_ai.workspaces import WorkspaceError, WorkspaceReadOnlyError, WorkspaceUnavailableError
from pydantic_ai_harness.shell import Shell, ShellToolset
from sqlalchemy import update

from .contracts import RunScope
from .database import Database
from .effects import SHELL_READINESS_SUPPORTED, SHELL_RECOVERY_VERSION, effect_id
from .models import Effect, new_id, now

UNAVAILABLE = (
    "The command did not run: this conversation's computer is no longer available. "
    "Moyai verified this before command dispatch; no outcome review is needed. "
    "Do not retry commands on this computer. Continue with connected read tools where "
    "possible and explain any unavailable local work. Do not replace it with an empty "
    "computer or claim that unsaved files were recovered."
)
NOT_READY = (
    "The command did not run: the computer could not be reached after two readiness checks. "
    "No outcome review is needed. Use other available tools or explain the connection problem."
)
UNCERTAIN = (
    "This shell call already has an execution claim. Its outcome cannot be inferred "
    "from computer availability. Do not repeat it."
)
logger = logging.getLogger(__name__)


class RecoveringShell(WrapperCapability[RunScope]):
    def __init__(self, shell: Shell[RunScope], database: Database):
        super().__init__(shell)
        self.database = database

    @cached_property
    def registered_toolset(self) -> FunctionToolset[RunScope]:
        native = self.wrapped.get_toolset()
        if not isinstance(native, ShellToolset) or set(native.tools) != {"shell"}:
            raise TypeError("Review the pinned Coder shell contract before upgrading.")
        return ReadyShellToolset(native, self.database)

    def get_toolset(self) -> FunctionToolset[RunScope]:
        return self.registered_toolset


class ReadyShellToolset(FunctionToolset[RunScope]):
    def __init__(self, native: ShellToolset[RunScope], database: Database):
        # Keep the SDK's schemas, validation, activity ID and output limits.
        super().__init__(tools=list(native.tools.values()), id=native.id)
        self.native = native
        self.database = database

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        return {
            name: replace(tool, tool_def=replace(
                tool.tool_def, metadata={**(tool.tool_def.metadata or {}), SHELL_READINESS_SUPPORTED: True},
            ))
            for name, tool in (await self.native.get_tools(ctx)).items()
        }

    async def transition(
        self, ctx: RunContext[RunScope], identity: str, expected: dict[str, str],
        replacement: dict[str, str], *, settled: bool = False,
    ) -> bool:
        async with self.database.sessions.begin() as db:
            row = await db.get(Effect, identity)
            if (
                row is None or row.session_id != ctx.deps.session_id
                or row.turn_id != ctx.deps.turn_id or row.actor_id != ctx.deps.actor_id
                or row.tool_name != "shell" or row.state != "pending" or not row.resolution
                or json.loads(self.database.security.unseal(row.resolution)) != expected
            ):
                return False
            values: dict[str, Any] = {"resolution": self.database.security.seal(replacement)}
            if settled:
                values.update(state="not_dispatched", settled_at=now())
            changed = await db.scalar(update(Effect).where(
                Effect.id == identity, Effect.state == "pending", Effect.resolution == row.resolution,
            ).values(**values).returning(Effect.id))
            return changed is not None

    async def readiness(self, ctx: RunContext[RunScope]) -> None:
        for attempt in range(2):
            try:
                await self.database.authorize_scope(ctx.deps)
                async with asyncio.timeout(15):
                    await ctx.workspace.working_dir()
                return
            except WorkspaceUnavailableError as error:
                raise ToolFailed(UNAVAILABLE) from error
            except (WorkspaceReadOnlyError, PermissionError, HTTPException) as error:
                raise ToolFailed(
                    "The command did not run: computer access was refused before dispatch. "
                    "No outcome review is needed."
                ) from error
            except (WorkspaceError, TimeoutError, OSError) as error:
                if attempt:
                    raise ToolFailed(NOT_READY) from error

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope],
        tool: ToolsetTool[RunScope],
    ) -> Any:
        if not (tool.tool_def.metadata or {}).get(SHELL_RECOVERY_VERSION):
            return await self.native.call_tool(name, tool_args, ctx, tool)
        identity = effect_id(ctx, ToolCallPart(name, tool_args, tool_call_id=ctx.tool_call_id or ""))
        claim = {"shell_phase": "checking", "claim": new_id()}
        if not await self.transition(ctx, identity, {"shell_phase": "reserved"}, claim):
            raise ToolFailed(UNCERTAIN)
        try:
            await self.readiness(ctx)
            # Stop or steering during the read check cannot authorize dispatch.
            await self.database.authorize_scope(ctx.deps)
        except BaseException:
            # No user command was dispatched, including when the check was cancelled.
            # Retain the original failure if receipt persistence itself is unavailable.
            try:
                with anyio.CancelScope(shield=True):
                    await self.transition(
                        ctx, identity, claim, {"shell_phase": "not_dispatched"}, settled=True,
                    )
            except Exception:  # noqa: BLE001 - preserve the original failure and the pending receipt
                logger.warning("Could not persist the shell's pre-dispatch refusal; its receipt remains pending.")
            raise
        if not await self.transition(ctx, identity, claim, {"shell_phase": "dispatched"}):
            raise ToolFailed(UNCERTAIN)
        # Do not classify errors from this point as known-unsent. The SDK may
        # have started the command even if its response or worker is lost.
        return await self.native.call_tool(name, tool_args, ctx, tool)
