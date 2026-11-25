"""Enroll personal Google accounts in the canonical Dot attention schedule."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from app.automation_definitions import Definition, Timing, Trigger
from fastapi import HTTPException
from sqlalchemy import select, text

from .automations.product_automation_models import ProductAutomationBinding as Binding
from .automations.product_automations import _definition, definition_digest
from .connection_policy import ConnectionPolicy
from .dot_models import PersonalDot
from .models import Connection, now
from .product_config import CORE_IDENTITY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .automations.product_automations import ProductAutomationService
    from .product_identity import ProductGrant

GOOGLE_KINDS = frozenset({"gmail", "google_calendar"})
EMPTY_PAUSE = "google_empty_pause_generation"


async def configure(
    service: ProductAutomationService, db: AsyncSession, grant: ProductGrant,
) -> str | None:
    """Commit account enrollment and scheduling intent in the caller's transaction.

    The existing coordinator dispatches pending Temporal synchronization. There
    is no provider polling loop here, and owner-paused schedules remain paused.
    """
    await service._writer(db)
    principal = await service.identity.authorize_admission(db, grant)
    if not principal.individual:
        raise HTTPException(403, "Proactive accounts require an individual sign-in.")
    accounts = list(await db.scalars(select(Connection).where(
        Connection.owner_id == principal.id, Connection.kind.in_(GOOGLE_KINDS),
        Connection.shared.is_(False),
    ).execution_options(populate_existing=True)))
    selected = {
        row.id for row in accounts if row.enabled and row.read_only
        and (policy := ConnectionPolicy.model_validate_json(row.policy)).oauth
        and policy.use_proactively
    }
    rows = (await db.execute(text(
        f"SELECT * FROM {service._table('automations')} WHERE owner_id=:owner"
    ), {"owner": principal.id})).mappings().all()
    saved = None
    binding = None
    for row in rows:
        definition = _definition(dict(row))
        candidate = await db.get(Binding, row["id"])
        if definition.attention_mode == "discovery" and (candidate is None or candidate.state != "deleted"):
            if saved is not None:
                raise HTTPException(409, "Review the duplicate Dot attention checks before changing accounts.")
            saved, binding = dict(row), candidate
    if saved is None and not selected:
        return None
    dot = await db.get(PersonalDot, principal.id)
    if dot is None and saved is None:
        raise HTTPException(409, "Open Your Dot before enabling proactive accounts.")
    is_new = saved is None
    if saved is None:
        assert dot is not None
        minute = (datetime.now(UTC) + timedelta(minutes=1)).strftime("00:%M")
        definition = Definition(
            name="Your Dot attention", prompt=(
                "Notice meaningful upcoming meetings, preparation needs, schedule changes, "
                "and email conversations that deserve a reply or follow-up. Connect these "
                "observations to my recent priorities. Notify me only when useful; otherwise stay quiet."
            ),
            kind="attention", attention_mode="discovery", visibility="private",
            session_id=dot.session_id, connection_ids=sorted(selected), environment_id="none",
            triggers=[Trigger(id="attention", schedule=Timing(frequency="hourly", time=minute, timezone="UTC"))],
            metadata={"created_by": "personal_connections"}, harness=CORE_IDENTITY,
        )
        definition = service.normalize_definition(definition)
        identity, revision, stamp = uuid4().hex, 1, now()
        await service._single_discovery(db, principal.id, identity, definition)
        await db.execute(text(
            f"INSERT INTO {service._table('automations')} "
            "(id,owner_id,definition,revision,paused,synced_revision,synced_schedule_version,"
            "sync_error,created_at,updated_at) VALUES (:id,:owner,:definition,1,1,0,0,'',:stamp,:stamp)"
        ), {"id": identity, "owner": principal.id, "definition": definition.model_dump_json(), "stamp": stamp})
    else:
        identity, revision = saved["id"], saved["revision"] + 1
        definition = _definition(saved)
        google_ids = {row.id for row in accounts}
        definition = Definition.model_validate({
            **definition.model_dump(), "session_id": dot.session_id if dot else definition.session_id,
            "connection_ids": sorted((set(definition.connection_ids) - google_ids) | selected),
        })
        await db.execute(text(
            f"UPDATE {service._table('automations')} SET definition=:definition,revision=:revision,"
            "paused=1,sync_error='',updated_at=:stamp WHERE id=:id"
        ), {"id": identity, "definition": definition.model_dump_json(), "revision": revision, "stamp": now()})
    # Existing manually prepared, never-activated definitions remain unactivated.
    if binding is None and not is_new:
        return None
    if binding is None:
        binding = Binding(
            id=identity, owner_id=principal.id, authority_version=grant.authority_version,
            source_revision=revision, generation=0, definition_digest=definition_digest(definition),
        )
        db.add(binding)
    previous_generation = binding.generation or 0
    was_active = (
        is_new or binding.state == "active"
        or definition.metadata.get(EMPTY_PAUSE) == str(previous_generation)
    )
    binding.generation = previous_generation + 1
    binding.authority_version = grant.authority_version
    binding.source_revision = revision
    binding.definition_digest = definition_digest(definition)
    binding.state = "active" if was_active and definition.connection_ids else "paused"
    binding.sync_state, binding.last_error, binding.updated_at = "pending", "", now()
    definition.metadata.pop(EMPTY_PAUSE, None)
    if was_active and not definition.connection_ids:
        definition.metadata[EMPTY_PAUSE] = str(binding.generation)
    await db.execute(text(
        f"UPDATE {service._table('automations')} SET definition=:definition WHERE id=:id"
    ), {"id": identity, "definition": definition.model_dump_json()})
    binding.definition_digest = definition_digest(definition)
    await db.flush()
    try:
        if binding.state == "active":
            if definition.session_id is None:
                raise HTTPException(409, "The Dot conversation is unavailable.")
            await service.database.dots.own_session(db, principal.id, definition.session_id)
        await service._capture_context(
            db, binding, definition, principal,
            capture=binding.state == "active", previous_generation=previous_generation,
        )
    except HTTPException as error:
        if error.status_code not in {403, 404, 409}:
            raise
        # Revocation must commit even when another account or destination is no
        # longer valid. Fence old work and pause until the owner repairs access.
        binding.state = "paused"
        binding.last_error = "Review your Dot conversation and selected accounts before resuming attention."
        await service._capture_context(
            db, binding, definition, principal, capture=False, previous_generation=previous_generation,
        )
    return identity


async def status(
    service: ProductAutomationService | None, db: AsyncSession, owner: str, connection: str,
) -> Literal["unavailable", "paused", "pending", "active"]:
    if service is None:
        return "unavailable"
    rows = (await db.execute(text(
        f"SELECT id,definition FROM {service._table('automations')} WHERE owner_id=:owner"
    ), {"owner": owner})).mappings()
    for row in rows:
        definition = Definition.model_validate(json.loads(row["definition"]))
        if definition.attention_mode != "discovery" or connection not in definition.connection_ids:
            continue
        binding = await db.get(Binding, row["id"])
        if binding is not None and binding.state == "active":
            return "active" if binding.sync_state == "ready" else "pending"
    return "paused"
