"""Real canonical/native PostgreSQL; Slack API calls are deterministic boundaries."""

import asyncio
import os
from types import SimpleNamespace

import pytest
import pytest_asyncio
import test_product_identity_admission as identities
from agentchat.channels.slack.host import SlackHost
from app.connector_errors import ConnectorError
from fastapi import HTTPException
from sqlalchemy import func, select, text, update

from moyai.contracts import NewMessage, NewSession
from moyai.models import Message, Session, SlackThread, Turn, User
from moyai.product_slack_conversations import SlackConversations
from moyai.product_slack_events import SlackEvents, SlackMessage
from moyai.product_slack_models import SlackWebLink, SlackWebTurn
from moyai.slack_web import SlackWeb

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Disposable PostgreSQL required.")
product_database = identities.product_database
admitted_product = identities.admitted_product
slack_admission = identities.slack_admission


@pytest_asyncio.fixture
async def linked(slack_admission):
    product = slack_admission
    settings = product.adapter.settings
    settings.slack_bot_enabled = True
    settings.slack_session_users = "*"
    settings.slack_dm_enabled = settings.slack_thread_chat_enabled = settings.slack_identity_linking_enabled = True
    sent = []
    state = {"drop": False, "reject": False}
    installed = {"team_id": "TLOCAL001", "user_id": "UBOT00001", "authority": "a" * 64}

    async def request(method, url, **kwargs):
        if url.endswith("users.lookupByEmail"):
            assert kwargs["params"] == {"email": "admin@example.test"}
            return {"ok": True, "user": {"id": "UACTUAL01", "team_id": "TLOCAL001"}}
        if url.endswith("conversations.open"):
            assert kwargs["json"] == {"users": "UACTUAL01"}
            return {"ok": True, "channel": {"id": "DLOCAL001"}}
        if url.endswith("conversations.history"):
            return {"ok": True, "messages": [
                {**item, "user": "UBOT00001", "ts": "1795300000.001"} for item in sent
            ]}
        assert url.endswith("chat.postMessage")
        if state["reject"]:
            raise ConnectorError("Rejected", rejected=True, dispatched=True)
        sent.append(kwargs["json"])
        if state["drop"]:
            raise ConnectorError("Lost response", dispatched=True)
        return {"ok": True, "channel": "DLOCAL001", "ts": "1795300000.001"}

    async def actor(team, user, **kwargs):
        principal = await product.adapter.current(f"slack:{team}:{user}")
        async with product.database.sessions.begin() as db:
            sender = await product.adapter.project_native_user(db, principal)
        return sender, principal

    async def installation_in(db):
        return installed

    async def token():
        return "synthetic"

    from moyai.skill_invocations import SkillInvocations
    runtime = SimpleNamespace(
        database=product.database, admission=product.database.admission,
        slack_activity=SimpleNamespace(wake=asyncio.Event()),
        product_identity=product.adapter, product_skill_catalog=None, security=product.database.security,
    )
    runtime.skill_invocations = SkillInvocations(runtime)
    service = SimpleNamespace(
        app="dev", bot_token=token,
        product=SimpleNamespace(identity=product.adapter), runtime=runtime, settings=settings,
        installation=lambda: installed, installation_in=installation_in,
        connectors=SimpleNamespace(request=request, slack_bot_token=token), actor=actor,
        delivery_wakeup=asyncio.Event(),
    )
    service.web = SlackWeb(service)
    session = await product.database.create_session(product.actor, NewSession(
        title="Release checklist", visibility="private",
    ))
    await product.database.admission.submit(product.actor, session.id, NewMessage(
        prompt="Review the launch checklist before Friday.", request_key="web",
    ))
    product.service, product.session, product.sent, product.state = service, session, sent, state
    yield product


async def test_dm_is_owner_verified_idempotent_and_keeps_private_web_session(linked):
    product = linked
    result = await product.service.web.connect(product.actor, product.session.id)
    again = await product.service.web.connect(product.actor, product.session.id)
    assert result == again and result.status == "linked"
    assert result.url.endswith("DLOCAL001-1795300000.001")
    assert len(product.sent) == 1
    assert "Release checklist" in product.sent[0]["text"]
    assert "Review the launch checklist before Friday." in product.sent[0]["text"]
    async with product.database.sessions() as db:
        session = await db.get(Session, product.session.id)
        assert session.surface == "web" and session.visibility == "private"
        assert await db.scalar(select(func.count()).select_from(Session)) == 1
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 1


async def test_shared_web_session_can_continue_in_slack(linked):
    session = await linked.database.create_session(linked.actor, NewSession(
        title="Shared checklist", visibility="shared",
    ))
    assert (await linked.service.web.status(linked.actor, session.id)).status == "available"
    assert (await linked.service.web.connect(linked.actor, session.id)).status == "linked"
    async with linked.database.sessions() as db:
        saved = await db.get(Session, session.id)
        assert saved.surface == "web" and saved.visibility == "shared"
        assert await db.scalar(select(SlackThread.id).where(SlackThread.session_id == session.id))


