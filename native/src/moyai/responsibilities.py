"""Private responsibilities: durable observation, bounded assignment and inbox receipts.

Temporal owns wake-ups. Each step applies domain rules under the native authority
fence, and each review uses native admission/Temporal/settlement.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .automations.product_automation_models import ProductAutomationTurn
from .continuations.product_continuation_models import ProductContinuation
from .contracts import NewMessage, NewSession, RunScope
from .delegation_models import DelegatedSession
from .dispatch_wakeups import notify_dispatch
from .goal_models import GoalTurn
from .models import Message, Session, Turn, User, new_id, now
from .product_identity import ProductGrant
from .responsibility_contracts import (
    NewResponsibility,
    ResponsibilityControl,
    ResponsibilityDecision,
    ResponsibilityInboxItem,
    ResponsibilityReview,
    ResponsibilityView,
)
from .responsibility_dispatch import request_wake
from .responsibility_models import (
    Responsibility,
    ResponsibilityAssignment,
    ResponsibilityCompletion,
    ResponsibilityFinding,
)
from .responsibility_workflow import ResponsibilityStep
from .swarm_models import SwarmMember

if TYPE_CHECKING:
    from .database import Database

GRANT = TypeAdapter(ProductGrant)
BUSY = ("queued", "starting", "running", "waiting", "saving")
CLOSED_CONTROLLER = "Responsibility workflow closed unexpectedly. Review activity, then resume."


def ordinary_session():
    from .dot_models import DotAttentionTurn
    return (~select(ResponsibilityAssignment.turn_id).join(
        Turn, Turn.id == ResponsibilityAssignment.turn_id,
    ).where(Turn.session_id == Session.id).correlate(Session).exists()) & (
        ~select(DotAttentionTurn.turn_id).join(Turn, Turn.id == DotAttentionTurn.turn_id)
        .where(Turn.session_id == Session.id).correlate(Session).exists()
    )


def due(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


class Responsibilities:
    def __init__(self, database: Database):
        self.database = database

    async def lock(self, db: AsyncSession, identity: str) -> Responsibility:
        found = await db.scalar(update(Responsibility).where(
            Responsibility.id == identity,
        ).values(updated_at=now()).returning(Responsibility.id))
        if found is None:
            raise HTTPException(404, "Responsibility not found.")
        row = await db.get(Responsibility, identity, populate_existing=True)
        assert row is not None
        return row

    async def worker_session(self, db: AsyncSession, session_id: str) -> bool:
        return bool(await db.scalar(select(ResponsibilityAssignment.turn_id).join(
            Turn, Turn.id == ResponsibilityAssignment.turn_id,
        ).where(Turn.session_id == session_id).limit(1)))

    async def editable_input(self, db: AsyncSession, turn: Turn) -> None:
        if await self.database.dots.worker_session(db, turn.session_id):
            raise HTTPException(409, "Attention checks are immutable. Reply in Your Dot.")
        if await db.get(ResponsibilityAssignment, turn.id) is not None:
            raise HTTPException(409, "Responsibility evidence reviews are immutable. Use the responsibility controls.")

    async def ordinary_chat(self, db: AsyncSession, session_id: str) -> None:
        if await self.database.dots.worker_session(db, session_id):
            raise HTTPException(409, "Attention checks are immutable. Reply in Your Dot.")
        if await self.worker_session(db, session_id):
            raise HTTPException(409, "Use Dot review activity for this immutable responsibility review.")

    async def read_authority(self, db: AsyncSession, actor: User, session: Session) -> None:
        row = await db.scalar(select(Responsibility).join(
            ResponsibilityAssignment, ResponsibilityAssignment.responsibility_id == Responsibility.id,
        ).join(Turn, Turn.id == ResponsibilityAssignment.turn_id).where(
            Turn.session_id == session.id,
        ).limit(1))
        if row is not None:
            if session.visibility != "private" or session.owner_id != actor.id:
                raise HTTPException(404, "Conversation not found.")
            await self.authority(db, row, actor)

    async def source(self, db: AsyncSession, actor: User, session_id: str, *, lock: bool = False) -> Session:
        session = await self.database.allowed_session(db, session_id, actor)
        if lock:
            await db.execute(update(Session).where(Session.id == session_id).values(revision=Session.revision))
            await db.refresh(session)
        if (
            session.owner_id != actor.id or session.visibility != "private" or session.surface != "web"
            or session.deleted or session.archived
            or await db.get(DelegatedSession, session_id) is not None
            or await db.get(SwarmMember, session_id) is not None
            or await self.worker_session(db, session_id)
            or await db.scalar(select(ProductContinuation.native_session_id).where(
                ProductContinuation.native_session_id == session_id,
            )) is not None
        ):
            raise HTTPException(403, "Choose an active private conversation you own.")
        return session

    def grant(self, row: Responsibility) -> ProductGrant | None:
        return GRANT.validate_json(self.database.security.unseal(row.product_grant)) if row.product_grant else None

    async def owner_authority(
        self, db: AsyncSession, row: Responsibility, actor: User | None = None, *, lock: bool = False,
    ) -> User:
        saved = await db.get(User, row.owner_id, populate_existing=True)
        if saved is None or not saved.enabled or saved.auth_version != row.auth_version:
            raise PermissionError("The responsibility's original requester authority changed.")
        grant = self.grant(row)
        if actor is not None and (
            actor.id != row.owner_id or actor.auth_version != row.auth_version
            or getattr(actor, "product_grant", None) != grant
        ):
            raise HTTPException(404, "Responsibility not found.")
        if self.database.product_authority_enabled:
            principal = await self.database._identity().authorize_admission(db, grant)
            if not principal.individual or principal.id != row.owner_id:
                raise PermissionError("Responsibilities require their original individual sign-in.")
        if lock:
            found = await db.scalar(update(User).where(
                User.id == row.owner_id, User.enabled.is_(True), User.auth_version == row.auth_version,
            ).values(auth_version=User.auth_version).returning(User.id))
            if found is None:
                raise PermissionError("The responsibility's original requester authority changed.")
        cast(Any, saved).product_grant = grant
        return saved

    async def authority(
        self, db: AsyncSession, row: Responsibility, actor: User | None = None, *, lock_source: bool = False,
    ) -> User:
        saved = await self.owner_authority(db, row, actor)
        await self.source(db, saved, row.source_session_id, lock=lock_source)
        return saved

    def view(self, row: Responsibility) -> ResponsibilityView:
        return ResponsibilityView.model_validate({
            **{name: getattr(row, name) for name in ResponsibilityView.model_fields if name != "objective"},
            "objective": self.database.security.unseal(row.objective).decode(),
        })

    async def create(self, actor: User, request: NewResponsibility) -> ResponsibilityView:
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        try:
            async with self.database._admission_session(None) as db:
                grant = await self.database._actor_product_grant(db, actor)
                if self.database.product_authority_enabled:
                    principal = await self.database._identity().authorize_admission(db, grant)
                    if not principal.individual:
                        raise HTTPException(403, "Responsibilities require an individual sign-in.")
                current = await db.scalar(update(User).where(
                    User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
                ).values(auth_version=User.auth_version).returning(User.id))
                if current is None:
                    raise HTTPException(401, "Your access changed. Sign in again.")
                await self.source(db, actor, request.source_session_id, lock=True)
                previous = await db.scalar(select(Responsibility).where(
                    Responsibility.owner_id == actor.id, Responsibility.request_key == request.request_key,
                ))
                if previous is not None:
                    await self.authority(db, previous, actor)
                    if previous.fingerprint != fingerprint:
                        raise HTTPException(409, "This request identity already belongs to another responsibility.")
                    return self.view(previous)
                if request.model not in self.database.settings.models:
                    raise HTTPException(422, "Choose an enabled model.")
                row = Responsibility(
                    id=new_id(), owner_id=actor.id, auth_version=actor.auth_version,
                    product_grant=self.database.security.seal(GRANT.dump_json(grant)) if grant else None,
                    request_key=request.request_key, fingerprint=fingerprint,
                    source_session_id=request.source_session_id,
                    objective=self.database.security.seal(request.objective), model=request.model,
                    interval_seconds=request.interval_seconds, max_evaluations=request.max_evaluations,
                    watermark=await db.scalar(select(func.max(ResponsibilityCompletion.sequence)).where(
                        ResponsibilityCompletion.session_id == request.source_session_id,
                    )) or 0,
                    next_check_at=due(request.interval_seconds),
                )
                db.add(row)
                await db.flush()
                await request_wake(db, row.id, row.generation, "start")
                await notify_dispatch(db, self.database.settings)
                return self.view(row)
        except IntegrityError:
            async with self.database.sessions() as db:
                previous = await db.scalar(select(Responsibility).where(
                    Responsibility.owner_id == actor.id, Responsibility.request_key == request.request_key,
                ))
                if previous is not None and previous.fingerprint == fingerprint:
                    await self.authority(db, previous, actor)
                    return self.view(previous)
            raise HTTPException(409, "A conflicting responsibility was submitted.") from None

    async def completed(self, db: AsyncSession, turn: Turn, session: Session | None) -> None:
        """Called inside the native completion transaction, holding its session fence."""
        if await self.worker_changed(db, turn):
            return
        if (
            session is None or session.visibility != "private" or session.surface != "web"
            or session.owner_id != turn.actor_id or turn.status != "completed" or not turn.result_accepted
            or turn.parent_turn is not None or turn.deferred_results is not None
            or await db.get(ProductAutomationTurn, turn.id) is not None
            or await db.get(GoalTurn, turn.id) is not None
            or await db.get(ResponsibilityAssignment, turn.id) is not None
            or not await db.scalar(select(Responsibility.id).where(
                Responsibility.source_session_id == session.id,
            ).limit(1))
        ):
            return
        authored = await db.scalar(select(Message.id).where(
            Message.turn_id == turn.id, Message.role == "user", Message.author_id == session.owner_id,
            Message.identity == f"user:{turn.id}",
        ))
        answer = await db.scalar(select(Message.id).where(Message.identity == f"answer:{turn.id}"))
        previous = await db.scalar(select(ResponsibilityCompletion.sequence).where(
            ResponsibilityCompletion.turn_id == turn.id,
        ))
        if authored is not None and answer is not None and previous is None:
            sequence = (await db.scalar(select(func.max(ResponsibilityCompletion.sequence)).where(
                ResponsibilityCompletion.session_id == session.id,
            )) or 0) + 1
            db.add(ResponsibilityCompletion(
                sequence=sequence, session_id=session.id, turn_id=turn.id, message_id=answer,
            ))
            # Append-only wake intents do not lock the responsibility or owner
            # while settlement holds the source Session.
            rows = (await db.execute(select(Responsibility.id, Responsibility.generation).where(
                Responsibility.source_session_id == session.id,
                Responsibility.status.in_(["waiting", "running"]),
            ))).all()
            for identity, generation in rows:
                await request_wake(db, identity, generation, f"source:{turn.id}")
            await notify_dispatch(db, self.database.settings)

    async def worker_changed(self, db: AsyncSession, turn: Turn) -> bool:
        assignment = await db.get(ResponsibilityAssignment, turn.id)
        if assignment is None:
            return False
        await request_wake(
            db, assignment.responsibility_id, assignment.generation, f"worker:{turn.id}:{turn.status}",
        )
        await notify_dispatch(db, self.database.settings)
        return True

    async def list(self, actor: User) -> list[ResponsibilityView]:
        async with self.database.sessions() as db:
            rows = list(await db.scalars(select(Responsibility).where(
                Responsibility.owner_id == actor.id,
            ).order_by(Responsibility.created_at.desc()).limit(100)))
            result = []
            for row in rows:
                try:
                    await self.authority(db, row, actor)
                except (PermissionError, HTTPException):
                    continue
                result.append(self.view(row))
            return result

    async def cancel_assignment(
        self, db: AsyncSession, row: Responsibility, *, preserve_accepted: bool = False,
    ) -> None:
        if not row.active_turn_id:
            return
        turn = await db.get(Turn, row.active_turn_id)
        if turn is not None:
            await db.execute(update(Session).where(Session.id == turn.session_id).values(
                revision=Session.revision,
            ))
            await db.refresh(turn)
            if preserve_accepted and turn.result_accepted and turn.status in {"saving", "completed"}:
                raise HTTPException(409, "Your last review is still finishing. Wait for its result before resuming.")
            if turn.status in BUSY:
                await db.execute(update(Session).where(Session.id == turn.session_id).values(
                    status="stopped", revision=Session.revision + 1,
                ))
                turn.status = "stopped"
                turn.finished_at = now()
        row.active_turn_id = None

    async def control(self, actor: User, identity: str, request: ResponsibilityControl) -> ResponsibilityView:
        async with self.database._admission_session(None) as db:
            original = await db.get(Responsibility, identity)
            if original is None:
                raise HTTPException(404, "Responsibility not found.")
            await self.owner_authority(db, original, actor, lock=True)
            row = await self.lock(db, identity)
            assert row is not None
            if request.action == "resume":
                await self.source(db, actor, row.source_session_id, lock=True)
            if row.generation != request.generation:
                raise HTTPException(409, "The responsibility changed. Refresh before controlling it.")
            if request.action == "resume" and row.evaluations >= row.max_evaluations:
                raise HTTPException(409, "The evaluation budget is exhausted. Create a new responsibility to authorize more.")
            if request.action == "resume" and row.status not in {"paused", "blocked"}:
                raise HTTPException(409, "Only a paused or blocked responsibility can resume.")
            await self.cancel_assignment(db, row, preserve_accepted=request.action == "resume")
            if row.status in {"waiting", "running"}:
                await request_wake(db, row.id, row.generation, "stop")
            row.generation += 1
            row.status = "paused" if request.action == "pause" else "waiting"
            row.reason = "Paused by you." if request.action == "pause" else ""
            row.next_check_at = now()
            if request.action == "resume":
                await request_wake(db, row.id, row.generation, "start")
            await notify_dispatch(db, self.database.settings)
            return self.view(row)

    async def authorize(
        self, db: AsyncSession, turn: Turn, *, scope: RunScope | None = None, publication: bool = False,
    ) -> ResponsibilityAssignment | None:
        assignment = await db.get(ResponsibilityAssignment, turn.id)
        if assignment is None:
            if scope is not None and scope.responsibility_id is not None:
                raise PermissionError("The responsibility assignment is unavailable.")
            return None
        row = await db.get(Responsibility, assignment.responsibility_id, populate_existing=True)
        actor = None
        if publication and row is not None:
            # Same order as ordinary submission: canonical authority, User,
            # responsibility, source Session, then the caller's worker Session.
            actor = await self.owner_authority(db, row, lock=True)
            row = await self.lock(db, assignment.responsibility_id)
        if row is None or (
            row.status != "running" or row.generation != assignment.generation
            or row.active_turn_id != turn.id or row.owner_id != turn.actor_id
            or assignment.reconciled or assignment.expires_at <= now()
        ):
            raise PermissionError("The responsibility assignment was paused, expired or superseded.")
        if scope is not None and (
            scope.responsibility_id != row.id or scope.responsibility_generation != row.generation
            or scope.grants or scope.product_grant != self.grant(row)
        ):
            raise PermissionError("The worker's responsibility authority changed.")
        if actor is not None:
            await self.source(db, actor, row.source_session_id, lock=True)
        else:
            await self.authority(db, row)
        return assignment

    def validated_decision(self, assignment: ResponsibilityAssignment, turn: Turn) -> ResponsibilityDecision:
        decision = ResponsibilityDecision.model_validate_json(self.database.security.unseal(turn.result or ""))
        evidence = json.loads(self.database.security.unseal(assignment.evidence))
        messages = {item["message_id"]: item["text"] for item in evidence}
        if any(item.message_id not in messages or item.quote not in messages[item.message_id]
               for item in decision.evidence):
            raise ValueError("The worker cited evidence outside its assignment.")
        return decision

    async def tick(
        self, identity: str, *, generation: int | None = None, closed: bool = False,
    ) -> ResponsibilityStep:
        async with self.database._admission_session(None) as db:
            original = await db.get(Responsibility, identity)
            if original is None or (generation is not None and original.generation != generation):
                return ResponsibilityStep(finished=True)
            actor = None
            try:
                actor = await self.owner_authority(db, original, lock=True)
            except (PermissionError, HTTPException):
                pass
            row = await self.lock(db, identity)
            recovering = closed and row.status == "blocked" and row.reason == CLOSED_CONTROLLER
            if (
                row.status not in {"waiting", "running"} and not recovering
                or generation is not None and row.generation != generation
            ):
                return ResponsibilityStep(finished=True)
            if self.database.product_drain is not None and not await self.database.product_drain.admit(db):
                # Temporal's Activity RetryPolicy owns the wait. No persisted
                # scanner backoff or scheduling mutation during maintenance.
                raise RuntimeError("Responsibility admission is waiting for maintenance to finish.")
            try:
                if actor is None:
                    raise PermissionError("The original requester authority changed.")
                await self.source(db, actor, row.source_session_id, lock=True)
            except (PermissionError, HTTPException):
                await self.cancel_assignment(db, row)
                row.status, row.reason = "blocked", "Source access or the original requester authority changed."
                return ResponsibilityStep(finished=True)
            if closed:
                # Reject new inference/result acceptance after controller loss,
                # but preserve native settlement of an already accepted save.
                row.status, row.reason = "blocked", CLOSED_CONTROLLER
            if row.active_turn_id:
                turn = await db.get(Turn, row.active_turn_id)
                assignment = await db.get(ResponsibilityAssignment, row.active_turn_id)
                if turn is None or assignment is None:
                    row.status, row.reason = "blocked", "The saved assignment is unavailable."
                    return ResponsibilityStep(finished=True)
                # Settlement owns the worker session before changing Turn.
                # Observe its committed result under that same fence.
                await db.execute(update(Session).where(Session.id == turn.session_id).values(
                    revision=Session.revision,
                ))
                await db.refresh(turn)
                if turn.status == "saving" and turn.result_accepted:
                    # Expiry gates new model work and result acceptance. An
                    # on-time accepted result keeps its native settlement path.
                    return ResponsibilityStep()
                if closed and turn.status in BUSY:
                    await self.cancel_assignment(db, row)
                    assignment.reconciled = True
                    return ResponsibilityStep(finished=True)
                if turn.status in BUSY and assignment.expires_at > now():
                    return ResponsibilityStep(wake_at=assignment.expires_at)
                if turn.status != "completed" or not turn.result_accepted:
                    await self.cancel_assignment(db, row)
                    assignment.reconciled = True
                    row.status, row.reason = "blocked", "Worker did not finish. Review its activity before resuming."
                    return ResponsibilityStep(finished=True)
                try:
                    decision = self.validated_decision(assignment, turn)
                except (ValueError, ValidationError):
                    assignment.reconciled = True
                    row.active_turn_id = None
                    row.status, row.reason = "blocked", "Worker returned an invalid or unsupported finding."
                    return ResponsibilityStep(finished=True)
                if decision.action == "notify":
                    evidence = json.loads(self.database.security.unseal(assignment.evidence))
                    fingerprint = hashlib.sha256(json.dumps(
                        [item["text"] for item in evidence], ensure_ascii=False,
                    ).encode()).hexdigest()
                    if not await db.scalar(select(ResponsibilityFinding.turn_id).where(
                        ResponsibilityFinding.responsibility_id == row.id,
                        ResponsibilityFinding.fingerprint == fingerprint,
                    )):
                        db.add(ResponsibilityFinding(
                            turn_id=turn.id, responsibility_id=row.id, fingerprint=fingerprint,
                            decision=self.database.security.seal(decision.model_dump_json()),
                        ))
                assignment.reconciled = True
                row.watermark = assignment.through_sequence
                row.active_turn_id = None
                row.status = "waiting"
            if row.evaluations >= row.max_evaluations:
                row.status, row.reason = "budget_limited", "Evaluation budget exhausted."
                return ResponsibilityStep(finished=True)
            if closed:
                row.status, row.reason = "blocked", CLOSED_CONTROLLER
                return ResponsibilityStep(finished=True)
            completed = (await db.execute(select(ResponsibilityCompletion.sequence, Message).join(
                Message, Message.id == ResponsibilityCompletion.message_id,
            ).where(
                ResponsibilityCompletion.session_id == row.source_session_id,
                ResponsibilityCompletion.sequence > row.watermark,
            ).order_by(ResponsibilityCompletion.sequence).limit(5))).all()
            if not completed:
                return ResponsibilityStep()
            if row.next_check_at > now():
                return ResponsibilityStep(wake_at=row.next_check_at)
            evidence = [{
                "message_id": message.id,
                "text": self.database.security.unseal(message.content).decode()[:6000],
            } for _, message in completed]
            # Truncated evidence is explicit; only exact quoted supplied content may be cited.
            objective = self.database.security.unseal(row.objective).decode()
            prompt = json.dumps({
                "objective": objective, "source_session_id": row.source_session_id,
                "new_messages": evidence,
                "scope": "Each message is limited to its first 6000 characters. Evaluate only this evidence.",
            }, ensure_ascii=False)
            session = await self.database.create_session(actor, NewSession(
                title="Responsibility review", visibility="private", model=row.model,
            ), db=db)
            await db.flush()
            turn = await self.database.admission.submit(actor, session.id, NewMessage(
                prompt=prompt, request_key=f"responsibility:{row.id}:{row.generation}:{completed[-1][0]}",
                model=row.model,
            ), db=db, responsibility_generated=True)
            assignment = ResponsibilityAssignment(
                turn_id=turn.id, responsibility_id=row.id, generation=row.generation,
                through_sequence=completed[-1][0], evidence=self.database.security.seal(json.dumps(evidence)),
                expires_at=due(300),
            )
            db.add(assignment)
            row.active_turn_id = turn.id
            row.next_check_at = due(row.interval_seconds)
            row.evaluations += 1
            row.status, row.reason = "running", ""
            await notify_dispatch(db, self.database.settings)
            return ResponsibilityStep(wake_at=assignment.expires_at)

    async def inbox(self, actor: User) -> list[ResponsibilityInboxItem]:
        async with self.database.sessions() as db:
            rows = (await db.execute(select(ResponsibilityFinding, Responsibility, Turn).join(
                Responsibility, Responsibility.id == ResponsibilityFinding.responsibility_id,
            ).join(Turn, Turn.id == ResponsibilityFinding.turn_id).where(
                Responsibility.owner_id == actor.id,
            ).order_by(ResponsibilityFinding.created_at.desc()).limit(100))).all()
            result = []
            for finding, row, turn in rows:
                try:
                    await self.authority(db, row, actor)
                except (PermissionError, HTTPException):
                    continue
                decision = ResponsibilityDecision.model_validate_json(self.database.security.unseal(finding.decision))
                result.append(ResponsibilityInboxItem(
                    turn_id=turn.id, responsibility_id=row.id, source_session_id=row.source_session_id,
                    worker_session_id=turn.session_id, objective=self.database.security.unseal(row.objective).decode(),
                    summary=decision.summary, evidence=decision.evidence,
                    created_at=finding.created_at, read_at=finding.read_at,
                ))
            return result

    async def mark_read(self, actor: User, turn_id: str) -> None:
        async with self.database.sessions.begin() as db:
            finding = await db.get(ResponsibilityFinding, turn_id)
            row = await db.get(Responsibility, finding.responsibility_id) if finding else None
            if finding is None or row is None:
                raise HTTPException(404, "Finding not found.")
            await self.authority(db, row, actor)
            finding.read_at = finding.read_at or now()

    async def history(self, actor: User, identity: str) -> list[ResponsibilityReview]:
        """A bounded audit surface; no generic chat tools or raw reasoning export."""
        async with self.database.sessions.begin() as db:
            row = await db.get(Responsibility, identity)
            if row is None:
                raise HTTPException(404, "Responsibility not found.")
            await self.owner_authority(db, row, actor, lock=True)
            row = await self.lock(db, identity)
            assert row is not None
            await self.source(db, actor, row.source_session_id, lock=True)
            reviews = (await db.execute(select(ResponsibilityAssignment, Turn).join(
                Turn, Turn.id == ResponsibilityAssignment.turn_id,
            ).where(ResponsibilityAssignment.responsibility_id == identity).order_by(
                Turn.created_at.desc(), Turn.id,
            ).limit(100))).all()
            result = []
            for assignment, turn in reviews:
                decision = None
                validation = "pending" if turn.result_accepted else "not_accepted"
                if turn.status == "completed" and turn.result_accepted:
                    try:
                        validated = self.validated_decision(assignment, turn)
                    except ValueError:
                        validation = "invalid"
                    else:
                        if assignment.reconciled:
                            validation, decision = "valid", validated
                result.append(ResponsibilityReview.model_validate({
                    "turn_id": turn.id, "generation": assignment.generation, "through_sequence": assignment.through_sequence,
                    "status": turn.status, "reconciled": assignment.reconciled, "accepted": bool(turn.result_accepted),
                    "created_at": turn.created_at, "finished_at": turn.finished_at, "error": turn.error,
                    "usage": json.loads(turn.usage), "validation": validation, "decision": decision,
                }))
            return result
