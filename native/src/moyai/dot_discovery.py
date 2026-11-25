"""Durable attention items and private judgment, behind the existing Dot boundary."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic_ai import ConversationTypeAdapter
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from sqlalchemy import select

from .connection_policy import ConnectionPolicy
from .contracts import Grant, RunScope
from .dot_contracts import DotItemEdit, DotItemsView, DotItemView, DotJudgment, DotObservation, DotResearch
from .dot_evidence import validate_evidence, verified_closed
from .dot_models import DotAttentionTurn, DotDiscoveryTurn, DotItem, PersonalDot
from .dot_sources import repositories, source_allowed
from .models import Connection, Message, Session, Turn, User, now
from .private_instructions import individual

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .database import Database

ITEM_LIMIT = 100


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def account_key(grant: Grant) -> str:
    """A replacement credential is a new account until provider identity is proven.

    Policy/name edits keep state; opaque credential or endpoint replacement does
    not inherit a previous account's private summaries or suppression decisions.
    """
    policy = ConnectionPolicy.model_validate(grant.policy)
    return digest([
        grant.id, grant.kind, policy.oauth_account or grant.credential,
        policy.mcp.url if policy.mcp else None,
    ])


class PrivateMessage(BaseModel):
    role: str
    text: str


class DiscoverySnapshot(BaseModel):
    research: DotResearch
    identities: list[tuple[str, str, str]]
    revisions: dict[str, int]
    items: list[DotItemView]
    conversation: list[PrivateMessage]
    current_time: str
    priorities: str
    accounts: dict[str, str]
    verified_closed: list[int]


class Discovery:
    def __init__(self, database: Database):
        self.database = database

    def unpack(self, item: DotItem) -> DotObservation:
        return DotObservation.model_validate_json(self.database.security.unseal(item.content))

    def view(self, item: DotItem) -> DotItemView:
        observation = self.unpack(item)
        return DotItemView(
            id=item.id, revision=item.revision, connection_id=item.connection_id,
            title=observation.title, summary=observation.summary, source=observation.evidence[0].source,
            state=item.state, snoozed_until=item.snoozed_until, last_seen_at=item.last_seen_at,
        )

    async def active(self, db: AsyncSession, destination: str) -> bool:
        return bool(await db.scalar(select(DotDiscoveryTurn.turn_id).join(
            DotAttentionTurn, DotAttentionTurn.turn_id == DotDiscoveryTurn.turn_id,
        ).join(Turn, Turn.id == DotDiscoveryTurn.turn_id).where(
            DotAttentionTurn.destination_session_id == destination,
            Turn.status.not_in(("completed", "failed", "stopped", "included")),
        ).limit(1)))

    async def scan_prompt(self, db: AsyncSession, turn: Turn, destination: str) -> str:
        """Research receives provider references, never private summaries or chat."""
        accounts = json.loads(turn.connections)
        grants = await self.database.dots.grants(db, turn.actor_id, accounts)
        occurrence = await db.get(DotDiscoveryTurn, turn.id)
        assert occurrence is not None
        sources = json.loads(self.database.security.unseal(occurrence.sources))
        restricted = repositories(grants, sources)
        items = list(await db.scalars(select(DotItem).where(
            DotItem.owner_id == turn.actor_id, DotItem.account_key.in_([account_key(grant) for grant in grants]),
            DotItem.state.in_(("open", "snoozed")),
        ).order_by(DotItem.last_seen_at).limit(20)))
        return json.dumps({
            "connection_ids": accounts, "current_time": now(),
            "starting_locations": sources,
            "revisit": [
                {"connection_id": item.connection_id, "source": self.unpack(item).evidence[0].source}
                for item in items
                if item.connection_id not in restricted
                or source_allowed(self.unpack(item).evidence[0].source, restricted[item.connection_id])
            ],
            "limits": "At most 12 tool calls, 30 records per call. Prefer recent activity and unfinished conversations.",
        }, ensure_ascii=False)

    async def judgment_input(self, scope: RunScope, research_json: str, conversation: str) -> str:
        """Validate current provider evidence and freeze the owner-edit fence once."""
        async with self.database.sessions.begin() as db:
            await self.database.authorize_scope(scope, db=db)
            occurrence = await db.scalar(select(DotDiscoveryTurn).where(
                DotDiscoveryTurn.turn_id == scope.turn_id,
            ).with_for_update())
            if occurrence is None:
                raise PermissionError("This is not a discovery occurrence.")
            if occurrence.snapshot is not None:
                return self.database.security.unseal(occurrence.snapshot).decode()
            turn = await db.get(Turn, scope.turn_id)
            assert turn is not None
            attention = await db.get(DotAttentionTurn, scope.turn_id)
            assert attention is not None
            research = DotResearch.model_validate_json(research_json)
            accounts = {grant.id: account_key(grant) for grant in scope.grants}
            identities = [
                validate_evidence(observation.evidence, conversation, scope.grants)[0]
                for observation in research.observations
            ]
            for observation in research.observations:
                for account, source, _ in validate_evidence(observation.evidence, conversation, scope.grants):
                    if account in scope.discovery_repositories and not source_allowed(source, scope.discovery_repositories[account]):
                        raise ValueError("Discovery evidence is outside the approved repositories.")
            if len({(account, source) for account, source, _ in identities}) != len(identities):
                raise ValueError("Combine findings about the same provider item.")
            allowed = {
                f"{grant.kind}_{grant.id[:8]}_{name}": grant.id
                for grant in scope.grants
                for name in (ConnectionPolicy.model_validate(grant.policy).allowed_tools or [])
            }
            checked: set[str] = set()
            partial = False
            for message in ConversationTypeAdapter.validate_json(conversation).messages:
                for part in message.parts:
                    if not isinstance(part, (ToolReturnPart, RetryPromptPart)) or part.tool_name not in allowed:
                        continue
                    if isinstance(part, RetryPromptPart) or part.outcome != "success":
                        partial = True
                    elif isinstance(part.content, dict) and part.content.get("dot_scan") is True:
                        partial |= bool(
                            part.content.get("limited") or part.content.get("error")
                            or part.content.get("scanned") is not True
                        )
                        if not part.content.get("error") and part.content.get("scanned"):
                            checked.add(allowed[part.tool_name])
                    else:
                        partial = True
            if not checked:
                research.coverage = "blocked"
            elif partial or checked != {grant.id for grant in scope.grants}:
                research.coverage = "partial"
            items = list(await db.scalars(select(DotItem).where(
                DotItem.owner_id == scope.actor_id, DotItem.account_key.in_(accounts.values()),
            ).order_by(DotItem.last_seen_at.desc()).limit(30)))
            # Include the exact revision of every observed identity even if its
            # older record falls outside the bounded context catalog.
            revisions = {}
            for account, source, _ in identities:
                key = digest([scope.actor_id, accounts[account], source])
                item = await db.get(DotItem, key)
                revisions[key] = item.revision if item else 0
            messages = list(await db.scalars(select(Message).where(
                Message.session_id == attention.destination_session_id,
                Message.role.in_(("user", "assistant")),
            ).order_by(Message.id.desc()).limit(15)))
            snapshot = DiscoverySnapshot(
                research=research, identities=identities, revisions=revisions,
                items=[self.view(item) for item in items],
                conversation=[PrivateMessage(
                    role=message.role, text=self.database.security.unseal(message.content).decode()[:1500],
                ) for message in reversed(messages)],
                current_time=now(),
                priorities=self.database.security.unseal(turn.prompt).decode(),
                accounts=accounts,
                verified_closed=[
                    index for index, observation in enumerate(research.observations)
                    if research.coverage == "complete"
                    and verified_closed(observation.evidence[0], conversation, scope.grants)
                ],
            )
            payload = snapshot.model_dump_json()
            occurrence.snapshot = self.database.security.seal(payload)
            return payload

    async def apply(
        self, db: AsyncSession, turn: Turn, output: str,
    ) -> tuple[str, str | None, str | None, str | None]:
        """Caller holds the Dot publication fence. Return outcome and optional delivery."""
        occurrence = await db.get(DotDiscoveryTurn, turn.id)
        attention = await db.get(DotAttentionTurn, turn.id)
        if occurrence is None or occurrence.snapshot is None or attention is None:
            raise PermissionError("Discovery has no validated research snapshot.")
        snapshot = DiscoverySnapshot.model_validate_json(self.database.security.unseal(occurrence.snapshot))
        if datetime.now(UTC) - datetime.fromisoformat(snapshot.current_time) > timedelta(minutes=15):
            return "expired", None, None, None
        judgment = DotJudgment.model_validate_json(output)
        outcome = "quiet" if snapshot.research.coverage == "complete" else snapshot.research.coverage
        notification = None
        stamp = now()
        for change in judgment.items:
            if change.observation >= len(snapshot.research.observations):
                raise ValueError("Use an observation from this check.")
            if change.state == "resolved" and (
                snapshot.research.coverage != "complete" or change.observation not in snapshot.verified_closed
            ):
                raise ValueError("Automatic resolution requires verified provider closure evidence.")
            observation = snapshot.research.observations[change.observation]
            account, source, version = snapshot.identities[change.observation]
            key = digest([turn.actor_id, snapshot.accounts[account], source])
            item = await db.get(DotItem, key)
            if (item.revision if item else 0) != snapshot.revisions[key]:
                continue  # A newer owner edit or publication wins over this observer.
            if item is not None and item.owner_controlled and (
                item.state in ("resolved", "dismissed")
                or item.state == "snoozed" and item.snoozed_until and item.snoozed_until > stamp
            ):
                continue
            if item is None:
                if change.state == "resolved":
                    continue
                item = DotItem(
                    id=key, owner_id=turn.actor_id, connection_id=account, account_key=snapshot.accounts[account],
                    source_key=digest([snapshot.accounts[account], source]), owner_revision=0, revision=0,
                )
                db.add(item)
            item.content = self.database.security.seal(observation.model_dump_json())
            item.source_version = self.database.security.seal(version)
            item.state, item.owner_controlled, item.snoozed_until = change.state, False, None
            item.revision += 1
            item.last_seen_at = stamp
            if change.observation == judgment.notify_observation and snapshot.research.coverage != "blocked":
                # Owner reopen/snooze grants a fresh delivery epoch. Schedule
                # generations and arbitrary model text never reset deduplication.
                fingerprint = digest([key, version, item.owner_revision])
                notification = judgment.message, source, fingerprint
        return (outcome, *notification) if notification else (outcome, None, None, None)

    async def owner_session(self, db: AsyncSession, actor: User, *, lock: bool = False) -> str:
        grant = await self.database._actor_product_grant(db, actor)
        if not await individual(self.database, db, actor, grant=grant):
            raise HTTPException(403, "Your Dot requires an individual sign-in.")
        dot = await db.get(PersonalDot, actor.id)
        if dot is None:
            raise HTTPException(404, "Open Your Dot first.")
        if lock:
            # Same ordering as publication, so controls and delivery serialize.
            current = await db.scalar(select(User).where(User.id == actor.id).with_for_update().execution_options(populate_existing=True))
            if current is None or not current.enabled or current.auth_version != actor.auth_version:
                raise HTTPException(401, "Your access changed. Sign in again.")
            await db.scalar(select(Session).where(Session.id == dot.session_id).with_for_update())
        await self.database.dots.own_session(db, actor.id, dot.session_id)
        return dot.session_id

    async def list(self, actor: User) -> DotItemsView:
        async with self.database.sessions.begin() as db:
            await self.owner_session(db, actor)
            accounts = list(await db.scalars(select(Connection).where(
                Connection.owner_id == actor.id, Connection.shared.is_(False),
                Connection.enabled.is_(True), Connection.read_only.is_(True),
            )))
            visible = []
            for account in accounts:
                try:
                    grants = await self.database.dots.grants(db, actor.id, [account.id])
                except (HTTPException, PermissionError):
                    continue
                visible.extend(account_key(grant) for grant in grants)
            items = list(await db.scalars(select(DotItem).where(
                DotItem.owner_id == actor.id, DotItem.account_key.in_(visible),
            ).order_by(DotItem.last_seen_at.desc()).limit(ITEM_LIMIT + 1)))
            return DotItemsView(items=[self.view(item) for item in items[:ITEM_LIMIT]], limited=len(items) > ITEM_LIMIT)

    async def edit(self, actor: User, identity: str, body: DotItemEdit) -> DotItemView:
        async with self.database.sessions.begin() as db:
            await self.owner_session(db, actor, lock=True)
            item = await db.get(DotItem, identity)
            if item is None or item.owner_id != actor.id:
                raise HTTPException(404, "Follow-up not found.")
            grants = await self.database.dots.grants(db, actor.id, [item.connection_id], lock=True)
            if item.account_key != account_key(grants[0]):
                raise HTTPException(404, "This account was replaced. The earlier follow-up is unavailable.")
            if item.revision != body.revision:
                raise HTTPException(409, "This follow-up changed. Refresh it before editing.")
            until = body.snoozed_until.astimezone(UTC).isoformat() if body.snoozed_until else None
            if until is not None and until <= datetime.now(UTC).isoformat():
                raise HTTPException(422, "Choose a future snooze time.")
            item.state, item.snoozed_until = body.state, until
            item.owner_controlled = body.state != "open"
            item.owner_revision += 1
            item.revision += 1
            return self.view(item)
