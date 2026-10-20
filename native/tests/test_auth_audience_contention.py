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


@pytest.mark.parametrize("path", ["/api/skills", "/api/connections"])
@pytest.mark.parametrize("delegated", [False, True])
async def test_query_scoped_catalog_prewarms_cold_and_expired_audiences(audience, read_product, path, delegated):
    from moyai.delegation import DelegationService

    product = audience
    runtime = read_product.runtime
    runtime.product_slack = product.slack
    session_id = product.session_id
    if delegated:
        turn = await product.database.claim_next()
        prepared = await product.database.prepare(turn.id)
        child = await DelegationService(product.database).start(
            prepared.scope, "catalog-child", "Inspect inherited catalog permissions",
        )
        session_id = child["session_id"]
    product.slack.audience_reads.snapshots.clear()
    product.state["calls"].clear()

    for expected_calls in (2, 4):
        response = await read_product.client.get(path, params={"session_id": session_id})
        assert response.status_code == 200, response.text
        if path == "/api/skills":
            page = response.json()
            assert page["skills"] == [] and page["has_more"] is False and page["next_offset"] is None
        else:
            assert response.json() == []
        assert len(product.state["calls"]) == expected_calls
        product.state["clock"] += fixtures.AUDIENCE_TTL

    product.state["members"] = []
    denied = await read_product.client.get(path, params={"session_id": session_id})
    assert denied.status_code == 404


async def test_ignored_session_query_does_not_add_an_authorization_scope(audience, read_product):
    product = audience
    read_product.runtime.product_slack = product.slack
    product.state["failure"] = TimeoutError("Do not fetch an ignored session's audience.")
    response = await read_product.client.get("/api/me", params={"session_id": product.session_id})
    assert response.status_code == 200
    assert product.state["calls"] == []


@pytest.mark.parametrize("deleted", [False, True])
async def test_session_listing_keeps_its_prefetched_candidate_set(audience, deleted):
    from moyai.models import Session
    from moyai.product_slack_models import ProductSlackBinding

    product = audience
    async with product.database.sessions.begin() as db:
        (await db.get(Session, product.session_id)).deleted = deleted
    original = product.database.slack_prefetch
    added_id = None

    async def insert_after_prefetch(actor, ids, *, deleted=False):
        nonlocal added_id
        await original(actor, ids, deleted=deleted)
        product.database.slack_prefetch = original
        # Another request admits a differently bound Slack session while this
        # listing is waiting for provider evidence for its planned rows.
        async with product.database.sessions.begin() as db:
            added = Session(
                owner_id=actor.id, title="Newly ranked Slack task", visibility="shared",
                surface="slack", deleted=deleted, updated_at="9999", model=product.settings.default_model,
            )
            db.add(added)
            await db.flush()
            added_id = added.id
            db.add(ProductSlackBinding(
                session_id=added.id, requester_id=actor.id, team_id="TLOCAL001",
                channel_id="GNEWCHANNEL", thread_ts="1792255800.001", started_ts="1792255800.001",
                connection_authority="a" * 64,
            ))

    product.database.slack_prefetch = insert_after_prefetch
    rows = await product.database.list_sessions(product.actor, deleted=deleted)
    assert [row.id for row in rows] == [product.session_id]
    # The next request plans and prepares the new audience normally.
    rows = await product.database.list_sessions(product.actor, deleted=deleted)
    assert {row.id for row in rows} == {product.session_id, added_id}


@pytest.mark.parametrize("source", ["native", "all"])
async def test_busy_history_returns_retryable_authority_error(audience, read_product, source):
    import httpx

    from moyai.api import create_app

    product = audience
    runtime = read_product.runtime
    # This identity fixture has native history, not a legacy transcript schema.
    # Exercise both native-only and combined-source error handling with the
    # optional legacy source disabled, as supported by the real router.
    runtime.product_history = runtime.product_continuations = None
    app = create_app(runtime, run_dispatcher=False)
    runtime.product_slack = product.slack
    async with product.database.engine.begin() as db:
        await db.execute(text("SET lock_timeout='100ms'"))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=read_product.client.base_url,
            cookies=read_product.client.cookies,
        ) as client:
            assert (await client.get("/api/history/sessions", params={"source": source})).status_code == 200
            async with product.source.begin() as writer:
                await writer.execute(text(
                    "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
                ))
                response = await client.get("/api/history/sessions", params={"source": source})
                assert response.status_code == 503
                assert response.headers.get("retry-after") == "1"
                assert response.headers["cache-control"] == "no-store"
                assert response.json() == {"detail": "Account access is busy. Try again shortly."}
    finally:
        async with product.database.engine.begin() as db:
            await db.execute(text("SET lock_timeout=0"))


