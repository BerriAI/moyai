"""Keep an admitted turn's existing computer alive until its save settles.

Temporal owns the timer. Each tick is a short, independently authorized activity;
it never allocates a computer, restores a snapshot, or repeats an agent action.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import modal
from fastapi import HTTPException
from opentelemetry.trace import Span, get_current_span
from pydantic_ai.workspaces import WorkspaceUnavailableError
from temporalio import activity

from .contracts import RunScope
from .lifecycle_tracing import failure, scope_attributes
from .workspace_recovery import locked_scope

if TYPE_CHECKING:
    from .runtime import Runtime

VERSION = "moyai-active-computer-keepalive-v1"
logger = logging.getLogger(__name__)


@dataclass
class Keepalive:
    active: bool
    delay: int = 20


@activity.defn(name="moyai_keep_computer_alive_v1")
async def keep_computer_alive(scope: RunScope) -> Keepalive:
    from .runtime import current

    runtime = current()
    span = get_current_span()
    span.set_attributes({
        **scope_attributes(scope), "moyai.computer.idle_timeout_seconds": runtime.settings.computer_idle_timeout,
    })
    state = await _keep_computer_alive(runtime, scope, span)
    span.set_attribute("moyai.computer.keepalive_active", state.active)
    return state


async def _keep_computer_alive(runtime: Runtime, scope: RunScope, span: Span) -> Keepalive:
    database = runtime.database
    delay = min(60, max(1, runtime.settings.computer_idle_timeout // 3))
    try:
        async with database.sessions.begin() as db:
            # Answer acceptance can change running -> saving while this tick
            # waits for a lock. Both are active phases of the same admitted turn.
            session, turn = await locked_scope(database, db, scope, status=("running", "saving"))
            await database.swarms.authorize(db, turn, scope=scope)
            reference = json.loads(session.workspace or "null")
            if reference is None:
                # On-demand acquisition may happen later in this same turn.
                span.set_attribute("moyai.keepalive.result", "no_computer")
                return Keepalive(True, delay)
            if session.workspace_scope != scope.workspace_key or reference.get("provider") not in {"modal", "lambda"}:
                span.set_attribute("moyai.keepalive.result", "scope_changed")
                return Keepalive(False)
            span.set_attribute("pydantic_ai.workspace.id", reference["id"])
            span.set_attribute("pydantic_ai.workspace.provider", reference["provider"])
    except (PermissionError, HTTPException):
        span.set_attribute("moyai.keepalive.result", "task_inactive_or_unauthorized")
        return Keepalive(False)

    try:
        async with asyncio.timeout(15):
            if reference["provider"] == "lambda":
                from .lambda_control import LambdaControl

                vm = await LambdaControl(runtime.settings).get(reference["id"])
                if not await vm.alive():
                    span.set_attribute("moyai.keepalive.result", "computer_unavailable")
                    return Keepalive(False)
                # Only endpoint traffic resets AWS's idle timer. This read-only
                # probe also allows native auto-resume; it never runs user code.
                await vm.request("/health", {})
                span.set_attribute("moyai.keepalive.result", "refreshed")
                return Keepalive(True, delay)
            sandbox = await modal.Sandbox.from_id.aio(reference["id"])
            if await sandbox.poll.aio() is not None:
                span.set_attribute("moyai.keepalive.result", "computer_unavailable")
                return Keepalive(False)
            # Polling alone does not keep Modal active. A fixed, read-only command
            # refreshes the idle clock without touching the files being saved.
            process = await sandbox.exec.aio("/bin/true", timeout=10)
            await process.wait.aio()
            span.set_attribute("moyai.keepalive.result", "refreshed")
    except (modal.exception.NotFoundError, modal.exception.SandboxTerminatedError,
            modal.exception.SandboxTimeoutError, WorkspaceUnavailableError) as error:
        failure(span, error)
        span.set_attribute("moyai.keepalive.result", "computer_unavailable")
        return Keepalive(False)
    except Exception as error:
        # A transient maintenance failure is not evidence of computer loss.
        # The next durable tick can retry; native execution owns task failures.
        logger.warning("Could not refresh the active computer's idle timeout.", exc_info=True)
        failure(span, error)
        span.set_attribute("moyai.keepalive.result", "retry_later")
    return Keepalive(True, delay)
