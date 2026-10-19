"""Real PostgreSQL audience waits and revocation races; actual-app proof is separate."""

import asyncio
from types import SimpleNamespace

import asyncpg
import pytest
import test_product_continuation as continuation_fixtures
import test_product_read_contention as read_fixtures
import test_slack_audience as fixtures
from fastapi import HTTPException
from sqlalchemy import text

product_database = fixtures.product_database
admitted_product = fixtures.admitted_product
slack_admission = fixtures.slack_admission
audience = fixtures.audience
continuation = continuation_fixtures.continuation
read_product = read_fixtures.read_product
pytestmark = fixtures.pytestmark


async def test_audience_releases_gate_before_queued_writer_and_token_refresh(audience):
    product = audience
    url = product.settings.postgres_url
    writer, refresh, observer = [await asyncpg.connect(url) for _ in range(3)]
    token_entered, allow_token = asyncio.Event(), asyncio.Event()
    writer_acquired, release_writer = asyncio.Event(), asyncio.Event()
    tasks = []

    async def token():
        token_entered.set()
        await allow_token.wait()
        # The actual rotating-token helper takes shared canonical ownership.
        # A queued exclusive writer can block it despite another shared reader.
        async with refresh.transaction():
            await refresh.execute(
                "SELECT pg_advisory_xact_lock_shared(726941,'moyai'::regnamespace::oid::int)",
            )
        return "synthetic-membership-token"

    async def write():
        async with writer.transaction():
            await writer.execute(
                "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
            )
            writer_acquired.set()
            await release_writer.wait()

    product.slack.connectors.slack_bot_token = token
    try:
        read = asyncio.create_task(product.database.messages(product.actor, product.session_id))
        tasks.append(read)
        await asyncio.wait_for(token_entered.wait(), 2)
        tasks.append(asyncio.create_task(write()))
        for _ in range(100):
            if await observer.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND classid=726941)",
                writer.get_server_pid(),
            ):
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("The canonical writer never reached the gate.")
        allow_token.set()
        # Before the fix the audience reader retains S, the writer queues X,
        # and token refresh queues another S: neither awaited operation settles.
        await asyncio.wait_for(writer_acquired.wait(), 1)
        release_writer.set()
        messages = await asyncio.wait_for(read, 2)
        assert messages[0]["source"] == "slack"
    finally:
        allow_token.set()
        release_writer.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*list(product.slack.audience_reads.pending.values()), return_exceptions=True)
        await writer.close()
        await refresh.close()
        await observer.close()


