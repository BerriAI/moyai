"""Real PG and Temporal maintenance boundaries; actual four-role proof is separate."""

from __future__ import annotations

import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
import test_product_authority as authority_fixtures
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from moyai.contracts import NewMessage, NewSession
from moyai.inference_usage import InferenceUsage
from moyai.models import Effect, Session, Turn, User, now
from moyai.product_drain import ProductDrainError, ProductDrainService
from moyai.product_drain_models import ProductDrainReceipt, ProductDrainState
from moyai.product_identity import ProductIdentityAdapter
from moyai.product_inference import ProductInferenceRequest
from moyai.product_role_authority import ProductOwnershipLost, ProductRoleAuthority
from moyai.runtime import Runtime
from moyai.storage import encrypted_converter
from moyai.stores import NativeStores
from moyai.worker import make_worker
from moyai.worker_capacity import ActivityCapacity
from moyai.workflow import TurnWorkflow

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable local PostgreSQL required.",
)
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product


@pytest_asyncio.fixture
async def drain(admitted_product):
    product = admitted_product
    async with product.source.begin() as db:
        await db.execute(text(
            "CREATE TABLE moyai.runtime_policy(id integer PRIMARY KEY,fingerprint text)"
        ))
        await db.execute(text(
            "CREATE TABLE moyai.runtime_worker_policy("
            "id integer PRIMARY KEY,cluster_fingerprint text,fingerprint text)"
        ))
        await db.execute(text("INSERT INTO moyai.runtime_policy VALUES(1,:policy)"), {"policy": "a" * 64})
        await db.execute(text("INSERT INTO moyai.runtime_worker_policy VALUES(1,:cluster,:role)"), {
            "cluster": "a" * 64, "role": "b" * 64,
        })
    owners = []
    worker_engines = []
    original_sessions = product.database.sessions

    async def worker(*, paused=False):
        owner = await asyncpg.connect(product.settings.postgres_url)
        owners.append(owner)
        await owner.execute("SELECT pg_advisory_lock_shared(726940,'moyai'::regnamespace::oid::int)")
        identity = await owner.fetchrow(
            "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()",
        )
        authority = ProductRoleAuthority(
            "moyai", "worker", identity["pid"], identity["backend_start"], "a" * 64, "b" * 64,
        )
        engine = product.database.engine
        if len(owners) > 1:
            # A deployed worker owns its pool. A marked pooled connection must
            # never be rebound to another canonical process in this fixture.
            engine = create_async_engine(
                product.settings.sqlalchemy_url,
                connect_args={"server_settings": {"search_path": f'"{product.schema}",pg_catalog'}},
            )
            worker_engines.append(engine)
        sessions = authority.sessions(engine)
        service = ProductDrainService(
            sessions=lambda: sessions, authority=lambda: authority,
            native_schema=product.schema, task_queue="drain-tests", maintenance_drain=paused,
        )
        await service.register_worker()
        return SimpleNamespace(owner=owner, authority=authority, sessions=sessions, service=service)

    first = await worker()
    product.database.sessions = first.sessions
    try:
        yield SimpleNamespace(product=product, first=first, worker=worker)
    finally:
        product.database.sessions = original_sessions
        for engine in worker_engines:
            await engine.dispose()
        for owner in owners:
            await owner.close()


async def test_bootstrap_and_worker_restart_never_implicitly_resume(drain):
    service = drain.first.service
    before = await service.status()
    assert before["generation"] == 0 and before["paused"] and before["state"] == "drained"
    async with drain.first.sessions.begin() as db:
        assert not await service.admit(db)
    await drain.worker()
    assert (await service.status())["generation"] == 0
    resumed = await service.request(paused=False, expected_generation=0, request_key="release:resume")
    assert resumed["generation"] == 1 and not resumed["paused"]
    assert await service.request(
        paused=False, expected_generation=0, request_key="release:resume",
    ) == resumed
    with pytest.raises(ProductDrainError, match="identity changed"):
        await service.request(paused=True, expected_generation=0, request_key="release:resume")
    with pytest.raises(ProductDrainError, match="generation changed"):
        await service.request(paused=True, expected_generation=0, request_key="stale")
    assert (await service.status())["generation"] == 1


