"""The stream still revalidates every authority boundary on every release."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import event, update

from moyai.contracts import NewSession
from moyai.live_stream import authorized_events
from moyai.models import Session, User
from moyai.stream_authority import authorize_stream_viewer


def request():
    return Request({"type": "http", "headers": []})


async def test_native_checks_share_one_checkout_without_caching(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    checkouts = []

    def checkout(*_):
        checkouts.append(True)

    event.listen(runtime.database.engine.sync_engine, "checkout", checkout)
    try:
        for _ in range(2):
            await authorize_stream_viewer(runtime.database, None, request(), actor, session.id)
        assert len(checkouts) == 2
    finally:
        event.remove(runtime.database.engine.sync_engine, "checkout", checkout)


@pytest.mark.parametrize("revocation", ["disable", "version", "delete", "owner"])
async def test_revoked_native_authority_never_releases_ready_content(runtime, actor, revocation):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))

    async def authorize():
        await authorize_stream_viewer(runtime.database, None, request(), actor, session.id)

    async def source():
        async with runtime.database.sessions.begin() as db:
            if revocation in {"disable", "version"}:
                values = {"enabled": False} if revocation == "disable" else {"auth_version": actor.auth_version + 1}
                await db.execute(update(User).where(User.id == actor.id).values(**values))
            elif revocation == "delete":
                await db.execute(update(Session).where(Session.id == session.id).values(deleted=True))
            else:
                other = User(username="other", display_name="Other", password="unused", role="member")
                db.add(other)
                await db.flush()
                await db.execute(update(Session).where(Session.id == session.id).values(owner_id=other.id))
        yield "private"

    async def disconnected():
        return False

    assert [item async for item in authorized_events(source(), authorize, disconnected)] == [
        "event: access_revoked\ndata: {}\n\n"
    ]


async def test_canonical_check_is_fresh_and_does_not_hold_native_connection(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    checked_out = 0
    checks = 0

    def checkout(*_):
        nonlocal checked_out
        checked_out += 1

    def checkin(*_):
        nonlocal checked_out
        checked_out -= 1

    async def canonical_check(_request):
        nonlocal checks
        assert checked_out == 0
        checks += 1
        if checks == 2:
            raise HTTPException(401, "Canonical authority changed.")

    identity = SimpleNamespace(require_request_current=canonical_check)
    event.listen(runtime.database.engine.sync_engine, "checkout", checkout)
    event.listen(runtime.database.engine.sync_engine, "checkin", checkin)
    try:
        await authorize_stream_viewer(runtime.database, identity, request(), actor, session.id)
        with pytest.raises(HTTPException) as revoked:
            await authorize_stream_viewer(runtime.database, identity, request(), actor, session.id)
        assert revoked.value.status_code == 401
        assert checks == 2
    finally:
        event.remove(runtime.database.engine.sync_engine, "checkout", checkout)
        event.remove(runtime.database.engine.sync_engine, "checkin", checkin)


async def test_source_audience_is_rechecked_each_release(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="shared"))
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == session.id).values(surface="slack"))
    runtime.database.product_mode = "existing"
    calls = 0

    async def audience(_db, _session, viewer):
        nonlocal calls
        assert viewer.slack_read_cache == {}
        viewer.slack_read_cache["previous-release"] = True
        calls += 1
        return calls == 1

    runtime.database.slack_access = audience
    await authorize_stream_viewer(runtime.database, None, request(), actor, session.id)
    with pytest.raises(HTTPException) as revoked:
        await authorize_stream_viewer(runtime.database, None, request(), actor, session.id)
    assert revoked.value.status_code == 404
    assert calls == 2
