"""Native review policy/Temporal qualification; not actual-app provider proof."""

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
import pytest_asyncio
import test_product_memory as memory_fixtures
from app.memory_review import Proposal, Review
from fastapi import HTTPException
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.profiles import ModelProfile
from sqlalchemy import text, update
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_product_memory_store import memory_execution

from moyai.inference_usage import InferenceActivity
from moyai.models import Session, Turn, User
from moyai.product_drain_models import ProductDrainState
from moyai.product_memory import Preferences
from moyai.product_memory_review import (
    MEMORY_REVIEW_ACTIVITIES,
    MEMORY_REVIEW_WORKFLOWS,
    REVIEW_WORKER_MARKER,
    WORKFLOW_PREFIX,
    MemoryReviewWorkflow,
    ProductMemoryReviewService,
    ReviewAuthority,
)
from moyai.product_memory_review_models import ProductMemoryReview
from moyai.storage import ObjectStore, encrypted_converter

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
product_database = memory_fixtures.product_database
admitted_product = memory_fixtures.admitted_product
memory_product = memory_fixtures.memory_product


class LocalBudget:
    async def measure(self, payload):
        return SimpleNamespace(input_budget=8000)


def proposal(message_id, **changes):
    return Proposal(**{
        "key": "backend-testing", "title": "Backend testing",
        "content": "Prefer pytest for focused backend tests.", "kind": "preference",
        "repository_specific": False, "source_message_id": message_id,
        "source_quote": "Prefer pytest for focused backend tests.", **changes,
    })


@pytest_asyncio.fixture
async def review_product(memory_product, monkeypatch):
    product = memory_product
    product.adapter.settings.agent_model = "test"
    product.adapter.settings.memory_review_idle_seconds = 0
    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart('{"memories":[]}')]),
        profile=ModelProfile(supports_json_schema_output=True),
    )
    product.review = ProductMemoryReviewService(product.memory, {"test": model})
    monkeypatch.setattr(product.review.context_budget, "measure", LocalBudget().measure)
    async with product.database.sessions.begin() as db:
        await db.execute(update(ProductDrainState).where(ProductDrainState.id == 1).values(paused=False, generation=1))
    return product


async def completed_review(product, *, private=False, surface="web"):
    session, turn, scope, message_id = await memory_execution(product, private=private, surface=surface)
    assert await product.database.save_result(
        scope, conversation='{"messages":[]}', output="Original task completed successfully.",
        workspace=None, usage={},
    )
    async with product.database.sessions.begin() as db:
        current_turn = await db.get(Turn, turn.id)
        current_session = await db.get(Session, session.id)
        admitted = await product.review.enqueue_in(db, current_turn, current_session)
    await product.database.settle(turn.id)
    return session, turn, scope, message_id, admitted


async def test_answer_and_review_admission_commit_together_but_review_waits_for_settlement(review_product):
    product = review_product
    product.database.product_memory_review = product.review
    _, turn, scope, _ = await memory_execution(product)
    assert await product.database.save_result(
        scope, conversation='{"messages":[]}', output="Confirmed answer", workspace=None, usage={},
    )
    async with product.database.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "saving"
        assert (await db.get(ProductMemoryReview, turn.id)).status == "pending"
    waiting = await product.review.prepare(turn.id)
    assert waiting.scope is None and waiting.wait_seconds > 0
    await product.database.settle(turn.id)
    assert (await product.review.prepare(turn.id)).scope is not None


async def test_failed_answer_transaction_cannot_leave_an_admitted_review(review_product, monkeypatch):
    product = review_product
    product.database.product_memory_review = product.review
    _, turn, scope, _ = await memory_execution(product)
    enqueue = product.review.enqueue_in

    async def failed_commit(*args):
        assert await enqueue(*args)
        raise RuntimeError("Synthetic transaction failure after admission")

    monkeypatch.setattr(product.review, "enqueue_in", failed_commit)
    with pytest.raises(RuntimeError, match="Synthetic transaction"):
        await product.database.save_result(
            scope, conversation='{"messages":[]}', output="Uncommitted answer", workspace=None, usage={},
        )
    async with product.database.sessions() as db:
        assert await db.get(ProductMemoryReview, turn.id) is None
        assert (await db.get(Turn, turn.id)).result_fingerprint is None


