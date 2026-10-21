"""Durable human sign-in handoff for the existing shared browser."""
from __future__ import annotations

import json
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import Field, TypeAdapter
from pydantic_ai import CallDeferred, ConversationTypeAdapter, RunContext
from pydantic_ai.capabilities import Toolset, durable_operation
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy.ext.asyncio import AsyncSession

from .computer import DESKTOP_VERSION, ComputerCommand
from .contracts import RunScope
from .database import Database
from .models import ComputerDesktop, ComputerLease, ComputerOperation, Session, Turn, User, new_id

TOOL_NAME = "request_browser_login"
LoginOutcome = Literal["signed_in", "cancelled"]


class BrowserLogin(Toolset[RunScope]):
    def __init__(self, database: Database):
        self.database = database
        tools: FunctionToolset[RunScope] = FunctionToolset(id="moyai_browser_login_v1")

        @tools.tool(metadata={"temporal": False}, sequential=True)
        async def request_browser_login(
            ctx: RunContext[RunScope],
            url: Annotated[str, Field(min_length=1, max_length=2048)],
            reason: Annotated[str, Field(min_length=1, max_length=1000)],
        ) -> str:
            """Ask the requester to sign in through the shared Computer panel.

            First use browser_open and inspect the current sign-in page. Leave
            that tab visible. Pass the destination site's URL without tokens or
            login query parameters and explain why access is needed. Call this
            tool alone in its round. It opens Computer for the requester and
            pauses durably until they confirm or cancel; never collect passwords,
            tokens, or SSO codes in chat. After confirmation use browser_read to
            verify access before continuing. Confirmation is not proof of login.
            """
            try:
                target = urlsplit(ComputerCommand.safe_url(url))
            except ValueError as error:
                raise ToolFailed("Provide an HTTP(S) website address without embedded credentials.") from error
            if not reason.strip():
                raise ToolFailed("Explain why browser sign-in is needed.")
            reference = ctx.workspace.ref
            if reference is None or reference.provider != "modal":
                raise ToolFailed("Use browser_open to start the shared browser before requesting sign-in.")
            metadata = await self.prepare(
                ctx.deps, reference.id, f"{target.scheme}://{target.netloc}", reason.strip(),
            )
            raise CallDeferred(metadata=metadata)

        super().__init__(tools, id="moyai_browser_login")

    @durable_operation("prepare")
    async def prepare(self, scope: RunScope, workspace_id: str, site: str, reason: str) -> dict[str, str]:
        await self.database.authorize_scope(scope)
        async with self.database.sessions() as db:
            desktop = await db.get(ComputerDesktop, scope.session_id)
            if not desktop or desktop.workspace_id != workspace_id or desktop.version != DESKTOP_VERSION:
                raise ToolFailed("Use browser_open to start the shared browser before requesting sign-in.")
        return {"kind": "browser_login", "workspace_id": workspace_id, "site": site, "reason": reason}


async def login_result(
    db: AsyncSession, session_id: str, metadata: dict, outcome: LoginOutcome,
) -> dict[str, str]:
    """An explicit acknowledgement is never an authentication-success receipt."""
    if metadata.get("kind") != "browser_login":
        raise HTTPException(409, "This browser sign-in request is no longer available.")
    if outcome == "cancelled":
        return {
            "status": "cancelled",
            "instruction": "The requester cancelled sign-in. Do not assume access or request it again without new guidance.",
        }
    session = await db.get(Session, session_id, populate_existing=True)
    desktop = await db.get(ComputerDesktop, session_id, populate_existing=True)
    reference = TypeAdapter(WorkspaceRef).validate_python(json.loads(session.workspace)) if session and session.workspace else None
    if (
        not reference or reference.provider != "modal" or reference.id != metadata.get("workspace_id")
        or not desktop or desktop.workspace_id != reference.id or desktop.version != DESKTOP_VERSION
    ):
        raise HTTPException(409, "The computer changed. Cancel this request and ask Moyai to reopen the sign-in page.")
    return {
        "status": "user_reported_signed_in",
        "site": metadata["site"],
        "instruction": "Use browser_read to verify that the requested site is accessible before continuing. The user's confirmation alone does not prove authentication succeeded.",
    }


async def handoff_login_control(
    database: Database, db: AsyncSession, actor: User, previous: Turn, results: dict,
) -> None:
    """Return the requester's control inside the locked resume transaction.

    Derive handoffs from the saved request, not arbitrary submitted tool results.
    Replays return their existing turn before reaching this mutation.
    """
    if not previous.conversation:
        return
    conversation = ConversationTypeAdapter.validate_json(database.security.unseal(previous.conversation))
    pending = conversation.deferred_tool_requests
    if pending is None:
        return
    calls = results.get("calls", {})
    confirmed = [
        call for call in pending.calls
        if call.tool_name == TOOL_NAME and isinstance(calls.get(call.tool_call_id), dict)
        and calls[call.tool_call_id].get("status") == "user_reported_signed_in"
    ]
    if not confirmed:
        return
    for call in confirmed:
        # The computer can change between HTTP validation and admission.
        await login_result(db, previous.session_id, (pending.metadata or {}).get(call.tool_call_id) or {}, "signed_in")
    control = await db.get(ComputerLease, previous.session_id)
    stamp = time.time()
    if control and control.expires_at > stamp and not await database.same_person(db, actor, control.actor_id):
        raise HTTPException(409, "Another person is controlling this computer. They must release control before you continue.")
    operation = await db.get(ComputerOperation, previous.session_id)
    if operation and operation.expires_at > stamp:
        raise HTTPException(409, "A computer action is still settling. Wait a moment, then confirm sign-in again.")
    if control is not None:
        await db.delete(control)
    desktop = await db.get(ComputerDesktop, previous.session_id)
    assert desktop is not None  # Revalidated by login_result above.
    # Revoke every old controller socket, including another tab owned by this
    # requester. A late heartbeat cannot recreate a released lease.
    desktop.control_epoch = new_id()
    desktop.control_client = ""