async def test_legacy_read_preserves_exact_canonical_gate_contention(read_product):
    from moyai.product_identity import ProductAuthorityBusy

    database = read_product.runtime.database
    async with database.engine.begin() as db:
        await db.execute(text("SET lock_timeout='100ms'"))
    try:
        async with read_product.writer.begin() as writer:
            await writer.execute(text(
                "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
            ))
            with pytest.raises(ProductAuthorityBusy):
                async with read_product.runtime.product_history.read(), database.sessions.begin() as db:
                    await read_product.identity.authorize_admission(db, read_product.principal.grant)
    finally:
        async with database.engine.begin() as db:
            await db.execute(text("SET lock_timeout=0"))


@pytest.mark.parametrize("operation", ["messages", "search"])
async def test_audience_releases_gate_before_queued_writer_and_token_refresh(audience, operation):
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
        read = asyncio.create_task(
            product.database.messages(product.actor, product.session_id) if operation == "messages" else
            product.reads.native_search(product.reader, "channel input", limit=20, offset=0)
        )
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
        result = await asyncio.wait_for(read, 2)
        if operation == "messages":
            assert result[0]["source"] == "slack"
        else:
            assert [row["id"] for row in result["sessions"]] == [product.session_id]
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


@pytest.mark.parametrize("facts", ["cold", "ready", "expired", "rebound"])
async def test_caller_owned_admission_uses_only_prefetched_current_facts(audience, facts):
    from moyai.contracts import NewMessage

    product = audience
    if facts != "cold":
        await product.database.prefetch_session_audiences(product.actor, [product.session_id])
    calls = list(product.state["calls"])
    if facts == "expired":
        product.state["clock"] += fixtures.AUDIENCE_TTL
    elif facts == "rebound":
        async with product.database.sessions.begin() as db:
            await db.execute(text(
                "UPDATE moyai_product_slack_bindings SET channel_id='GNEWCHANNEL' WHERE session_id=:id",
            ), {"id": product.session_id})
    body = NewMessage(prompt="Caller-owned admission", request_key="caller-owned-admission")
    if facts == "ready":
        async with product.database.sessions.begin() as db:
            turn = await product.database.submit(product.actor, product.session_id, body, db=db)
        assert turn.status == "queued"
    else:
        with pytest.raises(HTTPException) as unavailable:
            async with product.database.sessions.begin() as db:
                await product.database.submit(product.actor, product.session_id, body, db=db)
        assert unavailable.value.status_code == 503
        async with product.database.sessions() as db:
            assert not await db.scalar(text(
                "SELECT count(*) FROM moyai_turns WHERE request_key='caller-owned-admission'",
            ))
    assert product.state["calls"] == calls


async def test_cold_pending_action_reconciliation_prewarms_before_write(audience, read_product):
    from sqlalchemy import select

    from moyai.models import Effect, Turn

    product = audience
    read_product.runtime.product_slack = product.slack
    async with product.database.sessions.begin() as db:
        turn = await db.scalar(select(Turn).where(Turn.session_id == product.session_id))
        db.add(Effect(
            id="cold-effect", session_id=product.session_id, turn_id=turn.id, actor_id=product.actor.id,
            tool_name="synthetic_write", arguments=product.database.security.seal("{}"),
            fingerprint="synthetic", state="pending",
        ))
    me = (await read_product.client.get("/api/me")).json()
    read_product.client.headers.update({
        "x-csrf-token": me["csrf"], "origin": str(read_product.client.base_url).rstrip("/"),
    })
    response = await read_product.client.post(
        "/api/effects/cold-effect/resolve", json={"outcome": "not_executed", "evidence": "Synthetic inspection"},
    )
    assert response.status_code == 200, response.text
    assert len(product.state["calls"]) == 2
    async with product.database.sessions() as db:
        assert (await db.get(Effect, "cold-effect")).state == "reviewed_not_executed"


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
    parent = await database.submit(
        product.actor, product.session_id, NewMessage(prompt="Active parent", request_key="busy-parent"),
    )
    assert (await database.claim_next()).id == parent.id
    await database.prepare(parent.id)
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
                await product.client.post(
                    f"/api/sessions/{product.session_id}/read", json={"message_id": 9999},
                ),
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
        assert (await product.client.post(
            f"/api/sessions/{product.session_id}/read", json={"message_id": 9999},
        )).status_code == 200
        async with database.sessions() as db:
            through = (await db.get(SessionRead, (product.actor.id, product.session_id))).message_id
        assert through > 0
        assert (await product.client.post(
            f"/api/sessions/{product.session_id}/read", json={"message_id": 0},
        )).status_code == 200
        assert (await product.client.post(f"/api/turns/{turn.id}/steer")).status_code == 202
        repeated = await product.client.post(f"/api/turns/{turn.id}/steer")
        assert repeated.status_code == 202 and repeated.json()["id"] == turn.id
        async with database.sessions() as db:
            assert (await db.get(SessionRead, (product.actor.id, product.session_id))).message_id == through
            saved = await db.get(Turn, turn.id)
            assert saved.mode == "asap" and saved.status == "steering" and saved.parent_turn == parent.id
            assert await db.scalar(text(
                "SELECT count(*) FROM moyai_events WHERE turn_id=:id AND kind='steering_admitted'",
            ), {"id": turn.id}) == 1
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


