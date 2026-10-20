"""Goal controls and idempotent continuation through ordinary native turns.

Every writer fences the requester, then the session. The dispatcher reads only
settled native results and admits the next turn in that same transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import NewMessage, RunScope
from .delegation_models import DelegatedSession
from .goal_models import Goal, GoalReceipt, GoalTurn
from .models import Effect, Message, MessageSource, Session, Turn, TurnImage, User, now
from .swarm_models import SwarmMember

if TYPE_CHECKING:
    from .database import Database

BUSY = ("queued", "starting", "running", "saving", "waiting", "steering", "signal_sent", "accepted")
HELP = "Use /goal <objective>, /goal edit <objective>, /goal pause, /goal resume, or /goal clear."
COMMAND = re.compile(r"^/goal(?:\s|$)")


def is_goal_command(prompt: str) -> bool:
    return not prompt.startswith(("    ", "\t")) and bool(COMMAND.match(prompt.lstrip()))


def parse_command(prompt: str) -> tuple[str, str, int | None]:
    value = prompt.strip()[5:].strip()
    if not value:
        return "view", "", None
    if value in {"pause", "resume", "clear"}:
        return value, "", None
    action = "edit" if re.match(r"edit(?:\s|$)", value) else "set"
    if action == "edit":
        value = value[4:].strip()
    budget = None
    if value.startswith("--budget"):
        match = re.match(r"--budget\s+([1-9]\d{0,8}|none)\s+(.+)", value, re.DOTALL)
        if not match:
            raise HTTPException(422, "Use /goal --budget <positive token count> <objective>.")
        budget, value = int(match[1]) if match[1] != "none" else None, match[2].strip()
    if not value or len(value) > 4000:
        raise HTTPException(422, "A goal objective must contain 1–4,000 characters. " + HELP)
    return action, value, budget


class Goals:
    def __init__(self, database: Database):
        self.database = database

    def view(self, goal: Goal | None) -> dict[str, Any] | None:
        if goal is None or goal.status == "cleared":
            return None
        return {
            "objective": self.database.security.unseal(goal.objective).decode(), "elapsed_seconds": 0,
            "status": goal.status, "generation": goal.generation, "owner_id": goal.owner_id,
            "token_budget": goal.token_budget, "tokens_used": goal.tokens_used,
            "turns": goal.turns, "blocked_count": goal.blocked_count, "created_at": goal.created_at,
            "reason": self.database.security.unseal(goal.reason).decode() if goal.reason else "",
        }

    async def mark_source(self, db: AsyncSession, turn_id: str) -> None:
        source = await db.get(MessageSource, f"user:{turn_id}")
        if source is not None:
            provenance = json.loads(self.database.security.unseal(source.data))
            source.data = self.database.security.seal({**provenance, "sent_as_goal": True})

    async def elapsed_seconds(self, db: AsyncSession, goal: Goal) -> float:
        rows = (await db.execute(select(Turn.accepted_at, Turn.finished_at).join(
            GoalTurn, GoalTurn.turn_id == Turn.id,
        ).where(
            GoalTurn.session_id == goal.session_id, Turn.created_at >= goal.created_at,
            Turn.accepted_at.is_not(None), Turn.preparation.is_not(None), Turn.status != "included",
        ))).all()
        end = datetime.now(UTC) if goal.status == "active" else datetime.fromisoformat(goal.checked_at)
        return sum(max(0.0, ((datetime.fromisoformat(finished) if finished else end)
                            - datetime.fromisoformat(started)).total_seconds())
                   for started, finished in rows)

    async def lock(self, db: AsyncSession, actor: User, session_id: str) -> Session:
        await self.database._actor_product_grant(db, actor)
        authorized = await db.scalar(update(User).where(
            User.id == actor.id, User.enabled.is_(True), User.auth_version == actor.auth_version,
        ).values(auth_version=User.auth_version).returning(User.id))
        if authorized is None:
            raise HTTPException(401, "Your access changed. Sign in again.")
        await self.database.allowed_session(db, session_id, actor)
        await db.execute(update(Session).where(
            Session.id == session_id,
        ).values(revision=Session.revision + 1))
        session = await db.get(Session, session_id, populate_existing=True)
        assert session is not None
        return session

    def change(self, goal: Goal, status: str, reason: str = "") -> None:
        goal.status = status
        goal.reason = self.database.security.seal(reason) if reason else ""
        goal.checked_at = now()

    async def halt(self, db: AsyncSession, session: Session, goal: Goal, *, keep_queued: bool = False) -> None:
        # Reuse native Stop's authority fence and cancellation reconciliation.
        # A saving answer keeps its settlement; it cannot schedule more work.
        bound = select(GoalTurn.turn_id).where(
            GoalTurn.session_id == session.id, GoalTurn.generation == goal.generation,
        )
        active_binding = await db.get(GoalTurn, session.active_turn) if session.active_turn else None
        if (
            active_binding is not None and active_binding.generation == goal.generation
            and session.status != "saving" and (not keep_queued or session.status != "waiting")
        ):
            session.status = "stopped"
        stopped = list(await db.scalars(update(Turn).where(
            Turn.id.in_(bound), Turn.status.in_(tuple(
                s for s in BUSY if s != "saving" and (not keep_queued or s not in {"queued", "waiting"})
            )),
        ).values(status="stopped", finished_at=now()).returning(Turn.id)))
        if keep_queued and stopped:
            # An intentional pause is not an execution failure on Resume when
            # retained user input requires keeping the current generation.
            await db.execute(update(GoalTurn).where(
                GoalTurn.turn_id.in_(stopped),
            ).values(decision="paused"))

    async def pause_continuation(self, db: AsyncSession, goal: Goal, reason: str) -> None:
        """Hold future automatic work without revoking an already admitted turn."""
        self.change(goal, "paused", reason)
        cancelled = list(await db.scalars(update(Turn).where(
            Turn.id.in_(select(GoalTurn.turn_id).where(
                GoalTurn.session_id == goal.session_id, GoalTurn.generation == goal.generation,
                GoalTurn.generated.is_(True),
            )),
            Turn.status == "queued",
        ).values(status="stopped", finished_at=now()).returning(Turn.id)))
        if cancelled:
            await db.execute(update(GoalTurn).where(
                GoalTurn.turn_id.in_(cancelled),
            ).values(settled=True))

    async def command(self, actor: User, session_id: str, body: NewMessage) -> dict[str, Any]:
        action, objective, budget = parse_command(body.prompt)
        if body.reply_to or (body.attachments and action != "set"):
            raise HTTPException(422, "Attach files when starting a goal; send goal controls separately from files and question answers.")
        identity = hashlib.sha256(f"goal:{session_id}:{actor.id}:{body.request_key}".encode()).hexdigest()
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        async with self.database._admission_session(None) as db:
            session = await self.lock(db, actor, session_id)
            if session.owner_id != actor.id:
                raise HTTPException(403, "Only the conversation owner can control its goal.")
            if session.surface != "web" or await db.get(SwarmMember, session_id) or await db.get(DelegatedSession, session_id):
                raise HTTPException(409, "Use /goal in an ordinary web conversation.")
            previous = await db.get(GoalReceipt, identity)
            if previous is not None:
                if previous.fingerprint != fingerprint:
                    raise HTTPException(409, "This request identity already belongs to another goal command.")
                return json.loads(self.database.security.unseal(previous.result))
            goal = await db.get(Goal, session_id, populate_existing=True)
            turn: Turn | None = None
            if action == "view":
                return {"goal": self.view(goal), "help": HELP}
            if session.archived and action in {"set", "resume"}:
                raise HTTPException(409, "Unarchive the conversation before starting or resuming a goal.")
            if action == "set":
                if goal is not None:
                    if goal.status not in {"complete", "cleared"}:
                        await self.halt(db, session, goal)
                    # Preserve the label on goals created before provenance was recorded.
                    if goal.source_turn_id:
                        await self.mark_source(db, goal.source_turn_id)
                grants = await self.database.resolve_grants(
                    db, actor.id, body.connections, shared_session=session.visibility == "shared",
                    surface=session.surface,
                )
                model = body.model or session.model
                if goal is None:
                    goal = Goal(session_id=session_id, generation=1)
                    db.add(goal)
                else:
                    goal.generation += 1
                goal.owner_id, goal.auth_version = actor.id, actor.auth_version
                goal.objective, goal.model = self.database.security.seal(objective), model
                goal.connections = json.dumps([grant.id for grant in grants])
                goal.grants_key = RunScope(session_id, "", actor.id, model, grants).grants_key
                goal.token_budget, goal.tokens_used, goal.turns = budget, 0, 0
                goal.blocked_count, goal.blocker, goal.source_turn_id = 0, "", None
                goal.created_at = now()
                self.change(goal, "active")
                await db.flush()
                turn = await self.enqueue(
                    db, actor, goal, body.request_key, objective, generated=False, attachments=body.attachments,
                )
                goal.source_turn_id = turn.id
                # Keep the original goal message identifiable after replacement/clear.
                await self.mark_source(db, turn.id)
            else:
                if goal is None or goal.status == "cleared":
                    raise HTTPException(409, "There is no goal in this conversation. " + HELP)
                if action == "pause":
                    await self.pause_continuation(db, goal, "Paused by you.")
                    await self.halt(db, session, goal, keep_queued=True)
                elif action == "clear":
                    self.change(goal, "cleared")
                    await self.halt(db, session, goal)
                elif action == "edit":
                    # Fence stale tool results before replacing the objective.
                    active = goal.status == "active"
                    await self.halt(db, session, goal)
                    goal.generation += 1
                    goal.objective = self.database.security.seal(objective)
                    if re.match(r"^/goal\s+edit\s+--budget\s", body.prompt.strip()):
                        goal.token_budget = budget
                    goal.blocked_count, goal.blocker = 0, ""
                    self.change(goal, "active" if active else "paused",
                                "Goal updated." if active else "Goal updated. Resume when ready.")
                elif action == "resume":
                    if goal.status == "complete":
                        raise HTTPException(409, "This goal is complete. Set a new objective with /goal.")
                    await self.saved_actor(db, goal)
                    if goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
                        raise HTTPException(409, "The token budget is exhausted. Edit the goal with a larger --budget.")
                    if goal.status != "active":
                        # Admission can precede active_turn ownership. Preserve
                        # every pending goal input, including queued objectives,
                        # deferred replies and steering, until it has settled.
                        pending = await db.scalar(select(GoalTurn.turn_id).join(
                            Turn, Turn.id == GoalTurn.turn_id,
                        ).where(
                            GoalTurn.session_id == session_id, GoalTurn.generation == goal.generation,
                            Turn.status.in_(BUSY),
                        ).limit(1))
                        if pending is None:
                            goal.generation += 1
                        goal.blocked_count, goal.blocker = 0, ""
                        self.change(goal, "active")
            await db.flush()
            result: dict[str, Any] = {"goal": self.view(goal)}
            if result["goal"] is not None:
                result["goal"]["elapsed_seconds"] = await self.elapsed_seconds(db, goal)
            if turn is not None:
                from .api import turn_view
                result.update(turn_view(turn))
            db.add(GoalReceipt(
                id=identity, session_id=session_id, fingerprint=fingerprint,
                result=self.database.security.seal(json.dumps(result)),
            ))
            return result

    async def saved_actor(self, db: AsyncSession, goal: Goal) -> User:
        actor = await db.get(User, goal.owner_id, populate_existing=True)
        source = await db.get(Turn, goal.source_turn_id) if goal.source_turn_id else None
        if actor is None or not actor.enabled or actor.auth_version != goal.auth_version or source is None:
            raise HTTPException(403, "The goal's original requester access changed. Clear it and set a new goal.")
        if goal.model not in self.database.settings.models:
            raise HTTPException(403, "The goal's original model is no longer enabled.")
        grant = await self.database._saved_product_grant(db, source)
        if grant is not None:
            cast(Any, actor).product_grant = grant
        await self.database._check_product_turn(db, source, grant)
        session = await self.database.allowed_session(db, goal.session_id, actor)
        grants = await self.database.resolve_grants(
            db, actor.id, json.loads(goal.connections),
            shared_session=session.visibility == "shared", surface=session.surface,
        )
        scope = RunScope(session.id, source.id, actor.id, goal.model, grants, session.visibility, session.surface)
        if scope.grants_key != goal.grants_key:
            raise HTTPException(403, "The goal's original account permissions changed. Clear it and set a new goal.")
        if session.workspace and session.workspace_scope != scope.workspace_key:
            raise HTTPException(403, "The goal's original computer account context changed. Clear it and set a new goal.")
        return actor

    async def bind(self, db: AsyncSession, turn: Turn, *, resumed_from: str | None = None) -> None:
        goal = await db.get(Goal, turn.session_id, populate_existing=True)
        if goal is None:
            return
        if goal.status != "active":
            previous = await db.get(GoalTurn, resumed_from) if resumed_from and goal.status == "paused" else None
            if previous is None or previous.generation != goal.generation:
                return
            # Accepting an answer preserves the paused goal's input; it does
            # not authorize dispatch until the owner resumes the goal.
        if (
            turn.actor_id != goal.owner_id or turn.model != goal.model
            or set(json.loads(turn.connections)) != set(json.loads(goal.connections))
        ):
            await self.pause_continuation(
                db, goal, "The requester, model, or selected accounts changed. "
                "The current turn can finish; automatic continuation is paused. Resume with the original context.",
            )
            return
        db.add(GoalTurn(turn_id=turn.id, session_id=goal.session_id, generation=goal.generation))

    async def authorize(self, db: AsyncSession, turn: Turn) -> Goal | None:
        binding = await db.get(GoalTurn, turn.id)
        if binding is None:
            return None
        goal = await db.get(Goal, binding.session_id, populate_existing=True)
        actor = await db.get(User, turn.actor_id, populate_existing=True)
        if (
            goal is None or actor is None or not actor.enabled
            or goal.status not in {"active", "paused"} or goal.generation != binding.generation
            or goal.owner_id != actor.id or goal.auth_version != actor.auth_version
        ):
            raise PermissionError("This goal execution was superseded or lost its requester authority.")
        # Native turn/session status enforces explicit Pause and Stop. A paused
        # continuation alone does not revoke a turn already admitted to run.
        return goal

    async def invalidate_for_guidance(self, db: AsyncSession, turn: Turn) -> None:
        """New steering supersedes an earlier report under the admission session fence."""
        if turn.parent_turn is not None:
            await db.execute(update(GoalTurn).where(
                GoalTurn.turn_id == turn.parent_turn,
            ).values(decision=None, evidence=""))

    async def enqueue(
        self, db: AsyncSession, actor: User, goal: Goal, key: str, prompt: str, *, generated: bool,
        attachments: list[str] | None = None,
    ) -> Turn:
        if generated and attachments is None:
            attachments = await self.initial_attachments(db, goal)
        turn = await self.database.submit(actor, goal.session_id, NewMessage(
            prompt=prompt, request_key=key, model=goal.model, connections=json.loads(goal.connections),
            attachments=attachments or [],
        ), db=db)
        await db.flush()
        binding = await db.get(GoalTurn, turn.id)
        assert binding is not None
        binding.generated = generated
        if generated:
            await db.execute(update(Message).where(
                Message.turn_id == turn.id, Message.role == "user",
            ).values(role="system", author_id=None, identity=f"goal:{turn.id}"))
        return turn

    async def initial_attachments(self, db: AsyncSession, goal: Goal) -> list[str]:
        """Retain original inputs across edits and context resets without repeating them every turn."""
        session = await db.get(Session, goal.session_id)
        source = select(TurnImage.upload_id).where(
            TurnImage.turn_id == goal.source_turn_id,
        ).order_by(TurnImage.position)
        if session and (
            session.conversation and session.last_actor_id == goal.owner_id
            and session.last_model == goal.model and session.last_grants == goal.grants_key
        ):
            delivered = select(TurnImage.upload_id).join(Turn, Turn.id == TurnImage.turn_id).join(
                GoalTurn, GoalTurn.turn_id == Turn.id,
            ).where(
                GoalTurn.session_id == goal.session_id, Turn.created_at >= goal.created_at,
                Turn.result_accepted.is_(True),
            )
            source = source.where(TurnImage.upload_id.not_in(delivered))
        return list(await db.scalars(source))

    async def tick(self, session_id: str) -> None:
        # Merely inspecting an active goal must not wake this dispatcher again.
        # enqueue/submit emits a wake-up only when a new turn is admitted.
        async with self.database.sessions.begin() as db:
            goal = await db.get(Goal, session_id)
            if goal is None or goal.status != "active":
                return
            try:
                actor = await self.saved_actor(db, goal)
                session = await self.lock(db, actor, session_id)
                await db.refresh(goal)
                if goal.status != "active":
                    return
                await self.saved_actor(db, goal)
            except (HTTPException, PermissionError):
                # Use the same session fence even when the saved actor was revoked.
                await db.execute(update(Session).where(
                    Session.id == session_id,
                ).values(revision=Session.revision + 1))
                await db.refresh(goal)
                if goal.status == "active":
                    self.change(goal, "blocked", "Requester or account access changed. Clear the goal and start again.")
                return
            goal.checked_at = now()
            if session.deleted or session.archived:
                await self.pause_continuation(db, goal, "The conversation was archived or deleted.")
                return
            if not session.active_turn and await db.scalar(select(Effect.id).where(
                Effect.session_id == session_id, Effect.state == "pending",
            ).limit(1)):
                self.change(goal, "blocked", "An external action has an unconfirmed outcome. Resolve it before resuming.")
                return
            settled = (await db.execute(select(GoalTurn, Turn).join(
                Turn, Turn.id == GoalTurn.turn_id,
            ).where(
                GoalTurn.session_id == session_id, GoalTurn.settled.is_(False),
                Turn.status.not_in(BUSY),
            ).order_by(Turn.created_at, Turn.id))).all()
            for binding, turn in settled:
                binding.settled = True
                usage = json.loads(turn.usage)
                if turn.created_at >= goal.created_at:
                    goal.tokens_used += max(0, int(usage.get("input_tokens", 0))) + max(0, int(usage.get("output_tokens", 0)))
                # Edits/resume cannot give old decisions authority over the new objective.
                if binding.generation != goal.generation:
                    continue
                if turn.status in {"included", "resumed"}:
                    continue
                if turn.status == "stopped" and binding.decision == "paused":
                    continue
                goal.turns += 1
                if turn.status != "completed" or not turn.result_accepted:
                    self.change(goal, "blocked", "The last execution stopped or failed. Inspect it before resuming.")
                    return
                if goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
                    self.change(goal, "budget_limited", "The goal's token budget was reached.")
                    return
                if binding.decision == "complete":
                    newer = await db.scalar(select(Turn.id).join(
                        GoalTurn, GoalTurn.turn_id == Turn.id,
                    ).where(
                        GoalTurn.session_id == session_id, GoalTurn.generation == goal.generation,
                        GoalTurn.generated.is_(False), Turn.created_at > turn.created_at,
                        Turn.status != "stopped",
                        # Included steering was handled by this accepted result.
                        # Admission already invalidated any report made before
                        # that guidance, including an older queued "Send now".
                        or_(Turn.status != "included", Turn.parent_turn.is_distinct_from(turn.id)),
                    ).limit(1))
                    if newer is None:
                        self.change(goal, "complete", self.database.security.unseal(binding.evidence).decode())
                        return
                if binding.decision == "blocked":
                    evidence = self.database.security.unseal(binding.evidence).decode()
                    key = hashlib.sha256(" ".join(evidence.lower().split()).encode()).hexdigest()
                    goal.blocked_count = goal.blocked_count + 1 if goal.blocker == key else 1
                    goal.blocker = key
                    if goal.blocked_count >= 3:
                        self.change(goal, "blocked", evidence)
                        return
                else:
                    goal.blocked_count, goal.blocker = 0, ""
            if session.active_turn or await db.scalar(select(Turn.id).where(
                Turn.session_id == session_id, Turn.status.in_(BUSY),
            ).limit(1)):
                return
            if goal.token_budget is not None and goal.tokens_used >= goal.token_budget:
                self.change(goal, "budget_limited", "The goal's token budget was reached.")
                return
            if self.database.product_drain is not None and not await self.database.product_drain.admit(db):
                return
            objective = self.database.security.unseal(goal.objective).decode()
            await self.enqueue(
                db, actor, goal, f"goal:{goal.generation}:{goal.turns}",
                "[Host-generated goal continuation; not a new human request.]\n"
                f"Continue pursuing the existing goal:\n{objective}\n\n"
                "Read get_goal and the latest human guidance. Continue useful unfinished work. "
                "When verified complete, call update_goal(status='complete', evidence=...) and report the result. "
                "Do not invent new work, repeat uncertain external actions, or stop merely because a turn ended.",
                generated=True,
            )

    async def reconcile(self, limit: int = 20) -> None:
        async with self.database.sessions() as db:
            pending = list(await db.scalars(select(Goal.session_id).join(
                Session, Session.id == Goal.session_id,
            ).where(
                Goal.status == "active", Session.active_turn.is_(None),
            ).order_by(Goal.checked_at, Goal.session_id).limit(limit)))
        for session_id in pending:
            await self.tick(session_id)


def dispatchable():
    """Keep paused user inputs queued; already admitted turns use native authority."""
    return ~exists(select(GoalTurn.turn_id).where(GoalTurn.turn_id == Turn.id)) | exists(
        select(GoalTurn.turn_id).join(Goal, Goal.session_id == GoalTurn.session_id).join(
            Session, Session.id == Goal.session_id,
        ).where(
            GoalTurn.turn_id == Turn.id, GoalTurn.generation == Goal.generation, Goal.status == "active",
            Session.archived.is_(False), Session.deleted.is_(False),
        )
    )
