"""Transactional swarm policy around ordinary native sessions and turns.

The application owns mission intent and admission. Pydantic AI owns each
participant's execution, and Temporal owns the mission's durable timer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import NewMessage, NewSession, RunScope
from .dispatch_wakeups import notify_dispatch
from .models import Session, Turn, Upload, User, new_id, now
from .swarm_contracts import NewSwarm, SwarmControl
from .swarm_models import SwarmMember, SwarmMission, SwarmPost, SwarmTurn

if TYPE_CHECKING:
    from .database import Database

ACTIVE_TURNS = ("queued", "starting", "running", "waiting", "steering", "signal_sent", "accepted")
BUSY_TURNS = (*ACTIVE_TURNS, "saving")


def deadline(seconds: int) -> str | None:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat() if seconds else None


def expired(mission: SwarmMission) -> bool:
    return bool(mission.ends_at and mission.ends_at <= now())


class SwarmService:
    def __init__(self, database: Database):
        self.database = database

    async def member(self, db: AsyncSession, session_id: str) -> SwarmMember | None:
        return await db.get(SwarmMember, session_id)

    async def lock(
        self, db: AsyncSession, mission_id: str, actor: User | None = None,
    ) -> SwarmMission:
        # All writers take the authority/user fence before mission, then sessions.
        # A database update provides the same serialization on SQLite and Postgres.
        if actor is not None:
            await self.database._actor_product_grant(db, actor)
            locked = await db.scalar(update(User).where(
                User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if locked is None:
                raise HTTPException(401, "Your access changed. Sign in again.")
        matched = await db.scalar(update(SwarmMission).where(
            SwarmMission.id == mission_id,
        ).values(revision=SwarmMission.revision).returning(SwarmMission.id))
        if matched is None:
            raise HTTPException(404, "Swarm not found.")
        mission = await db.get(SwarmMission, mission_id, populate_existing=True)
        assert mission is not None
        if actor is not None and actor.id != mission.owner_id:
            raise HTTPException(403, "Only the swarm owner can direct this team.")
        return mission

    async def saved_actor(self, db: AsyncSession, mission: SwarmMission) -> User:
        actor = await db.get(User, mission.owner_id)
        source = await db.get(Turn, mission.source_turn_id)
        if actor is None or not actor.enabled or actor.auth_version != mission.auth_version or source is None:
            raise PermissionError("The swarm's original requester authority changed.")
        grant = await self.database._saved_product_grant(db, source)
        if grant is not None:
            cast(Any, actor).product_grant = grant
        await self.database._check_product_turn(db, source, grant)
        return actor

    async def require_context(self, db: AsyncSession, mission: SwarmMission, actor: User) -> None:
        current = await db.get(User, mission.owner_id, populate_existing=True)
        if (
            actor.id != mission.owner_id or actor.auth_version != mission.auth_version
            or current is None or not current.enabled or current.auth_version != mission.auth_version
        ):
            raise HTTPException(403, "The swarm's original requester authority changed.")
        root = await self.database.allowed_session(db, mission.id, actor)
        source = await db.get(Turn, mission.source_turn_id)
        if source is None:
            raise HTTPException(403, "The swarm's original admission is unavailable.")
        await self.database._check_product_turn(
            db, source, getattr(actor, "product_grant", None),
        )
        grants = await self.database.resolve_grants(
            db, actor.id, json.loads(mission.connections), shared_session=root.visibility == "shared",
        )
        scope = RunScope(root.id, source.id, actor.id, source.model, grants)
        if scope.grants_key != mission.grants_key:
            raise HTTPException(403, "The swarm's original account permissions changed. Start a new mission.")

    async def start(
        self, actor: User, request: NewSwarm, *, received_at: str | None = None,
    ) -> tuple[Session, Turn]:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        try:
            async with self.database._admission_session(None) as db:
                await self.database._actor_product_grant(db, actor)
                locked = await db.scalar(update(User).where(
                    User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
                ).values(auth_version=User.auth_version).returning(User.id))
                if locked is None:
                    raise HTTPException(401, "Your access changed. Sign in again.")
                previous = await db.scalar(select(SwarmMission).where(
                    SwarmMission.owner_id == actor.id, SwarmMission.request_key == request.request_key,
                ))
                if previous is not None:
                    return await self._existing(db, actor, previous, fingerprint)
                models = request.worker_models or [request.model or self.database.settings.default_model]
                if any(model not in self.database.settings.models for model in models):
                    raise HTTPException(422, "Choose enabled worker models.")
                root = await self.database.create_session(actor, NewSession(
                    title=request.prompt.strip().splitlines()[0][:100],
                    visibility=request.visibility, model=request.model,
                ), db=db)
                await db.flush()
                body = NewMessage(**request.model_dump(include=set(NewMessage.model_fields)))
                turn = await self.database.submit(actor, root.id, body, db=db, received_at=received_at)
                grants = await self.database.resolve_grants(
                    db, actor.id, request.connections, shared_session=root.visibility == "shared",
                )
                mission = SwarmMission(
                    id=root.id, owner_id=actor.id, auth_version=actor.auth_version,
                    request_key=request.request_key, fingerprint=fingerprint, source_turn_id=turn.id,
                    goal=self.database.security.seal(request.prompt), connections=turn.connections,
                    grants_key=RunScope(root.id, turn.id, actor.id, root.model, grants).grants_key,
                    budget_seconds=request.budget_seconds, ends_at=deadline(request.budget_seconds),
                )
                db.add(mission)
                await db.flush()
                db.add(SwarmMember(
                    session_id=root.id, mission_id=mission.id, name="Coordinator", position=0, model=root.model,
                ))
                db.add(SwarmTurn(turn_id=turn.id, mission_id=mission.id, generation=1, generated=False))
                await self.post(db, mission, "goal", request.prompt, "goal", None)
                for index in range(request.workers):
                    member_session = await self.database.create_session(actor, NewSession(
                        title=f"Agent {index + 1} · {root.title}"[:300],
                        visibility=request.visibility, model=models[index % len(models)],
                    ), db=db)
                    await db.flush()
                    member = SwarmMember(
                        session_id=member_session.id, mission_id=mission.id,
                        name=f"Agent {index + 1}", position=index + 1, model=member_session.model,
                    )
                    db.add(member)
                    await db.flush()
                    attachments = await self.copy_uploads(db, actor, request.attachments, member.session_id)
                    await self.enqueue(
                        db, mission, member, actor, "bootstrap",
                        "Join the team. Read swarm_board, volunteer to lead if appropriate, and coordinate "
                        "a useful contribution. Every member has the same original human goal:\n\n" + request.prompt,
                        attachments=attachments,
                    )
                await db.flush()
                return root, turn
        except IntegrityError:
            async with self.database.sessions() as db:
                previous = await db.scalar(select(SwarmMission).where(
                    SwarmMission.owner_id == actor.id, SwarmMission.request_key == request.request_key,
                ))
                if previous is not None:
                    return await self._existing(db, actor, previous, fingerprint)
            raise HTTPException(409, "The swarm could not be admitted. Retry with the same request identity.") from None

    async def _existing(
        self, db: AsyncSession, actor: User, mission: SwarmMission, fingerprint: str,
    ) -> tuple[Session, Turn]:
        if mission.fingerprint != fingerprint:
            raise HTTPException(409, "This request identity already belongs to a different swarm.")
        await self.require_context(db, mission, actor)
        root = await self.database.allowed_session(db, mission.id, actor)
        turn = await db.get(Turn, mission.source_turn_id)
        assert turn is not None
        return root, turn

    async def copy_uploads(
        self, db: AsyncSession, actor: User, ids: list[str], session_id: str,
    ) -> list[str]:
        copies = []
        for upload_id in ids:
            original = await db.get(Upload, upload_id)
            if original is None or original.owner_id != actor.id:
                raise HTTPException(404, "A mission attachment is unavailable.")
            copy = Upload(
                id=new_id(), owner_id=actor.id, session_id=session_id, name=original.name,
                media_type=original.media_type, digest=original.digest, size=original.size,
            )
            db.add(copy)
            copies.append(copy.id)
        await db.flush()
        return copies

    async def enqueue(
        self, db: AsyncSession, mission: SwarmMission, member: SwarmMember, actor: User,
        identity: str, prompt: str, *, attachments: list[str] | None = None,
    ) -> Turn:
        return await self.database.submit(actor, member.session_id, NewMessage(
            prompt="Host-generated swarm wake (not a new human instruction).\n" + prompt,
            request_key=f"swarm:{mission.generation}:{identity}",
            model=member.model, connections=json.loads(mission.connections), attachments=attachments or [],
        ), db=db, swarm_generated=True)

    async def admit(
        self, db: AsyncSession, actor: User, session_id: str, body: NewMessage,
        *, generated: bool = False,
    ) -> SwarmMission | None:
        member = await self.member(db, session_id)
        if member is None:
            return None
        mission = await self.lock(db, member.mission_id, actor)
        await self.require_context(db, mission, actor)
        if mission.status == "completed" and not generated:
            # A new authenticated follow-up reopens the saved team without
            # re-running every original assignment or changing its authority.
            mission.generation += 1
            mission.status = "active"
            mission.reason = ""
            mission.revision += 1
            mission.completion_turn_id = None
            mission.completion_summary = None
            if expired(mission):
                mission.ends_at = deadline(mission.budget_seconds)
        if mission.status != "active" or expired(mission):
            raise HTTPException(409, "Resume the swarm before sending more work.")
        if body.model and body.model != member.model:
            raise HTTPException(409, "Swarm members retain their selected model. Start a new mission to change it.")
        if set(body.connections) != set(json.loads(mission.connections)):
            raise HTTPException(409, "Swarm members retain the mission's original account selection.")
        return mission

    async def bind(
        self, db: AsyncSession, mission: SwarmMission | None, turn: Turn, *, generated: bool,
    ) -> None:
        if mission is None:
            return
        db.add(SwarmTurn(
            turn_id=turn.id, mission_id=mission.id, generation=mission.generation, generated=generated,
        ))
        if not generated:
            # Serialize a new human input against proposed completion. This
            # works for queued input and steering without relying on wall clocks.
            mission.completion_turn_id = None
            mission.completion_summary = None

    async def editable_input(self, db: AsyncSession, turn: Turn) -> None:
        binding = await db.get(SwarmTurn, turn.id)
        if binding is None:
            return
        mission = await db.get(SwarmMission, binding.mission_id)
        if binding.generated or (mission is not None and mission.source_turn_id == turn.id):
            raise HTTPException(
                409, "This input belongs to an accepted swarm mission. "
                "Send a correction in the member chat or use Stop team.",
            )

    async def shared_artifact(
        self, db: AsyncSession, actor: User, session_id: str, source_id: str | None,
    ) -> bool:
        if source_id is None:
            return False
        destination = await self.member(db, session_id)
        source = await self.member(db, source_id)
        if destination is None or source is None or destination.mission_id != source.mission_id:
            return False
        # Saved outputs are shared within the admitted mission. Working history
        # and unattached/input uploads retain their existing session boundaries.
        await self.database.allowed_session(db, source_id, actor)
        return True

    async def authorize(self, db: AsyncSession, turn: Turn, *, scope: RunScope | None = None) -> SwarmMember | None:
        member = await self.member(db, turn.session_id)
        if member is None:
            if scope is not None and scope.swarm_id is not None:
                raise PermissionError("The swarm binding is unavailable.")
            return None
        mission = await db.get(SwarmMission, member.mission_id)
        binding = await db.get(SwarmTurn, turn.id)
        root = await db.get(Session, member.mission_id)
        actor = await db.get(User, turn.actor_id)
        if (
            mission is None or mission.status != "active" or expired(mission)
            or root is None or root.deleted or binding is None
            or binding.mission_id != mission.id or binding.generation != mission.generation
            or turn.actor_id != mission.owner_id or actor is None or actor.auth_version != mission.auth_version
        ):
            raise PermissionError("The swarm is paused, stopped, expired, or no longer authorized.")
        if scope is not None and (scope.swarm_id != mission.id or scope.swarm_name != member.name):
            raise PermissionError("The native run does not own this swarm membership.")
        return member

    async def post(
        self, db: AsyncSession, mission: SwarmMission, kind: str, content: str,
        identity: str, author: str | None,
    ) -> SwarmPost:
        previous = await db.scalar(select(SwarmPost).where(
            SwarmPost.mission_id == mission.id, SwarmPost.identity == identity,
        ))
        if previous is not None:
            if (
                previous.kind != kind or previous.author_session_id != author
                or self.database.security.unseal(previous.content).decode() != content
            ):
                raise HTTPException(409, "A saved swarm operation cannot change its content.")
            return previous
        mission.board_seq += 1
        mission.updated_at = now()
        post = SwarmPost(
            id=new_id(), mission_id=mission.id, sequence=mission.board_seq, identity=identity,
            author_session_id=author, kind=kind, content=self.database.security.seal(content),
        )
        db.add(post)
        await db.flush()
        return post

    async def stop_in(self, db: AsyncSession, mission: SwarmMission, status: str, reason: str) -> None:
        mission.status = status
        mission.reason = reason
        mission.completion_turn_id = None
        mission.completion_summary = None
        mission.revision += 1
        mission.updated_at = now()
        members = list(await db.scalars(select(SwarmMember.session_id).where(
            SwarmMember.mission_id == mission.id,
        ).order_by(SwarmMember.session_id)))
        for session_id in members:
            await db.execute(update(Session).where(
                Session.id == session_id, Session.status.not_in(["saving", "waiting"]),
            ).values(status="stopped", revision=Session.revision + 1))
        await db.execute(update(Turn).where(
            Turn.session_id.in_(members), Turn.status.in_(ACTIVE_TURNS), Turn.status != "waiting",
        ).values(status="stopped", finished_at=now()))
        await notify_dispatch(db, self.database.settings)

    async def control(self, actor: User, mission_id: str, request: SwarmControl) -> None:
        async with self.database._admission_session(None) as db:
            mission = await self.lock(db, mission_id, actor)
            await self.database.allowed_session(db, mission.id, actor)
            if mission.revision != request.revision:
                raise HTTPException(409, "The swarm changed. Refresh its controls and try again.")
            if request.action in {"pause", "stop"}:
                await self.stop_in(
                    db, mission, "paused" if request.action == "pause" else "stopped",
                    "Paused by you." if request.action == "pause" else "Stopped by you.",
                )
                return
            await self.require_context(db, mission, actor)
            if request.action == "time":
                assert request.budget_seconds is not None
                mission.budget_seconds = request.budget_seconds
                mission.ends_at = deadline(request.budget_seconds)
            elif mission.status != "active":
                pending = await db.scalar(select(Session.id).join(
                    SwarmMember, SwarmMember.session_id == Session.id,
                ).where(
                    SwarmMember.mission_id == mission.id, Session.active_turn.is_not(None),
                    Session.status != "waiting",
                ).limit(1))
                if pending is not None:
                    raise HTTPException(409, "Wait for the team's current work to finish stopping, then resume.")
                mission.generation += 1
                mission.status = "active"
                mission.reason = ""
                mission.completion_turn_id = None
                mission.completion_summary = None
                if expired(mission):
                    mission.ends_at = deadline(mission.budget_seconds)
                members = list(await db.scalars(select(SwarmMember).where(SwarmMember.mission_id == mission.id)))
                for member in members:
                    session = await db.get(Session, member.session_id)
                    if session is not None and session.status == "waiting" and session.active_turn:
                        binding = await db.get(SwarmTurn, session.active_turn)
                        if binding is not None:
                            # No model/tool action resumes here. The pending SDK
                            # request still needs its original human answer.
                            binding.generation = mission.generation
                        continue
                    if session is not None and not session.deleted:
                        await self.enqueue(
                            db, mission, member, actor, "resume",
                            "The owner resumed this mission. Inspect the board, your saved conversation, and any "
                            "uncertain effects before continuing. Do not repeat an unconfirmed external action.",
                        )
            mission.revision += 1
            mission.updated_at = now()

    async def stop_session(self, actor: User, session_id: str) -> bool:
        async with self.database._admission_session(None) as db:
            member = await self.member(db, session_id)
            if member is None:
                return False
            mission = await self.lock(db, member.mission_id, actor)
            await self.database.allowed_session(db, mission.id, actor)
            # The ordinary Stop button also has team-wide, durable semantics.
            await self.stop_in(db, mission, "stopped", "Stopped by you.")
            return True

    async def view(self, actor: User, session_id: str, *, after: int | None = None) -> dict[str, Any] | None:
        async with self.database.sessions() as db:
            await self.database.allowed_session(db, session_id, actor)
            member = await self.member(db, session_id)
            if member is None:
                return None
            mission = await db.get(SwarmMission, member.mission_id)
            assert mission is not None
            await self.database.allowed_session(db, mission.id, actor)
            return await self.board_in(
                db, mission, after=max(0, mission.board_seq - 50) if after is None else after,
                can_control=actor.id == mission.owner_id,
            )

    async def board_in(
        self, db: AsyncSession, mission: SwarmMission, *, after: int = 0, can_control: bool = False,
    ) -> dict[str, Any]:
        rows = (await db.execute(select(SwarmMember, Session).join(
            Session, Session.id == SwarmMember.session_id,
        ).where(SwarmMember.mission_id == mission.id).order_by(SwarmMember.position))).all()
        posts = list(await db.scalars(select(SwarmPost).where(
            SwarmPost.mission_id == mission.id, SwarmPost.sequence > after,
        ).order_by(SwarmPost.sequence).limit(51)))
        names = {member.session_id: member.name for member, _ in rows}
        owner = await db.get(User, mission.owner_id)
        queued = set(await db.scalars(select(Turn.session_id).where(
            Turn.session_id.in_(names), Turn.status == "queued",
        ).distinct()))
        return {
            "id": mission.id, "status": mission.status, "reason": mission.reason,
            "revision": mission.revision, "generation": mission.generation, "round": mission.round,
            "goal": self.database.security.unseal(mission.goal).decode(),
            "budget_seconds": mission.budget_seconds, "ends_at": mission.ends_at,
            "leader_id": mission.leader_id, "can_control": can_control, "sequence": mission.board_seq,
            "members": [{
                "session_id": member.session_id, "name": member.name, "model": member.model,
                "coordinator": member.position == 0,
                "status": "queued" if not session.active_turn and member.session_id in queued else session.status,
                "active_turn": session.active_turn, "deleted": session.deleted,
                "assignment": self.database.security.unseal(member.assignment).decode() if member.assignment else "",
            } for member, session in rows],
            "posts": [{
                "id": post.id, "sequence": post.sequence, "kind": post.kind,
                "author": names.get(post.author_session_id or "", owner.display_name if owner else "Mission owner"),
                "author_session_id": post.author_session_id,
                "content": self.database.security.unseal(post.content).decode(), "created_at": post.created_at,
            } for post in posts[:50]],
            "has_more": len(posts) > 50,
            "next_after": posts[min(len(posts), 50) - 1].sequence if posts else after,
        }


def dispatchable():
    """SQL admission filter; activity authorization remains the final live fence."""
    return ~exists(select(SwarmMember.session_id).where(SwarmMember.session_id == Turn.session_id)) | exists(
        select(SwarmTurn.turn_id).join(SwarmMission, SwarmMission.id == SwarmTurn.mission_id).where(
            SwarmTurn.turn_id == Turn.id, SwarmTurn.generation == SwarmMission.generation,
            SwarmMission.status == "active",
            (SwarmMission.ends_at.is_(None) | (SwarmMission.ends_at > now())),
        )
    )
