"""Shared Slack conversations retain their accounts across different participants."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_context_slack as context_tests
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_product_connections import save

from moyai.models import Session, Turn, User

bridge = context_tests.bridge
slack = context_tests.slack
ingress = context_tests.ingress


@pytest_asyncio.fixture(params=[False, True], ids=["source-store", "admission-transaction"])
async def connections(bridge, request):
    if not request.param:
        yield bridge.resolve_grants
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{bridge.store.path}")
    bridge.product.identity = SimpleNamespace(schema=None)

    async def resolve(*args, **kwargs):
        async with AsyncSession(engine) as db:
            return await bridge.resolve_grants(*args, **kwargs, db=db)

    try:
        yield resolve
    finally:
        await engine.dispose()


async def test_personal_slack_setup_does_not_change_shared_thread_accounts(bridge, connections, monkeypatch):
    save(bridge, "slack", bot={"access_token": "synthetic-bot"})
    original = await connections("google:owner", ["slack"], shared_session=True, surface="slack")
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))

    async def shared():
        return await connections("google:owner", ["slack"], shared_session=True, surface="slack")

    assert await shared() == original
    assert all(g.policy["product"]["personal_owner"] is None for g in original)

    def no_personal_lookup(*args, **kwargs):
        pytest.fail("A shared Slack thread must not depend on a personal Slack account or its link state.")

    monkeypatch.setattr(type(personal), "owner", no_personal_lookup)
    assert await shared() == original
    personal.disconnect("google:owner")
    assert await shared() == original


async def test_legacy_bot_grant_remains_narrow_and_requires_current_installation(bridge, connections):
    save(bridge, "slack", bot={"access_token": "synthetic-bot"})
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))
    private = await connections("google:owner", ["slack"])
    bot = next(g for g in private if g.policy["product"]["access"] == "bot")
    personal_grant = next(g for g in private if g.policy["product"]["personal_owner"])
    personal.disconnect("google:owner")

    assert await connections(
        "teammate", [bot.id], shared_session=True, surface="slack",
    ) == [bot]
    with pytest.raises(HTTPException):
        await connections("teammate", [personal_grant.id], shared_session=True, surface="slack")

    save(bridge, "slack", bot={"access_token": "replacement-bot"})
    with pytest.raises(HTTPException):
        await connections("teammate", [bot.id], shared_session=True, surface="slack")


@pytest.mark.parametrize("first_personal,legacy_bot", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("busy", [False, True])
async def test_second_participant_reaches_execution_in_existing_slack_workspace(
    ingress, actor, bridge, monkeypatch, first_personal, legacy_bot, busy,
):
    runtime, service = ingress.runtime, ingress.service
    personal = bridge.connectors.personal_slack
    personal.save("google:owner", {"access_token": "synthetic-personal"}, "Personal", personal.revision("google:owner"))
    async with runtime.database.sessions.begin() as db:
        participant = User(id="google:owner", username="participant", display_name="Participant", password="")
        db.add(participant)
    first, second = (participant, actor) if first_personal else (actor, participant)
    principal = await service.product.identity.require_current(ingress.grant)

    async def sender(team, user):
        selected = first if user == "UTEST0001" else second
        return selected, replace(principal, id=selected.id)

    async def request(method, url, **kwargs):
        return {"ok": True, "messages": [context_tests.root_message()], "has_more": False}

    monkeypatch.setattr(service, "actor", sender)
    monkeypatch.setattr(service.connectors, "request", request)
    if legacy_bot:
        # Seed the exact account selection used by pre-fix threads. Subsequent
        # messages use the real current catalog and admission code.
        private = await bridge.resolve_grants("google:owner", ["slack"])
        bot = next(g for g in private if g.policy["product"]["access"] == "bot")
        with monkeypatch.context() as seed:
            async def old_catalog(*args, **kwargs):
                return [{"id": bot.id}]
            seed.setattr(service.connections, "catalog", old_catalog)
            await service.receive_payload(context_tests.envelope(context_tests.mention_message()))
    else:
        await service.receive_payload(context_tests.envelope(context_tests.mention_message()))
    initial = await runtime.database.claim_next()
    prepared = await runtime.database.prepare(initial.id)
    async with runtime.database.sessions.begin() as db:
        session = await db.get(Session, initial.session_id)
        session.workspace, session.workspace_scope = json.dumps({"id": "retained-computer"}), prepared.scope.workspace_key
        if not busy:
            (await db.get(Turn, initial.id)).status = "completed"
            session.active_turn, session.status = None, "idle"

    event = context_tests.envelope({
        **context_tests.mention_message(), "type": "message", "user": "UOTHER001",
        "ts": "1792255890.001", "text": "Add the requested text to the existing image.",
    })
    await service.receive_payload(event)
    await service.receive_payload(event)  # Slack retry must not create another task.
    async with runtime.database.sessions.begin() as db:
        turns = list(await db.scalars(select(Turn).order_by(Turn.created_at)))
        assert len(turns) == 2
        followup = turns[-1]
        assert followup.actor_id == second.id and followup.status == "queued"
        assert followup.connections == initial.connections
        (await db.get(Turn, initial.id)).status = "completed"
        session = await db.get(Session, initial.session_id)
        session.active_turn, session.status = None, "idle"
    assert (await runtime.database.claim_next()).id == followup.id
    continuation = await runtime.database.prepare(followup.id)
    assert continuation.prompt == "Add the requested text to the existing image."
    assert continuation.scope.actor_id == second.id
    assert continuation.scope.workspace_key == prepared.scope.workspace_key
    assert all(g.policy["product"]["personal_owner"] is None for g in continuation.scope.grants)
    if legacy_bot:
        assert [g.policy["product"]["access"] for g in continuation.scope.grants] == ["bot"]
