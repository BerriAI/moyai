"""Tool-free, native durable review of successfully completed native inputs.

Assembly contract (parent-owned files):
* Construct ProductMemoryReviewService(memory, models, repository_scope=...).
* Call enqueue_in(db, turn, session) with the accepted answer transaction.
* Call dispatch_pending(runtime) from the existing coordinator dispatcher.
* Register MEMORY_REVIEW_WORKFLOWS/ACTIVITIES, the agent, and ReviewAuthority.
* Broker prechecks/admission must call authorize_scope/authorize_inference for
  the exact server-only ``memory_review`` surface, never authorize_scope's
  ordinary active-turn guard. All other surfaces retain that ordinary guard.
* Only mark_registered() after worker and broker hooks have been qualified.

No source job receives freshly minted authority. Legacy review rows remain
retained/readable; this adapter requires a native turn's original saved grant.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.context_budget import ContextBudget
from app.library_views import fit_context
from app.memory import Note, check_content, search_terms
from app.memory_review import INSTRUCTIONS, Review, review_pages
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow, TemporalDurability
from pydantic_ai.models import Model
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.usage import UsageLimits
from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity, workflow
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

from .contracts import PreparedTurn, RunScope
from .inference_usage import INFERENCE_ACTIVITY, InferenceActivity, UsageCapture
from .models import Message, Session, Turn, User, now
from .product_drain_models import ProductDrainState
from .product_memory import ProductMemoryService
from .product_memory_models import ProductMemoryBarrier
from .product_memory_review_models import ProductMemoryReview
from .product_memory_store import ProductMemoryStore, RepositoryScope

WORKFLOW_PREFIX = "moyai-memory-review-v1:"
AGENT_NAME = "moyai_memory_review"
REVIEW_WORKER_MARKER = "-memory-review-v1-"
CONTROL = {
    "start_to_close_timeout": timedelta(seconds=60),
    "retry_policy": RetryPolicy(maximum_attempts=3),
}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass
class ReviewInput:
    scope: RunScope | None = None
    prompts: list[str] | None = None
    snapshot_fingerprint: str = ""
    wait_seconds: float = 0


class ReviewDeferred(PermissionError):
    """Retain a completed model result while foreground work or drain owns the session."""


class ProductMemoryReviewService:
    def __init__(
        self, memory: ProductMemoryService, models: dict[str, Model], *,
        repository_scope: RepositoryScope | None = None,
        runtime: Any = None,
    ):
        self.memory, self.database = memory, memory.database
        self.settings = memory.identity.settings
        self.models, self.repository_scope = models, repository_scope
        self.runtime = runtime
        self.context_budget = ContextBudget(self.settings)
        self.registered = False
        self.backfilled = False
        self._ready = False
        self._ready_until = 0.0
        self.durability = TemporalDurability(
            models=models,
            model_activity_config={
                "start_to_close_timeout": timedelta(seconds=self.settings.memory_review_timeout_seconds),
                "schedule_to_close_timeout": timedelta(seconds=self.settings.memory_review_timeout_seconds * 3),
                "retry_policy": RetryPolicy(maximum_attempts=3),
            },
        )
        # The SDK owns structured output and durable model activities. No tool
        # definitions, conversation history, workspace or session credentials.
        self.agent = Agent(
            next(iter(models.values())), name=AGENT_NAME, deps_type=RunScope,
            output_type=NativeOutput(Review), instructions=INSTRUCTIONS, retries=0,
            capabilities=[Instrumentation(InstrumentationSettings(
                include_content=False, include_binary_content=False, include_model_request_parameters=False,
            )), self.durability],
        )
        self.memory.native_review = self

    def mark_registered(self) -> None:
        self.registered = True

    async def worker_ready(self, runtime: Any = None) -> bool:
        runtime = runtime or self.runtime
        if runtime is None:
            return False
        if time.monotonic() < self._ready_until:
            return self._ready
        try:
            async with asyncio.timeout(2):
                client = await runtime.connect()
                descriptions = await asyncio.gather(*[
                    client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                        namespace=client.namespace, task_queue=TaskQueue(name=self.database.settings.temporal_queue),
                        task_queue_type=kind,
                    )) for kind in (
                        TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
                    )
                ])
                cutoff = datetime.now(UTC) - timedelta(seconds=90)
                self._ready = all(any(
                    REVIEW_WORKER_MARKER in poller.identity
                    and poller.last_access_time.ToDatetime(tzinfo=UTC) >= cutoff
                    for poller in description.pollers
                ) for description in descriptions)
        except (RPCError, TimeoutError, OSError, RuntimeError):
            self._ready = False
        self._ready_until = time.monotonic() + 10
        return self._ready

    async def status(self, owner: str) -> dict[str, Any]:
        ready = bool(
            self.registered and self.settings.memory_review_enabled and await self.worker_ready()
        )
        async with self.database.sessions() as db:
            drain = await db.get(ProductDrainState, 1)
            self.memory.background_review_available = ready and drain is not None and not drain.paused
            pending = await db.scalar(select(func.count()).select_from(ProductMemoryReview).where(
                ProductMemoryReview.owner_id == owner, ProductMemoryReview.status.in_(["pending", "running"]),
            ))
            latest = await db.scalar(select(ProductMemoryReview).where(
                ProductMemoryReview.owner_id == owner, ProductMemoryReview.status.in_(["completed", "failed"]),
            ).order_by(ProductMemoryReview.updated_at.desc(), ProductMemoryReview.turn_id.desc()).limit(1))
            return {
                "pending": pending or 0,
                "latest": {
                    "status": latest.status, "updated_at": latest.updated_at, "saved_count": latest.saved_count,
                } if latest is not None else None,
            }

    async def _sources(self, db: AsyncSession, turn: Turn) -> list[dict[str, Any]]:
        rows = await db.scalars(select(Message).join(Turn, Turn.id == Message.turn_id).where(
            Message.session_id == turn.session_id, Message.author_id == turn.actor_id,
            Message.role == "user", Turn.actor_id == turn.actor_id,
            or_(Turn.id == turn.id, and_(Turn.parent_turn == turn.id, Turn.status.in_(["accepted", "included"]))),
        ).order_by(Message.id))
        sources = []
        for row in rows:
            content = self.database.security.unseal(row.content).decode()
            try:
                check_content(content)
            except HTTPException:
                continue
            if content.strip():
                sources.append({"id": row.id, "content": content, "created_at": row.created_at})
        return sources

    async def enqueue_in(self, db: AsyncSession, turn: Turn, session: Session) -> bool:
        """Metadata-only admission in the answer's transaction; no model/RPC."""
        if (
            not self.settings.memory_review_enabled or turn.status not in {"saving", "completed"}
            or turn.result_accepted is not True or turn.parent_turn is not None
            or session.deleted or session.visibility != "shared" or session.surface not in {"web", "slack"}
            or not turn.preparation or await db.get(ProductMemoryReview, turn.id) is not None
        ):
            return False
        try:
            grant = await self.database._saved_product_grant(db, turn)
            if grant is None:
                return False
            principal = await self.memory.identity.authorize_admission(db, grant)
            owner = await self.memory.owner_in(db, principal)
        except (HTTPException, PermissionError):
            return False
        prefs = await self.memory._preferences(db, owner)
        sources = await self._sources(db, turn)
        barrier = await db.get(ProductMemoryBarrier, owner)
        if (
            not prefs["enabled"] or not prefs["auto_save"] or not sources
            or (barrier is not None and max(item["id"] for item in sources) <= barrier.last_message_id)
        ):
            return False
        try:
            model = self.settings.resolve_model(fallback=self.settings.memory_review_model or turn.model)
        except ValueError:
            return False
        if model not in self.models:
            return False
        admitted = await db.scalar(insert(ProductMemoryReview).values(
            turn_id=turn.id, session_id=session.id, actor_id=turn.actor_id, owner_id=owner,
            authority_version=grant.authority_version, preferences_revision=prefs["revision"],
            input_fingerprint=fingerprint(sources), workflow_id=WORKFLOW_PREFIX + turn.id,
            model=model,
            available_at=(datetime.now(UTC) + timedelta(seconds=self.settings.memory_review_idle_seconds)).isoformat(),
        ).on_conflict_do_nothing(index_elements=["turn_id"]).returning(ProductMemoryReview.turn_id))
        return admitted is not None

    async def _state(
        self, db: AsyncSession, turn_id: str, *, scope: RunScope | None = None,
    ) -> tuple[ProductMemoryReview, RunScope, list[dict[str, Any]], bool]:
        """Source -> User -> Session -> job lock order, shared with memory writes."""
        await db.execute(text(
            "SELECT pg_advisory_xact_lock_shared(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
        ), {"schema": self.memory.identity.schema})
        actor_id = await db.scalar(select(Turn.actor_id).where(Turn.id == turn_id))
        actor = await db.scalar(select(User).where(User.id == actor_id).with_for_update(read=True))
        turn = await db.get(Turn, turn_id)
        if actor is None or not actor.enabled or turn is None:
            raise PermissionError("The original memory review requester is unavailable.")
        grant = await self.database._saved_product_grant(db, turn)
        if grant is None:
            raise PermissionError("Memory review requires original persisted product authority.")
        principal = await self.memory.identity.authorize_admission(db, grant)
        session = await db.scalar(select(Session).where(
            Session.id == turn.session_id,
        ).with_for_update(read=True))
        job = await db.get(ProductMemoryReview, turn_id, with_for_update=True)
        if (
            session is None or session.deleted or session.visibility != "shared"
            or session.surface not in {"web", "slack"} or turn.status != "completed"
            or turn.parent_turn is not None or turn.result_accepted is not True or not turn.preparation
            or job is None or job.session_id != turn.session_id or job.actor_id != turn.actor_id
            or job.authority_version != grant.authority_version or job.workflow_id != WORKFLOW_PREFIX + turn.id
            or job.status not in {"pending", "running"} or job.model not in self.models
        ):
            raise PermissionError("The original memory review is no longer eligible.")
        owner = await self.memory.owner_in(db, principal)
        prefs = await self.memory._preferences(db, owner)
        sources = await self._sources(db, turn)
        barrier = await db.get(ProductMemoryBarrier, owner)
        if (
            owner != job.owner_id or not prefs["enabled"] or not prefs["auto_save"]
            or prefs["revision"] != job.preferences_revision or fingerprint(sources) != job.input_fingerprint
            or not sources or (barrier is not None and max(item["id"] for item in sources) <= barrier.last_message_id)
        ):
            raise PermissionError("Current memory preferences, owner guidance or source inputs superseded the review.")
        prepared = TypeAdapter(PreparedTurn).validate_json(self.database.security.unseal(turn.preparation))
        if prepared.scope.visibility != "shared" or prepared.scope.surface not in {"web", "slack"}:
            raise PermissionError("The original task did not permit personal-memory export.")
        expected = replace(
            prepared.scope, model=job.model, surface="memory_review",
            grants=[replace(item, credential="", policy={}) for item in prepared.scope.grants],
        )
        if (
            expected.turn_id != turn.id or expected.session_id != session.id or expected.actor_id != actor.id
            or expected.product_grant != grant or (scope is not None and scope != expected)
        ):
            raise PermissionError("A memory review scope cannot replace the admitted requester.")
        grants = await self.database.resolve_grants(
            db, actor.id, [item.id for item in expected.grants], shared_session=True, surface=session.surface,
        )
        if [(item.id, item.revision) for item in grants] != [(item.id, item.revision) for item in expected.grants]:
            raise PermissionError("Connection authority changed before memory review.")
        active = await db.scalar(select(Turn.id).where(
            Turn.session_id == session.id,
            Turn.status.in_(["queued", "starting", "running", "saving", "stopping", "waiting"]),
        ).limit(1))
        drain = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        idle = session.active_turn is None and active is None and drain is not None and not drain.paused
        return job, expected, sources, idle

    async def _notes(self, db: AsyncSession, owner: str, repository: str) -> list[dict[str, Any]]:
        rows = (await db.execute(text(
            f"SELECT * FROM {self.memory._table('personal_memories')} "
            "WHERE owner_id=:owner AND deleted=0 ORDER BY updated_at DESC,id"
        ), {"owner": owner})).mappings()
        notes = [self.memory._unpack(dict(row)) for row in rows]
        return [note for note in notes if not note["repo_url"] or note["repo_url"] == repository]

    async def _repository(self, scope: RunScope) -> str:
        return await ProductMemoryStore(
            self.memory, scope, scope.turn_id, repository_scope=self.repository_scope,
        )._repository()

    def _snapshot(self, job: ProductMemoryReview) -> dict[str, Any]:
        if not job.snapshot:
            raise PermissionError("The memory review has no admitted input snapshot.")
        value = json.loads(self.database.security.unseal(job.snapshot))
        if fingerprint(value) != job.snapshot_fingerprint:
            raise PermissionError("The memory review snapshot failed integrity verification.")
        return value

    async def prepare(self, turn_id: str) -> ReviewInput:
        async with self.database.sessions.begin() as db:
            existing = await db.get(ProductMemoryReview, turn_id)
            if existing is None or existing.status in {"completed", "failed", "skipped"}:
                return ReviewInput()
            source = await db.get(Turn, turn_id)
            if source is not None and source.status == "saving" and source.result_accepted:
                return ReviewInput(wait_seconds=5)
            job, scope, _, idle = await self._state(db, turn_id)
            if not idle:
                return ReviewInput(wait_seconds=30)
            available = max(0, (datetime.fromisoformat(job.available_at) - datetime.now(UTC)).total_seconds())
            if available:
                return ReviewInput(wait_seconds=available)
            model = job.model
        repository = await self._repository(scope)
        budget = await self.context_budget.measure({"model": model, "messages": []})
        async with self.database.sessions.begin() as db:
            job, scope, sources, idle = await self._state(db, turn_id, scope=scope)
            if not idle:
                return ReviewInput(wait_seconds=30)
            notes = await self._notes(db, job.owner_id, repository)
            if job.snapshot:
                snapshot = self._snapshot(job)
                if (
                    snapshot["input_fingerprint"] != fingerprint(sources)
                    or snapshot["notes_fingerprint"] != fingerprint(notes) or snapshot["repository"] != repository
                ):
                    raise PermissionError("The original review snapshot was superseded.")
                allocation = snapshot["input_budget"]
            else:
                allocation = budget.input_budget
            terms = search_terms(" ".join(item["content"] for item in sources))
            ranked = sorted(notes, key=lambda note: (
                len(terms & search_terms(note["title"] + " " + note["content"])), note["updated_at"],
            ), reverse=True)
            detailed = [{
                **{key: note[key] for key in ("key", "title", "content", "kind", "repo_url", "revision")},
                "manual": note["source"]["type"] == "manual",
            } for note in ranked]
            fitted = fit_context("Existing scoped memories\n" + json.dumps({"notes": detailed}, ensure_ascii=False),
                                 max(1, allocation // 4))
            view = json.loads(fitted.partition("\n")[2]) if fitted else {"notes": []}
            visible = [item["key"] for item in view["notes"] if not item.get("content_omitted")]
            pages = review_pages(sources, max(1, allocation // 2))
            prompts = [json.dumps({
                "repository": repository, "messages": page, "existing_memories": view["notes"],
                "memory_index": [{"key": item["key"], "title": item["title"]} for item in view["notes"]],
            }, ensure_ascii=False) for page in pages]
            snapshot = {
                "input_fingerprint": fingerprint(sources), "notes_fingerprint": fingerprint(notes),
                "repository": repository, "visible": visible, "input_budget": allocation,
                "prompt_fingerprint": fingerprint(prompts),
            }
            if job.snapshot and fingerprint(snapshot) != job.snapshot_fingerprint:
                raise PermissionError("The original review prompt cannot be replaced.")
            if not job.snapshot:
                # Job rows retain encrypted provenance, hashes and revisions,
                # not copies of the canonical library or source message bodies.
                # Model inputs travel through the normal encrypted Temporal codec.
                job.snapshot = self.database.security.seal(snapshot)
                job.snapshot_fingerprint = fingerprint(snapshot)
            job.status, job.updated_at = "running", now()
            job.attempts += 1
            return ReviewInput(scope, prompts, job.snapshot_fingerprint or "")

    async def authorize_scope(self, scope: RunScope) -> None:
        async with self.database.sessions.begin() as db:
            job, _, _, idle = await self._state(db, scope.turn_id, scope=scope)
            if not idle or job.status != "running":
                raise PermissionError("The memory review is paused or has no active admitted job.")
            self._snapshot(job)

    async def authorize_inference(self, db: AsyncSession, capture: UsageCapture) -> None:
        """Call before ordinary admission locks, then retain the same ledger/slots."""
        scope = capture.activity.scope
        if scope.surface != "memory_review":
            raise PermissionError("Only an explicit memory-review scope uses maintenance admission.")
        job, _, sources, idle = await self._state(db, scope.turn_id, scope=scope)
        expected = f"{self.database.settings.temporal_namespace}/{job.workflow_id}/{scope.turn_id}/"
        if (
            not idle or job.status != "running"
            or not (capture.activity.logical_identity or "").startswith(expected)
            or capture.values["model"] != job.model
        ):
            raise PermissionError("The model request is not the admitted durable memory review.")
        snapshot = self._snapshot(job)
        if (
            snapshot["input_fingerprint"] != fingerprint(sources)
            or snapshot["notes_fingerprint"] != fingerprint(await self._notes(db, job.owner_id, snapshot["repository"]))
        ):
            raise PermissionError("The memory review input or library changed before inference.")

    async def apply(self, prepared: ReviewInput, reviews: list[Review]) -> int:
        scope = prepared.scope
        if scope is None or scope.product_grant is None:
            raise PermissionError("Memory review needs its original saved authority.")
        # A confirmed mutation is evidence, not new authority. A lost activity
        # acknowledgement must not misreport committed saves after revocation.
        async with self.database.sessions() as db:
            receipt = await db.get(ProductMemoryReview, scope.turn_id)
            if receipt is not None and receipt.status == "completed":
                if (
                    receipt.snapshot_fingerprint != prepared.snapshot_fingerprint
                    or receipt.actor_id != scope.actor_id or receipt.session_id != scope.session_id
                    or scope.product_grant.user_id != receipt.actor_id
                    or receipt.authority_version != scope.product_grant.authority_version
                    or scope.surface != "memory_review"
                ):
                    raise PermissionError("A completed review receipt cannot be rebound.")
                return receipt.saved_count
        repository = await self._repository(scope)
        async with self.memory.writer(scope.product_grant) as (db, owner):
            existing = await db.get(ProductMemoryReview, scope.turn_id)
            if existing is not None and existing.status == "completed":
                if (
                    existing.snapshot_fingerprint != prepared.snapshot_fingerprint
                    or existing.actor_id != scope.actor_id or existing.owner_id != owner
                    or existing.session_id != scope.session_id
                    or existing.authority_version != scope.product_grant.authority_version
                ):
                    raise PermissionError("A completed review snapshot cannot be replaced.")
                return existing.saved_count
            job, _, sources, idle = await self._state(db, scope.turn_id, scope=scope)
            if not idle:
                raise ReviewDeferred("Foreground work or maintenance currently owns this session.")
            if job.status != "running" or job.owner_id != owner:
                raise PermissionError("The memory review was superseded before applying its result.")
            snapshot = self._snapshot(job)
            notes = await self._notes(db, owner, repository)
            if (
                prepared.snapshot_fingerprint != job.snapshot_fingerprint
                or snapshot["input_fingerprint"] != fingerprint(sources)
                or snapshot["repository"] != repository or snapshot["notes_fingerprint"] != fingerprint(notes)
            ):
                raise PermissionError("Memory source, repository or library revisions changed during review.")
            proposals = {}
            for review in reviews:
                if len({item.key for item in review.memories}) != len(review.memories):
                    raise ValueError("Duplicate memory proposals in one review.")
                proposals.update((item.key, item) for item in review.memories)
            old_by_key = {item["key"]: item for item in notes}
            source_by_id = {item["id"]: item for item in sources}
            saved = 0
            for item in proposals.values():
                source = source_by_id.get(item.source_message_id)
                if source is None or item.source_quote not in source["content"]:
                    raise ValueError("Unsupported memory evidence.")
                old = old_by_key.get(item.key)
                if old and (
                    item.key not in snapshot["visible"] or old["source"]["type"] == "manual"
                    or old["updated_at"] >= source["created_at"]
                ):
                    continue
                repo = repository if item.repository_specific else ""
                if (item.repository_specific and not repo) or (old and old["repo_url"] != repo):
                    raise ValueError("Memory scope changed.")
                body = Note(
                    key=item.key, title=item.title, content=item.content, kind=item.kind, repo_url=repo,
                    revision=old["revision"] if old else 0,
                    request_id=f"native-review-{scope.turn_id.replace('-', '')}-{fingerprint(item.key)[:16]}",
                )
                if old and all(old[key] == getattr(body, key) for key in ("title", "content", "kind", "repo_url")):
                    continue
                await self.memory.save_in(db, owner, body, source={
                    "type": "chat", "capture": "background", "core": "pydantic-v1",
                    "native_session_id": scope.session_id, "native_turn_id": scope.turn_id,
                    "native_message_id": item.source_message_id, "quote": item.source_quote,
                })
                saved += 1
            job.status, job.saved_count, job.updated_at = "completed", saved, now()
            return saved

    async def failed(self, turn_id: str) -> None:
        # Evidence may settle after requester revocation. Parent's native SQL
        # session fence still requires a current runtime owner/policy epoch.
        async with self.database.sessions.begin() as db:
            job = await db.get(ProductMemoryReview, turn_id, with_for_update=True)
            if job is not None and job.status in {"pending", "running"}:
                job.status, job.updated_at = "failed", now()

    async def dispatch_pending(self, runtime: Any, *, limit: int = 5) -> int:
        if not self.settings.memory_review_enabled or not await self.worker_ready(runtime):
            return 0
        async with self.database.sessions.begin() as db:
            drain = await db.get(ProductDrainState, 1)
            if drain is None or drain.paused:
                return 0
            if not self.backfilled and self.settings.memory_review_backfill_limit:
                cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
                candidates = await db.scalars(select(Turn).where(
                    Turn.status == "completed", Turn.created_at >= cutoff, Turn.parent_turn.is_(None),
                ).order_by(Turn.created_at.desc()).limit(self.settings.memory_review_backfill_limit))
                for turn in candidates:
                    session = await db.get(Session, turn.session_id)
                    if session is not None:
                        await self.enqueue_in(db, turn, session)
                self.backfilled = True
            jobs = list(await db.scalars(select(ProductMemoryReview).join(
                Turn, Turn.id == ProductMemoryReview.turn_id,
            ).where(
                ProductMemoryReview.status == "pending", ProductMemoryReview.available_at <= now(),
                Turn.status == "completed",
            ).order_by(ProductMemoryReview.created_at).limit(limit)))
        client = await runtime.connect()
        for job in jobs:
            try:
                await client.start_workflow(
                    MemoryReviewWorkflow.run, job.turn_id, id=job.workflow_id,
                    task_queue=self.database.settings.temporal_queue,
                )
            except WorkflowAlreadyStartedError:
                pass
        return len(jobs)


def current_review() -> ProductMemoryReviewService:
    from .runtime import current
    service = getattr(current(), "product_memory_review", None)
    if not isinstance(service, ProductMemoryReviewService):
        raise TypeError("The native memory-review service is not configured.")
    return service


@activity.defn(name="moyai_memory_review_prepare_v1")
async def prepare_review(turn_id: str) -> ReviewInput:
    return await current_review().prepare(turn_id)


@activity.defn(name="moyai_memory_review_apply_v1")
async def apply_review(prepared: ReviewInput, reviews: list[Review]) -> int:
    try:
        return await current_review().apply(prepared, reviews)
    except ReviewDeferred:
        # Native Temporal retries only this idempotent application activity;
        # the already-paid native model result stays in encrypted history.
        raise ApplicationError(
            "Memory review is waiting for foreground work or maintenance.",
            type="MoyaiMemoryReviewDeferred", next_retry_delay=timedelta(seconds=30),
        ) from None
    except (HTTPException, PermissionError, ValueError):
        raise ApplicationError(
            "Memory review was superseded or its evidence was invalid.",
            type="MoyaiMemoryReviewSuperseded", non_retryable=True,
        ) from None


@activity.defn(name="moyai_memory_review_failed_v1")
async def failed_review(turn_id: str) -> None:
    await current_review().failed(turn_id)


@workflow.defn(name="MoyaiMemoryReviewV1", sandboxed=False)
class MemoryReviewWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__: Sequence[AbstractAgent[Any, Any]] = ()

    @workflow.run
    async def run(self, turn_id: str) -> dict[str, Any]:
        try:
            while True:
                prepared = await workflow.execute_activity(
                    "moyai_memory_review_prepare_v1", turn_id, result_type=ReviewInput, **CONTROL,
                )
                if not prepared.wait_seconds:
                    break
                await workflow.sleep(prepared.wait_seconds)
            if prepared.scope is None:
                return {"status": "skipped", "saved_count": 0}
            service = current_review()
            reviews = []
            for index, prompt in enumerate(prepared.prompts or []):
                result = await service.agent.run(
                    prompt, deps=prepared.scope, model=service.models[prepared.scope.model],
                    run_id=f"memory-review:{turn_id}:{index}", usage_limits=UsageLimits(request_limit=1),
                )
                reviews.append(result.output)
            count = await workflow.execute_activity(
                apply_review, args=[prepared, reviews],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=0),
            )
            return {"status": "completed", "saved_count": count}
        except Exception:  # noqa: BLE001 - failures settle only this review, never the user's successful task
            await workflow.execute_activity(failed_review, turn_id, **CONTROL)
            # Private model/input errors never become public conversation errors.
            return {"status": "failed", "saved_count": 0}


class ReviewActivity(ActivityInboundInterceptor):
    def __init__(self, next: ActivityInboundInterceptor, service: ProductMemoryReviewService):
        super().__init__(next)
        self.service = service

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        if not info.activity_type.startswith(f"agent__{AGENT_NAME}__"):
            return await self.next.execute_activity(input)
        scope = next((item for item in input.args if isinstance(item, RunScope)), None)
        if scope is None:
            raise ApplicationError("Memory-review activity lacks original authority.", non_retryable=True)
        try:
            await self.service.authorize_scope(scope)
        except (HTTPException, PermissionError):
            raise ApplicationError("Memory-review authority was superseded.", non_retryable=True) from None
        usage = InferenceActivity(
            self.service.database, scope,
            f"{info.workflow_namespace}/{info.workflow_id}/{info.workflow_run_id}/{info.activity_id}/{info.attempt}",
            logical_identity=f"{info.workflow_namespace}/{info.workflow_id}/{scope.turn_id}/{info.activity_id}",
        )

        async def invoke():
            token = INFERENCE_ACTIVITY.set(usage)
            try:
                result = await self.next.execute_activity(input)
                await usage.result(result)
                return result
            except Exception as error:
                if usage.admission_error is not None:
                    raise ApplicationError(
                        "Product admission refused this memory review.", non_retryable=True,
                    ) from error
                raise
            finally:
                INFERENCE_ACTIVITY.reset(token)

        inference = getattr(self.service.database, "product_inference", None)
        slots = getattr(inference, "slots", None)
        if slots is not None:
            return await slots.run_maintenance(invoke())
        return await invoke()


class ReviewAuthority(Interceptor):
    def __init__(self, service: ProductMemoryReviewService):
        self.service = service

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return ReviewActivity(next, self.service)


MEMORY_REVIEW_WORKFLOWS = [MemoryReviewWorkflow]
MEMORY_REVIEW_ACTIVITIES = [prepare_review, apply_review, failed_review]
