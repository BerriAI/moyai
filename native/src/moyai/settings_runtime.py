"""Settings adapters for the native capability and Temporal execution owners.

Integration:
* Add settings_capability(database) alongside the existing bundled Skills(...).
* Register SETTINGS_WORKFLOWS and SETTINGS_ACTIVITIES in make_worker.
* Keep the existing dispatcher: scheduled admissions use Database.submit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from temporalio import activity, workflow
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.common import RetryPolicy
from temporalio.service import RPCError, RPCStatusCode

with workflow.unsafe.imports_passed_through():
    from fastapi import HTTPException
    from pydantic_ai import RunContext
    from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
    from sqlalchemy import or_, select, update

    from .contracts import NewMessage, RunScope
    from .database import Database
    from .models import Turn, now
    from .settings_models import SettingAutomation, SettingAutomationRun, SettingSkill

SETTINGS_WORKER_PREFIX = "moyai-settings-v1-"


def settings_worker_identity() -> str:
    """Pass as Worker(identity=...) only when registering both exported lists."""
    return f"{SETTINGS_WORKER_PREFIX}{uuid4()}"


async def automation_readiness(runtime: Any) -> dict[str, Any]:
    """Use Temporal pollers to distinguish registered workers from a stored row."""
    try:
        async with asyncio.timeout(3):
            client = await runtime.connect()
            kinds = (TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)
            descriptions = await asyncio.gather(*[
                client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                    namespace=client.namespace, task_queue=TaskQueue(name=runtime.settings.temporal_queue),
                    task_queue_type=kind,
                )) for kind in kinds
            ])
            cutoff = datetime.now(UTC) - timedelta(seconds=90)
            ready = all(any(
                poller.identity.startswith(SETTINGS_WORKER_PREFIX)
                and poller.last_access_time.ToDatetime(tzinfo=UTC) >= cutoff
                for poller in description.pollers
            ) for description in descriptions)
            return {
                "create": ready, "update": True, "delete": True, "run": False,
                "runtime_available": ready, "execution_backend": "temporal",
                "reason": (
                    "Temporal schedules enqueue native turns; pause/delete fence future admissions."
                    if ready else "No active Settings worker is registered. Existing schedules can still be paused or deleted."
                ),
            }
    except (RPCError, TimeoutError, RuntimeError, OSError):
        return {
            "create": False, "update": True, "delete": True, "run": False,
            "runtime_available": False, "execution_backend": "temporal",
            "reason": "Temporal is unavailable. Pause/delete save local intent and require retry for remote confirmation.",
        }


class SavedSkill(AbstractCapability[RunScope]):
    """A native deferred instruction capability, matching Harness Skills."""

    def __init__(self, row: dict[str, Any]):
        # Distinct IDs preserve bundled names and personal/shared namesakes.
        self.id = f"saved_skill_{row['id'].replace('-', '_')}"
        self.description = f"{row['name']}: {row['description']}".replace("\n", "\n  ")
        self.defer_loading = True
        self.instructions = f"# Skill: {row['name']}\n\n{row['content']}"

    def get_instructions(self) -> str:
        return self.instructions


class SettingsSkills(AbstractCapability[RunScope]):
    id = "moyai_settings_skills"

    def __init__(self, database: Database):
        self.database = database

    async def catalog(self, scope: RunScope) -> list[dict[str, Any]]:
        await self.database.authorize_scope(scope)
        if scope.surface == "slack":
            return []
        async with self.database.sessions() as db:
            visible = SettingSkill.scope_key == "shared"
            if scope.visibility == "private":
                visible = or_(visible, SettingSkill.scope_key == scope.actor_id)
            rows = await db.scalars(select(SettingSkill).where(
                visible, SettingSkill.enabled.is_(True),
            ).order_by(SettingSkill.name, SettingSkill.id))
            return [{
                "id": row.id, "name": row.name, "description": row.description,
                "content": self.database.security.unseal(row.content).decode(),
            } for row in rows]

    async def for_run(self, ctx: RunContext[RunScope]) -> AbstractCapability[RunScope]:
        # for_run executes before native capability-operation wrapping. Do not
        # perform database I/O here in a workflow, even through a decorator.
        if workflow.in_workflow() and not workflow.patched("moyai-settings-skills-v1"):
            # Histories admitted before Settings existed have no catalog
            # activity; replay must retain their original capability set.
            return self
        rows = await workflow.execute_activity(
            "moyai_settings_skill_catalog", ctx.deps,
            result_type=list[dict[str, Any]],
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        ) if workflow.in_workflow() else await self.catalog(ctx.deps)
        return CombinedCapability([SavedSkill(row) for row in rows]) if rows else self


def settings_capability(database: Database) -> SettingsSkills:
    """Add alongside Skills(...), never replace or shadow the bundled library."""
    return SettingsSkills(database)


def schedule_id(automation_id: str) -> str:
    return f"moyai-setting-{automation_id}"


async def reconcile_schedule(runtime: Any, automation_id: str, *, attempts: int = 3) -> None:
    """Reconcile saved intent; repeated calls repair missing acknowledgments.

    Pause/delete fence new admission locally *before* the Temporal RPC. Already
    admitted work remains in the normal native turn path.
    """
    async with runtime.database.sessions() as db:
        row = await db.get(SettingAutomation, automation_id)
        if row is None:
            return
        revision, deleted = row.revision, row.state == "deleted"
        paused = row.paused
        interval = row.interval_minutes
    client = await runtime.connect()
    handle = client.get_schedule_handle(schedule_id(automation_id))
    if deleted:
        try:
            await handle.delete()
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise
        return
    spec = Schedule(
        action=ScheduleActionStartWorkflow(
            SettingsAutomationWorkflow.run, automation_id,
            id=f"moyai-automation-admit-{automation_id}",
            task_queue=runtime.settings.temporal_queue,
            execution_timeout=timedelta(minutes=10),
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=interval))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=5)),
        state=ScheduleState(paused=paused),
    )
    try:
        await client.create_schedule(schedule_id(automation_id), spec)
    except ScheduleAlreadyRunningError:
        if paused:
            await handle.pause(note="Paused in Moyai Settings")
        else:
            await handle.unpause(note="Enabled in Moyai Settings")
    async with runtime.database.sessions.begin() as db:
        # An older RPC must never acknowledge a newer desired state.
        await db.execute(update(SettingAutomation).where(
            SettingAutomation.id == automation_id, SettingAutomation.revision == revision,
            SettingAutomation.state != "deleted",
        ).values(state="ready", updated_at=now()))
        current = await db.get(SettingAutomation, automation_id)
        changed = current is not None and current.revision != revision
    if changed:
        # An older pause/unpause RPC may finish after the newer one. Repair the
        # latest intent, rather than leaving the remote schedule in stale state.
        if attempts > 1:
            return await reconcile_schedule(runtime, automation_id, attempts=attempts - 1)
        async with runtime.database.sessions.begin() as db:
            await db.execute(update(SettingAutomation).where(
                SettingAutomation.id == automation_id, SettingAutomation.state != "deleted",
            ).values(state="pending"))
        raise HTTPException(409, "Automation changed while saving. Retry its current state.")


async def admit_automation(runtime: Any, automation_id: str, occurrence_id: str) -> dict[str, Any]:
    """Idempotently enqueue one occurrence through the existing native API."""
    identity = hashlib.sha256(f"{automation_id}:{occurrence_id}".encode()).hexdigest()
    async with runtime.database.sessions.begin() as db:
        # Serializes admission versus pause/delete on PostgreSQL and SQLite.
        locked = await db.scalar(update(SettingAutomation).where(
            SettingAutomation.id == automation_id,
        ).values(revision=SettingAutomation.revision).returning(SettingAutomation.id))
        if locked is None:
            return {"status": "skipped"}
        receipt = await db.get(SettingAutomationRun, identity)
        row = await db.get(SettingAutomation, automation_id)
        assert row is not None
        if receipt is None:
            if row.paused or row.state in {"deleted", "provisioning"}:
                return {"status": "skipped"}
            snapshot = {
                "owner_id": row.owner_id, "auth_version": row.owner_auth_version,
                "session_id": row.session_id, "prompt": runtime.security.unseal(row.prompt).decode(),
                "model": row.model, "connections": json.loads(row.connections),
            }
            receipt = SettingAutomationRun(
                id=identity, automation_id=automation_id, request=runtime.security.seal(snapshot),
            )
            db.add(receipt)
        else:
            snapshot = json.loads(runtime.security.unseal(receipt.request))
        if receipt.turn_id:
            return {"status": "admitted", "turn_id": receipt.turn_id}
        if receipt.status == "denied":
            return {"status": "denied"}
    # If submit committed but its acknowledgment/receipt was lost, its unique
    # request identity resolves the retry to the same native turn.
    async with runtime.database.sessions() as db:
        existing = await db.scalar(select(Turn).where(
            Turn.session_id == snapshot["session_id"], Turn.actor_id == snapshot["owner_id"],
            Turn.request_key == f"automation:{identity}",
        ))
    turn = existing
    if turn is None:
        try:
            owner = await runtime.database.user(snapshot["owner_id"])
            if owner.auth_version != snapshot["auth_version"]:
                raise HTTPException(403, "Automation owner's access changed.")
            turn = await runtime.database.submit(owner, snapshot["session_id"], NewMessage(
                prompt=snapshot["prompt"], model=snapshot["model"], connections=snapshot["connections"],
                request_key=f"automation:{identity}",
            ))
        except HTTPException:
            async with runtime.database.sessions.begin() as db:
                await db.execute(update(SettingAutomationRun).where(
                    SettingAutomationRun.id == identity,
                ).values(status="denied"))
            return {"status": "denied"}
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(SettingAutomationRun).where(
            SettingAutomationRun.id == identity,
        ).values(turn_id=turn.id, status="admitted"))
    return {"status": "admitted", "turn_id": turn.id}


@activity.defn(name="moyai_settings_admit")
async def settings_admit(automation_id: str, occurrence_id: str) -> dict[str, Any]:
    from .runtime import current
    return await admit_automation(current(), automation_id, occurrence_id)


@activity.defn(name="moyai_settings_skill_catalog")
async def settings_skill_catalog(scope: RunScope) -> list[dict[str, Any]]:
    from .runtime import current
    return await SettingsSkills(current().database).catalog(scope)


@workflow.defn
class SettingsAutomationWorkflow:
    @workflow.run
    async def run(self, automation_id: str) -> dict[str, Any]:
        return await workflow.execute_activity(
            "moyai_settings_admit",
            args=[automation_id, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=5),
        )


SETTINGS_WORKFLOWS = [SettingsAutomationWorkflow]
SETTINGS_ACTIVITIES = [settings_admit, settings_skill_catalog]
