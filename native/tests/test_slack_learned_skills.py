"""Canonical PostgreSQL authority with a declared synthetic Slack HTTP boundary."""

import json
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import test_slack_audience as audience_fixtures
from sqlalchemy import text

from moyai.context.learned_skills import LearnedSkills
from moyai.product_skill_catalog import ProductSkillCatalog
from moyai.product_skill_learning import SkillLearningService
from moyai.product_skill_learning_models import LearnedSkill, SkillLearningPreference
from moyai.product_slack_models import ProductSlackBinding, SlackTurnSource
from moyai.slack_audience import prefetch_sessions
from moyai.slack_audience_reads import AUDIENCE_TTL

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = audience_fixtures.product_database
admitted_product = audience_fixtures.admitted_product
slack_admission = audience_fixtures.slack_admission
audience = audience_fixtures.audience


@pytest_asyncio.fixture
async def recall(audience, monkeypatch):
    product = audience
    catalog = ProductSkillCatalog(product.adapter)
    service = SkillLearningService(SimpleNamespace(
        database=product.database, product_skill_catalog=catalog,
        product_slack=product.slack, product_workspace=None,
    ))
    service.settings.skill_learning_enabled = True
    repository = AsyncMock(return_value="https://github.com/example/acorn")
    monkeypatch.setattr(service, "_repository", repository)
    async with product.source.begin() as db:
        await db.execute(text(
            "CREATE TABLE moyai.skills (id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,"
            "encrypted TEXT NOT NULL,scope TEXT NOT NULL,owner_id TEXT NOT NULL,namespace TEXT NOT NULL,"
            "revision INTEGER NOT NULL,archived INTEGER NOT NULL)"
        ))
        for identity, key, instructions in (
            ("a" * 32, "organization", "TEAM PROCEDURE: verify the private channel importer using the approved smoke command."),
            ("b" * 32, product.user_id, "PERSONAL PROCEDURE MUST NEVER ENTER SLACK"),
        ):
            await db.execute(text(
                "INSERT INTO moyai.skills VALUES(:id,'private-channel','Private channel importer procedure',"
                ":encrypted,:scope,:owner,:key,1,0)"
            ), {"id": identity, "encrypted": product.adapter.legacy_security.encrypt(instructions),
                "scope": "organization" if key == "organization" else "personal", "owner": product.user_id, "key": key})
    turn = await product.database.claim_next()
    assert turn is not None
    async with product.database.sessions.begin() as db:
        db.add(SlackTurnSource(
            turn_id=turn.id, team_id="TLOCAL001", channel_id="GPRIVATE1",
            thread_ts="1792255700.001", message_ts="1792255700.001", user_id="UACTUAL01",
        ))
        for identity, key in (("a" * 32, "organization"), ("b" * 32, product.user_id)):
            db.add(SkillLearningPreference(scope_key=key, enabled=True, revision=1))
            db.add(LearnedSkill(
                skill_id=identity, scope_key=key, topic_hash=identity * 2, revision=1,
                source_turn_id=turn.id,
                evidence=product.database.security.seal(json.dumps({
                    "key": "private-channel", "repository": repository.return_value,
                })),
            ))
        sender = await product.adapter.project_native_user(db, product.raw_principal)
    await prefetch_sessions(product.slack, sender, [product.session_id])
    prepared = await product.database.admission.prepare(turn.id)
    product.scope, product.recall = prepared.scope, LearnedSkills(service)
    product.repository = repository
    return product


async def test_internal_channel_recalls_only_opted_in_relevant_repository_procedures(recall):
    product = recall
    result = await product.recall.render(product.scope)
    assert "TEAM PROCEDURE" in result
    assert "PERSONAL PROCEDURE" not in result
    product.repository.return_value = "https://github.com/example/other"
    assert not await product.recall.render(product.scope)
    product.repository.return_value = "https://github.com/example/acorn"
    async with product.database.sessions.begin() as db:
        (await db.get(SkillLearningPreference, "organization")).enabled = False
    assert not await product.recall.render(product.scope)


@pytest.mark.parametrize("change", ["external", "unknown_external", "not_channel", "wrong_channel"])
async def test_external_or_unverified_slack_audience_never_receives_organization_lessons(recall, change):
    product = recall
    product.state["clock"] += AUDIENCE_TTL
    original = product.slack.connectors.request

    async def request(method, url, **kwargs):
        result = await original(method, url, **kwargs)
        if url.endswith("conversations.info"):
            if change in {"external", "unknown_external"}:
                result["channel"]["is_ext_shared"] = True if change == "external" else None
            elif change == "not_channel":
                result["channel"]["is_channel"] = False
            else:
                result["channel"]["id"] = "COTHER001"
        return result

    product.slack.connectors.request = request
    # The same production audience check may deny execution before recall.
    from fastapi import HTTPException
    try:
        assert not await product.recall.render(product.scope)
    except HTTPException as error:
        assert error.status_code in {403, 404, 503}


@pytest.mark.parametrize("change", ["dm", "source", "binding", "paused", "delegate", "swarm"])
async def test_non_channel_or_changed_provenance_cannot_use_recall(recall, change):
    product = recall
    scope = product.scope
    if change == "delegate":
        scope = replace(scope, delegation_depth=1)
    elif change == "swarm":
        scope = replace(scope, swarm_id="unrelated")
    else:
        async with product.database.sessions.begin() as db:
            binding = await db.get(ProductSlackBinding, product.session_id)
            source = await db.get(SlackTurnSource, scope.turn_id)
            if change == "dm":
                binding.channel_id = source.channel_id = "DPRIVATE1"
            elif change == "source":
                source.user_id = "UOTHER001"
            elif change == "binding":
                binding.connection_authority = "b" * 64
            else:
                binding.paused = True
    assert await product.recall._slack_audience(scope) is None


async def test_installation_change_while_loading_catalog_discards_collected_content(recall, monkeypatch):
    product = recall
    original = product.recall.service._catalog

    async def changed(*args):
        result = await original(*args)
        product.state["authority"] = "b" * 64
        return result

    monkeypatch.setattr(product.recall.service, "_catalog", changed)
    from fastapi import HTTPException
    try:
        assert not await product.recall.render(product.scope)
    except HTTPException as error:
        assert error.status_code in {403, 404, 503}
