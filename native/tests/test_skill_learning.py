"""Real PostgreSQL policy, durable intent and canonical skill publication."""

import asyncio
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
import pytest_asyncio
import test_product_memory as memory_fixtures
from fastapi import HTTPException
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles import ModelProfile
from sqlalchemy import text, update
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_product_authority import demote
from test_product_memory_store import memory_execution

from moyai.context.learned_skills import LearnedSkills
from moyai.contracts import NewMessage, NewSession
from moyai.inference_usage import InferenceActivity
from moyai.models import MessageSource, Session, User
from moyai.product_drain_models import ProductDrainState
from moyai.product_memory_review import ReviewAuthority
from moyai.product_skill_catalog import ProductSkillCatalog
from moyai.product_skill_learning import AGENT_NAME, WORKFLOW_PREFIX, LearningUpdate, SkillLearningService
from moyai.product_skill_learning_models import LearnedSkill, SkillLearningJob
from moyai.skill_learning_prompt import SkillProposal, SkillReview
from moyai.skill_learning_workflow import (
    SKILL_LEARNING_ACTIVITIES,
    SKILL_LEARNING_WORKFLOWS,
    SkillLearningWorkflow,
)
from moyai.storage import ObjectStore, encrypted_converter
from moyai.worker_capacity import ActivityCapacity

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = memory_fixtures.product_database
admitted_product = memory_fixtures.admitted_product
memory_product = memory_fixtures.memory_product

ANSWER = (
    "Verified the Acorn importer regression: remove the local fixture cache before running "
    "pytest tests/importer/test_reload.py. The stale cache hid the changed fixture; the test "
    "passes after clearing it. This applies only to the Acorn importer test environment."
)


@pytest_asyncio.fixture
async def learning(memory_product, monkeypatch):
    product = memory_product
    async with product.source.begin() as db:
        await db.execute(text(
            "CREATE TABLE moyai.skills (id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,"
            "encrypted TEXT NOT NULL,scope TEXT NOT NULL,owner_id TEXT NOT NULL,namespace TEXT NOT NULL,"
            "revision INTEGER NOT NULL,archived INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,"
            "client_id TEXT NOT NULL,icon TEXT NOT NULL,UNIQUE(namespace,name),UNIQUE(owner_id,client_id))"
        ))
        await db.execute(text(
            "CREATE TABLE moyai.skill_bundles(skill_id TEXT,revision INTEGER,encrypted TEXT,manifest TEXT,"
            "PRIMARY KEY(skill_id,revision))"
        ))
        await db.execute(text(
            "CREATE TABLE moyai.skill_audit(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,"
            "skill_id TEXT,actor_id TEXT,action TEXT,created_at TEXT)"
        ))
    catalog = ProductSkillCatalog(product.adapter)
    product.adapter.settings.agent_model = "test"
    product.adapter.settings.skill_learning_idle_seconds = 0
    runtime = SimpleNamespace(
        database=product.database, product_skill_catalog=catalog, product_memory=product.memory,
        product_workspace=None,
    )
    product.learning = SkillLearningService(runtime)
    product.learning.configure({"test": TestModel()})
    product.catalog = catalog
    product.database.product_skill_learning = product.learning
    monkeypatch.setattr(product.learning, "worker_ready", AsyncMock(return_value=True))
    async with product.database.sessions.begin() as db:
        await db.execute(update(ProductDrainState).where(ProductDrainState.id == 1).values(paused=False, generation=1))
    return product


async def enable(product, kind="organization"):
    view = await product.learning.settings_view(product.principal.grant)
    await product.learning.update(
        product.principal.grant, kind,
        LearningUpdate(enabled=True, revision=getattr(view, kind).revision),
    )


async def completed(product, *, private=False, surface="web"):
    session, turn, scope, _ = await memory_execution(product, private=private, surface=surface)
    assert await product.database.save_result(
        scope, conversation='{"messages":[]}', output=ANSWER, workspace=None, usage={},
    )
    await product.database.settle(turn.id)
    return session, turn, scope


def review(turn_id, **changes):
    return SkillReview(skills=[SkillProposal(**{
        "key": "acorn-importer-reload", "description": "Verify changed fixtures in the Acorn importer tests.",
        "instructions": (
            "Use for Acorn importer fixture changes. Clear the local test fixture cache, "
            "then run pytest tests/importer/test_reload.py and verify the result. "
            "The fixture cache can otherwise hide changes."
        ),
        "source_id": f"answer:{turn_id}", "source_quote": ANSWER[:140], **changes,
    })])


