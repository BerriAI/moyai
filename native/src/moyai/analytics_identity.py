"""Read-only person attribution for reports; never an authentication resolver."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, text

from .models import User
from .product_billing import IdentityMappings, SourceRef

MAX_IDENTITIES = 50_000


@dataclass(frozen=True)
class ReportIdentities:
    canonical_ids: dict[str, str]
    users: list[dict[str, Any]]
    product_ids: set[str] = field(default_factory=set)

    def canonical(self, actor_id: str) -> str:
        return self.canonical_ids.get(actor_id, actor_id)

    def actors(self, user_id: str | None) -> set[str] | None:
        if not user_id:
            return None
        canonical = self.canonical(user_id)
        return {canonical} | {
            actor_id for actor_id, person_id in self.canonical_ids.items() if person_id == canonical
        }

    def accounting_mappings(self, configured: IdentityMappings) -> IdentityMappings:
        """Join verified product mirrors for this report, preserving explicit overrides.

        canonical_ids contains only mirrors verified against the product directory,
        including saved Slack links. This is actor attribution, never request
        reconciliation or permission to disclose private session details.
        """
        actors = dict(configured.actors)
        for actor, canonical in self.canonical_ids.items():
            target = configured.resolve(SourceRef("legacy", canonical), actor=True)
            assert target is not None
            actors.setdefault(SourceRef("native", actor), target.id)
        return IdentityMappings(
            configured.version + "+verified-product-mirrors-v1",
            configured.provenance + ";verified-product-directory",
            actors=actors, sessions=configured.sessions,
        ) if actors != configured.actors else configured


async def report_identities(runtime: Any) -> ReportIdentities:
    # Release the native connection before borrowing from the product pool.
    async with runtime.database.sessions() as db:
        native = list(await db.scalars(select(User).order_by(User.username).limit(MAX_IDENTITIES + 1)))
    if len(native) > MAX_IDENTITIES:
        raise HTTPException(422, "Too many report identities.")
    users = {
        row.id: {key: getattr(row, key) for key in (
            "id", "username", "display_name", "role", "enabled", "auth_version",
        )} for row in native
    }
    identity = getattr(runtime, "product_identity", None)
    if identity is None:
        return ReportIdentities({}, list(users.values()))

    async with identity._reader(identity.source_engine) as db:
        source = (await db.execute(text(
            f"SELECT id,kind,name,email,linked_user_id FROM {identity._table('users')} "
            "ORDER BY id LIMIT :limit"
        ), {"limit": MAX_IDENTITIES + 1})).mappings().all()
    if len(source) > MAX_IDENTITIES:
        raise HTTPException(422, "Too many product report identities.")
    known = {row["id"]: row for row in source}
    canonical_ids: dict[str, str] = {}
    product_ids: set[str] = set()
    labels: dict[str, dict[str, Any]] = {}
    for actor in native:
        person = known.get(actor.id)
        # Only the product adapter's exact-ID mirrors belong to this directory.
        if person is None or actor.username != "product:" + hashlib.sha256(actor.id.encode()).hexdigest():
            labels[actor.id] = users[actor.id]
            continue
        target = known.get(person["linked_user_id"])
        if target is not None and target["id"] in users and users[target["id"]]["username"] != (
            "product:" + hashlib.sha256(target["id"].encode()).hexdigest()
        ):
            target = None
        # Match existing product accounting semantics: retain established links
        # for historical attribution even when a profile later needs review.
        # Neither a name/email match nor a link to another Slack account suffices.
        if person["kind"] == "slack" and target is not None and target["kind"] in {"google", "cloudflare"}:
            person = target
        canonical = person["id"]
        product_ids.add(canonical)
        canonical_ids[actor.id] = canonical
        labels[canonical] = {
            "id": canonical, "username": person["email"] or canonical,
            "display_name": person["name"] or person["email"] or canonical,
            "email": person["email"],
        }
    return ReportIdentities(
        canonical_ids, sorted(labels.values(), key=lambda row: (row["display_name"], row["id"])), product_ids,
    )
