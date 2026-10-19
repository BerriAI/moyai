"""Root-agent goal tools; completion takes effect only after answer settlement."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.toolsets import FunctionToolset

from .contracts import RunScope
from .goal_models import Goal, GoalReceipt, GoalTurn
from .goals import Goals
from .input_identity import input_identity
from .models import User

INSTRUCTIONS = """
This conversation has an explicit persistent goal. Call get_goal to inspect it.
Keep working until that objective is achieved. The host will continue after a
saved turn while the goal is active. Incorporate follow-up messages as steering;
do not replace the objective or consider a status question a cancellation.
When get_goal reports paused but this turn remains authorized, finish the
current admitted work. Future automatic turns stay paused; do not resume them.
An explicit human Pause or Stop separately revokes execution authority.
Use update_goal(status="complete", evidence=...) only after verifying the whole
objective is achieved and no required work remains. Then provide the final answer.
If the same blocker prevents all useful progress, report it with
update_goal(status="blocked", evidence=...) using the same concise blocker text
on consecutive blocked turns. The host requires three consecutive blocked turns,
including the original turn, before stopping. A resumed goal starts a fresh audit.
Do not claim a blocker merely because work is difficult, slow, or incomplete.
Use the question tool when a required answer is missing. Never bypass approvals.
Do not mark a goal complete because its budget is nearly exhausted. Token limits
take precedence; report observed usage. Only the human can pause, edit, resume,
or clear a goal. Explain /goal pause when they want to pause.
"""


class InactiveGoal(AbstractCapability[RunScope]):
    id = "moyai_goal_v1"


class GoalCapability(AbstractCapability[RunScope]):
    id = "moyai_goal_v1"

    def __init__(self, service: Goals):
        tools: FunctionToolset[RunScope] = FunctionToolset(id=self.id)

        async def operate(
            ctx: RunContext[RunScope], status: str | None = None, evidence: str = "",
        ) -> dict[str, Any]:
            scope = ctx.deps
            if ctx.run_id != scope.root_run_id or not ctx.tool_call_id or scope.goal_generation is None:
                raise ToolFailed("Only the goal's current root agent can control it.")
            database = service.database
            input_id = (ctx.metadata or {}).get("moyai_input_id") or input_identity(ctx)
            await database.authorize_scope(scope, input_id=input_id)
            async with database.sessions.begin() as db:
                actor = await db.get(User, scope.actor_id)
                if actor is None:
                    raise ToolFailed("The goal requester is unavailable.")
                if scope.product_grant is not None:
                    from typing import cast
                    cast(Any, actor).product_grant = scope.product_grant
                if status is not None:
                    await service.lock(db, actor, scope.session_id)
                await database.authorize_scope(scope, input_id=input_id, db=db)
                goal = await db.get(Goal, scope.session_id, populate_existing=True)
                binding = await db.get(GoalTurn, scope.turn_id)
                if (
                    goal is None or binding is None or goal.owner_id != scope.actor_id
                    or goal.status not in {"active", "paused"} or goal.generation != scope.goal_generation
                    or binding.generation != goal.generation
                ):
                    raise ToolFailed("The goal was cleared or changed. Stop and follow the current user direction.")
                if status is None:
                    return {"goal": service.view(goal)}
                identity = hashlib.sha256(f"goal-tool:{scope.turn_id}:{ctx.run_id}:{ctx.tool_call_id}".encode()).hexdigest()
                fingerprint = hashlib.sha256(json.dumps([status, evidence]).encode()).hexdigest()
                previous = await db.get(GoalReceipt, identity)
                if previous is not None:
                    if previous.fingerprint != fingerprint:
                        raise ToolFailed("This tool identity already belongs to another goal update.")
                    return json.loads(database.security.unseal(previous.result))
                binding.decision = status
                binding.evidence = database.security.seal(evidence)
                result = {
                    "goal": service.view(goal), "pending_status": status,
                    "instruction": "Return your answer. Automatic continuation remains paused; this report does not resume it."
                    if goal.status == "paused" else
                    "Return your answer. The host applies this decision only after the result is saved."
                    if status == "complete" else
                    "Return the blocker and any progress. The host stops only after three consecutive turns report the same blocker.",
                }
                db.add(GoalReceipt(
                    id=identity, session_id=scope.session_id, fingerprint=fingerprint,
                    result=database.security.seal(json.dumps(result)),
                ))
                return result

        @tools.tool
        async def get_goal(ctx: RunContext[RunScope]) -> dict[str, Any]:
            """Read this conversation's objective, status, token budget, usage, and blocked-turn count."""
            return await operate(ctx)

        @tools.tool
        async def update_goal(
            ctx: RunContext[RunScope], status: Literal["complete", "blocked"],
            evidence: Annotated[str, Field(min_length=1, max_length=4000)],
        ) -> dict[str, Any]:
            """Report a verified achievement or an identical recurring blocker, then return the final answer.

            Complete only when the whole objective is achieved. Blocked requires the
            same obstruction on three consecutive turns and no useful independent
            work. Do not self-pause; the human controls pause/resume.
            """
            return await operate(ctx, status, evidence)

        self.tools = tools

    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        if ctx.deps.goal_generation is None or ctx.run_id != ctx.deps.root_run_id:
            return InactiveGoal()
        return self

    def get_toolset(self) -> FunctionToolset[RunScope]:
        return self.tools

    def get_instructions(self) -> str:
        return INSTRUCTIONS
