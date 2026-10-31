"""Keep an admitted turn's existing computer alive until its save settles.

Temporal owns the timer. Each tick is a short, independently authorized activity;
it never allocates a computer, restores a snapshot, or repeats an agent action.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

import modal
from fastapi import HTTPException
from pydantic_ai.workspaces import WorkspaceUnavailableError
from temporalio import activity

from .contracts import RunScope
from .workspace_recovery import locked_scope

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
                return Keepalive(True, delay)
            if session.workspace_scope != scope.workspace_key or reference.get("provider") not in {"modal", "lambda"}:
                return Keepalive(False)
    except (PermissionError, HTTPException):
        return Keepalive(False)

    try:
        async with asyncio.timeout(15):
            if reference["provider"] == "lambda":
                from .lambda_control import LambdaControl

                vm = await LambdaControl(runtime.settings).get(reference["id"])
                if not await vm.alive():
                    return Keepalive(False)
                # Only endpoint traffic resets AWS's idle timer. This read-only
                # probe also allows native auto-resume; it never runs user code.
                await vm.request("/health", {})
                return Keepalive(True, delay)
            sandbox = await modal.Sandbox.from_id.aio(reference["id"])
            if await sandbox.poll.aio() is not None:
                return Keepalive(False)
            # Polling alone does not keep Modal active. A fixed, read-only command
            # refreshes the idle clock without touching the files being saved.
            process = await sandbox.exec.aio("/bin/true", timeout=10)
            await process.wait.aio()
    except (modal.exception.NotFoundError, modal.exception.SandboxTerminatedError,
            modal.exception.SandboxTimeoutError, WorkspaceUnavailableError):
        return Keepalive(False)
    except Exception:
        # A transient maintenance failure is not evidence of computer loss.
        # The next durable tick can retry; native execution owns task failures.
        logger.warning("Could not refresh the active computer's idle timeout.", exc_info=True)
    return Keepalive(True, delay)