async def test_learning_requires_opt_in_and_never_backfills(learning):
    product = learning
    _, old, _ = await completed(product)
    async with product.database.sessions() as db:
        assert await db.get(SkillLearningJob, old.id) is None
    await enable(product)
    _, new, _ = await completed(product)
    async with product.database.sessions() as db:
        assert await db.get(SkillLearningJob, old.id) is None
        assert (await db.get(SkillLearningJob, new.id)).status == "pending"


@pytest.mark.parametrize("private", [False, True])
async def test_learned_skill_is_canonical_scoped_encrypted_and_idempotent(learning, private):
    product = learning
    await enable(product, "personal" if private else "organization")
    session, turn, scope = await completed(product, private=private)
    prepared = await product.learning.prepare(turn.id)
    assert prepared.scope.surface == "skill_learning"
    assert prepared.scope.product_grant == scope.product_grant
    assert await product.learning.apply(prepared, review(turn.id)) == 1
    assert await product.learning.apply(prepared, review(turn.id)) == 1
    listing = await product.catalog.listing(product.principal.grant, shared=not private)
    skill = listing["skills"][0]
    assert skill["scope"] == ("personal" if private else "organization")
    detail = await product.catalog.detail(product.principal.grant, skill["id"])
    assert "pytest tests/importer/test_reload.py" in detail["instructions"]
    provenance = json.loads(await product.catalog.file(product.principal.grant, skill["id"], "learning-source.json"))
    assert provenance["source_session_id"] == session.id and provenance["source_quote"] == ANSWER[:140]
    async with product.database.sessions() as db:
        job = await db.get(SkillLearningJob, turn.id)
        learned = await db.get(LearnedSkill, skill["id"])
        assert job.status == "completed" and job.saved_count == 1
        assert "Acorn" not in job.snapshot and "Acorn" not in learned.evidence
    if private:
        assert not (await product.catalog.listing(product.principal.grant, shared=True))["skills"]
    with pytest.raises(PermissionError):
        await product.learning.apply(replace(prepared, scope=replace(prepared.scope, actor_id="someone-else")), review(turn.id))


@pytest.mark.parametrize("change", ["disable", "visibility", "delete", "revoke", "edit", "archive"])
async def test_inflight_learning_cannot_overwrite_changed_authority_or_manual_work(learning, change):
    product = learning
    await enable(product)
    _, first, _ = await completed(product)
    assert await product.learning.apply(await product.learning.prepare(first.id), review(first.id)) == 1
    session, second, _ = await completed(product)
    prepared = await product.learning.prepare(second.id)
    if change == "disable":
        await product.learning.update(product.principal.grant, "organization", LearningUpdate(enabled=False, revision=1))
    elif change in {"edit", "archive"}:
        async with product.source.begin() as db:
            await db.execute(text(
                "UPDATE moyai.skills SET revision=revision+1,archived=:archived"
            ), {"archived": int(change == "archive")})
    else:
        async with product.database.sessions.begin() as db:
            if change == "visibility":
                (await db.get(Session, session.id)).visibility = "private"
            elif change == "delete":
                (await db.get(Session, session.id)).deleted = True
            else:
                await db.execute(update(User).where(User.id == product.user_id).values(auth_version=2))
    with pytest.raises(HTTPException if change == "revoke" else PermissionError):
        await product.learning.apply(prepared, review(second.id))
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_audit")) == 1


@pytest.mark.parametrize("mutation", ["archive", "edit", "rename", "delete"])
async def test_manual_control_freezes_the_topic_for_future_learning(learning, mutation):
    product = learning
    await enable(product)
    _, first, _ = await completed(product)
    await product.learning.apply(await product.learning.prepare(first.id), review(first.id))
    async with product.source.begin() as db:
        if mutation == "delete":
            await db.execute(text("DELETE FROM moyai.skills"))
        elif mutation == "rename":
            await db.execute(text("UPDATE moyai.skills SET name='human-owned-name',revision=revision+1"))
        else:
            await db.execute(text("UPDATE moyai.skills SET revision=revision+1,archived=:archived"),
                             {"archived": int(mutation == "archive")})
    _, second, _ = await completed(product)
    assert await product.learning.apply(await product.learning.prepare(second.id), review(second.id)) == 0
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_audit")) == 1


