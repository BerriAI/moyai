"""Durable human sign-in handoff for the existing shared browser."""
from __future__ import annotations

import json
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import Field, TypeAdapter
from pydantic_ai import CallDeferred, RunContext
from pydantic_ai.capabilities import Toolset, durable_operation
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.workspaces import WorkspaceRef
from sqlalchemy.ext.asyncio import AsyncSession

from .computer import DESKTOP_VERSION, ComputerCommand
from .contracts import RunScope
from .database import Database
from .models import ComputerDesktop, ComputerLease, Session

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
    session = await db.get(Session, session_id)
    desktop = await db.get(ComputerDesktop, session_id)
    reference = TypeAdapter(WorkspaceRef).validate_python(json.loads(session.workspace)) if session and session.workspace else None
    if (
        not reference or reference.provider != "modal" or reference.id != metadata.get("workspace_id")
        or not desktop or desktop.workspace_id != reference.id or desktop.version != DESKTOP_VERSION
    ):
        raise HTTPException(409, "The computer changed. Cancel this request and ask Moyai to reopen the sign-in page.")
    control = await db.get(ComputerLease, session_id)
    if control and control.expires_at > time.time():
        raise HTTPException(409, "Release control in Computer, then confirm that you have signed in.")
    return {
        "status": "user_reported_signed_in",
        "site": metadata["site"],
        "instruction": "Use browser_read to verify that the requested site is accessible before continuing. The user's confirmation alone does not prove authentication succeeded.",
    }
