"""Personal connection consent joins the existing canonical attention lifecycle."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from app.automation_definitions import Definition, Timing, Trigger
from fastapi import HTTPException, Request
from sqlalchemy import select

from .attention_policy import AttentionReason, ConnectionAttentionView, attention_reads, declared_reads
from .automations.product_automation_models import ProductAutomationBinding as Binding
from .connection_policy import ConnectionPolicy
from .contracts import Grant
from .dot_models import PersonalDot
from .models import Connection, User, now
from .oauth_models import OAuthCredential
from .preferences.general_preferences import preference_document
from .product_config import CORE_IDENTITY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .automations.product_automations import ProductAutomationService
    from .database import Database
    from .product_identity import ProductGrant
    from .runtime import Runtime

SLACK_PREFERENCE = "personal_slack_attention"
SLACK_CONSENT_REVISION = "personal_slack_attention_revision"


async def authorize_mutation(runtime: Runtime, db: AsyncSession, request: Request, user: User) -> None:
    """Recheck the original request authority on the transaction that will commit."""
    if runtime.product_automations is not None:
        await runtime.product_automations._writer(db)
    if runtime.product_identity is not None:
        principal = await runtime.product_identity.authorize_admission(
            db, getattr(request.state, "product_identity_grant", None),
        )
        if principal.id != user.id:
            raise HTTPException(401, "The signed-in account changed.")
    fresh = await db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(
        populate_existing=True,
    ))
    if fresh is None or not fresh.enabled or fresh.auth_version != user.auth_version:
        raise HTTPException(401, "Your account changed. Sign in again.")


def native_grant(row: Connection) -> Grant:
    return Grant(
        id=row.id, kind=row.kind, revision=row.revision, credential=row.credential,
        read_only=row.read_only, policy=json.loads(row.policy),
    )


def slack_consent(user: User | None) -> str | None:
    value = preference_document(user.preferences).get(SLACK_PREFERENCE) if user is not None else None
    return value if isinstance(value, str) else None


def slack_consent_revision(user: User | None) -> int:
    value = preference_document(user.preferences).get(SLACK_CONSENT_REVISION) if user is not None else None
    return value if type(value) is int and value >= 0 else 0


def set_slack_consent(user: User, identity: str | None) -> None:
    document = preference_document(user.preferences)
    document[SLACK_PREFERENCE] = identity
    document[SLACK_CONSENT_REVISION] = slack_consent_revision(user) + 1
    user.preferences = json.dumps(document)


async def provider_enabled(database: Database, db: AsyncSession, kind: str) -> bool:
    product = database.product_connections
    if product is None or database.product_identity is None:
        return True
    from .product_connection_store import ConnectionSnapshot

    def read(session):
        store = ConnectionSnapshot(session, product.product.identity.schema)
        rows = store.rows(
            "SELECT enabled,read_only,checked_at,check_status FROM connection_policies WHERE provider=?", (kind,),
        )
        return not rows or bool(rows[0]["enabled"])

    return await db.run_sync(read)


async def status(
    service: ProductAutomationService | None, db: AsyncSession, owner: str, connection: str,
) -> Literal["unavailable", "paused", "pending", "active"]:
    if service is None:
        return "unavailable"
    for record in await service.attention_records(db, owner):
        if connection not in record.definition.connection_ids:
            continue
        binding = await db.get(Binding, record.id)
        if binding is not None and binding.state == "active":
            return "active" if binding.sync_state == "ready" else "pending"
    return "paused"


async def view(
    service: ProductAutomationService | None, database: Database, db: AsyncSession,
    owner: str, grant: Grant, *, shared: bool, enabled: bool,
) -> ConnectionAttentionView:
    policy = ConnectionPolicy.model_validate(grant.policy)
    reference = policy.product
    personal_slack = reference is not None and grant.kind == "slack" and reference.get("personal_owner") == owner
    owner_row = await db.get(User, owner) if personal_slack else None
    opted = slack_consent(owner_row) == grant.id if personal_slack else policy.use_proactively
    declared = declared_reads(grant.kind, policy)
    # Before first opt-in, show the fixed provider catalog. Afterwards report
    # the saved scope, including an empty reduction, without suggesting it grew.
    reads = sorted(
        name for name in (policy.attention_tools if policy.attention_tools is not None else declared or [])
        if policy.allows(name) and (declared is None or name in declared)
    )
    reason: AttentionReason | None = None
    if shared or (reference is not None and not personal_slack):
        reason = "shared_account"
    elif service is None:
        reason = "automation_unavailable"
    elif not enabled:
        reason = "disconnected"
    elif declared is None and grant.kind not in {"mcp", "litellm"}:
        reason = "unsupported_provider"
    elif grant.kind == "litellm" and (
        policy.gateway is None or not policy.gateway.mcp
        or policy.gateway_discovery is None or policy.gateway_discovery.mcp.status != "ready"
    ):
        reason = "read_tools_required"
        reads = []
    elif not reads:
        reason = "read_tools_required"
    elif policy.oauth and not await provider_enabled(database, db, grant.kind):
        reason = "provider_disabled"
    state = (
        "disabled" if not opted else
        "unavailable" if reason is not None else await status(service, db, owner, grant.id)
    )
    return ConnectionAttentionView(
        eligible=reason is None, reason=reason, use_proactively=opted,
        status=state, readable_tools=reads,
        consent_revision=slack_consent_revision(owner_row) if personal_slack else None,
    )


async def canonical_slack(database: Database, db: AsyncSession, owner: str, identity: str) -> Grant | None:
    if database.product_connections is None:
        return None
    try:
        grants = await database.resolve_grants(db, owner, [identity], surface="automation")
    except HTTPException as error:
        if error.status_code != 403:
            raise
        return None
    return next((
        grant for grant in grants if grant.kind == "slack"
        and (grant.policy.get("product") or {}).get("personal_owner") == owner
        and (grant.policy.get("product") or {}).get("access") == "personal_reads"
        and grant.read_only
    ), None)


async def configure(
    service: ProductAutomationService, db: AsyncSession, grant: ProductGrant,
    *, changed: frozenset[str] = frozenset(),
) -> str | None:
    """Persist consent, scope and sync intent under the caller's writer transaction."""
    await service._writer(db)
    accounts = tuple(await db.scalars(select(Connection).where(
        Connection.owner_id == grant.user_id,
    ).execution_options(populate_existing=True)))
    managed, selected = set(changed), set()
    for row in accounts:
        policy = ConnectionPolicy.model_validate_json(row.policy)
        if (
            policy.use_proactively or policy.attention_tools is not None
            or row.kind in {"gmail", "google_calendar"}
        ):
            managed.add(row.id)
        if (
            policy.use_proactively and row.enabled and not row.shared
            and attention_reads(native_grant(row))
            and (not policy.oauth or await provider_enabled(service.database, db, row.kind))
        ):
            credential = await db.get(OAuthCredential, row.id) if policy.oauth else None
            if not policy.oauth or (credential is not None and credential.encrypted):
                selected.add(row.id)
    user = await db.get(User, grant.user_id, populate_existing=True)
    identity = slack_consent(user)
    if identity is not None:
        managed.add(identity)
        if await canonical_slack(service.database, db, grant.user_id, identity) is not None:
            selected.add(identity)
    dot = await db.get(PersonalDot, grant.user_id)
    initial = Definition(
        name="Your Dot attention", prompt=(
            "Notice meaningful activity in my selected accounts: requests, changes, "
            "upcoming meetings and work that deserves preparation or follow-up. Connect "
            "these observations to my recent priorities. Notify me only when useful; "
            "otherwise stay quiet."
        ),
        kind="attention", attention_mode="discovery", visibility="private",
        session_id=dot.session_id, environment_id="none",
        triggers=[Trigger(id="attention", schedule=Timing(
            frequency="hourly", time=(datetime.now(UTC) + timedelta(minutes=1)).strftime("00:%M"), timezone="UTC",
        ))],
        metadata={"created_by": "personal_connections"}, harness=CORE_IDENTITY,
    ) if dot is not None else None
    return await service.reconcile_attention_accounts(
        db, grant, managed=frozenset(managed), selected=frozenset(selected), initial=initial,
    )