async def test_lost_response_recovers_receipt_without_resending(linked):
    product = linked
    product.state["drop"] = True
    with pytest.raises(ConnectorError):
        await product.service.web.connect(product.actor, product.session.id)
    assert (await product.service.web.status(product.actor, product.session.id)).status == "pending"
    result = await product.service.web.connect(product.actor, product.session.id)
    assert result.status == "linked" and len(product.sent) == 1


async def test_concurrent_clicks_create_one_dm_and_binding(linked):
    product = linked
    results = await asyncio.gather(*[
        product.service.web.connect(product.actor, product.session.id) for _ in range(3)
    ], return_exceptions=True)
    assert any(not isinstance(result, Exception) and result.status == "linked" for result in results)
    assert all(not isinstance(result, Exception) or (
        isinstance(result, HTTPException) and result.status_code == 409
    ) for result in results)
    assert len(product.sent) == 1
    async with product.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(SlackThread)) == 1


async def test_slack_account_cannot_link_someone_elses_web_session(linked):
    product = linked
    sender, _ = await product.service.actor("TLOCAL001", "UACTUAL01")
    with pytest.raises(HTTPException):
        await product.service.web.connect(sender, product.session.id)
    assert product.sent == []


@pytest.mark.parametrize("change", ["bot_disabled", "sender_disabled"])
async def test_disabled_slack_conversations_do_not_send_a_dm(linked, change):
    product = linked
    if change == "bot_disabled":
        product.service.settings.slack_bot_enabled = False
    else:
        product.service.settings.slack_session_users = "UOTHER001"
    with pytest.raises(HTTPException):
        await product.service.web.connect(product.actor, product.session.id)
    assert product.sent == []


async def test_confirmed_rejection_can_retry(linked):
    product = linked
    product.state["reject"] = True
    with pytest.raises(ConnectorError):
        await product.service.web.connect(product.actor, product.session.id)
    product.state["reject"] = False
    assert (await product.service.web.connect(product.actor, product.session.id)).status == "linked"
    assert len(product.sent) == 1


async def test_slack_reply_uses_same_private_session_and_preserves_signed_grant(linked):
    product = linked
    await product.service.web.connect(product.actor, product.session.id)
    sender, principal = await product.service.actor("TLOCAL001", "UACTUAL01")
    event = {"type": "message", "channel_type": "im", "channel": "DLOCAL001", "user": "UACTUAL01",
             "ts": "1795300001.001", "thread_ts": "1795300000.001", "text": "Add the rollback plan."}
    payload = {"type": "event_callback", "event_id": "ELOCAL001", "team_id": "TLOCAL001", "event": event}
    installed = product.service.installation()
    message = SlackMessage.parse(payload, event, SlackHost(
        bot_user_id=installed["user_id"], workspace_id=installed["team_id"],
    ))
    conversations = SlackConversations(product.service)
    thread = await conversations.find(message.origin)
    conversation = await conversations.existing(
        thread, sender, message.origin, product.service.installation(), mentioned=False, command="",
    )
    events = SlackEvents(product.service)
    await events._admit(message, conversation, sender, principal, product.service.installation())
    await events._admit(message, conversation, sender, principal, product.service.installation())
    async with product.database.sessions() as db:
        turns = list(await db.scalars(select(Turn).where(Turn.session_id == product.session.id)))
        assert len(turns) == 2
        turn = next(turn for turn in turns if turn.request_key != "web")
        assert turn.actor_id == product.actor.id
        source = await db.get(SlackWebTurn, turn.id)
        assert source.requester_id == sender.id and source.authority_version == principal.authority_version
        messages = list(await db.scalars(select(Message).where(Message.turn_id == turn.id)))
        assert product.database.security.unseal(messages[0].content).decode() == "Add the rollback plan."
        await product.database._saved_product_grant(db, turn)
    async with product.database.sessions.begin() as db:
        await db.execute(update(User).where(User.id == sender.id).values(enabled=False))
    async with product.database.sessions() as db:
        with pytest.raises(HTTPException):
            await product.database._saved_product_grant(db, turn)


@pytest.mark.parametrize("change", [
    "email", "conflict", "installation", "owner",
    "slack_bot_enabled", "slack_identity_linking_enabled", "slack_dm_enabled", "slack_thread_chat_enabled",
])
async def test_changed_identity_cannot_continue_or_receive_private_session(linked, change):
    product = linked
    await product.service.web.connect(product.actor, product.session.id)
    if change == "installation":
        product.service.installation()["authority"] = "b" * 64
    elif change == "owner":
        async with product.database.sessions.begin() as db:
            await db.execute(update(User).where(User.id == product.actor.id).values(enabled=False))
    elif change.startswith("slack_"):
        setattr(product.service.settings, change, False)
    else:
        async with product.source.begin() as db:
            if change == "email":
                await db.execute(text("UPDATE moyai.users SET email='other@example.test' WHERE kind='slack'"))
            else:
                await db.execute(text("UPDATE moyai.users SET profile_conflict=1 WHERE kind='slack'"))
    async with product.database.sessions.begin() as db:
        link = await db.get(SlackWebLink, product.session.id)
        with pytest.raises(HTTPException):
            await product.service.web.current(db, link)
