"""Opt-in, durable learning into the canonical skill library.

Only direct web conversations can publish learned skills: private work remains
personal; organization learning requires an administrator and shared source.
The learner has no tools and cannot rewrite manually maintained skills.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from app.memory import check_content
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models import Model
from pydantic_ai.models.instrumented import InstrumentationSettings
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.common import RetryPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from .automations.product_automation_models import ProductAutomationTurn
from .contracts import PreparedTurn, RunScope
from .inference_usage import UsageCapture
from .message_sources import memory_text, public_message
from .models import Message, Session, Turn, User, now
from .product_drain_models import ProductDrainState
from .product_identity import ProductGrant, ProductPrincipal
from .product_memory_review import ReviewDeferred, ReviewInput, fingerprint
from .product_memory_store import ProductMemoryStore
from .product_skill_learning_models import LearnedSkill, SkillLearningJob, SkillLearningPreference
from .skill_learning_prompt import INSTRUCTIONS, SkillReview

AGENT_NAME = "moyai_skill_learning"
WORKFLOW_PREFIX = "moyai-skill-learning-v1:"
WORKER_MARKER = "-skill-learning-v1-"
MAX_SOURCE_CHARS = 32000
MAX_CATALOG_CHARS = 24000
LearningOutcome = Literal["completed", "failed", "skipped"]


class LearningResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class LearningPreference(LearningResponse):
    enabled: bool = False
    revision: int = 0
    can_manage: bool = False
    pending: int = 0
    learned: int = 0
    last_status: LearningOutcome | None = None
    last_saved_count: int = 0


class LearningSettings(LearningResponse):
    personal: LearningPreference
    organization: LearningPreference
    available: bool


class LearningUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    revision: int = Field(ge=0)


class SkillLearningService:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.database = runtime.database
        self.catalog = runtime.product_skill_catalog
        self.identity = self.catalog.identity
        self.settings = self.identity.settings
        self.models: dict[str, Model] = {}
        self.agent: Agent[RunScope, SkillReview]
        self._ready, self._ready_until = False, 0.0

    def configure(self, models: dict[str, Model]) -> None:
        self.models = models
        self.durability = TemporalDurability(
            models=models,
            model_activity_config={
                "start_to_close_timeout": timedelta(seconds=self.settings.skill_learning_timeout_seconds),
                "schedule_to_close_timeout": timedelta(seconds=self.settings.skill_learning_timeout_seconds * 3),
                "retry_policy": RetryPolicy(maximum_attempts=3),
            },
        )
        self.agent = Agent(
            next(iter(models.values())), name=AGENT_NAME, deps_type=RunScope,
            output_type=NativeOutput(SkillReview), instructions=INSTRUCTIONS, retries=0,
            capabilities=[Instrumentation(InstrumentationSettings(
                include_content=False, include_binary_content=False, include_model_request_parameters=False,
            )), self.durability],
        )

    async def _lock(self, db: AsyncSession) -> None:
        await db.execute(text(
            "SELECT pg_advisory_xact_lock_shared(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
        ), {"schema": self.identity.schema})

    async def _preference(self, db: AsyncSession, key: str) -> SkillLearningPreference | None:
        return await db.get(SkillLearningPreference, key, with_for_update={"read": True})

    def _individual(self, principal: ProductPrincipal) -> bool:
        return (
            principal.individual and self.settings.person_login_enabled()
            and principal.email.rpartition("@")[2] in self.settings.identity_domains()
        )

    async def settings_view(self, grant: ProductGrant) -> LearningSettings:
        principal = await self.identity.require_current(grant)
        values = {}
        async with self.database.sessions() as db:
            for label, key, allowed in (
                ("personal", principal.id, self._individual(principal)),
                ("organization", "organization", self._individual(principal) and principal.role == "admin"),
            ):
                pref = await db.get(SkillLearningPreference, key)
                pending = await db.scalar(select(func.count()).select_from(SkillLearningJob).where(
                    SkillLearningJob.scope_key == key, SkillLearningJob.status.in_(["pending", "running"]),
                ))
                learned = await db.scalar(select(func.count()).select_from(LearnedSkill).where(
                    LearnedSkill.scope_key == key,
                ))
                latest = await db.scalar(select(SkillLearningJob).where(
                    SkillLearningJob.scope_key == key,
                    SkillLearningJob.status.in_(["completed", "failed", "skipped"]),
                ).order_by(SkillLearningJob.updated_at.desc(), SkillLearningJob.turn_id.desc()).limit(1))
                values[label] = LearningPreference(
                    enabled=bool(pref and pref.enabled), revision=pref.revision if pref else 0,
                    can_manage=allowed, pending=pending or 0, learned=learned or 0,
                    last_status=TypeAdapter(LearningOutcome).validate_python(latest.status) if latest else None,
                    last_saved_count=latest.saved_count if latest else 0,
                )
        await self.identity.require_current(grant)
        return LearningSettings(**values, available=self.settings.skill_learning_enabled and await self.worker_ready())

    async def update(self, grant: ProductGrant, kind: str, body: LearningUpdate) -> LearningSettings:
        async with self.database.sessions.begin() as db:
            await self._lock(db)
            principal = await self.identity.authorize_admission(db, grant)
            if kind not in {"personal", "organization"}:
                raise HTTPException(404, "Unknown learning scope.")
            if not self._individual(principal) or (kind == "organization" and principal.role != "admin"):
                raise HTTPException(403, "This account cannot manage this learning scope.")
            key = "organization" if kind == "organization" else principal.id
            await db.execute(insert(SkillLearningPreference).values(scope_key=key).on_conflict_do_nothing())
            pref = await db.get(SkillLearningPreference, key, with_for_update=True)
            if pref is None or pref.revision != body.revision:
                raise HTTPException(409, "Learning settings changed. Refresh and try again.")
            pref.enabled, pref.revision, pref.updated_at = body.enabled, pref.revision + 1, now()
        return await self.settings_view(grant)

    def _scope(self, principal: ProductPrincipal, session: Session) -> str | None:
        # Slack channels can have narrower audiences than the organization.
        # Sharing their content requires a separate audience-preserving design.
        if session.surface != "web" or not self._individual(principal):
            return None
        if session.visibility == "private" and session.owner_id == principal.id and principal.individual:
            return principal.id
        if session.visibility == "shared" and principal.role == "admin":
            return "organization"
        return None

    async def _sources(self, db: AsyncSession, turn: Turn) -> list[dict[str, str]]:
        rows = await db.scalars(select(Message).join(Turn, Turn.id == Message.turn_id).where(
            Message.session_id == turn.session_id, Message.author_id == turn.actor_id, Message.role == "user",
            Turn.actor_id == turn.actor_id,
            or_(Turn.id == turn.id, and_(Turn.parent_turn == turn.id, Turn.status.in_(["accepted", "included"]))),
        ).order_by(Message.id))
        sources = []
        for row in rows:
            _, transport = await public_message(self.database, db, row)
            if (transport and transport.get("kind") != "web") or (row.author_id or "").startswith("slack:"):
                # A linked DM continues a web session without changing its
                # surface. Exclude the whole answer, including mixed steering.
                return []
            content = await memory_text(self.database, db, row)
            try:
                check_content(content)
            except HTTPException:
                continue
            sources.append({"id": f"user:{row.id}", "content": content[:12000]})
        if turn.result:
            content = self.database.security.unseal(turn.result).decode()
            try:
                check_content(content)
            except HTTPException:
                pass
            else:
                sources.append({"id": f"answer:{turn.id}", "content": content[:16000]})
        # Bound a review independently of transcript size. Never read model
        # history, recalled private memory, attachments, or hidden tool data.
        bounded, size = [], 0
        for source in reversed(sources):
            if size + len(source["content"]) <= MAX_SOURCE_CHARS:
                bounded.append(source)
                size += len(source["content"])
        return list(reversed(bounded))

    async def enqueue_in(self, db: AsyncSession, turn: Turn, session: Session) -> bool:
        if (
            not self.settings.skill_learning_enabled or turn.status not in {"saving", "completed"}
            or turn.result_accepted is not True or turn.parent_turn is not None or not turn.preparation
            or session.deleted or await db.get(ProductAutomationTurn, turn.id) is not None
        ):
            return False
        try:
            grant = await self.database._saved_product_grant(db, turn)
            if grant is None:
                return False
            principal = await self.identity.authorize_admission(db, grant)
            key = self._scope(principal, session)
            pref = await self._preference(db, key) if key else None
            if pref is None or not pref.enabled:
                return False
            sources = await self._sources(db, turn)
            if not any(source["id"].startswith("user:") for source in sources):
                return False
            model = self.settings.resolve_model(fallback=self.settings.skill_learning_model or turn.model)
            if model not in self.models:
                return False
        except (HTTPException, PermissionError, ValueError):
            return False
        admitted = await db.scalar(insert(SkillLearningJob).values(
            turn_id=turn.id, session_id=session.id, actor_id=turn.actor_id, scope_key=key,
            authority_version=grant.authority_version, preferences_revision=pref.revision,
            input_fingerprint=fingerprint(sources), workflow_id=WORKFLOW_PREFIX + turn.id, model=model,
            available_at=(datetime.now(UTC) + timedelta(seconds=self.settings.skill_learning_idle_seconds)).isoformat(),
        ).on_conflict_do_nothing(index_elements=["turn_id"]).returning(SkillLearningJob.turn_id))
        return admitted is not None

    async def _state(
        self, db: AsyncSession, turn_id: str, *, scope: RunScope | None = None,
    ) -> tuple[SkillLearningJob, RunScope, list[dict[str, str]], bool]:
        await self._lock(db)
        actor_id = await db.scalar(select(Turn.actor_id).where(Turn.id == turn_id))
        actor = await db.scalar(select(User).where(User.id == actor_id).with_for_update(read=True))
        turn = await db.get(Turn, turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise PermissionError("The original learning requester is unavailable.")
        grant = await self.database._saved_product_grant(db, turn)
        if grant is None:
            raise PermissionError("Learning requires original persisted product authority.")
        principal = await self.identity.authorize_admission(db, grant)
        session = await db.scalar(select(Session).where(Session.id == turn.session_id).with_for_update(read=True))
        if session is None or session.deleted or not self.settings.skill_learning_enabled:
            raise PermissionError("The source conversation is no longer available for learning.")
        key = self._scope(principal, session)
        pref = await self._preference(db, key) if key else None
        job = await db.get(SkillLearningJob, turn_id, with_for_update=True)
        if (
            not key or pref is None or not pref.enabled or job is None or job.scope_key != key
            or job.preferences_revision != pref.revision or job.authority_version != grant.authority_version
            or job.actor_id != actor.id or job.session_id != session.id or job.model not in self.models
            or job.workflow_id != WORKFLOW_PREFIX + turn.id or job.status not in {"pending", "running"}
            or turn.status != "completed" or turn.result_accepted is not True or turn.parent_turn is not None
            or not turn.preparation or await db.get(ProductAutomationTurn, turn.id) is not None
        ):
            raise PermissionError("Learning preferences, source or authority changed.")
        sources = await self._sources(db, turn)
        if fingerprint(sources) != job.input_fingerprint:
            raise PermissionError("The source evidence changed.")
        prepared = TypeAdapter(PreparedTurn).validate_json(self.database.security.unseal(turn.preparation))
        expected = replace(
            prepared.scope, surface="skill_learning", model=job.model,
            grants=[replace(item, credential="", policy={}) for item in prepared.scope.grants],
        )
        if (
            expected.actor_id != actor.id or expected.session_id != session.id or expected.turn_id != turn.id
            or expected.product_grant != grant or prepared.scope.surface != "web"
            or expected.visibility != session.visibility or (scope is not None and scope != expected)
        ):
            raise PermissionError("Learning cannot change the original conversation authority.")
        grants = await self.database.resolve_grants(
            db, actor.id, [item.id for item in expected.grants],
            shared_session=session.visibility == "shared", surface=session.surface,
        )
        if [(item.id, item.revision) for item in grants] != [(item.id, item.revision) for item in expected.grants]:
            raise PermissionError("Connection access changed before learning.")
        active = await db.scalar(select(Turn.id).where(
            Turn.session_id == session.id,
            Turn.status.in_(["queued", "starting", "running", "saving", "stopping", "waiting"]),
        ).limit(1))
        drain = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        return job, expected, sources, session.active_turn is None and active is None and drain is not None and not drain.paused

    async def _repository(self, scope: RunScope) -> str:
        if scope.surface == "skill_learning":
            # Repository validation (not model input) needs the original
            # connection policy, including a GitLab project receipt.
            async with self.database.sessions() as db:
                turn = await db.get(Turn, scope.turn_id)
                if turn is None or not turn.preparation:
                    raise PermissionError("The original repository authority is unavailable.")
                original = TypeAdapter(PreparedTurn).validate_json(
                    self.database.security.unseal(turn.preparation),
                ).scope
            if replace(
                original, surface="skill_learning", model=scope.model,
                grants=[replace(item, credential="", policy={}) for item in original.grants],
            ) != scope:
                raise PermissionError("Repository learning cannot change its original authority.")
            scope = replace(scope, grants=[replace(item, credential="") for item in original.grants])
        workspace = self.runtime.product_workspace
        return await ProductMemoryStore(
            self.runtime.product_memory, scope, scope.turn_id,
            repository_scope=workspace.repository_scope if workspace else None,
        )._repository()

    async def _catalog(self, db: AsyncSession, key: str, repository: str) -> list[dict[str, Any]]:
        learned = list(await db.scalars(select(LearnedSkill).where(
            LearnedSkill.scope_key == key,
        ).order_by(LearnedSkill.updated_at.desc()).limit(100)))
        result, size = [], 0
        for entry in learned:
            evidence = json.loads(self.database.security.unseal(entry.evidence))
            if evidence["repository"] != repository:
                continue
            row = (await db.execute(text(
                f"SELECT * FROM {self.catalog._table('skills')} WHERE id=:id"
            ), {"id": entry.skill_id})).mappings().first()
            expected_scope = "organization" if key == "organization" else "personal"
            if row is None or row["namespace"] != key or row["scope"] != expected_scope:
                continue
            item = {
                "key": evidence["key"], "skill_id": entry.skill_id, "revision": row["revision"],
                "editable": entry.revision == row["revision"] and not row["archived"],
                "archived": bool(row["archived"]), "description": row["description"],
                "instructions": self.catalog._decrypt(row["encrypted"]),
            }
            length = len(json.dumps(item))
            if size + length <= MAX_CATALOG_CHARS:
                result.append(item)
                size += length
        return result

    def _snapshot(self, job: SkillLearningJob) -> dict[str, Any]:
        if not job.snapshot:
            raise PermissionError("The learning job has no snapshot.")
        result = json.loads(self.database.security.unseal(job.snapshot))
        if fingerprint(result) != job.snapshot_fingerprint:
            raise PermissionError("The learning snapshot changed.")
        return result

    async def prepare(self, turn_id: str) -> ReviewInput:
        async with self.database.sessions.begin() as db:
            job = await db.get(SkillLearningJob, turn_id)
            if job is None or job.status in {"completed", "failed", "skipped"}:
                return ReviewInput()
            turn = await db.get(Turn, turn_id)
            if turn is not None and turn.status == "saving":
                return ReviewInput(wait_seconds=5)
            job, scope, _, idle = await self._state(db, turn_id)
            wait = max(0, (datetime.fromisoformat(job.available_at) - datetime.now(UTC)).total_seconds())
            if not idle or wait:
                return ReviewInput(wait_seconds=wait or 30)
        repository = await self._repository(scope)
        async with self.database.sessions.begin() as db:
            job, scope, sources, idle = await self._state(db, turn_id, scope=scope)
            if not idle:
                return ReviewInput(wait_seconds=30)
            catalog = await self._catalog(db, job.scope_key, repository)
            snapshot = {"sources": sources, "repository": repository, "catalog": catalog}
            if job.snapshot and self._snapshot(job) != snapshot:
                raise PermissionError("The learning input or library changed.")
            job.snapshot, job.snapshot_fingerprint = self.database.security.seal(json.dumps(snapshot)), fingerprint(snapshot)
            job.status, job.updated_at = "running", now()
            return ReviewInput(scope, [json.dumps(snapshot, ensure_ascii=False)], job.snapshot_fingerprint or "")

    async def authorize_scope(self, scope: RunScope) -> None:
        async with self.database.sessions.begin() as db:
            job, _, _, idle = await self._state(db, scope.turn_id, scope=scope)
            if not idle or job.status != "running":
                raise PermissionError("Learning is paused or has no active job.")
            self._snapshot(job)

    async def authorize_inference(self, db: AsyncSession, capture: UsageCapture) -> None:
        scope = capture.activity.scope
        job, _, _, idle = await self._state(db, scope.turn_id, scope=scope)
        expected = f"{self.database.settings.temporal_namespace}/{job.workflow_id}/{scope.turn_id}/"
        if (
            scope.surface != "skill_learning" or not idle or job.status != "running"
            or not (capture.activity.logical_identity or "").startswith(expected)
            or capture.values["model"] != job.model
        ):
            raise PermissionError("Only the admitted learning workflow can use maintenance inference.")
        self._snapshot(job)

    async def apply(self, prepared: ReviewInput, review: SkillReview) -> int:
        scope = prepared.scope
        if scope is None or scope.product_grant is None or scope.surface != "skill_learning":
            raise PermissionError("Missing learning authority.")
        async with self.database.sessions.begin() as db:
            await self._lock(db)
            receipt = await db.get(SkillLearningJob, scope.turn_id, with_for_update=True)
            if receipt is not None and receipt.status == "completed":
                if (
                    receipt.snapshot_fingerprint != prepared.snapshot_fingerprint
                    or receipt.actor_id != scope.actor_id or receipt.session_id != scope.session_id
                    or scope.product_grant.user_id != scope.actor_id
                    or receipt.authority_version != scope.product_grant.authority_version
                ):
                    raise PermissionError("A learning receipt cannot be rebound.")
                return receipt.saved_count
        repository = await self._repository(scope)
        async with self.database.sessions.begin() as db:
            job, _, sources, idle = await self._state(db, scope.turn_id, scope=scope)
            if not idle:
                raise ReviewDeferred("Foreground work or maintenance owns this session.")
            if job.status != "running" or prepared.snapshot_fingerprint != job.snapshot_fingerprint:
                raise PermissionError("The learning snapshot was superseded.")
            snapshot = self._snapshot(job)
            if snapshot != {"sources": sources, "repository": repository,
                            "catalog": await self._catalog(db, job.scope_key, repository)}:
                raise PermissionError("The evidence or skill library changed.")
            # Serialize all learners in this scope, including overlapping sessions.
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                             {"key": "moyai-skill-learning:" + job.scope_key})
            count = 0
            keys: set[str] = set()
            for proposal in review.skills:
                if proposal.key in keys:
                    continue
                keys.add(proposal.key)
                source = next((item for item in sources if item["id"] == proposal.source_id), None)
                if source is None or proposal.source_quote not in source["content"]:
                    raise ValueError("A learned skill requires an exact supporting source quote.")
                check_content(proposal.instructions)
                check_content(proposal.description)
                check_content(proposal.source_quote)
                topic = fingerprint([repository, proposal.key])
                learned = await db.scalar(select(LearnedSkill).where(
                    LearnedSkill.scope_key == job.scope_key, LearnedSkill.topic_hash == topic,
                ).with_for_update())
                name = f"learned-{proposal.key}-{topic[:8]}"
                namespace = job.scope_key
                existing = (await db.execute(text(
                    f"SELECT * FROM {self.catalog._table('skills')} WHERE namespace=:namespace AND name=:name FOR UPDATE"
                ), {"namespace": namespace, "name": name})).mappings().first()
                # A manual edit, rename, archive or deletion freezes the learned
                # topic. Never recreate it under a new ID or overwrite a human.
                if learned is not None:
                    if (
                        existing is None or existing["id"] != learned.skill_id
                        or existing["revision"] != learned.revision or existing["archived"]
                        or not any(item["skill_id"] == learned.skill_id and item["editable"]
                                   and item["revision"] == learned.revision
                                   for item in snapshot["catalog"])
                    ):
                        continue
                elif existing is not None:
                    continue
                identity = learned.skill_id if learned else uuid4().hex
                revision = learned.revision + 1 if learned else 1
                skill_scope = "organization" if job.scope_key == "organization" else "personal"
                scope_line = f"Repository: {repository}\n\n" if repository else "Scope: the environment described below.\n\n"
                instructions = scope_line + proposal.instructions
                if existing is not None and (
                    self.catalog._decrypt(existing["encrypted"]) == instructions
                    and existing["description"] == proposal.description
                ):
                    continue
                encrypted = self.identity.legacy_security.encrypt(instructions)
                stamp = now()
                if learned is None:
                    await db.execute(text(
                        f"INSERT INTO {self.catalog._table('skills')} "
                        "(id,name,description,encrypted,scope,owner_id,namespace,revision,archived,"
                        "created_at,updated_at,client_id,icon) "
                        "VALUES (:id,:name,:description,:encrypted,:scope,:owner,:namespace,1,0,:stamp,:stamp,:client,'auto')"
                    ), {"id": identity, "name": name, "description": proposal.description, "encrypted": encrypted,
                        "scope": skill_scope, "owner": scope.actor_id, "namespace": namespace,
                        "stamp": stamp, "client": WORKFLOW_PREFIX + scope.turn_id + ":" + proposal.key})
                else:
                    await db.execute(text(
                        f"UPDATE {self.catalog._table('skills')} SET description=:description,encrypted=:encrypted,"
                        "revision=:revision,updated_at=:stamp WHERE id=:id"
                    ), {"id": identity, "description": proposal.description, "encrypted": encrypted,
                        "revision": revision, "stamp": stamp})
                evidence = {
                    "key": proposal.key, "repository": repository, "source_session_id": scope.session_id,
                    "source_turn_id": scope.turn_id, "source_id": proposal.source_id,
                    "source_quote": proposal.source_quote, "captured_at": stamp,
                }
                if learned is None:
                    learned = LearnedSkill(skill_id=identity, scope_key=job.scope_key, topic_hash=topic)
                    db.add(learned)
                learned.revision, learned.source_turn_id = revision, scope.turn_id
                learned.evidence, learned.updated_at = self.database.security.seal(json.dumps(evidence)), stamp
                # Source evidence is available with the skill's supporting files,
                # under the existing library's exact personal/organization scope.
                body = json.dumps(evidence, indent=2)
                manifest = [{"path": "learning-source.json", "size": len(body.encode()), "characters": len(body),
                             "sha256": hashlib.sha256(body.encode()).hexdigest()}]
                await db.execute(text(
                    f"INSERT INTO {self.catalog._table('skill_bundles')} (skill_id,revision,encrypted,manifest) "
                    "VALUES (:id,:revision,:encrypted,:manifest)"
                ), {"id": identity, "revision": revision,
                    "encrypted": self.identity.legacy_security.encrypt(json.dumps({"learning-source.json": body})),
                    "manifest": json.dumps(manifest)})
                await db.execute(text(
                    f"INSERT INTO {self.catalog._table('skill_audit')} (skill_id,actor_id,action,created_at) "
                    "VALUES (:id,:actor,'automatic_learning',:stamp)"
                ), {"id": identity, "actor": scope.actor_id, "stamp": stamp})
                count += 1
            job.status, job.saved_count, job.updated_at = "completed", count, now()
            return count

    async def failed(self, turn_id: str) -> None:
        async with self.database.sessions.begin() as db:
            job = await db.get(SkillLearningJob, turn_id, with_for_update=True)
            if job is not None and job.status in {"pending", "running"}:
                job.status, job.updated_at = "failed", now()

    async def worker_ready(self) -> bool:
        if time.monotonic() < self._ready_until:
            return self._ready
        try:
            async with asyncio.timeout(2):
                client = await self.runtime.connect()
                queues = await asyncio.gather(*[
                    client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                        namespace=client.namespace, task_queue=TaskQueue(name=self.database.settings.temporal_queue),
                        task_queue_type=kind,
                    ))
                    for kind in (TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY)
                ])
                cutoff = datetime.now(UTC) - timedelta(seconds=90)
                self._ready = all(any(
                    WORKER_MARKER in poller.identity and poller.last_access_time.ToDatetime(tzinfo=UTC) > cutoff
                    for poller in queue.pollers
                ) for queue in queues)
        except (RPCError, TimeoutError, OSError, RuntimeError):
            self._ready = False
        self._ready_until = time.monotonic() + 10
        return self._ready

    async def dispatch_pending(self, runtime: Any, *, limit: int = 5) -> int:
        if not self.settings.skill_learning_enabled:
            return 0
        async with self.database.sessions() as db:
            jobs = list(await db.scalars(select(SkillLearningJob).join(Turn, Turn.id == SkillLearningJob.turn_id).where(
                SkillLearningJob.status == "pending", SkillLearningJob.available_at <= now(), Turn.status == "completed",
            ).order_by(SkillLearningJob.created_at).limit(limit)))
        if not jobs or not await self.worker_ready():
            return 0
        from .skill_learning_workflow import SkillLearningWorkflow
        client = await runtime.connect()
        for job in jobs:
            try:
                await client.start_workflow(
                    SkillLearningWorkflow.run, job.turn_id, id=job.workflow_id,
                    task_queue=self.database.settings.temporal_queue,
                )
            except WorkflowAlreadyStartedError:
                pass
        return len(jobs)


def routes(service: SkillLearningService) -> APIRouter:
    router = APIRouter(prefix="/api/product/skill-learning")

    @router.get("", operation_id="getSkillLearning")
    async def listing(request: Request) -> LearningSettings:
        principal = await service.identity.authenticate(request)
        return await service.settings_view(principal.grant)

    @router.put("/{scope}", operation_id="updateSkillLearning")
    async def update(scope: Literal["personal", "organization"], body: LearningUpdate, request: Request) -> LearningSettings:
        principal = await service.identity.authenticate(request, mutation=True)
        return await service.update(principal.grant, scope, body)

    return router