async def test_later_evidence_updates_one_learned_skill_and_preserves_versions(learning):
    product = learning
    await enable(product)
    for index in range(2):
        _, turn, _ = await completed(product)
        proposed = review(turn.id)
        if index:
            proposed.skills[0].instructions += " Check the fixture reload test result before continuing."
        assert await product.learning.apply(await product.learning.prepare(turn.id), proposed) == 1
    rows = (await product.catalog.listing(product.principal.grant))["skills"]
    assert len(rows) == 1 and rows[0]["revision"] == 2
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_bundles")) == 2


async def test_invalid_quote_or_secret_never_publishes(learning):
    product = learning
    await enable(product)
    _, turn, _ = await completed(product)
    prepared = await product.learning.prepare(turn.id)
    with pytest.raises(ValueError, match="supporting source"):
        await product.learning.apply(prepared, review(turn.id, source_quote="This evidence was not actually in the source."))
    with pytest.raises(HTTPException):
        await product.learning.apply(prepared, review(turn.id, instructions="Use api_key=synthetic_sensitive_value to connect to the test service."))
    assert not (await product.catalog.listing(product.principal.grant))["skills"]


@pytest.mark.parametrize("private,surface", [(True, "web"), (False, "slack"), (False, "automation"), (False, "continued")])
async def test_organization_opt_in_never_promotes_private_or_automated_sources(learning, monkeypatch, private, surface):
    product = learning
    if surface == "slack":
        monkeypatch.setattr(product.database, "slack_access", AsyncMock(return_value=True))
    await enable(product)
    _, turn, _ = await completed(product, private=private, surface=surface)
    async with product.database.sessions() as db:
        assert await db.get(SkillLearningJob, turn.id) is None


async def test_competing_sessions_do_not_duplicate_a_topic(learning):
    product = learning
    await enable(product)
    _, a, _ = await completed(product)
    _, b, _ = await completed(product)
    pa, pb = await product.learning.prepare(a.id), await product.learning.prepare(b.id)
    results = await asyncio.gather(
        product.learning.apply(pa, review(a.id)), product.learning.apply(pb, review(b.id)), return_exceptions=True,
    )
    assert any(result == 1 for result in results)
    async with product.source.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai.skills")) == 1
        assert await db.scalar(text("SELECT count(*) FROM moyai.skill_audit")) == 1


async def test_skill_learning_preferences_reject_stale_updates(learning):
    product = learning
    await enable(product)
    with pytest.raises(HTTPException) as error:
        await product.learning.update(product.principal.grant, "organization", LearningUpdate(enabled=False, revision=0))
    assert error.value.status_code == 409
    assert (await product.learning.settings_view(product.principal.grant)).organization.enabled


async def test_members_cannot_enable_organization_learning(learning):
    await demote(learning)
    principal = await learning.adapter.current(learning.user_id)
    assert principal.role == "member"
    with pytest.raises(HTTPException) as error:
        await learning.learning.update(principal.grant, "organization", LearningUpdate(enabled=True, revision=0))
    assert error.value.status_code == 403
    view = await learning.learning.settings_view(principal.grant)
    assert not view.organization.can_manage and not view.organization.enabled