async def test_review_applies_exact_evidence_atomically_with_canonical_policy_and_replays_once(review_product):
    product = review_product
    _, turn, scope, message_id, admitted = await completed_review(product)
    assert admitted and not product.memory.background_review_available
    prepared = await product.review.prepare(turn.id)
    assert prepared.scope.product_grant == scope.product_grant
    assert prepared.scope.surface == "memory_review"
    result = Review(memories=[proposal(message_id)])
    assert await product.review.apply(prepared, [result]) == 1
    assert await product.review.apply(prepared, [result]) == 1
    notes = (await product.memory.listing(product.principal.grant))["memories"]
    assert len(notes) == 1 and notes[0]["revision"] == 1
    assert notes[0]["source"]["capture"] == "background"
    assert notes[0]["source"]["native_message_id"] == message_id
    async with product.database.sessions() as db:
        job = await db.get(ProductMemoryReview, turn.id)
        assert job.status == "completed" and job.saved_count == 1
        assert "Prefer pytest" not in job.snapshot
        assert "Prefer pytest" not in product.database.security.unseal(job.snapshot).decode()
        original = await db.get(Turn, turn.id)
        assert original.status == "completed" and original.result_accepted
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.memory_operations")) == 1
    async with product.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == product.user_id).values(auth_version=2))
    # No new write/authorization after revocation: return only the already
    # committed operation's count on a lost-acknowledgement activity replay.
    assert await product.review.apply(prepared, [result]) == 1
    with pytest.raises(PermissionError):
        await product.review.apply(
            replace(prepared, scope=replace(prepared.scope, actor_id="google:other")), [result],
        )


@pytest.mark.parametrize("private,surface", [(True, "web"), (False, "automation")])
async def test_review_does_not_admit_private_or_automated_sources(review_product, private, surface):
    product = review_product
    _, turn, _, _, admitted = await completed_review(product, private=private, surface=surface)
    assert not admitted
    async with product.database.sessions() as db:
        assert await db.get(ProductMemoryReview, turn.id) is None


@pytest.mark.parametrize("change", ["preferences", "forget", "manual_edit", "revoke", "deleted", "drain"])
async def test_review_cannot_apply_stale_results_after_model_await(review_product, change):
    product = review_product
    saved = await product.memory.save(product.principal.grant, memory_fixtures.note())
    session, turn, _, message_id, _ = await completed_review(product)
    prepared = await product.review.prepare(turn.id)
    if change == "preferences":
        await product.memory.preferences(product.principal.grant, Preferences(enabled=False, revision=0))
    elif change == "forget":
        await product.memory.forget(product.principal.grant, saved["id"], 1)
    elif change == "manual_edit":
        await product.memory.save(product.principal.grant, memory_fixtures.note(
            revision=1, request_id="manual-later-edit", content="A newer owner instruction.",
        ), note_id=saved["id"])
    else:
        async with product.database.sessions.begin() as db:
            if change == "revoke":
                await db.execute(update(User).where(User.id == product.user_id).values(auth_version=2))
            elif change == "deleted":
                await db.execute(update(Session).where(Session.id == session.id).values(deleted=True))
            else:
                await db.execute(update(ProductDrainState).where(ProductDrainState.id == 1).values(paused=True))
    with pytest.raises((HTTPException, PermissionError)):
        await product.review.apply(prepared, [Review(memories=[proposal(message_id)])])
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 1
    async with product.database.sessions() as db:
        assert (await db.get(Turn, turn.id)).status == "completed"


async def test_review_rejects_forged_sources_and_duplicate_proposals_without_partial_saves(review_product):
    product = review_product
    _, turn, _, message_id, _ = await completed_review(product)
    prepared = await product.review.prepare(turn.id)
    first = proposal(message_id)
    for review in (
        Review(memories=[first, proposal(message_id, key="another-note", source_quote="Not a user quote")]),
        Review(memories=[first, first]),
    ):
        with pytest.raises(ValueError):
            await product.review.apply(prepared, [review])
        async with product.source.connect() as db:
            assert await db.scalar(text("SELECT count(*) FROM moyai.personal_memories")) == 0


async def test_review_preserves_manual_notes_and_skips_unseen_or_newer_facts(review_product):
    product = review_product
    old = await product.memory.save(product.principal.grant, memory_fixtures.note(key="backend-testing"))
    _, turn, _, message_id, _ = await completed_review(product)
    prepared = await product.review.prepare(turn.id)
    assert await product.review.apply(prepared, [Review(memories=[proposal(message_id)])]) == 0
    note = (await product.memory.listing(product.principal.grant))["memories"][0]
    assert note["id"] == old["id"] and note["revision"] == 1 and note["source"] == {"type": "manual"}


