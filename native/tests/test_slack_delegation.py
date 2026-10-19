"""A real Slack principal can delegate without gaining private human sessions."""

import os
from types import SimpleNamespace

import pytest
import test_product_authority as authority
from fastapi import HTTPException
from test_product_identity_admission import slack_admission as slack_admission  # noqa: PLC0414 - shared pytest fixture

from moyai.contracts import NewMessage, NewSession
from moyai.delegation import DelegationService
from moyai.delegation_models import DelegatedSession
from moyai.models import ProductTurnAuthority, Session
from moyai.product_slack_models import ProductSlackBinding
from moyai.slack_audience import can_read

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = authority.product_database
admitted_product = authority.admitted_product


async def test_slack_delegation_keeps_original_authority_and_private_audience(slack_admission):
    product = slack_admission
    database, principal = product.database, product.raw_principal
    actor = await database.user(principal.id)
    actor.product_grant = principal.grant
    assert not principal.individual
    with pytest.raises(HTTPException) as refusal:
        await database.create_session(actor, NewSession(visibility="private"))
    assert refusal.value.status_code == 403
    session = await database.create_session(actor, NewSession(visibility="shared"))
    slack = SimpleNamespace(runtime=SimpleNamespace(database=database),
                            installation=lambda: {"team_id": "TLOCAL001", "authority": "a" * 64})
    database.slack_access = lambda db, current, viewer: can_read(slack, db, current, viewer)
    async with database.sessions.begin() as db:
        (await db.get(Session, session.id)).surface = "slack"
        db.add(ProductSlackBinding(session_id=session.id, requester_id=actor.id, team_id="TLOCAL001",
            channel_id="DTEST0001", thread_ts="1792255740.000", started_ts="1792255740.000",
            connection_authority="a" * 64))
    turn = await database.submit(actor, session.id, NewMessage(prompt="Inspect the repository.", request_key="slack-work"))
    assert (await database.claim_next()).id == turn.id
    prepared = await database.prepare(turn.id)
    child = await DelegationService(database).start(prepared.scope, "child-once", "Inspect the UI.")
    assert await DelegationService(database).start(prepared.scope, "child-once", "Inspect the UI.") == child
    async with database.sessions() as db:
        delegated = await db.get(Session, child["session_id"])
        assert delegated.visibility == "private" and delegated.owner_id == principal.id
        child_turn = (await db.get(DelegatedSession, delegated.id)).initial_turn_id
        saved = await db.get(ProductTurnAuthority, child_turn)
        assert saved.actor_id == principal.id
    assert (await database.claim_next()).id == child_turn
    child_scope = (await database.prepare(child_turn)).scope
    assert child_scope.product_grant == principal.grant
    product.adapter.settings.slack_session_users = "UREVOKED"
    with pytest.raises((HTTPException, PermissionError)):
        await database.authorize_scope(child_scope)