@pytest.mark.parametrize("after_expiry", ["healthy", "membership", "busy", "role", "gate"])
async def test_sse_refreshes_expired_audience_before_authorizing_each_frame(audience, after_expiry):
    from app.connector_errors import ConnectorError
    from fastapi import Request

    from moyai.live_stream import authorized_events
    from moyai.product_identity import ProductAuthorityBusy
    from moyai.stream_authority import authorize_stream_viewer

    product = audience
    request = Request({"type": "http", "state": {"product_identity_grant": product.principal.grant}})

    async def authorize():
        await authorize_stream_viewer(
            product.database, product.adapter, request, product.actor,
            product.session_id, product.raw_principal.id,
        )

    async def disconnected():
        return False

    async def events():
        yield "id: 0\ndata: first\n\n"
        product.state["clock"] += fixtures.AUDIENCE_TTL
        if after_expiry == "membership":
            product.state["members"] = []
        elif after_expiry == "busy":
            product.state["failure"] = ConnectorError(
                "private provider detail", code="rate_limited", status_code=429, retryable=True, retry_after=1,
            )
        elif after_expiry == "role":
            async with product.source.begin() as db:
                await db.execute(text(
                    "INSERT INTO moyai.user_roles VALUES "
                    "('admin@example.test','member',1,'other@example.test','now')",
                ))
        elif after_expiry == "gate":
            async def unavailable(*args, **kwargs):
                raise ProductAuthorityBusy("Account access is busy. Try again shortly.")
            product.database.slack_prefetch = unavailable
        yield "id: 1\ndata: second\n\n"

    frames = [frame async for frame in authorized_events(events(), authorize, disconnected)]
    assert frames[0] == "id: 0\ndata: first\n\n"
    if after_expiry == "healthy":
        assert frames == ["id: 0\ndata: first\n\n", "id: 1\ndata: second\n\n"]
        assert len(product.state["calls"]) == 4
    else:
        assert not any("id: 1" in frame or "second" in frame for frame in frames)
        assert frames[-1] == (
            "event: refresh\ndata: {}\n\n" if after_expiry in {"busy", "gate"} else
            "event: access_revoked\ndata: {}\n\n"
        )


async def test_cold_non_http_answer_actor_prewarms_before_its_transaction(audience):
    from sqlalchemy import select

    from moyai.models import Turn
    from moyai.product_requester import answer_actor

    product = audience
    async with product.database.sessions() as db:
        turn = await db.scalar(select(Turn).where(Turn.session_id == product.session_id))
    runtime = SimpleNamespace(database=product.database, product=SimpleNamespace(identity=product.adapter))
    actor = await answer_actor(runtime, product.actor, turn)
    assert actor.id == product.raw_principal.id
    assert len(product.state["calls"]) == 2


@pytest.mark.parametrize("operation", ["mark_read", "send_now"])
async def test_http_role_insert_during_prefetch_denies_old_grant_without_mutation(audience, runtime, operation):
    import httpx

    from moyai.api import create_app
    from moyai.contracts import NewMessage
    from moyai.models import SessionRead, Turn

    product = audience
    original_database = runtime.database
    runtime.database = product.database
    turn = await product.database.submit(
        product.actor, product.session_id,
        NewMessage(prompt="Queued browser input", request_key="role-race-input", mode="when_idle"),
    )
    product.slack.audience_reads.snapshots.clear()
    product.state["calls"].clear()
    app = create_app(runtime, run_dispatcher=False)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/sessions/{session_id}")
    dependency = next(item.call for item in route.dependant.dependencies if item.name == "user")
    entered, release = asyncio.Event(), asyncio.Event()
    request_provider = product.slack.connectors.request

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await request_provider(*args, **kwargs)

    async def user():
        await product.database.prefetch_session_audiences(product.actor, [product.session_id])
        return product.actor

    product.slack.connectors.request = delayed
    app.dependency_overrides[dependency] = user
    task = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = asyncio.create_task(
                client.post(f"/api/sessions/{product.session_id}/read", json={"message_id": 9999})
                if operation == "mark_read" else client.post(f"/api/turns/{turn.id}/steer")
            )
            await asyncio.wait_for(entered.wait(), 2)
            async with product.source.begin() as db:
                await db.execute(text("SET LOCAL lock_timeout='500ms'"))
                await db.execute(text(
                    "SELECT pg_advisory_xact_lock(726941,'moyai'::regnamespace::oid::int)",
                ))
                await db.execute(text(
                    "INSERT INTO moyai.user_roles VALUES "
                    "('admin@example.test','member',1,'other@example.test','now')",
                ))
            release.set()
            response = await asyncio.wait_for(task, 2)
            assert response.status_code == 401
        async with product.database.sessions() as db:
            assert await db.get(SessionRead, (product.actor.id, product.session_id)) is None
            saved = await db.get(Turn, turn.id)
            assert saved.status == "queued" and saved.mode == "when_idle"
    finally:
        release.set()
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*list(product.slack.audience_reads.pending.values()), return_exceptions=True)
        runtime.database = original_database
