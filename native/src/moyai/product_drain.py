"""Maintenance control around the existing SDK/Temporal execution owner."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from .inference_usage import InferenceUsage
from .models import ComputerOperation, Effect, Turn, now
from .product_drain_models import ProductDrainReceipt, ProductDrainState, ProductDrainWorker
from .product_inference import ProductInferenceRequest
from .product_role_authority import ProductRoleAuthority

if TYPE_CHECKING:
    from .runtime import Runtime

DRAIN_VERSION = "moyai-product-maintenance-v1"
# Included steering inputs are finished; the parent retains its own drain checks.
TERMINAL_TURNS = ("completed", "failed", "stopped", "resumed", "included")
SETTLED_REQUESTS = ("completed", "failed", "not_dispatched")


class ProductDrainError(RuntimeError):
    """Fixed operator messages; no credentials, content or SQL details."""


@dataclass(frozen=True)
class DrainDecision:
    generation: int
    paused: bool


class ProductDrainService:
    def __init__(
        self, *, sessions: Callable[[], async_sessionmaker[AsyncSession]],
        authority: Callable[[], ProductRoleAuthority | None],
        native_schema: str, task_queue: str, maintenance_drain: bool = False,
    ):
        self.sessions, self.authority = sessions, authority
        self.native_schema, self.task_queue = native_schema, task_queue
        self.maintenance_drain = maintenance_drain

    @classmethod
    def from_runtime(cls, runtime: Runtime) -> ProductDrainService:
        product, config = runtime.product, runtime.product_config
        if product is None or config is None or config.role == "standalone":
            raise ProductDrainError("Maintenance control requires a distributed product role.")
        return cls(
            sessions=lambda: runtime.database.sessions, authority=lambda: product.authority,
            native_schema=config.native_schema or "", task_queue=config.native_task_queue,
            maintenance_drain=product.settings.maintenance_drain,
        )

    async def admit(self, db: AsyncSession) -> bool:
        """Caller retains this share lock through queued -> starting admission."""
        row = await db.get(ProductDrainState, 1, with_for_update={"read": True})
        # Only the reviewed v12 migration may create the bootstrap receipt.
        return row is not None and not row.paused

    def _worker_authority(self) -> ProductRoleAuthority:
        authority = self.authority()
        if authority is None or authority.role != "worker":
            raise ProductDrainError("A verified canonical worker owner is required.")
        return authority

    @staticmethod
    def _started(owner: ProductRoleAuthority) -> str:
        return owner.owner_started.astimezone(UTC).isoformat()

    @staticmethod
    async def _live_workers(db: AsyncSession) -> list[ProductDrainWorker]:
        return list(await db.scalars(select(ProductDrainWorker).where(text("""
            EXISTS (
                SELECT 1 FROM pg_stat_activity a
                WHERE a.pid=moyai_product_drain_workers.owner_pid
                AND a.backend_start=CAST(moyai_product_drain_workers.owner_started AS timestamptz)
            )
        """)).order_by(ProductDrainWorker.registered_at, ProductDrainWorker.id)))

    async def register_worker(self) -> None:
        """Publish intent before SDK polling; an undrained worker never auto-resumes."""
        owner = self._worker_authority()
        started = self._started(owner)
        identity = hashlib.sha256(f"{owner.owner_pid}:{started}".encode()).hexdigest()
        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1, with_for_update=True)
            if state is None:
                raise ProductDrainError("The maintenance bootstrap receipt is missing.")
            previous = await db.get(ProductDrainWorker, identity)
            if previous is None:
                db.add(ProductDrainWorker(
                    id=identity, owner_pid=owner.owner_pid, owner_started=started,
                    requested_drain=self.maintenance_drain, source_policy=owner.cluster_policy,
                    worker_policy=owner.role_policy,
                ))
            elif previous.requested_drain != self.maintenance_drain:
                raise ProductDrainError("A live worker cannot change its declared maintenance mode.")
            if self.maintenance_drain and not state.paused:
                self._write_intent(
                    state, owner, paused=True, request_key=f"worker:{identity}",
                    fingerprint=hashlib.sha256(f"worker-pause:{identity}".encode()).hexdigest(),
                )

    @staticmethod
    def _write_intent(
        state: ProductDrainState, owner: ProductRoleAuthority, *,
        paused: bool, request_key: str, fingerprint: str,
    ) -> None:
        state.generation += 1
        state.paused, state.request_key, state.request_fingerprint = paused, request_key, fingerprint
        state.requested_at = now()
        state.owner_pid, state.owner_started = owner.owner_pid, ProductDrainService._started(owner)
        state.source_policy = owner.cluster_policy

    async def request(
        self, *, paused: bool, expected_generation: int, request_key: str,
    ) -> dict:
        """Single CAS intent; an uncertain caller must inspect status before retrying."""
        owner = self._worker_authority()
        if not 1 <= len(request_key) <= 200 or expected_generation < 0:
            raise ProductDrainError("The maintenance intent is invalid.")
        fingerprint = hashlib.sha256(json.dumps({
            "paused": paused, "expected_generation": expected_generation, "request_key": request_key,
            "owner_pid": owner.owner_pid, "owner_started": self._started(owner),
            "source_policy": owner.cluster_policy, "worker_policy": owner.role_policy,
            "native_schema": self.native_schema, "task_queue": self.task_queue,
        }, sort_keys=True).encode()).hexdigest()
        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1, with_for_update=True)
            if state is None:
                raise ProductDrainError("The maintenance bootstrap receipt is missing.")
            if state.request_key == request_key:
                if state.request_fingerprint != fingerprint:
                    raise ProductDrainError("The maintenance intent identity changed.")
                return self._receipt(state)
            if state.generation != expected_generation:
                raise ProductDrainError("The maintenance generation changed; inspect its receipt.")
            workers = await self._live_workers(db)
            expected = [
                w for w in workers if w.owner_pid == owner.owner_pid
                and w.owner_started == self._started(owner)
                and w.source_policy == owner.cluster_policy and w.worker_policy == owner.role_policy
            ]
            if len(expected) != 1:
                raise ProductDrainError("The expected native worker has not registered.")
            if not paused:
                if expected[0].requested_drain or any(w.requested_drain for w in workers):
                    raise ProductDrainError("A live maintenance worker has not retired.")
                await self._require_adoption_retired(db)
                counts = await self._counts(db, state)
                if counts["unsafe_turns"] or counts["effects"] or counts["requests"]:
                    raise ProductDrainError("Execution or unknown outcomes still prevent resume.")
            self._write_intent(state, owner, paused=paused, request_key=request_key, fingerprint=fingerprint)
            return self._receipt(state)

    async def _require_adoption_retired(self, db: AsyncSession) -> None:
        # Seal holds this same native pause row FOR SHARE until its role/member
        # capture commits. Resume already holds it FOR UPDATE, so it cannot miss
        # a concurrent seal. No pool or connection is retained through shutdown.
        from .fleet_registry import SCHEMA

        installed = await db.scalar(text("SELECT to_regclass(:table)"), {"table": SCHEMA + ".schema_version"})
        if installed is None:
            return
        live = await db.scalar(text(f"""
            SELECT EXISTS(
                SELECT 1 FROM {SCHEMA}.fleets f
                JOIN {SCHEMA}.roles r ON r.fleet=f.fleet
                JOIN {SCHEMA}.members m
                    ON (m.fleet,m.generation,m.role)=(r.fleet,r.generation,r.role)
                JOIN pg_stat_activity a ON a.pid=m.owner_pid AND a.backend_start=m.owner_started
                WHERE f.native_schema=:schema AND f.task_queue=:queue AND f.enabled
                    AND r.role='worker' AND r.worker_deployment='' AND r.worker_build=''
                    AND r.state IN ('draining','retired') AND m.drain_request=r.drain_request
            )
        """), {"schema": self.native_schema, "queue": self.task_queue})
        if live:
            raise ProductDrainError("The sealed adoption worker has not retired.")

    async def require_checkpoint_retirement(self, request_id: str) -> None:
        """Recheck durable safety immediately before an unversioned poller stops."""
        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
            if state is None or not state.paused or state.request_key != request_id:
                raise ProductDrainError("The adoption checkpoint pause changed.")
            counts = await self._counts(db, state)
            if any(counts[key] for key in ("unsafe_turns", "effects", "requests")):
                raise ProductDrainError("The adoption worker still has unsafe work.")

    def _receipt(self, state: ProductDrainState) -> dict:
        return {
            "protocol": 1, "core": "pydantic-v1", "native_schema": self.native_schema,
            "task_queue": self.task_queue, "generation": state.generation, "paused": state.paused,
            "request_key": state.request_key, "request_fingerprint": state.request_fingerprint,
            "requester": {"pid": state.owner_pid, "backend_start": state.owner_started},
            "source_policy": state.source_policy,
        }

    @staticmethod
    async def _unsettled(db: AsyncSession, turn_id: str | None = None) -> tuple[int, int]:
        from .failed_action_attempts import failed_action_attempt

        effects = select(func.count()).select_from(Effect).where(
            Effect.state.not_in(["confirmed", "not_dispatched"]),
            or_(
                ~failed_action_attempt(),
                # A failed attempt no longer requires human evidence,
                # but its bounded guest operation must finish before maintenance.
                select(ComputerOperation.session_id).where(
                    ComputerOperation.session_id == Effect.session_id,
                    ComputerOperation.expires_at > time.time(),
                ).exists(),
            ),
        )
        if turn_id is not None:
            effects = effects.where(Effect.turn_id == turn_id)
        request_count = 0
        for model in (ProductInferenceRequest, InferenceUsage):
            query = select(func.count()).select_from(model).where(model.status.not_in(SETTLED_REQUESTS))
            if turn_id is not None:
                query = query.where(model.turn_id == turn_id)
            request_count += (await db.scalar(query)) or 0
        return (await db.scalar(effects)) or 0, request_count

    async def checkpoint(
        self, turn_id: str, *, workflow_id: str, workflow_run_id: str, boundary: str,
    ) -> DrainDecision:
        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
            if state is None:
                return DrainDecision(-1, True)  # Fail closed; no invented bootstrap/park receipt.
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.workflow_id != workflow_id:
                raise ProductDrainError("The maintenance checkpoint does not own this turn.")
            if not state.paused:
                receipts = await db.scalars(select(ProductDrainReceipt).where(
                    ProductDrainReceipt.turn_id == turn_id,
                    ProductDrainReceipt.resumed_generation.is_(None),
                ))
                for receipt in receipts:
                    receipt.resumed_generation, receipt.resumed_at = state.generation, now()
                return DrainDecision(state.generation, False)
            effects, requests = await self._unsettled(db, turn_id)
            await self._park(
                db, turn_id, state.generation, workflow_id=workflow_id, workflow_run_id=workflow_run_id,
                boundary=boundary, phase="blocked" if effects or requests or turn.status == "stopping" else "parked",
            )
            return DrainDecision(state.generation, True)

    @staticmethod
    async def _park(
        db: AsyncSession, turn_id: str, generation: int, *, workflow_id: str,
        workflow_run_id: str, boundary: str, phase: str,
    ) -> None:
        values = {
            "workflow_id": workflow_id, "workflow_run_id": workflow_run_id,
            "boundary": boundary, "phase": phase,
        }
        await db.execute(insert(ProductDrainReceipt).values(
            turn_id=turn_id, generation=generation, **values,
        ).on_conflict_do_update(
            index_elements=[ProductDrainReceipt.turn_id, ProductDrainReceipt.generation], set_=values,
        ))

    async def park_deferred(
        self, turn_id: str, generation: int, *, workflow_id: str, workflow_run_id: str,
    ) -> None:
        """Caller first verifies this exact Temporal run completed with status waiting."""
        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1, with_for_update={"read": True})
            if state is None or not state.paused or state.generation != generation:
                return
            turn = await db.get(Turn, turn_id, with_for_update=True)
            if (
                turn is None or turn.status != "waiting" or not turn.result_accepted
                or not turn.conversation or turn.workflow_id != workflow_id
            ):
                return
            effects, requests = await self._unsettled(db, turn_id)
            await self._park(
                db, turn_id, generation, workflow_id=workflow_id, workflow_run_id=workflow_run_id,
                boundary="deferred_waiting", phase="blocked" if effects or requests else "parked",
            )

    async def _counts(self, db: AsyncSession, state: ProductDrainState) -> dict[str, int]:
        turns = list(await db.scalars(select(Turn).where(Turn.status.not_in(TERMINAL_TURNS))))
        receipts = list(await db.scalars(select(ProductDrainReceipt).where(
            ProductDrainReceipt.generation == state.generation,
            ProductDrainReceipt.phase == "parked",
            ProductDrainReceipt.resumed_generation.is_(None),
        )))
        parked = {r.turn_id: r for r in receipts}
        queued = sum(t.status == "queued" for t in turns)
        safe = {
            t.id for t in turns
            if t.id in parked and parked[t.id].workflow_id == t.workflow_id
            and (
                t.status in {"starting", "running", "saving"}
                or (t.status == "waiting" and bool(t.result_accepted) and bool(t.conversation)
                    and parked[t.id].boundary == "deferred_waiting")
            )
        }
        accepted = [t for t in turns if t.status == "accepted"]
        parents = {
            t.id: t for t in await db.scalars(select(Turn).where(
                Turn.id.in_(select(Turn.parent_turn).where(Turn.status == "accepted")),
            ))
        } if accepted else {}
        # Accepted steering is durably enqueued in its parent's workflow, not
        # dispatched as a separate workflow. Its parent must still prove safety;
        # never invent a checkpoint or make all accepted rows terminal.
        def safe_input(t: Turn) -> bool:
            parent = parents.get(t.parent_turn or "")
            if not (
                t.mode == "asap" and t.enqueue_id and t.accepted_at
                and t.preparation is None and t.result is None and t.conversation is None
                and t.deferred_results is None and t.result_accepted is None and t.finished_at is None
                and t.result_fingerprint is None and t.error is None
                and parent is not None and parent.parent_turn is None and parent.workflow_id
                and parent.session_id == t.session_id
                and parent.actor_id == t.actor_id and parent.model == t.model
            ):
                return False
            try:
                usage = json.loads(t.usage)
                connections = (json.loads(t.connections), json.loads(parent.connections))
            except (TypeError, ValueError):
                return False
            if (
                not isinstance(usage, dict) or usage
                or any(not isinstance(ids, list) or not all(isinstance(key, str) for key in ids)
                       for ids in connections)
                or set(connections[0]) != set(connections[1])
            ):
                return False
            # SDK enqueue cannot precede preparation of a live agent run.
            # A deferred-waiting receipt instead proves its exact completed
            # Temporal run and saved result; it does not require preparation.
            return parent.status in {"completed", "failed", "stopped", "resumed"} or (
                parent.id in safe and (parent.status == "waiting" or bool(parent.preparation))
            )

        safe_inputs = sum(safe_input(t) for t in accepted)
        effects, requests = await self._unsettled(db)
        return {
            "queued": queued, "parked": len(safe),
            "unsafe_turns": len(turns) - queued - len(safe) - safe_inputs,
            "effects": effects, "requests": requests,
        }

    async def status(self) -> dict:
        """Read observations only; never changes intent or settles an effect."""
        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1)
            if state is None:
                raise ProductDrainError("The maintenance bootstrap receipt is missing.")
            counts = await self._counts(db, state)
            workers = await self._live_workers(db)
            return {
                **self._receipt(state), "counts": counts,
                "state": (
                    "running" if not state.paused else
                    "draining" if counts["unsafe_turns"] or counts["effects"] or counts["requests"] else "drained"
                ),
                "workers": [{
                    "pid": w.owner_pid, "backend_start": w.owner_started,
                    "maintenance_drain": w.requested_drain, "source_policy": w.source_policy,
                    "worker_policy": w.worker_policy,
                } for w in workers],
            }

    async def notify(self, runtime: Runtime) -> None:
        """Wake existing workflow waits; Temporal remains the execution owner."""
        from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
        from temporalio.service import RPCError

        async with self.sessions().begin() as db:
            state = await db.get(ProductDrainState, 1)
            if state is None:
                return
            generation, paused = state.generation, state.paused
            turns = list(await db.scalars(select(Turn).where(
                Turn.status.in_(["starting", "running", "saving", "waiting"]),
            ).order_by(Turn.created_at, Turn.id).limit(100)))
            receipts = {
                r.turn_id: r for r in await db.scalars(select(ProductDrainReceipt).where(
                    ProductDrainReceipt.turn_id.in_([t.id for t in turns]),
                    ProductDrainReceipt.resumed_generation.is_(None),
                ).order_by(ProductDrainReceipt.generation))
            } if turns else {}
        client = await runtime.connect()
        for turn in turns:
            row = receipts.get(turn.id)
            if paused and row is not None and row.generation == generation and row.phase == "parked":
                continue
            if not paused and row is None:
                continue
            try:
                handle = client.get_workflow_handle(turn.workflow_id)
                if turn.status == "waiting":
                    if not paused:
                        continue  # User input, not maintenance, resumes deferred SDK requests.
                    description = await handle.describe()
                    if description.status != WorkflowExecutionStatus.COMPLETED:
                        continue
                    result = await client.get_workflow_handle(
                        turn.workflow_id, run_id=description.run_id,
                    ).result(follow_runs=False)
                    if (
                        isinstance(result, dict) and result.get("turn_id") == turn.id
                        and result.get("status") == "waiting"
                    ):
                        await self.park_deferred(
                            turn.id, generation, workflow_id=turn.workflow_id,
                            workflow_run_id=description.run_id,
                        )
                    continue
                if paused and row is not None and row.generation == generation and row.phase == "blocked":
                    async with self.sessions().begin() as db:
                        if any(await self._unsettled(db, turn.id)):
                            continue
                await handle.signal("maintenance_changed", generation)
            except (RPCError, WorkflowFailureError):
                pass  # Startup/terminal race; the dispatcher retains the durable intent.


@activity.defn(name="moyai_product_drain_checkpoint_v1")
async def drain_checkpoint(turn_id: str, boundary: str) -> DrainDecision:
    from .runtime import current

    service = getattr(current(), "product_drain", None)
    if service is None:
        return DrainDecision(0, False)  # Standalone/test contexts have no release controller.
    info = activity.info()
    return await service.checkpoint(
        turn_id, workflow_id=info.workflow_id or "", workflow_run_id=info.workflow_run_id or "",
        boundary=boundary,
    )


def main() -> None:
    from .product_drain_cli import main as command
    command()


if __name__ == "__main__":
    main()
