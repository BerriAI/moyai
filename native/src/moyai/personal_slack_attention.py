"""Join canonical personal Slack OAuth to the existing attention lifecycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.connection_attention_models import ConnectionAttentionView
from app.personal_slack import PersonalSlackAttentionChoice, PersonalSlackStatus
from fastapi import HTTPException, Request
from sqlalchemy import select

from .connection_attention import (
    configure,
    provider_changed,
    provider_enabled,
    set_slack_consent,
    slack_consent,
    slack_consent_revision,
    view,
)
from .contracts import Grant
from .models import User
from .product_connection_store import ConnectionSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .runtime import Runtime


async def personal_grant(runtime: Runtime, db: AsyncSession, owner: str) -> Grant | None:
    try:
        grants = await runtime.database.resolve_grants(db, owner, ["slack"], surface="automation")
    except HTTPException as error:
        if error.status_code != 403:
            raise
        return None
    return next((
        grant for grant in grants if grant.kind == "slack" and grant.read_only
        and (grant.policy.get("product") or {}).get("personal_owner") == owner
        and (grant.policy.get("product") or {}).get("access") == "personal_reads"
    ), None)


async def require_revision(runtime: Runtime, db: AsyncSession, owner: str, revision: str) -> None:
    assert runtime.product_identity is not None
    schema = runtime.product_identity.schema

    def read(session):
        return ConnectionSnapshot(session, schema).rows(
            "SELECT * FROM personal_slack_grants WHERE owner_id=?", (owner,),
        )

    rows = await db.run_sync(read)
    if not rows or rows[0]["revision"] != revision:
        raise HTTPException(409, "Personal Slack changed. Start again from Settings.")


async def choice(
    runtime: Runtime, request: Request, owner: str, revision: str, selected: bool | None,
    consent_revision: int | None = None,
) -> PersonalSlackAttentionChoice:
    identity = runtime.product_identity
    if identity is None:
        raise HTTPException(409, "Personal Slack requires product sign-in.")
    async with runtime.database.sessions.begin() as db:
        if runtime.product_automations is not None:
            await runtime.product_automations._writer(db)
        principal = await identity.authorize_admission(db, request.state.product_identity_grant)
        if principal.id != owner or not principal.individual:
            raise HTTPException(403, "Sign in with your individual work account.")
        await require_revision(runtime, db, owner, revision)
        await identity.project_native_user(db, principal)
        user = await db.scalar(select(User).where(User.id == owner).with_for_update().execution_options(
            populate_existing=True,
        ))
        current_revision = slack_consent_revision(user)
        if (selected is not None or consent_revision is not None) and consent_revision != current_revision:
            raise HTTPException(409, "Personal Slack attention changed. Refresh and try again.")
        if selected is None:
            grant = await personal_grant(runtime, db, owner)
            selected = grant is not None and slack_consent(user) == grant.id
        if selected:
            if runtime.product_automations is None:
                raise HTTPException(409, "Background attention is unavailable on this installation.")
            if not await provider_enabled(runtime.database, db, "slack"):
                raise HTTPException(409, "This provider is disabled by the workspace.")
        return PersonalSlackAttentionChoice(selected, current_revision)


async def status(runtime: Runtime, owner: str, result: PersonalSlackStatus) -> PersonalSlackStatus:
    async with runtime.database.sessions() as db:
        grant = await personal_grant(runtime, db, owner)
        if grant is not None:
            result.connection_id, result.revision = grant.id, grant.revision
            attention = await view(
                runtime.product_automations, runtime.database, db, owner, grant,
                shared=False, enabled=result.connected,
            )
            result.attention = attention
            result.use_proactively = attention.use_proactively
        else:
            result.attention = ConnectionAttentionView(
                eligible=False,
                reason=(
                    "automation_unavailable" if runtime.product_automations is None else
                    "provider_disabled" if not await provider_enabled(runtime.database, db, "slack") else "disconnected"
                ),
                use_proactively=False, status="disabled", readable_tools=[],
                consent_revision=slack_consent_revision(await db.get(User, owner)),
            )
    return result


async def changed(
    runtime: Runtime, owner: str, *, request: Request | None = None,
    revision: str | None = None, use_proactively: bool | None = None, consent_revision: int | None = None,
) -> None:
    if request is None:
        await provider_changed(runtime, "slack", owner=owner)
        return
    identity, service = runtime.product_identity, runtime.product_automations
    if identity is None or revision is None or use_proactively is None or consent_revision is None:
        raise HTTPException(409, "The personal Slack consent receipt is incomplete.")
    if use_proactively and service is None:
        raise HTTPException(409, "Background attention is unavailable on this installation.")
    principal = await identity.require_request_current(request)
    if principal.id != owner or not principal.individual:
        raise HTTPException(403, "Sign in with your individual work account.")
    async with runtime.database.sessions.begin() as db:
        actor = await identity.project_native_user(db, principal)
    if use_proactively:
        await runtime.database.dots.open(actor)
    sync_id = None
    async with runtime.database.sessions.begin() as db:
        from .connection_attention import authorize_mutation

        await authorize_mutation(runtime, db, request, actor)
        # A different callback/revocation must never receive this OAuth choice.
        await require_revision(runtime, db, owner, revision)
        grant = await personal_grant(runtime, db, owner)
        if use_proactively and grant is None:
            raise HTTPException(409, "The personal Slack account is unavailable.")
        user = await db.get(User, owner)
        assert user is not None
        if consent_revision != slack_consent_revision(user):
            raise HTTPException(409, "Personal Slack attention changed. Refresh and try again.")
        previous = slack_consent(user)
        selected = grant.id if use_proactively and grant is not None else None
        set_slack_consent(user, selected)
        await db.flush()
        if service is not None:
            sync_id = await configure(
                service, db, principal.grant,
                changed=frozenset(value for value in (previous, selected) if value is not None),
            )
    if sync_id is not None and service is not None:
        await service.request_sync(sync_id)