async def test_pause_serializes_with_inflight_admission_transaction(drain):
    service = drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    claimed = asyncio.Event()
    release = asyncio.Event()

    async def admitted_transaction():
        async with drain.first.sessions.begin() as db:
            assert await service.admit(db)
            claimed.set()
            await release.wait()

    current = asyncio.create_task(admitted_transaction())
    await claimed.wait()
    pause = asyncio.create_task(service.request(paused=True, expected_generation=1, request_key="pause"))
    done, _ = await asyncio.wait({pause}, timeout=.1)
    assert not done
    release.set()
    await current
    assert (await pause)["generation"] == 2
    async with drain.first.sessions.begin() as db:
        assert not await service.admit(db)


async def test_old_drain_worker_must_retire_before_explicit_resume(drain):
    service = drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    draining = await drain.worker(paused=True)
    assert (await service.status())["paused"]
    replacement = await drain.worker()
    with pytest.raises(ProductDrainError, match="has not retired"):
        await replacement.service.request(paused=False, expected_generation=2, request_key="resume")
    await draining.owner.close()
    resumed = await replacement.service.request(paused=False, expected_generation=2, request_key="resume")
    assert resumed["generation"] == 3 and not resumed["paused"]
    await replacement.owner.close()
    with pytest.raises(ProductOwnershipLost):
        await replacement.service.request(paused=True, expected_generation=3, request_key="revoked-owner")


async def test_checkpoint_retirement_requires_same_pause_and_saved_work(drain):
    service = drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    with pytest.raises(ProductDrainError, match="pause changed"):
        await service.require_checkpoint_retirement("start")
    receipt = await service.request(paused=True, expected_generation=1, request_key="adopt")
    await service.require_checkpoint_retirement("adopt")
    with pytest.raises(ProductDrainError, match="pause changed"):
        await service.require_checkpoint_retirement("other")
    session = await drain.product.database.create_session(drain.product.actor, NewSession())
    turn = await drain.product.database.submit(
        drain.product.actor, session.id, NewMessage(prompt="queued work", request_key="queued"),
    )
    # Queued inputs are preserved; an uncheckpointed running turn is unsafe.
    await service.require_checkpoint_retirement("adopt")
    async with drain.first.sessions.begin() as db:
        row = await db.get(Turn, turn.id)
        row.status = "running"
    with pytest.raises(ProductDrainError, match="unsafe work"):
        await service.require_checkpoint_retirement("adopt")
    assert (await service.status())["generation"] == receipt["generation"]


async def test_sealed_unversioned_owner_blocks_resume_even_after_drain_receipt(drain):
    import psycopg

    from moyai.fleet_registry import FleetRegistry

    first = drain.first
    replacement = await drain.worker()
    with psycopg.connect(drain.product.settings.postgres_url, autocommit=True) as connection:
        registry = FleetRegistry(connection)
        registry.initialize()
        connection.execute(registry.query(
            "INSERT INTO {schema}.fleets VALUES('adoption','moyai',%s,'drain-tests',true)",
        ), (drain.product.schema,))
        connection.execute(registry.query("""
            INSERT INTO {schema}.roles VALUES(
                'adoption','blue','worker','source','srv-test',%s,%s,%s,'','','draining','pause',%s
            )
        """), ("a" * 40, "a" * 64, "b" * 64, "c" * 64))
        connection.execute(registry.query("""
            INSERT INTO {schema}.members VALUES(
                'adoption','blue','worker','srv-test','srv-test-local',%s,%s,%s,%s,'pause','pause',now(),now()
            )
        """), ("d" * 32, first.authority.owner_pid, first.authority.owner_started, "e" * 64))
    with pytest.raises(ProductDrainError, match="sealed adoption worker"):
        await replacement.service.request(paused=False, expected_generation=0, request_key="resume")
    await first.owner.close()
    receipt = await replacement.service.request(paused=False, expected_generation=0, request_key="resume")
    assert not receipt["paused"] and receipt["generation"] == 1


