"""Read public commentary from the Harness's authoritative step snapshots."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from sqlalchemy import select

from .commentary import saved_commentary
from .history_handoff import latest_turn_snapshot
from .models import Event, Turn
from .presentation import redact

if TYPE_CHECKING:
    from .runtime import Runtime


async def turn_progress(runtime: Runtime, turn: Turn) -> list[dict[str, str]]:
    async with runtime.database.sessions() as db:
        rows = list(await db.scalars(select(Event).where(
            Event.turn_id == turn.id, Event.kind == "commentary",
        ).order_by(Event.id)))
    if rows:
        messages = [json.loads(runtime.security.unseal(row.content)) for row in rows]
    else:
        # Older turns predate the projection. Their native snapshot still offers
        # whatever public commentary remains in that saved conversation.
        snapshot = await latest_turn_snapshot(runtime, turn.id)
        messages = saved_commentary(snapshot.messages, turn.id) if snapshot else []
    secrets = [runtime.settings.gateway_key.get_secret_value()]
    if turn.preparation:
        prepared = json.loads(runtime.security.unseal(turn.preparation))
        for grant in prepared["scope"]["grants"]:
            if grant.get("credential") and not grant.get("policy", {}).get("product"):
                secrets.append(runtime.security.unseal(grant["credential"]).decode())
    return redact(messages, secrets)
