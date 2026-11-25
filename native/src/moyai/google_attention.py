"""Google account selection for the canonical Dot attention lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from app.automation_definitions import Definition, Timing, Trigger
from sqlalchemy import select

from .automations.product_automation_models import ProductAutomationBinding as Binding
from .connection_policy import ConnectionPolicy
from .dot_models import PersonalDot
from .models import Connection
from .product_config import CORE_IDENTITY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .automations.product_automations import ProductAutomationService
    from .product_identity import ProductGrant

GOOGLE_KINDS = frozenset({"gmail", "google_calendar"})


async def configure(
    service: ProductAutomationService, db: AsyncSession, grant: ProductGrant,
) -> str | None:
    accounts = tuple(await db.scalars(select(Connection).where(
        Connection.owner_id == grant.user_id, Connection.kind.in_(GOOGLE_KINDS),
        Connection.shared.is_(False),
    ).execution_options(populate_existing=True)))
    selected = frozenset(
        row.id for row in accounts if row.enabled and row.read_only
        and (policy := ConnectionPolicy.model_validate_json(row.policy)).oauth
        and policy.use_proactively
    )
    dot = await db.get(PersonalDot, grant.user_id)
    initial = Definition(
        name="Your Dot attention", prompt=(
            "Notice meaningful upcoming meetings, preparation needs, schedule changes, "
            "and email conversations that deserve a reply or follow-up. Connect these "
            "observations to my recent priorities. Notify me only when useful; otherwise stay quiet."
        ),
        kind="attention", attention_mode="discovery", visibility="private",
        session_id=dot.session_id, environment_id="none",
        triggers=[Trigger(id="attention", schedule=Timing(
            frequency="hourly", time=(datetime.now(UTC) + timedelta(minutes=1)).strftime("00:%M"), timezone="UTC",
        ))],
        metadata={"created_by": "personal_connections"}, harness=CORE_IDENTITY,
    ) if dot is not None else None
    return await service.reconcile_attention_accounts(
        db, grant, managed=frozenset(row.id for row in accounts), selected=selected, initial=initial,
    )


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
