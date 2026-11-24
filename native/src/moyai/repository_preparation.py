"""Durable preparation receipts shared by the GitHub and GitLab adapters."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from .contracts import RunScope
from .models import Effect, now
from .storage import ObjectStore

if TYPE_CHECKING:
    from .database import Database


def directory_size(root: Path) -> int:
    size = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                size += path.stat().st_size
        except FileNotFoundError:
            pass  # Git atomically renames temporary pack files.
    return size


class RepositoryPreparation:
    """Own receipt locking and storage; adapters retain provider authorization."""

    def __init__(
        self, database: Database, scope: RunScope, descriptor: dict[str, Any],
        provider: Literal["github", "gitlab"],
    ):
        self.database, self.scope, self.descriptor, self.provider = database, scope, descriptor, provider
        self.fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()
        self.identity = hashlib.sha256((f"product-{provider}-bootstrap:" + self.fingerprint).encode()).hexdigest()
        self.objects = ObjectStore(database.settings, database.security)

    async def reserve(self) -> dict[str, Any]:
        async with self.database.sessions() as db:
            previous = await db.get(Effect, self.identity)
        if self.provider == "gitlab":
            if previous and (
                previous.fingerprint != self.fingerprint or previous.state not in {"preparing", "confirmed"}
            ):
                raise ValueError("The GitLab preparation identity or receipt state changed.")
        elif previous and previous.fingerprint != self.fingerprint:
            raise ValueError("The repository preparation identity changed.")
        state = json.loads(self.database.security.unseal(previous.resolution)) if previous and previous.resolution else {}
        if self.provider == "github" and previous and previous.state not in {"preparing", "confirmed"}:
            raise ValueError("This repository preparation has an unsupported receipt state.")
        if not previous:
            async with self.database.sessions.begin() as db:
                if await db.scalar(select(Effect.id).where(
                    Effect.session_id == self.scope.session_id, Effect.state == "pending",
                )):
                    raise ValueError("Resolve the pending workspace action before repository preparation.")
                db.add(Effect(
                    id=self.identity, session_id=self.scope.session_id, turn_id=self.scope.turn_id,
                    actor_id=self.scope.actor_id, tool_name="product_repository_prepare",
                    arguments=self.database.security.seal(self.descriptor),
                    fingerprint=self.fingerprint, state="preparing",
                ))
        return state

    async def resolve(self, repository: str, branch: str, commit: str) -> dict[str, Any]:
        state = {
            **self.descriptor, "repository": repository, "branch": branch,
            "commit_sha": commit, "prepared": True,
        }
        state["marker"] = self.database.security.seal({"fingerprint": self.fingerprint, "commit_sha": commit})
        async with self.database.sessions.begin() as db:
            row = await db.get(Effect, self.identity, with_for_update=True)
            assert row is not None
            if row.resolution:
                state = json.loads(self.database.security.unseal(row.resolution))
            else:
                row.resolution = self.database.security.seal(state)
        return state

    async def store_bundle(self, state: dict[str, Any], raw: bytes) -> dict[str, Any]:
        state["bundle"], state["bundle_sha256"] = await self.objects.put(raw), hashlib.sha256(raw).hexdigest()
        async with self.database.sessions.begin() as db:
            row = await db.get(Effect, self.identity, with_for_update=True)
            assert row is not None and row.resolution is not None
            current = json.loads(self.database.security.unseal(row.resolution))
            if current.get("bundle"):
                state = current
            else:
                row.resolution = self.database.security.seal(state)
        return state

    async def read_bundle(self, state: dict[str, Any]) -> bytes:
        return await self.objects.get(state["bundle"])

    async def confirm(self) -> None:
        async with self.database.sessions.begin() as db:
            row = await db.get(Effect, self.identity, with_for_update=True)
            assert row is not None
            row.state, row.settled_at = "confirmed", now()