async def test_unknown_effect_blocks_park_and_resume_without_rewriting_it(drain):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, turn, _ = await authority_fixtures.active_turn(product)
    async with product.database.sessions.begin() as db:
        db.add(Effect(
            id="unknown-test-effect", session_id=session.id, turn_id=turn.id,
            actor_id=product.actor.id, tool_name="shell", arguments="synthetic-fixture",
            fingerprint="e" * 64, state="pending",
        ))
    await service.request(paused=True, expected_generation=1, request_key="pause")
    decision = await service.checkpoint(
        turn.id, workflow_id=turn.workflow_id, workflow_run_id="00000000-0000-0000-0000-000000000001",
        boundary="native_node:1",
    )
    assert decision.paused and decision.generation == 2
    observed = await service.status()
    assert observed["state"] == "draining" and observed["counts"]["effects"] == 1
    assert observed["counts"]["unsafe_turns"] == 1
    with pytest.raises(ProductDrainError, match="unknown outcomes"):
        await service.request(paused=False, expected_generation=2, request_key="unsafe-resume")
    async with product.database.sessions() as db:
        effect = await db.get(Effect, "unknown-test-effect")
        receipt = await db.get(ProductDrainReceipt, (turn.id, 2))
        assert effect.state == "pending" and effect.resolution is None
        assert receipt.phase == "blocked"
        assert (await db.get(ProductDrainState, 1)).generation == 2


@pytest.mark.parametrize("status", ["included", "steering", "signal_sent", "accepted", "unknown"])
async def test_terminal_steering_input_does_not_block_drain_or_resume(drain, status):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, parent, _ = await authority_fixtures.active_turn(product)
    async with product.database.sessions.begin() as db:
        stored = await db.get(Turn, parent.id)
        assert stored is not None
        stored.status, stored.finished_at = "failed", now()
        saved_session = await db.get(Session, session.id)
        assert saved_session is not None
        saved_session.status, saved_session.active_turn = "failed", None
        child = Turn(
            session_id=session.id, actor_id=product.actor.id, prompt="Synthetic steering input",
            request_key="steering-input", request_fingerprint="f" * 64, model=parent.model,
            parent_turn=parent.id, status=status, workflow_id="steering-input-workflow",
            finished_at=now() if status == "included" else None,
        )
        db.add(child)
        await db.flush()
        child_id, finished_at = child.id, child.finished_at
    await service.request(paused=True, expected_generation=1, request_key="pause")
    observed = await service.status()
    terminal = status == "included"
    assert observed["counts"]["unsafe_turns"] == (0 if terminal else 1)
    assert observed["counts"]["parked"] == 0
    assert observed["state"] == ("drained" if terminal else "draining")
    if terminal:
        assert not (await service.request(
            paused=False, expected_generation=2, request_key="resume",
        ))["paused"]
    else:
        with pytest.raises(ProductDrainError, match="unknown outcomes"):
            await service.request(paused=False, expected_generation=2, request_key="resume")
    async with product.database.sessions() as db:
        saved = await db.get(Turn, child_id)
        assert saved is not None
        assert (saved.status, saved.finished_at, saved.parent_turn) == (status, finished_at, parent.id)
        assert await db.get(ProductDrainReceipt, (child_id, 2)) is None


async def test_included_input_does_not_hide_an_unsettled_effect(drain):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, turn, _ = await authority_fixtures.active_turn(product)
    async with product.database.sessions.begin() as db:
        saved = await db.get(Turn, turn.id)
        assert saved is not None
        saved.status, saved.finished_at = "included", now()
        db.add(Effect(
            id="included-unknown-effect", session_id=session.id, turn_id=turn.id,
            actor_id=product.actor.id, tool_name="shell", arguments="synthetic-fixture",
            fingerprint="e" * 64, state="pending",
        ))
    await service.request(paused=True, expected_generation=1, request_key="pause")
    observed = await service.status()
    assert observed["counts"]["unsafe_turns"] == 0 and observed["counts"]["effects"] == 1
    assert observed["state"] == "draining"
    with pytest.raises(ProductDrainError, match="unknown outcomes"):
        await service.request(paused=False, expected_generation=2, request_key="resume")


async def accepted_steering(product, session, parent):
    """Use the real admission and durable steering acknowledgement transitions."""
    child = await product.database.submit(product.actor, session.id, NewMessage(
        prompt="Keep the same task, with this correction.", request_key="accepted-correction", mode="asap",
    ))
    assert child.status == "steering" and child.parent_turn == parent.id
    await product.database.signal_attempted(child.id)
    await product.database.steering_receipt(child.id, "durable-sdk-enqueue")
    async with product.database.sessions() as db:
        return await db.get(Turn, child.id)