@pytest.mark.parametrize("change", ["account", "installation", "binding", "membership"])
async def test_prefetch_does_not_publish_authority_changed_during_provider_wait(audience, change):
    product = audience
    entered, release = asyncio.Event(), asyncio.Event()
    original = product.slack.connectors.request

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    product.slack.connectors.request = delayed
    read = asyncio.create_task(product.database.messages(product.actor, product.session_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # A real canonical writer must proceed while the provider is waiting.
        async with product.source.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout='500ms'"))
            await db.execute(text(
                "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
            ))
            if change == "account":
                await db.execute(text("DELETE FROM moyai.users WHERE id=:id"), {"id": product.actor.id})
            elif change == "installation":
                product.state["authority"] = "b" * 64
            elif change == "binding":
                await db.execute(text(
                    f'UPDATE "{product.schema}".moyai_product_slack_bindings '
                    "SET connection_authority=:authority WHERE session_id=:session",
                ), {"authority": "b" * 64, "session": product.session_id})
            else:
                product.state["members"] = []
        release.set()
        with pytest.raises(HTTPException) as denied:
            await asyncio.wait_for(read, 2)
        assert denied.value.status_code == (401 if change == "account" else 404)
    finally:
        release.set()
        if not read.done():
            read.cancel()
        await asyncio.gather(read, return_exceptions=True)
        await asyncio.gather(*list(product.slack.audience_reads.pending.values()), return_exceptions=True)


async def test_cache_only_check_never_starts_provider_io_inside_admission(audience):
    product = audience
    async with product.database.sessions.begin() as db:
        with pytest.raises(HTTPException) as missing:
            await product.database.allowed_session(db, product.session_id, product.actor)
        assert missing.value.status_code == 503
    assert product.state["calls"] == []


async def test_expired_prefetch_cannot_be_used_by_a_mutation(audience):
    from moyai.contracts import NewMessage

    product = audience
    original = product.database.slack_prefetch

    async def expire_after_prefetch(actor, ids, *, deleted=False):
        await original(actor, ids, deleted=deleted)
        product.state["clock"] += fixtures.AUDIENCE_TTL

    product.database.slack_prefetch = expire_after_prefetch
    async with product.database.sessions() as db:
        before = await db.scalar(text("SELECT count(*) FROM moyai_turns"))
    with pytest.raises(HTTPException) as unavailable:
        await product.database.submit(
            product.actor, product.session_id, NewMessage(prompt="Do not admit", request_key="expired-facts"),
        )
    assert unavailable.value.status_code == 503
    async with product.database.sessions() as db:
        assert await db.scalar(text("SELECT count(*) FROM moyai_turns")) == before


@pytest.mark.parametrize("signed_ingress", [False, True])
async def test_cold_legacy_continuation_prewarms_or_uses_verified_ingress(continuation, signed_ingress):
    from moyai.contracts import NewMessage
    from moyai.models import User
    from moyai.product_continuation import ContinuationRequest
    from moyai.product_slack_conversations import SlackConversations
    from moyai.slack_audience import historical_access, prefetch_historical

    product = await fixtures.slack_admission.__wrapped__(continuation)
    await fixtures.audience.__wrapped__(product)
    product.slack.runtime = product.runtime
    product.runtime.product_slack = product.slack
    product.slack.events = SimpleNamespace(conversations=SlackConversations(product.slack))
    product.runtime.product_history.slack_access = lambda conn, principal, ids, **options: historical_access(
        product.slack, conn, principal, ids, **options,
    )
    product.runtime.product_history.slack_prefetch = lambda principal, ids: prefetch_historical(
        product.slack, principal, ids,
    )
    root = continuation_fixtures.ROOT
    async with product.source.begin() as db:
        await db.execute(text(
            "ALTER TABLE moyai.slack_threads ADD COLUMN team_id text, ADD COLUMN channel text, "
            "ADD COLUMN thread_ts text, ADD COLUMN paused integer DEFAULT 0",
        ))
        await db.execute(text(
            "ALTER TABLE moyai.slack_events ADD COLUMN user_id text, ADD COLUMN created_at text",
        ))
        await db.execute(text(
            "UPDATE moyai.runs SET private_owner_id='',owner_id=:owner WHERE id=:id",
        ), {"owner": product.raw_principal.id, "id": root})
        await db.execute(text(
            "INSERT INTO moyai.slack_threads VALUES(:id,'TLOCAL001','GPRIVATE1','1792255800.001',0)",
        ), {"id": root})
        await db.execute(text(
            "INSERT INTO moyai.slack_events VALUES(:id,'UACTUAL01','2026-10-17')",
        ), {"id": root})
    preview = await product.service.preview(product.principal, root)
    product.slack.audience_reads.snapshots.clear()
    product.state["calls"].clear()
    if signed_ingress:
        async with product.database.sessions() as db:
            actor = await db.get(User, product.raw_principal.id)
        actor.product_grant = product.raw_principal.grant
        actor.slack_channel_evidence = ("TLOCAL001", "GPRIVATE1")
        async with product.database.sessions.begin() as db:
            result = await product.service.continue_for_admission(
                db, actor, product.raw_principal, root,
                NewMessage(prompt="Verified Slack reply", request_key="signed-continuation"),
                surface="slack", provenance={"message_ts": "1792255800.002"},
            )
        assert product.state["calls"] == []
    else:
        result = await product.service.continue_session(
            product.principal, product.actor, root,
            ContinuationRequest(
                prompt="Browser continuation", request_key="cold-browser-continuation",
                expected_source_fingerprint=preview["source_fingerprint"],
            ),
        )
        assert product.state["calls"] == [
            "https://slack.com/api/conversations.info", "https://slack.com/api/conversations.members",
        ]
    assert result["source"] == "native" and result["turn_id"]


async def test_http_contention_is_503_and_rolls_back_mark_read_and_send_now(read_product):
    from moyai.contracts import NewMessage
    from moyai.models import SessionRead, Turn

    product = read_product
    database = product.runtime.database
    turn = await database.submit(
        product.actor, product.session_id, NewMessage(prompt="Queued input", request_key="busy-input"),
    )
    me = (await product.client.get("/api/me")).json()
    product.client.headers.update({"x-csrf-token": me["csrf"], "origin": str(product.client.base_url).rstrip("/")})
    async with database.engine.begin() as connection:
        await connection.execute(text("SET lock_timeout='100ms'"))
    try:
        async with product.writer.begin() as writer:
            await writer.execute(text(
                "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
            ))
            assert (await product.client.get("/api/me")).status_code == 200
            replies = [
                await product.client.patch(f"/api/sessions/{product.session_id}", json={"unread": False}),
                await product.client.post(f"/api/turns/{turn.id}/steer"),
            ]
            for response in replies:
                assert response.status_code == 503
                assert response.headers["retry-after"] == "1"
                assert response.json() == {"detail": "Account access is busy. Try again shortly."}
            async with database.sessions() as db:
                assert await db.get(SessionRead, (product.actor.id, product.session_id)) is None
                current = await db.get(Turn, turn.id)
                assert current.status == "queued" and current.mode == "when_idle"
        assert (await product.client.patch(
            f"/api/sessions/{product.session_id}", json={"unread": False},
        )).status_code == 200
        assert (await product.client.post(f"/api/turns/{turn.id}/steer")).status_code == 202
        async with database.sessions() as db:
            assert (await db.get(Turn, turn.id)).mode == "asap"
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(text("SET lock_timeout=0"))


async def test_authority_contention_is_not_a_permanent_worker_permission_failure(monkeypatch):
    from unittest.mock import AsyncMock

    from temporalio.worker import ExecuteActivityInput

    from moyai.contracts import RunScope
    from moyai.execution import AuthorizedActivity
    from moyai.product_identity import ProductAuthorityBusy

    monkeypatch.setattr("moyai.execution.activity.info", lambda: SimpleNamespace(
        activity_type="agent__moyai__capability__test__read",
    ))
    error = ProductAuthorityBusy("Account access is busy. Try again shortly.")
    database = SimpleNamespace(authorize_scope=AsyncMock(side_effect=error))
    next_activity = SimpleNamespace(execute_activity=AsyncMock())
    interceptor = AuthorizedActivity(next_activity, database)
    request = ExecuteActivityInput(
        fn=lambda: None, args=[RunScope("session", "turn", "actor", "model")], executor=None, headers={},
    )
    with pytest.raises(ProductAuthorityBusy) as observed:
        await interceptor.execute_activity(request)
    assert observed.value is error
    next_activity.execute_activity.assert_not_awaited()
