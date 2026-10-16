"""Durable mission coordination; all model/tool execution uses TurnWorkflow."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from temporalio import activity, workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from fastapi import HTTPException
    from sqlalchemy import and_, select, update

    from .models import Session, Turn, now
    from .swarm_contracts import MissionRun, MissionTick
    from .swarm_models import SwarmMember, SwarmMission, SwarmPost, SwarmTurn
    from .swarm_service import SwarmService, expired

if TYPE_CHECKING:
    from .runtime import Runtime

CONTROL = {
    "start_to_close_timeout": timedelta(seconds=60),
    "schedule_to_close_timeout": timedelta(minutes=3),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}


async def tick(service: SwarmService, run: MissionRun) -> MissionTick:
    async with service.database._admission_session(None) as db:
        mission = await db.get(SwarmMission, run.mission_id)
        if mission is None or mission.status != "active" or mission.generation != run.generation:
            return MissionTick(exists=False)
        try:
            actor = await service.saved_actor(db, mission)
            mission = await service.lock(db, mission.id, actor)
            # A competing control may have won while this transaction waited.
            if mission.status != "active" or mission.generation != run.generation:
                return MissionTick(exists=False)
            await service.require_context(db, mission, actor)
        except (HTTPException, PermissionError):
            mission = await service.lock(db, run.mission_id)
            if mission.status == "active" and mission.generation == run.generation:
                await service.stop_in(
                    db, mission, "attention", "The original requester or account access changed. Start a new mission.",
                )
            return MissionTick(exists=False)
        if expired(mission):
            await service.stop_in(db, mission, "paused", "The work window ended. Your team's saved work is preserved.")
            return MissionTick(exists=False)
        if service.database.product_drain is not None and not await service.database.product_drain.admit(db):
            return MissionTick()

        human_inputs = list(await db.scalars(select(Turn).join(
            SwarmTurn, SwarmTurn.turn_id == Turn.id,
        ).outerjoin(SwarmPost, and_(
            SwarmPost.mission_id == mission.id, SwarmPost.identity == "human:" + Turn.id,
        )).where(
            SwarmTurn.mission_id == mission.id, SwarmTurn.generated.is_(False),
            Turn.id != mission.source_turn_id, SwarmPost.id.is_(None),
            Turn.status.in_(["running", "accepted", "included", "completed", "saving", "waiting", "resumed"]),
        ).order_by(Turn.created_at, Turn.id).limit(100)))
        for turn in human_inputs:
            member = await db.get(SwarmMember, turn.session_id)
            assert member is not None
            await service.post(
                db, mission, "human",
                f"Human direction to {member.name}:\n{service.database.security.unseal(turn.prompt).decode()}",
                f"human:{turn.id}", None,
            )
        members = list(await db.scalars(select(SwarmMember).where(
            SwarmMember.mission_id == mission.id,
        ).order_by(SwarmMember.position)))
        sessions = {member.session_id: await db.get(Session, member.session_id) for member in members}
        if any(session is None or session.deleted for session in sessions.values()):
            await service.stop_in(db, mission, "attention", "A team conversation was deleted. Inspect the saved mission.")
            return MissionTick(exists=False)
        completed = (await db.execute(select(SwarmTurn, Turn).join(
            Turn, Turn.id == SwarmTurn.turn_id,
        ).where(
            SwarmTurn.mission_id == mission.id, SwarmTurn.reported.is_(False),
            Turn.status.in_(["completed", "failed", "stopped", "waiting", "saving"]),
        ).order_by(Turn.created_at, Turn.id).limit(100))).all()
        for binding, turn in completed:
            if turn.status == "saving":
                continue  # The computer/result settlement owns this boundary.
            binding.reported = True
            if turn.session_id == mission.id:
                continue  # Coordinator replies already live in the root chat.
            if turn.status == "completed" and turn.result_accepted and turn.result:
                answer = service.database.security.unseal(turn.result).decode()
                await service.post(
                    db, mission, "result", answer[:20_000], f"answer:{turn.id}", turn.session_id,
                )
            elif turn.status in {"failed", "waiting"}:
                await service.post(
                    db, mission, "attention",
                    "This member needs a human answer." if turn.status == "waiting" else
                    "This member failed. Inspect its saved activity before explicitly continuing it.",
                    f"status:{turn.id}:{turn.status}", turn.session_id,
                )

        async def idle(member: SwarmMember) -> bool:
            session = sessions[member.session_id]
            if session is None or session.active_turn or session.status in {"failed", "stopped", "waiting"}:
                return False
            return await db.scalar(select(Turn.id).where(
                Turn.session_id == member.session_id, Turn.status == "queued",
            ).limit(1)) is None

        leader = next((member for member in members if member.session_id == mission.leader_id), None)
        if leader is not None and await idle(leader):
            unseen = await db.scalar(select(SwarmPost.sequence).where(
                SwarmPost.mission_id == mission.id, SwarmPost.sequence > leader.seen_seq,
                (SwarmPost.author_session_id.is_(None) | (SwarmPost.author_session_id != leader.session_id)),
                SwarmPost.kind.in_(["finding", "result", "human", "attention"]),
            ).order_by(SwarmPost.sequence.desc()).limit(1))
            if unseen is not None:
                await service.enqueue(
                    db, mission, leader, actor, f"board:{mission.board_seq}",
                    "New public findings or human guidance are available. Read the board, incorporate the "
                    "evidence, and assign only useful remaining work. Finish when nothing independent remains.",
                )
                leader.seen_seq = mission.board_seq

        coordinator = members[0]
        busy = await db.scalar(select(Turn.id).join(
            SwarmTurn, SwarmTurn.turn_id == Turn.id,
        ).where(
            SwarmTurn.mission_id == mission.id, Turn.session_id != mission.id,
            Turn.status.in_(["queued", "starting", "running", "saving"]),
        ).limit(1))
        if busy is None and await idle(coordinator):
            unseen = await db.scalar(select(SwarmPost.sequence).where(
                SwarmPost.mission_id == mission.id, SwarmPost.sequence > coordinator.seen_seq,
                SwarmPost.author_session_id != mission.id,
                SwarmPost.kind.in_(["finding", "result", "attention"]),
            ).order_by(SwarmPost.sequence.desc()).limit(1))
            if unseen is not None:
                await service.enqueue(
                    db, mission, coordinator, actor, f"summary:{mission.board_seq}",
                    "The team's current work settled. Read the shared board and synthesize the observed results "
                    "for the human. Surface blockers and link useful member conversations or outputs.",
                )
                coordinator.seen_seq = mission.board_seq
                mission.next_round_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
            elif mission.next_round_at is None:
                mission.next_round_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
            elif mission.next_round_at <= now():
                target = leader if leader is not None and await idle(leader) else None
                if target is not None:
                    mission.round += 1
                    await service.enqueue(
                        db, mission, target, actor, f"round:{mission.round}",
                        "Continue the same original mission within its current work window. Read the board and "
                        "latest human guidance. Find useful unfinished work; do not invent new goals or repeat "
                        "external actions. Publish a clear blocker if further work needs human direction.",
                    )
                mission.next_round_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        return MissionTick()


@activity.defn(name="moyai_swarm_tick_v1")
async def mission_tick(run: MissionRun) -> MissionTick:
    from .runtime import current
    return await tick(current().database.swarms, run)


@activity.defn(name="moyai_swarm_failed_v1")
async def mission_failed(run: MissionRun) -> None:
    from .runtime import current
    service = current().database.swarms
    async with service.database._admission_session(None) as db:
        mission = await service.lock(db, run.mission_id)
        if mission.status == "active" and mission.generation == run.generation:
            await service.stop_in(db, mission, "attention", "Team coordination stopped. Inspect saved work, then resume.")


@workflow.defn(name="MoyaiPydanticSwarmV1")
class SwarmWorkflow:
    @workflow.run
    async def run(self, request: MissionRun) -> None:
        try:
            for _ in range(100):
                state = await workflow.execute_activity(
                    "moyai_swarm_tick_v1", request, result_type=MissionTick, **CONTROL,
                )
                if not state.exists:
                    return
                await workflow.sleep(state.delay_seconds)
        except Exception:
            await workflow.execute_activity("moyai_swarm_failed_v1", request, **CONTROL)
            raise
        workflow.continue_as_new(request)


async def dispatch_missions(runtime: Runtime, *, limit: int = 10) -> None:
    """Reconcile durable mission intent with deterministic Temporal workflow IDs."""
    async with runtime.database.sessions() as db:
        pending = list(await db.scalars(select(SwarmMission).where(
            SwarmMission.status == "active", SwarmMission.dispatched_generation != SwarmMission.generation,
        ).order_by(SwarmMission.created_at).limit(limit)))
    client = await runtime.connect()
    for mission in pending:
        request = MissionRun(mission.id, mission.generation)
        try:
            async with asyncio.timeout(2):
                await client.start_workflow(
                    SwarmWorkflow.run, request,
                    id=f"moyai-swarm-{mission.id}-{mission.generation}",
                    task_queue=runtime.settings.temporal_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
        except WorkflowAlreadyStartedError:
            pass
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(SwarmMission).where(
                SwarmMission.id == mission.id, SwarmMission.generation == mission.generation,
            ).values(dispatched_generation=mission.generation))


SWARM_ACTIVITIES = [mission_tick, mission_failed]