@pytest.mark.parametrize("parent_state", [
    "parked", "prepared_before_prepare", "deferred_waiting", "deferred_waiting_unprepared",
    "completed", "failed", "stopped", "resumed",
])
async def test_accepted_steering_inherits_only_its_safe_parent_without_rewriting_rows(drain, parent_state):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, parent, _ = await authority_fixtures.active_turn(product)
    child = await accepted_steering(product, session, parent)
    original = (child.status, child.parent_turn, child.enqueue_id, child.accepted_at, child.finished_at)
    await service.request(paused=True, expected_generation=1, request_key="pause")
    assert (await service.status())["counts"]["unsafe_turns"] == 2
    if parent_state in {"parked", "prepared_before_prepare"}:
        await service.checkpoint(
            parent.id, workflow_id=parent.workflow_id, workflow_run_id="parent-run",
            boundary="before_prepare" if parent_state == "prepared_before_prepare" else "native_node:2",
        )
    elif parent_state.startswith("deferred_waiting"):
        async with product.database.sessions.begin() as db:
            stored = await db.get(Turn, parent.id)
            stored.status, stored.result_accepted, stored.conversation = "waiting", True, "saved-conversation"
            if parent_state == "deferred_waiting_unprepared":
                stored.preparation = None
        await service.park_deferred(
            parent.id, 2, workflow_id=parent.workflow_id, workflow_run_id="completed-parent-run",
        )
    else:
        async with product.database.sessions.begin() as db:
            stored = await db.get(Turn, parent.id)
            stored.status, stored.finished_at = parent_state, now()
    observed = await service.status()
    assert observed["state"] == "drained"
    assert observed["counts"] == {
        "queued": 0, "parked": int(parent_state not in {"completed", "failed", "stopped", "resumed"}),
        "unsafe_turns": 0, "effects": 0, "requests": 0,
    }
    assert not (await service.request(
        paused=False, expected_generation=2, request_key="resume",
    ))["paused"]
    async with product.database.sessions() as db:
        saved = await db.get(Turn, child.id)
        assert (saved.status, saved.parent_turn, saved.enqueue_id, saved.accepted_at, saved.finished_at) == original
        assert await db.get(ProductDrainReceipt, (child.id, 2)) is None


@pytest.mark.parametrize("invalid", [
    "missing_parent", "no_parent", "self_parent", "other_session", "other_actor", "other_model",
    "not_steering", "no_enqueue", "no_acceptance", "parent_is_input",
    "preparation", "result", "conversation", "deferred_results", "result_accepted", "finished_at",
    "result_fingerprint", "error", "usage", "malformed_usage", "usage_wrong_shape",
    "connections", "malformed_connections", "connections_wrong_shape", "connection_id_wrong_type",
    "parent_malformed_connections", "parent_connections_wrong_shape", "parent_connection_id_wrong_type",
])
async def test_accepted_steering_with_invalid_parent_or_acceptance_still_blocks_resume(drain, invalid):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, parent, _ = await authority_fixtures.active_turn(product)
    child = await accepted_steering(product, session, parent)
    other_session = await product.database.create_session(product.actor, NewSession())
    await service.request(paused=True, expected_generation=1, request_key="pause")
    await service.checkpoint(
        parent.id, workflow_id=parent.workflow_id, workflow_run_id="parent-run", boundary="native_node:2",
    )
    async with product.database.sessions.begin() as db:
        saved = await db.get(Turn, child.id)
        stored_parent = await db.get(Turn, parent.id)
        if invalid == "missing_parent":
            saved.parent_turn = "missing"
        elif invalid == "no_parent":
            saved.parent_turn = None
        elif invalid == "self_parent":
            saved.parent_turn = child.id
        elif invalid == "other_session":
            saved.session_id = other_session.id
        elif invalid == "other_actor":
            # Both are real users; only the stored relationship is invalid.
            db.add(User(
                id="google:other-sub", username="other", display_name="Other",
                password="synthetic-unused-password", role="member",
            ))
            await db.flush()
            stored_parent.actor_id = "google:other-sub"
        elif invalid == "other_model":
            saved.model = "other-model"
        elif invalid == "not_steering":
            saved.mode = "when_idle"
        elif invalid == "no_enqueue":
            saved.enqueue_id = None
        elif invalid == "no_acceptance":
            saved.accepted_at = None
        elif invalid in {
            "preparation", "result", "conversation", "deferred_results", "finished_at", "result_fingerprint", "error",
        }:
            setattr(saved, invalid, "independent-execution-evidence")
        elif invalid == "result_accepted":
            saved.result_accepted = True
        elif invalid == "usage":
            saved.usage = '{"requests":1}'
        elif invalid == "malformed_usage":
            saved.usage = "{"
        elif invalid == "usage_wrong_shape":
            saved.usage = "[]"
        elif invalid == "connections":
            saved.connections = '["different-connection"]'
        elif invalid == "malformed_connections":
            saved.connections = "["
        elif invalid == "connections_wrong_shape":
            saved.connections = "{}"
        elif invalid == "connection_id_wrong_type":
            saved.connections = "[{}]"
        elif invalid == "parent_malformed_connections":
            stored_parent.connections = "["
        elif invalid == "parent_connections_wrong_shape":
            stored_parent.connections = "{}"
        elif invalid == "parent_connection_id_wrong_type":
            stored_parent.connections = "[{}]"
        else:
            stored_parent.parent_turn = child.id
    observed = await service.status()
    assert observed["state"] == "draining" and observed["counts"]["unsafe_turns"] == 1
    with pytest.raises(ProductDrainError, match="unknown outcomes"):
        await service.request(paused=False, expected_generation=2, request_key="resume")


