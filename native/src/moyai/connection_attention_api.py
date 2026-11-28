"""Owner-authorized attention selection; provider discovery precedes write locks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from .attention_policy import (
    ConnectionAttentionEdit,
    ConnectionAttentionTools,
    ConnectionAttentionUpdated,
    declared_reads,
)
from .connection_attention import (
    authorize_mutation,
    canonical_slack,
    configure,
    native_grant,
    set_slack_consent,
    slack_consent,
    slack_consent_revision,
    view,
)
from .connection_policy import ConnectionPolicy
from .contracts import Grant
from .models import Connection, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .runtime import Runtime


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()
    database = runtime.database

    async def current(request: Request, user: User) -> None:
        if runtime.product_identity is not None:
            principal = await runtime.product_identity.require_request_current(request)
            if not principal.individual:
                raise HTTPException(403, "Sign in with your individual work account.")
        fresh = await database.user(user.id)
        if not fresh.enabled or fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your account changed. Sign in again.")

    async def load(db: AsyncSession, user: User, identity: str, *, lock: bool = False) -> tuple[Grant, Connection | None]:
        query = select(Connection).where(Connection.id == identity).execution_options(populate_existing=True)
        row = await db.scalar(query.with_for_update() if lock else query)
        if row is not None:
            if row.owner_id != user.id or row.shared:
                raise HTTPException(403, "Attention requires your private accounts. Shared workspace accounts are excluded.")
            return native_grant(row), row
        grant = await canonical_slack(database, db, user.id, identity)
        if grant is None:
            raise HTTPException(403, "Select your current Personal Slack account; shared workspace accounts are excluded.")
        return grant, None

    async def discover(request: Request, user: User, grant: Grant) -> list[str]:
        from .connection_attention_discovery import discover_reads

        async def authorize() -> None:
            await current(request, user)
            async with database.sessions() as db:
                fresh, row = await load(db, user, grant.id)
                if fresh != grant or (row is not None and not row.enabled):
                    raise HTTPException(409, "The connection changed during discovery. Refresh and try again.")

        return await discover_reads(runtime, grant, authorize)

    @router.post("/api/connections/{connection_id}/attention/tools", operation_id="listConnectionAttentionTools")
    async def tools(
        connection_id: str, request: Request, user: User = Depends(actor),
    ) -> ConnectionAttentionTools:
        await current(request, user)
        if runtime.product_automations is None:
            raise HTTPException(409, "Background attention is unavailable on this installation.")
        async with database.sessions() as db:
            grant, row = await load(db, user, connection_id)
            if row is not None and not row.enabled:
                raise HTTPException(409, "Connect this account before selecting attention tools.")
        return ConnectionAttentionTools(revision=grant.revision, readable_tools=await discover(request, user, grant))

    @router.patch("/api/connections/{connection_id}/attention", operation_id="updateConnectionAttention")
    async def attention(
        connection_id: str, body: ConnectionAttentionEdit, request: Request, user: User = Depends(actor),
    ) -> ConnectionAttentionUpdated:
        await current(request, user)
        service = runtime.product_automations
        if body.use_proactively and service is None:
            raise HTTPException(409, "Background attention is unavailable on this installation.")
        async with database.sessions() as db:
            original, row = await load(db, user, connection_id)
            if original.revision != body.revision:
                raise HTTPException(409, "The connection changed. Refresh and try again.")
            if row is None and body.consent_revision != slack_consent_revision(await db.get(User, user.id)):
                raise HTTPException(409, "Personal Slack attention changed. Refresh and try again.")
            if body.use_proactively and row is not None and not row.enabled:
                raise HTTPException(409, "Connect this account before enabling attention.")
        selected: list[str] | None = None
        if body.use_proactively:
            available = await discover(request, user, original)
            policy = ConnectionPolicy.model_validate(original.policy)
            proposed = (
                body.readable_tools if body.readable_tools is not None else
                sorted(set(policy.attention_tools) & set(available)) if policy.attention_tools is not None else available
            )
            if not proposed or not set(proposed) <= set(available):
                raise HTTPException(422, "Choose explicitly declared read tools from this account's current catalog.")
            selected = sorted(set(proposed))
            await database.dots.open(user)
        sync_id = None
        async with database.sessions.begin() as db:
            await authorize_mutation(runtime, db, request, user)
            fresh, account = await load(db, user, connection_id, lock=True)
            if fresh != original:
                raise HTTPException(409, "The connection changed. Refresh and try again.")
            changed = {connection_id}
            if account is not None:
                policy = ConnectionPolicy.model_validate_json(account.policy)
                policy.use_proactively = body.use_proactively
                if selected is not None:
                    policy.attention_tools = selected
                account.policy = policy.model_dump_json()
                account.revision += 1
                await db.flush()
                fresh = native_grant(account)
            else:
                saved_user = await db.scalar(select(User).where(User.id == user.id).with_for_update())
                assert saved_user is not None
                if body.consent_revision != slack_consent_revision(saved_user):
                    raise HTTPException(409, "Personal Slack attention changed. Refresh and try again.")
                previous = slack_consent(saved_user)
                if previous is not None:
                    changed.add(previous)
                if selected is not None and set(selected) != set(declared_reads(
                    fresh.kind, ConnectionPolicy.model_validate(fresh.policy),
                ) or []):
                    raise HTTPException(422, "Personal Slack attention uses search, channel and thread reads.")
                set_slack_consent(saved_user, connection_id if body.use_proactively else None)
                await db.flush()
            attention_view = await view(
                service, database, db, user.id, fresh, shared=False,
                enabled=account.enabled if account is not None else True,
            )
            if body.use_proactively and not attention_view.eligible:
                raise HTTPException(409, f"Attention is unavailable: {attention_view.reason}.")
            if service is not None:
                sync_id = await configure(
                    service, db, request.state.product_identity_grant, changed=frozenset(changed),
                )
            attention_view = await view(
                service, database, db, user.id, fresh, shared=False,
                enabled=account.enabled if account is not None else True,
            )
        if sync_id is not None and service is not None:
            await service.request_sync(sync_id)
        return ConnectionAttentionUpdated(revision=fresh.revision, attention=attention_view)

    return router
