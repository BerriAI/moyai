"""Goal controls and idempotent continuation through ordinary native turns.

Every writer fences the requester, then the session. The dispatcher reads only
settled native results and admits the next turn in that same transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import NewMessage, RunScope
from .delegation_models import DelegatedSession
from .goal_models import Goal, GoalReceipt, GoalTurn
from .models import Effect, Message, Session, Turn, User, now
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
    action = "edit" if value == "edit" or value.startswith("edit ") else "set"
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
            "objective": self.database.security.unseal(goal.objective).decode(),
            "status": goal.status, "generation": goal.generation, "owner_id": goal.owner_id,
            "token_budget": goal.token_budget, "tokens_used": goal.tokens_used,
            "turns": goal.turns, "blocked_count": goal.blocked_count, "created_at": goal.created_at,
            "reason": self.database.security.unseal(goal.reason).decode() if goal.reason else "",
        }

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

    async def halt(self, db: AsyncSession, session: Session, goal: Goal) -> None:
        # Reuse native Stop's authority fence and cancellation reconciliation.
        # A saving answer keeps its settlement; it cannot schedule more work.
        bound = select(GoalTurn.turn_id).where(
            GoalTurn.session_id == session.id, GoalTurn.generation == goal.generation,
        )
        active_binding = await db.get(GoalTurn, session.active_turn) if session.active_turn else None
        if (
            active_binding is not None and active_binding.generation == goal.generation
            and session.status not in {"saving", "waiting"}
        ):
            session.status = "stopped"
        await db.execute(update(Turn).where(
            Turn.id.in_(bound), Turn.status.in_(tuple(s for s in BUSY if s not in {"saving", "waiting"})),
        ).values(status="stopped", finished_at=now()))

    async def command(self, actor: User, session_id: str, body: NewMessage) -> dict[str, Any]:
        action, objective, budget = parse_command(body.prompt)
        if body.attachments or body.reply_to:
            raise HTTPException(422, "Send goal controls separately from files and question answers.")
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
            if action == "set":
                if goal is not None and goal.status not in {"complete", "cleared"}:
                    raise HTTPException(409, "A goal already exists. Use /goal edit, /goal resume, or /goal clear.")
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
                turn = await self.enqueue(db, actor, goal, body.request_key, objective, generated=False)
                goal.source_turn_id = turn.id
            else:
                if goal is None or goal.status == "cleared":
                    raise HTTPException(409, "There is no goal in this conversation. " + HELP)
                if action == "pause":
                    self.change(goal, "paused", "Paused by you.")
                    await self.halt(db, session, goal)
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
                        goal.generation += 1
                        goal.blocked_count, goal.blocker = 0, ""
                        self.change(goal, "active")
            await db.flush()
            result: dict[str, Any] = {"goal": self.view(goal)}
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
        if RunScope(session.id, source.id, actor.id, goal.model, grants).grants_key != goal.grants_key:
            raise HTTPException(403, "The goal's original account permissions changed. Clear it and set a new goal.")
        return actor

    async def bind(self, db: AsyncSession, turn: Turn) -> None:
        goal = await db.get(Goal, turn.session_id, populate_existing=True)
        if goal is None or goal.status != "active":
            return
        if (
            turn.actor_id != goal.owner_id or turn.model != goal.model
            or set(json.loads(turn.connections)) != set(json.loads(goal.connections))
        ):
            self.change(goal, "paused", "The requester, model, or selected accounts changed. Resume with the original context.")
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
            or goal.status != "active" or goal.generation != binding.generation
            or goal.owner_id != actor.id or goal.auth_version != actor.auth_version
        ):
            raise PermissionError("This goal execution was paused, superseded, or lost its requester authority.")
        return goal

    async def enqueue(
        self, db: AsyncSession, actor: User, goal: Goal, key: str, prompt: str, *, generated: bool,
    ) -> Turn:
        turn = await self.database.submit(actor, goal.session_id, NewMessage(
            prompt=prompt, request_key=key, model=goal.model, connections=json.loads(goal.connections),
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
                self.change(goal, "paused", "The conversation was archived or deleted.")
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