async def provider_changed(runtime: Runtime, provider: str, *, owner: str | None = None) -> None:
    """After canonical revocation commits, reconcile existing consent, never grant new consent."""
    from .product_identity import ProductGrant

    service = runtime.product_automations
    if service is None:
        return
    if owner is None:
        async with runtime.database.sessions() as db:
            owners = set(await db.scalars(select(Connection.owner_id).where(Connection.kind == provider)))
            if provider == "slack":
                owners.update(await db.scalars(select(User.id).where(User.preferences.contains(SLACK_PREFERENCE))))
    else:
        owners = {owner}
    for identity in sorted(owners):
        sync_ids = []
        async with runtime.database.sessions.begin() as db:
            await service._writer(db)
            records = await service.attention_records(db, identity)
            bindings = [
                binding for record in records
                if (binding := await db.get(Binding, record.id)) is not None and binding.state != "deleted"
            ]
            if not bindings:
                continue
            original = ProductGrant(identity, bindings[0].authority_version)
            try:
                await service.identity.authorize_admission(db, original)
            except HTTPException as error:
                if error.status_code not in {401, 403}:
                    raise
                # Existing sync already pauses schedules whose original owner
                # authority expired. Never capture a new authority on their behalf.
                for binding in bindings:
                    binding.sync_state, binding.updated_at = "pending", now()
                    sync_ids.append(binding.id)
            else:
                sync_id = await configure(service, db, original)
                if sync_id is not None:
                    sync_ids.append(sync_id)
        for sync_id in sync_ids:
            await service.request_sync(sync_id)