@pytest.mark.parametrize("invalid", [
    "running", "stopping", "queued", "unknown", "blocked", "stale_generation",
    "wrong_workflow", "resumed_receipt", "waiting_no_result", "waiting_no_conversation", "waiting_wrong_boundary",
    "unprepared_running", "unprepared_starting", "unprepared_saving",
])
async def test_accepted_steering_cannot_cover_an_unsafe_parent(drain, invalid):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, parent, _ = await authority_fixtures.active_turn(product)
    await accepted_steering(product, session, parent)
    await service.request(paused=True, expected_generation=1, request_key="pause")
    async with product.database.sessions.begin() as db:
        stored = await db.get(Turn, parent.id)
        if invalid in {"running", "stopping", "queued", "unknown"}:
            stored.status = invalid
        elif invalid.startswith("waiting"):
            stored.status = "waiting"
            stored.result_accepted = invalid != "waiting_no_result"
            stored.conversation = None if invalid == "waiting_no_conversation" else "saved-conversation"
        elif invalid.startswith("unprepared"):
            stored.status = invalid.removeprefix("unprepared_")
            stored.preparation = None
        if invalid != "running":
            db.add(ProductDrainReceipt(
                turn_id=parent.id, generation=1 if invalid == "stale_generation" else 2,
                workflow_id="wrong-workflow" if invalid == "wrong_workflow" else parent.workflow_id,
                workflow_run_id="parent-run",
                boundary="deferred_waiting" if invalid.startswith("waiting") and invalid != "waiting_wrong_boundary"
                else "before_prepare" if invalid == "unprepared_starting" else "native_node:2",
                phase="blocked" if invalid == "blocked" else "parked",
                resumed_generation=3 if invalid == "resumed_receipt" else None,
            ))
    observed = await service.status()
    assert observed["state"] == "draining" and observed["counts"]["unsafe_turns"] >= 1
    with pytest.raises(ProductDrainError, match="unknown outcomes"):
        await service.request(paused=False, expected_generation=2, request_key="resume")


async def test_accepted_steering_compares_connection_sets_and_empty_usage_semantically(drain):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, parent, _ = await authority_fixtures.active_turn(product)
    child = await accepted_steering(product, session, parent)
    async with product.database.sessions.begin() as db:
        saved, stored_parent = await db.get(Turn, child.id), await db.get(Turn, parent.id)
        saved.usage = " { } "
        saved.connections = '["connection-b", "connection-a"]'
        stored_parent.connections = '["connection-a", "connection-b"]'
    await service.request(paused=True, expected_generation=1, request_key="pause")
    await service.checkpoint(
        parent.id, workflow_id=parent.workflow_id, workflow_run_id="parent-run", boundary="native_node:2",
    )
    assert (await service.status())["state"] == "drained"
    assert not (await service.request(
        paused=False, expected_generation=2, request_key="resume",
    ))["paused"]


