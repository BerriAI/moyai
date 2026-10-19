"""Versioned lazy startup around the pinned SDK's native workspace lifecycle."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, Awaitable, Callable
from typing import Any, cast

import anyio
from pydantic import TypeAdapter
from pydantic_ai import RunContext
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, WrapperCapability
from pydantic_ai.capabilities.abstract import WrapRunHandler
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import AgentStreamEvent, ToolCallPart
from pydantic_ai.run import AgentRunResult
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.workspaces import WorkspaceRef
from pydantic_ai_harness import Coder, RepoContext, Shell
from temporalio import workflow

from .contracts import RunScope
from .database import Database
from .delegation import delegation_mode
from .effects import EffectReceipts
from .live_events import PUBLIC_STREAM_VERSION, public_workflow_event, publish_workflow_event
from .shell_recovery import RecoveringShell

STARTUP_VERSION = "moyai-on-demand-computer-v1"
COMPUTER_TOOLS = frozenset({
    "shell", "read_file", "write_file", "edit_file", "list_files", "grep",
    "save_artifact", "attachment_copy", "browser_open", "browser_read", "browser_action",
    "desktop_read", "desktop_action", "credentials_run", "delegate_task", "github_checkout",
})
ProjectInstructions = Callable[[RunContext[RunScope]], Awaitable[str]]
PublishWorkspace = Callable[[RunScope, dict[str, Any]], Awaitable[None]]


def on_demand() -> bool:
    return not workflow.in_workflow() or workflow.patched(STARTUP_VERSION)


class OnDemandAcquisition(WrapperCapability[RunScope]):
    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        if not on_demand():
            return await self.wrapped.for_run(ctx)
        return await super().for_run(ctx)

    async def wrap_run(
        self, ctx: RunContext[RunScope], *, handler: WrapRunHandler,
    ) -> AgentRunResult[Any]:
        if ctx.workspace.ref is None:
            return await handler()
        return await self.wrapped.wrap_run(ctx, handler=handler)


class OnDemandDurability(TemporalDurability[RunScope]):
    async def wrap_run(
        self, ctx: RunContext[RunScope], *, handler: WrapRunHandler,
    ) -> AgentRunResult[Any]:
        if workflow.in_workflow():
            ctx.deps.workflow_public_events = workflow.patched(PUBLIC_STREAM_VERSION)
        return await super().wrap_run(ctx, handler=handler)

    async def wrap_run_event_stream(
        self, ctx: RunContext[RunScope], *, stream: AsyncIterable[AgentStreamEvent],
    ) -> AsyncIterable[AgentStreamEvent]:
        if not workflow.in_workflow() or not ctx.deps.workflow_public_events:
            async for event in super().wrap_run_event_stream(ctx, stream=stream):
                yield event
            return
        try:
            async for event in stream:
                if ctx.run_id == ctx.deps.root_run_id and public_workflow_event(event):
                    publish_workflow_event(event)
                # This only changes the public projection. Every original event
                # still reaches the SDK; persistence and effect receipts remain.
                yield event
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    def for_agent(self, agent: AbstractAgent[RunScope, Any]) -> AbstractCapability[RunScope]:
        bound = super().for_agent(agent)
        matches: list[AbstractCapability[RunScope]] = []

        def replace(capability: AbstractCapability[RunScope]) -> AbstractCapability[RunScope]:
            # 2.55.0 has no eager-acquisition option. Rewrite only its pinned
            # companion through the public capability visitor; never change
            # DurableWorkspace or its journaled first-operation acquisition.
            if (
                type(capability).__module__ == "pydantic_ai.durable_exec._workspace"
                and type(capability).__name__ == "WorkspaceEnsurer"
            ):
                matches.append(capability)
                return OnDemandAcquisition(capability)
            return capability

        result = bound.visit_and_replace(replace)
        if result is None or len(matches) != 1:
            raise TypeError("Review the pinned SDK workspace startup contract before upgrading.")
        return result


class OnDemandProjectInstructions(WrapperCapability[RunScope]):
    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        if not on_demand():
            return await self.wrapped.for_run(ctx)
        return await super().for_run(ctx)

    def get_instructions(self) -> list[str | ProjectInstructions]:
        instructions = self.wrapped.get_instructions()
        if (
            not isinstance(instructions, list) or len(instructions) != 2
            or not isinstance(instructions[0], str) or not callable(instructions[1])
        ):
            raise TypeError("Review the pinned Coder project-instruction contract before upgrading.")
        project = cast(ProjectInstructions, instructions[1])

        async def when_available(ctx: RunContext[RunScope]) -> str:
            return await project(ctx) if ctx.workspace.ref is not None else ""

        return [instructions[0], when_available]


class OnDemandRepository(WrapperCapability[RunScope]):
    def __init__(
        self, wrapped: RepoContext[RunScope], publish: PublishWorkspace, *, deferred: bool = False,
    ) -> None:
        super().__init__(wrapped)
        self.publish = publish
        self.deferred = deferred
        self.ready = False
        self.instructions = ""
        self.context_step: int | None = None
        self.lock = anyio.Lock()

    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        prepared = await self.wrapped.for_run(ctx)
        if not on_demand() or ctx.workspace.ref is not None:
            return prepared
        if not isinstance(prepared, RepoContext):
            raise TypeError("The pinned RepoContext no longer returns its native run capability.")
        return OnDemandRepository(prepared, self.publish, deferred=True)

    def get_ordering(self) -> CapabilityOrdering | None:
        # Preparation may return repository instructions without executing the
        # requested action. Do that before creating an external-effect receipt.
        return CapabilityOrdering(wraps=[EffectReceipts]) if self.deferred else self.wrapped.get_ordering()

    async def before_run(self, ctx: RunContext[RunScope]) -> None:
        if not self.deferred:
            await self.wrapped.before_run(ctx)

    async def before_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_def.name == "delegate_task" and delegation_mode(ctx.deps) != "legacy":
            return args  # Only legacy SDK delegates share the parent's computer.
        if tool_def.name not in COMPUTER_TOOLS and not re.fullmatch(r"github_[A-Za-z0-9]{8}_checkout", tool_def.name):
            return args
        async with self.lock:
            if not self.ready:
                await ctx.workspace.working_dir()
                reference = ctx.workspace.ref
                if reference is None:
                    raise RuntimeError("Computer acquisition returned no durable workspace reference.")
                await self.publish(ctx.deps, TypeAdapter(WorkspaceRef).dump_python(reference, mode="json"))
                if not isinstance(self.wrapped, RepoContext):
                    raise TypeError("The native repository context capability changed.")
                await self.wrapped.before_run(ctx)
                instructions = self.wrapped.get_instructions()
                if callable(instructions):
                    value = instructions(ctx)
                    if not isinstance(value, str) and value is not None:
                        raise TypeError("The pinned repository instructions must resolve synchronously.")
                    self.instructions = value or ""
                elif isinstance(instructions, str):
                    self.instructions = instructions
                self.context_step = ctx.run_step if self.instructions else None
                self.ready = True
        if self.context_step is not None and ctx.run_step <= self.context_step:
            raise ToolFailed(
                "The computer is ready. Review its repository instructions before choosing the next action. "
                "The requested tool did not execute.\n\n" + self.instructions
            )
        return args


def on_demand_coder(publish: PublishWorkspace, database: Database) -> AbstractCapability[RunScope]:
    def replace(capability: AbstractCapability[RunScope]) -> AbstractCapability[RunScope]:
        if isinstance(capability, RepoContext):
            return OnDemandRepository(capability, publish)
        if capability.id == "coder_instructions":
            return OnDemandProjectInstructions(capability)
        if isinstance(capability, Shell):
            return RecoveringShell(capability, database)
        return capability

    result = Coder[RunScope]().visit_and_replace(replace)
    if result is None:
        raise TypeError("The native Coder capability is unavailable.")
    return result