async def test_review_inference_requires_exact_persisted_job_and_original_authority(review_product):
    product = review_product
    _, turn, _, _, _ = await completed_review(product)
    prepared = await product.review.prepare(turn.id)
    scope = prepared.scope
    logical = f"{product.settings.temporal_namespace}/{WORKFLOW_PREFIX}{turn.id}/{turn.id}/model-0"
    for selected, identity, allowed in [
        (scope, logical, True),
        (replace(scope, surface="web"), logical, False),
        (replace(scope, actor_id="google:somebody-else"), logical, False),
        (scope, "unrelated-workflow/model-0", False),
        (replace(scope, product_grant=None), logical, False),
    ]:
        capture = InferenceActivity(product.database, selected, "local-activity", logical_identity=identity).capture(
            httpx2.Request("POST", "https://gateway.example.test/v1/responses", json={"model": "test"})
        )
        async with product.database.sessions.begin() as db:
            if allowed:
                await product.review.authorize_inference(db, capture)
            else:
                with pytest.raises((PermissionError, HTTPException)):
                    await product.review.authorize_inference(db, capture)


async def test_review_waits_for_explicit_resume_and_session_idle(review_product):
    product = review_product
    _, turn, _, _, _ = await completed_review(product)
    async with product.database.sessions.begin() as db:
        await db.execute(update(ProductDrainState).where(ProductDrainState.id == 1).values(paused=True))
    paused = await product.review.prepare(turn.id)
    assert paused.wait_seconds and paused.scope is None
    async with product.database.sessions.begin() as db:
        await db.execute(update(ProductDrainState).where(ProductDrainState.id == 1).values(paused=False))
    assert (await product.review.prepare(turn.id)).scope is not None


async def test_native_review_agent_temporal_execution_and_history_replay(review_product, monkeypatch):
    """Actual local Temporal/public SDK, controlled model; not provider evidence."""
    product = review_product
    _, turn, _, message_id, _ = await completed_review(product)
    calls = []

    def respond(messages, info):
        calls.append(messages)
        assert not info.function_tools
        return ModelResponse(parts=[TextPart(Review(memories=[proposal(message_id)]).model_dump_json())])

    product.review.models["test"] = FunctionModel(
        respond, profile=ModelProfile(supports_json_schema_output=True),
    )
    product.review = ProductMemoryReviewService(product.memory, product.review.models)
    monkeypatch.setattr(product.review.context_budget, "measure", LocalBudget().measure)
    runtime = SimpleNamespace(product_memory_review=product.review)
    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    MemoryReviewWorkflow.__pydantic_ai_agents__ = [product.review.agent]
    objects = ObjectStore(product.settings, product.database.security)
    converter = encrypted_converter(product.database.security, objects)
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        async def connect():
            return environment.client
        runtime.connect = connect
        product.review.runtime = runtime
        product.review.mark_registered()
        assert not await product.review.worker_ready()
        async with Worker(
            environment.client, task_queue=product.settings.temporal_queue,
            identity="memory-qualification" + REVIEW_WORKER_MARKER,
            workflows=MEMORY_REVIEW_WORKFLOWS, activities=MEMORY_REVIEW_ACTIVITIES,
            interceptors=[ReviewAuthority(product.review)],
        ):
            for _ in range(30):
                product.review._ready_until = 0
                if await product.review.worker_ready():
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Both real Temporal poller types must advertise review registration.")
            await product.review.status(product.user_id)
            assert product.memory.background_review_available
            handle = await environment.client.start_workflow(
                MemoryReviewWorkflow.run, turn.id, id=WORKFLOW_PREFIX + turn.id,
                task_queue=product.settings.temporal_queue,
            )
            assert await asyncio.wait_for(handle.result(), 45) == {"status": "completed", "saved_count": 1}
            history = await handle.fetch_history()
        await Replayer(
            workflows=MEMORY_REVIEW_WORKFLOWS, plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    assert len(calls) == 1
    async with product.database.sessions() as db:
        assert (await db.get(ProductMemoryReview, turn.id)).saved_count == 1