@pytest.mark.parametrize("kind", ["effect", "usage", "broker_request"])
@pytest.mark.parametrize("target", ["parent", "child"])
async def test_accepted_steering_never_hides_outstanding_effects_or_requests(drain, kind, target):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    session, parent, _ = await authority_fixtures.active_turn(product)
    child = await accepted_steering(product, session, parent)
    await service.request(paused=True, expected_generation=1, request_key="pause")
    await service.checkpoint(
        parent.id, workflow_id=parent.workflow_id, workflow_run_id="parent-run", boundary="native_node:2",
    )
    turn = parent if target == "parent" else child
    async with product.database.sessions.begin() as db:
        if kind == "effect":
            row = Effect(
                id="pending-input-effect", session_id=session.id, turn_id=turn.id,
                actor_id=product.actor.id, tool_name="shell", arguments="synthetic-fixture",
                fingerprint="e" * 64, state="pending",
            )
        else:
            values = {
                "id": "pending-input-request", "request_key": "pending-input-request",
                "session_id": session.id, "turn_id": turn.id, "actor_id": product.actor.id,
                "model": parent.model, "status": "pending", "created_at": now(),
            }
            row = InferenceUsage(**values, provider_scope="a" * 64) if kind == "usage" else ProductInferenceRequest(
                **values, logical_key="pending-input-request", authority_version="a" * 64,
                gateway_scope="a" * 64, key_hash="b" * 64,
                owner_pid=drain.first.authority.owner_pid, owner_started=service._started(drain.first.authority),
            )
        db.add(row)
    observed = await service.status()
    assert observed["counts"]["unsafe_turns"] == 0
    assert observed["counts"]["effects"] == int(kind == "effect")
    assert observed["counts"]["requests"] == int(kind != "effect")
    assert observed["state"] == "draining"
    with pytest.raises(ProductDrainError, match="unknown outcomes"):
        await service.request(paused=False, expected_generation=2, request_key="resume")
    async with product.database.sessions() as db:
        saved = await db.get(type(row), row.id)
        assert (saved.state if kind == "effect" else saved.status) == "pending"


async def test_checkpoint_is_idempotent_and_resume_preserves_generation_receipts(drain):
    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="start")
    _, turn, _ = await authority_fixtures.active_turn(product)
    await service.request(paused=True, expected_generation=1, request_key="pause")
    args = {
        "workflow_id": turn.workflow_id, "workflow_run_id": "00000000-0000-0000-0000-000000000002",
        "boundary": "native_node:2",
    }
    await asyncio.gather(service.checkpoint(turn.id, **args), service.checkpoint(turn.id, **args))
    observed = await service.status()
    assert observed["state"] == "drained" and observed["counts"]["parked"] == 1
    await service.request(paused=False, expected_generation=2, request_key="resume")
    assert not (await service.checkpoint(turn.id, **args)).paused
    async with product.database.sessions() as db:
        receipts = list(await db.scalars(select(ProductDrainReceipt).where(
            ProductDrainReceipt.turn_id == turn.id,
        )))
        assert len(receipts) == 1 and receipts[0].generation == 2
        assert receipts[0].resumed_generation == 3 and receipts[0].resumed_at


@asynccontextmanager
async def drain_runtime(drain, tmp_path, model):
    """Real SDK/Temporal/PG stores with a controlled model, not live-provider proof."""
    product = drain.product
    settings = product.settings.model_copy(update={"product_mode": "standalone"})
    provision = NativeStores(settings)
    try:
        await provision.start(provision=True)
    finally:
        await provision.close()
    runtime = Runtime(settings)
    runtime.product_identity = ProductIdentityAdapter(
        product.adapter.settings, product.source, schema="moyai", native_engine=runtime.database.engine,
    )
    runtime.database.product_identity = runtime.product_identity
    computer = tmp_path / "computer"
    computer.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "skills", computer / "skills")
    try:
        await runtime.start(models={"test": model}, workspace=LocalWorkspace(computer), skills_directory="skills")
        runtime.database.sessions = drain.first.authority.sessions(runtime.database.engine)
        runtime.stores.bind_authority(drain.first.authority)
        runtime.product_drain = drain.first.service
        runtime.database.product_drain = drain.first.service
        yield runtime
    finally:
        await runtime.close()


async def wait_state(service, expected, *, timeout=15):
    async with asyncio.timeout(timeout):
        while True:
            value = await service.status()
            if value["state"] == expected:
                return value
            await asyncio.sleep(.05)