async def test_shared_password_admin_cannot_enable_or_publish_learned_skills(learning):
    product = learning
    async with product.source.begin() as db:
        await db.execute(text(
            "INSERT INTO moyai.users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    principal = await product.adapter.current("shared:password:admin")
    async with product.database.sessions.begin() as db:
        await product.adapter.project_native_user(db, principal)
    assert principal.role == "admin" and not principal.individual
    view = await product.learning.settings_view(principal.grant)
    assert not view.personal.can_manage and not view.organization.can_manage
    for kind in ("personal", "organization"):
        with pytest.raises(HTTPException) as error:
            await product.learning.update(principal.grant, kind, LearningUpdate(enabled=True, revision=0))
        assert error.value.status_code == 403
    assert product.learning._scope(principal, Session(surface="web", visibility="shared")) is None
    assert product.learning._scope(principal, Session(surface="web", visibility="private", owner_id=principal.id)) is None


async def test_slack_continuation_of_web_chat_is_not_a_learning_source(learning):
    product = learning
    await enable(product, "personal")
    _, turn, scope, _ = await memory_execution(product, private=True)
    async with product.database.sessions.begin() as db:
        await db.execute(update(MessageSource).where(MessageSource.message_identity == f"user:{turn.id}").values(
            kind="slack",
            data=product.database.security.seal({"kind": "slack", "channel_id": "DTEST"}),
        ))
    assert not await LearnedSkills(product.learning).render(scope)
    assert await product.database.save_result(scope, conversation='{"messages":[]}', output=ANSWER, workspace=None, usage={})
    await product.database.settle(turn.id)
    async with product.database.sessions() as db:
        assert await db.get(SkillLearningJob, turn.id) is None


async def test_inference_is_bound_to_the_admitted_maintenance_job(learning):
    product = learning
    await enable(product)
    _, turn, _ = await completed(product)
    prepared = await product.learning.prepare(turn.id)
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
                await product.learning.authorize_inference(db, capture)
            else:
                with pytest.raises((PermissionError, HTTPException)):
                    await product.learning.authorize_inference(db, capture)


@pytest.mark.parametrize("personal", [False, True])
async def test_recall_requires_relevance_repository_and_authorized_audience(learning, monkeypatch, personal):
    product = learning
    kind = "personal" if personal else "organization"
    await enable(product, kind)
    repository = AsyncMock(return_value="https://github.com/example/acorn")
    monkeypatch.setattr(product.learning, "_repository", repository)
    _, old, _ = await completed(product, private=personal)
    await product.learning.apply(await product.learning.prepare(old.id), review(old.id))
    source = LearnedSkills(product.learning)

    async def context(prompt, private):
        session = await product.database.create_session(product.actor, NewSession(visibility="private" if private else "shared"))
        turn = await product.database.admission.submit(product.actor, session.id, NewMessage(prompt=prompt, request_key=session.id))
        assert (await product.database.claim_next()).id == turn.id
        prepared = await product.database.admission.prepare(turn.id)
        result = await source.render(prepared.scope)
        await product.database.save_result(prepared.scope, conversation='{"messages":[]}', output="Done.", workspace=None, usage={})
        await product.database.settle(turn.id)
        return result

    relevant = "What are the steps to verify Acorn importer fixtures?"
    assert "pytest tests/importer/test_reload.py" in await context(relevant, personal)
    assert not await context("How should I write a release announcement?", personal)
    if personal:
        assert not await context(relevant, False)
    repository.return_value = "https://github.com/example/another-repository"
    assert not await context(relevant, personal)
    repository.return_value = "https://github.com/example/acorn"
    product.adapter.settings.skill_learning_enabled = False
    assert not await context(relevant, personal)
    product.adapter.settings.skill_learning_enabled = True
    await product.learning.update(product.principal.grant, kind, LearningUpdate(enabled=False, revision=1))
    assert not await context(relevant, personal)


async def test_native_learning_temporal_execution_and_history_replay(learning, monkeypatch):
    """Real Temporal workflow with a controlled model; supplements actual-app E2E."""
    product = learning
    await enable(product)
    _, turn, _ = await completed(product)
    calls = []

    def respond(messages, info):
        calls.append(messages)
        assert not info.function_tools
        return ModelResponse(parts=[TextPart(review(turn.id).model_dump_json())])

    product.learning.configure({"test": FunctionModel(respond, profile=ModelProfile(supports_json_schema_output=True))})
    runtime = product.learning.runtime
    runtime.product_skill_learning = product.learning
    monkeypatch.setattr("moyai.runtime.current", lambda: runtime)
    SkillLearningWorkflow.__pydantic_ai_agents__ = [product.learning.agent]
    converter = encrypted_converter(product.database.security, ObjectStore(product.settings, product.database.security))
    environment = await WorkflowEnvironment.start_local(plugins=[PydanticAIPlugin()], data_converter=converter)
    async with environment:
        async with Worker(
            environment.client, task_queue=product.settings.temporal_queue,
            workflows=SKILL_LEARNING_WORKFLOWS, activities=SKILL_LEARNING_ACTIVITIES,
            interceptors=[ReviewAuthority(product.learning, AGENT_NAME)],
        ):
            handle = await environment.client.start_workflow(
                SkillLearningWorkflow.run, turn.id, id=WORKFLOW_PREFIX + turn.id,
                task_queue=product.settings.temporal_queue,
            )
            assert await asyncio.wait_for(handle.result(), 45) == {"status": "completed", "saved_count": 1}
            history = await handle.fetch_history()
        await Replayer(
            interceptors=[ActivityCapacity()], workflows=SKILL_LEARNING_WORKFLOWS,
            plugins=[PydanticAIPlugin()], data_converter=converter,
        ).replay_workflow(history)
    assert len(calls) == 1
    async with product.database.sessions() as db:
        assert (await db.get(SkillLearningJob, turn.id)).saved_count == 1
