"""Fence possibly completed external writes without replacing the SDK's tool ledger."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, durable_operation
from pydantic_ai.capabilities.abstract import WrapToolExecuteHandler
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import ToolCallPart, ToolReturn
from pydantic_ai.tools import ToolDefinition
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from .browser_outcomes import BROWSER_OUTCOME, BROWSER_WRITES, BrowserNotDispatched, BrowserOutcomeUnknown
from .connection_policy import read_only_tool
from .contracts import RunScope
from .database import Database
from .effect_gate import EFFECT_GATE_VERSION, workflow_effect_lock
from .models import Effect, now
from .pr_publications import CreationReceipt, creation_receipt, persist_publication

logger = logging.getLogger(__name__)

COMPUTER_EFFECTS = {"shell", "run_command", "start_command", "credentials_run"}
SKILL_EFFECTS = {"skill_create", "skill_update", "skill_delete"}
AUTOMATION_EFFECTS = {
    "automation_create", "automation_update", "automation_migrate",
    "automation_enable", "automation_pause", "automation_retire",
    "automation_grant", "automation_self_update", "automation_claim_item",
    "automation_webhook_setup",
}
SCOPED_AUTOMATION_EFFECTS = {
    "automation_grant", "automation_self_update", "automation_claim_item", "automation_webhook_setup",
}
SERVICE_PREFIXES = ("github_", "linear_", "notion_", "slack_")
SHELL_RECOVERY_VERSION = "moyai-shell-readiness-v1"
SHELL_READINESS_SUPPORTED = "moyai_shell_readiness_supported"


class PendingEffectError(PermissionError):
    """A new call was refused before dispatch; the earlier receipt stays pending."""


def requires_receipt(tool: ToolDefinition) -> bool:
    if tool.name in SKILL_EFFECTS:
        return not workflow.in_workflow() or workflow.patched("moyai-skill-receipts-v1")
    if tool.name == "credentials_run":
        return not workflow.in_workflow() or workflow.patched("moyai-credential-command-receipts-v1")
    if tool.name in SCOPED_AUTOMATION_EFFECTS:
        return not workflow.in_workflow() or workflow.patched("moyai-scoped-automation-receipts-v1")
    if tool.name in BROWSER_WRITES:
        # Recorded browser calls predate these hook activities. New executions
        # record the marker; replay does not insert receipts into old history.
        return not workflow.in_workflow() or workflow.patched("moyai-browser-receipts-v1")
    return tool.name in COMPUTER_EFFECTS | AUTOMATION_EFFECTS or (
        tool.name.startswith(SERVICE_PREFIXES) and not read_only_tool(tool)
    )


def effect_id(ctx: RunContext[RunScope], call: ToolCallPart) -> str:
    return hashlib.sha256(f"{ctx.deps.turn_id}:{ctx.run_id}:{call.tool_call_id}".encode()).hexdigest()


def browser_phase(database: Database, row: Effect) -> str | None:
    if not row.resolution:
        return None
    value = json.loads(database.security.unseal(row.resolution))
    return value.get("browser_phase") if isinstance(value, dict) else None


async def claim_browser_dispatch(database: Database, scope: RunScope, identity: str, name: str) -> None:
    """Claim one guest attempt inside the tool activity, including redelivery.

    Workflow-side reservation alone cannot fence a repeated activity delivery.
    A committed dispatch marker is never reset automatically, even if its worker
    dies before it can send the command.
    """
    await database.authorize_scope(scope)
    async with database.sessions.begin() as db:
        row = await db.get(Effect, identity)
        if row is None:
            raise BrowserNotDispatched("This browser call has no durable reservation. It did not execute.")
        if (
            row.session_id != scope.session_id or row.turn_id != scope.turn_id
            or row.actor_id != scope.actor_id or row.tool_name != name or name not in BROWSER_WRITES
        ):
            raise BrowserNotDispatched("This browser call does not own its durable reservation.")
        if row.state != "pending" or browser_phase(database, row) != "reserved":
            raise BrowserOutcomeUnknown("This browser call was already dispatched or settled. It will not be repeated.")
        claimed = await db.scalar(update(Effect).where(
            Effect.id == identity, Effect.state == "pending", Effect.resolution == row.resolution,
        ).values(resolution=database.security.seal({"browser_phase": "dispatched"})).returning(Effect.id))
        if claimed is None:
            raise BrowserOutcomeUnknown("Another worker already claimed this browser call. It will not be repeated.")


async def confirm_effect(database: Database, identity: str) -> None:
    """Persist received evidence without making another provider call."""
    async with database.sessions.begin() as db:
        await db.execute(update(Effect).where(
            # A late authoritative receipt can still confirm the original
            # attempt after its turn failed; this never dispatches it again.
            Effect.id == identity, Effect.state.in_(("pending", "failed")),
        ).values(state="confirmed", settled_at=now()))


class EffectReceipts(AbstractCapability[RunScope]):
    id = "moyai_effects"

    def __init__(self, database: Database):
        self.database = database

    @durable_operation("reserve")
    async def reserve(
        self, scope: RunScope, identity: str, name: str, args: dict[str, Any],
        shell_readiness: bool = False,
    ) -> None:
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
                    raise PendingEffectError("An earlier action needs reconciliation before another write.")
                db.add(Effect(
                    id=identity, session_id=scope.session_id, turn_id=scope.turn_id,
                    actor_id=scope.actor_id, tool_name=name,
                    arguments=self.database.security.seal(serialized), fingerprint=fingerprint,
                    resolution=(
                        self.database.security.seal({"browser_phase": "reserved"})
                        if name in BROWSER_WRITES else
                        self.database.security.seal({"shell_phase": "reserved"})
                        if name == "shell" and shell_readiness else None
                    ),
                ))
        except IntegrityError as error:
            raise PendingEffectError("Another action is awaiting a confirmed outcome.") from error

    @durable_operation("confirm")
    async def confirm(self, identity: str) -> None:
        # A received result is factual evidence even if Stop/revocation arrived meanwhile.
        await confirm_effect(self.database, identity)

    @durable_operation("browser_state")
    async def browser_state(self, identity: str) -> str | None:
        """Read already-admitted evidence even after Stop or access revocation."""
        async with self.database.sessions() as db:
            row = await db.get(Effect, identity)
            return row.state if row is not None and row.tool_name in BROWSER_WRITES else None

    @durable_operation("not_dispatched")
    async def not_dispatched(self, identity: str) -> bool:
        """Settle a trusted refusal without clearing an earlier unknown attempt."""
        async with self.database.sessions.begin() as db:
            row = await db.get(Effect, identity)
            if row is None or row.tool_name not in BROWSER_WRITES:
                return False
            if row.state == "not_dispatched":
                return True
            if row.state != "pending" or browser_phase(self.database, row) != "reserved":
                return False
            matched = await db.scalar(update(Effect).where(
                Effect.id == identity, Effect.state == "pending", Effect.resolution == row.resolution,
            ).values(state="not_dispatched", settled_at=now()).returning(Effect.id))
            return matched is not None

    @durable_operation("record_pr_publication")
    async def record_pr_publication(self, scope: RunScope, identity: str, receipt: dict[str, Any]) -> None:
        await persist_publication(self.database, scope, identity, CreationReceipt(**receipt))

    async def prepare_tools(
        self, ctx: RunContext[RunScope], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        return [
            replace(
                tool, sequential=True,
                metadata={**(tool.metadata or {}), SHELL_RECOVERY_VERSION: True},
            ) if tool.name == "shell" and (tool.metadata or {}).get(SHELL_READINESS_SUPPORTED) and (
                not workflow.in_workflow() or workflow.patched(SHELL_RECOVERY_VERSION)
            ) else replace(tool, sequential=True) if requires_receipt(tool) else tool
            for tool in tool_defs
        ]

    async def wrap_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], handler: WrapToolExecuteHandler,
    ) -> Any:
        if (
            not workflow.in_workflow()
            or not requires_receipt(tool_def)
            or not workflow.patched(EFFECT_GATE_VERSION)
        ):
            return await handler(args)
        # The SDK handler includes reservation, dispatch and confirmation. Only
        # guarded effects wait here: models, reads and delegate_task stay parallel.
        # Cancelling a waiter creates no receipt. Cancelling an in-flight effect
        # leaves its existing uncertainty fence intact, never authorizing replay.
        async with workflow_effect_lock(ctx.deps.session_id):
            return await handler(args)

    async def before_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any],
    ) -> dict[str, Any]:
        if requires_receipt(tool_def):
            # A deferred native tool has not dispatched yet. ApprovalRequired is
            # enforced by the SDK before this hook once the tool is marked below.
            identity = effect_id(ctx, call)
            try:
                if tool_def.name == "shell" and (tool_def.metadata or {}).get(SHELL_RECOVERY_VERSION):
                    await self.reserve(ctx.deps, identity, call.tool_name, args, shell_readiness=True)
                else:
                    await self.reserve(ctx.deps, identity, call.tool_name, args)
            except Exception as error:
                cause = error.cause if isinstance(error, ActivityError) else error
                if isinstance(cause, PendingEffectError) or (
                    isinstance(cause, ApplicationError) and cause.type == "PendingEffectError"
                ):
                    raise ToolFailed(
                        "This action did not run. An earlier action has an unconfirmed outcome. "
                        "Use available read tools to inspect it, then report the failure and end this turn. "
                        "Do not repeat the earlier action. The user can send another message to continue."
                    ) from error
                if tool_def.name in BROWSER_WRITES and await self.not_dispatched(identity):
                    raise ToolFailed("Browser reservation failed before dispatch. The action did not execute.") from error
                raise
        return args

    async def after_tool_execute(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], result: Any,
    ) -> Any:
        if tool_def.name in BROWSER_WRITES:
            surface = "desktop" if tool_def.name == "desktop_action" else "browser"
            outcome = (result.metadata or {}).get(BROWSER_OUTCOME) if isinstance(result, ToolReturn) else None
            if outcome in {"not_dispatched", "unknown"}:
                guarded = requires_receipt(tool_def)
                identity = effect_id(ctx, call)
                if not guarded and outcome == "not_dispatched":
                    raise ToolFailed(str(result.return_value))
                if guarded and outcome == "not_dispatched" and await self.not_dispatched(identity):
                    raise ToolFailed(str(result.return_value))
                state = await self.browser_state(identity) if guarded else None
                if state == "confirmed":
                    raise ToolFailed(f"The {surface} action completed, but its result could not be delivered. Do not repeat it.")
                if state == "not_dispatched":
                    raise ToolFailed(f"This {surface} call was already refused before dispatch. It was not repeated.")
                raise ToolFailed(
                    f"The {surface} outcome is uncertain. Use {surface}_read to inspect it and report "
                    "the failed attempt. Do not repeat the action automatically or ask the user to verify its outcome."
                )
            if requires_receipt(tool_def) and outcome != "completed":
                # A missing/malformed result marker cannot attest success.
                raise ToolFailed(
                    f"The {surface} result has no confirmed outcome. Inspect it and report the failed attempt. "
                    "Do not repeat the action automatically."
                )
        if requires_receipt(tool_def):
            await self.confirm(effect_id(ctx, call))
            is_creation = call.tool_name.startswith("github_") and call.tool_name.endswith(
                ("_create_pull_request", "_open_pull_request")
            )
            if is_creation and (not workflow.in_workflow() or workflow.patched("moyai-pr-publications-v1")):
                try:
                    receipt = creation_receipt(
                        ctx.deps, call.tool_name, args, result,
                        workflow.now().isoformat() if workflow.in_workflow() else now(),
                    )
                    if receipt:
                        await self.record_pr_publication(ctx.deps, effect_id(ctx, call), asdict(receipt))
                except Exception:  # noqa: BLE001
                    # This projection cannot turn a confirmed write into a
                    # failed tool or invite repetition of the GitHub write.
                    logger.warning("Confirmed PR analytics receipt was not saved; coverage is incomplete.")
        return result

    async def on_tool_execute_error(
        self, ctx: RunContext[RunScope], *, call: ToolCallPart,
        tool_def: ToolDefinition, args: dict[str, Any], error: Exception,
    ) -> Any:
        cause = error.cause if isinstance(error, ActivityError) else None
        if tool_def.name in BROWSER_WRITES and requires_receipt(tool_def):
            surface = "desktop" if tool_def.name == "desktop_action" else "browser"
            identity = effect_id(ctx, call)
            if isinstance(cause, ApplicationError) and cause.type in {
                "MoyaiGuidanceChanged", "MoyaiPermissionDenied", "MoyaiMissingExecutionScope",
            } and await self.not_dispatched(identity):
                raise ToolFailed("Execution authority changed before browser dispatch. The action did not execute.") from error
            state = await self.browser_state(identity)
            if state == "confirmed":
                raise ToolFailed(f"The {surface} action completed, but its result could not be delivered. Do not repeat it.") from error
            if state == "not_dispatched":
                raise ToolFailed(f"This {surface} call was already refused before dispatch. It was not repeated.") from error
            raise ToolFailed(
                f"The {surface} outcome is uncertain. Inspect with {surface}_read and report the failed attempt. "
                "Do not repeat the action automatically or ask the user to verify its outcome."
            ) from error
        if (
            requires_receipt(tool_def) and isinstance(cause, ApplicationError)
            and cause.type in {"MoyaiGuidanceChanged", "MoyaiPermissionDenied", "MoyaiMissingExecutionScope"}
        ):
            # Our inbound interceptor refused before calling the tool body. This
            # outcome is known, so it must not leave a false uncertainty barrier.
            await self.confirm(effect_id(ctx, call))
        raise error