async def test_temporal_parks_before_prepare_then_resumes_without_repeating_model(drain, tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    service, product = drain.first.service, drain.product
    await service.request(paused=False, expected_generation=0, request_key="initial")
    _, turn, _ = await authority_fixtures.active_turn(product)
    await service.request(paused=True, expected_generation=1, request_key="pause")
    calls = []

    def model(messages, info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("Resumed exactly once.")])

    async def stream(messages, info):
        calls.append(True)
        yield "Resumed exactly once."

    async with drain_runtime(drain, tmp_path, FunctionModel(model, stream_function=stream)) as runtime:
        converter = encrypted_converter(runtime.security, runtime.objects)
        environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                state = await wait_state(service, "drained")
                assert state["counts"]["parked"] == 1 and calls == []
                assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
            # Polling restarts while the workflow is safely parked. It must not
            # reinterpret the pause as Stop or execute the model before resume.
            async with make_worker(runtime):
                await service.request(paused=False, expected_generation=2, request_key="resume")
                await service.notify(runtime)
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
                history = await handle.fetch_history()
            assert calls == [True]
            await Replayer(
                interceptors=[ActivityCapacity()],
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(history)
            assert calls == [True]


async def test_completed_deferred_workflow_gets_evidence_based_park_receipt(drain, tmp_path):
    from scenarios import integration_model

    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="initial")
    _, turn, _ = await authority_fixtures.active_turn(product)
    async with drain_runtime(drain, tmp_path, integration_model("question")) as runtime:
        converter = encrypted_converter(runtime.security, runtime.objects)
        environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "waiting"
                description = await handle.describe()
                assert description.status == WorkflowExecutionStatus.COMPLETED
                await service.request(paused=True, expected_generation=1, request_key="pause")
                assert (await service.status())["state"] == "draining"
                await service.notify(runtime)
                state = await service.status()
                assert state["state"] == "drained" and state["counts"]["parked"] == 1
                async with product.database.sessions() as db:
                    receipt = await db.get(ProductDrainReceipt, (turn.id, 2))
                    assert receipt.boundary == "deferred_waiting"
                    assert receipt.workflow_run_id == description.run_id
                await service.request(paused=False, expected_generation=2, request_key="resume")
                # Maintenance does not answer the user's question or start another model call.
                assert (await handle.describe()).status == WorkflowExecutionStatus.COMPLETED


async def test_active_parallel_batch_settles_before_parking_without_cancellation(drain, tmp_path):
    from scenarios import integration_model
    from test_effect_concurrency import ParallelChildren, install_agent

    product, service = drain.product, drain.first.service
    await service.request(paused=False, expected_generation=0, request_key="initial")
    _, turn, _ = await authority_fixtures.active_turn(product)
    async with drain_runtime(drain, tmp_path, integration_model("test")) as runtime:
        scenario = ParallelChildren()
        install_agent(runtime, scenario)
        converter = encrypted_converter(runtime.security, runtime.objects)
        environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
        async with environment:
            runtime.client = environment.client
            async with make_worker(runtime):
                handle = await runtime.client.start_workflow(
                    TurnWorkflow.run, turn.id, id=turn.workflow_id, task_queue=runtime.settings.temporal_queue,
                )
                await asyncio.wait_for(scenario.shell_started.wait(), 20)
                await service.request(paused=True, expected_generation=1, request_key="pause")
                await service.notify(runtime)
                assert (await service.status())["state"] == "draining"
                assert scenario.active_shells == 1  # Pause is not cancellation.
                scenario.release_shell.set()
                await wait_state(service, "drained")
                assert scenario.active_shells == 0 and sorted(scenario.executed) == list(range(5))
                async with product.database.sessions() as db:
                    rows = list(await db.scalars(select(Effect).where(Effect.turn_id == turn.id)))
                    assert len(rows) == 5 and all(row.state == "confirmed" for row in rows)
                assert (await handle.describe()).status == WorkflowExecutionStatus.RUNNING
                await service.request(paused=False, expected_generation=2, request_key="resume")
                await service.notify(runtime)
                assert (await asyncio.wait_for(handle.result(), 30))["status"] == "completed"
                history = await handle.fetch_history()
            await Replayer(
                interceptors=[ActivityCapacity()],
                workflows=[TurnWorkflow], plugins=[PydanticAIPlugin()], data_converter=converter,
            ).replay_workflow(history)
            assert sorted(scenario.executed) == list(range(5))
