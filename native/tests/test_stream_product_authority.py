"""Real canonical/native PostgreSQL checks; supplementary to running-app proof."""

import asyncio
import os

import pytest
import test_product_identity_admission as identities
from sqlalchemy import event, text, update
from test_product_identity import request

from moyai.contracts import NewSession
from moyai.live_stream import authorized_events
from moyai.models import User
from moyai.stream_authority import authorize_stream_viewer

pytestmark = pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Requires disposable PostgreSQL.")
admitted_product = identities.admitted_product
slack_admission = identities.slack_admission


@pytest.mark.parametrize("change", ["role", "native_epoch", "account_deleted"])
async def test_ready_frame_cannot_cross_real_product_revocation(admitted_product, change):
    product = admitted_product
    session = await product.database.create_session(product.actor, NewSession(visibility="private"))
    viewer_request = request(state={"product_identity_grant": product.principal.grant})

    async def authorize():
        await authorize_stream_viewer(
            product.database, product.adapter, viewer_request, product.actor, session.id,
        )

    async def source():
        if change == "native_epoch":
            async with product.database.sessions.begin() as db:
                await db.execute(update(User).where(User.id == product.actor.id).values(auth_version=2))
        else:
            async with product.source.begin() as db:
                if change == "role":
                    await db.execute(text(
                        "INSERT INTO moyai.user_roles VALUES ('admin@example.test','member',1,'other','now')"
                    ))
                else:
                    await db.execute(text("DELETE FROM moyai.users WHERE id=:id"), {"id": product.actor.id})
        yield "private"

    async def disconnected():
        return False

    assert [frame async for frame in authorized_events(source(), authorize, disconnected)] == [
        "event: access_revoked\ndata: {}\n\n"
    ]


async def test_real_product_release_reduces_native_checkouts_and_returns_all_connections(admitted_product):
    product = admitted_product
    session = await product.database.create_session(product.actor, NewSession(visibility="private"))
    viewer_request = request(state={"product_identity_grant": product.principal.grant})
    checkouts = []

    def checkout(*_):
        checkouts.append(True)

    event.listen(product.database.engine.sync_engine, "checkout", checkout)
    try:
        # Baseline's exact order: standalone user reader, canonical reader,
        # session reader, requester reader (the last needs no SQL for direct self).
        await product.database.user(product.actor.id)
        await product.adapter.require_request_current(viewer_request)
        await product.database.session(product.actor, session.id)
        async with product.database.sessions() as db:
            assert await product.database.same_person(db, product.actor, product.actor.id)
        assert len(checkouts) == 3
        checkouts.clear()
        await asyncio.wait_for(authorize_stream_viewer(
            product.database, product.adapter, viewer_request, product.actor, session.id,
        ), 2)
        assert len(checkouts) == 2
        assert product.database.engine.pool.checkedout() == 0
    finally:
        event.remove(product.database.engine.sync_engine, "checkout", checkout)


async def test_shared_activity_does_not_require_the_original_requester_link(slack_admission):
    product = slack_admission
    session = await product.database.create_session(product.actor, NewSession(visibility="shared"))
    viewer_request = request(state={"product_identity_grant": product.principal.grant})

    async def authorize():
        await authorize_stream_viewer(
            product.database, product.adapter, viewer_request, product.actor,
            session.id,
        )

    await authorize()
    async with product.source.begin() as db:
        await db.execute(
            text("UPDATE moyai.users SET profile_conflict=1 WHERE id=:id"),
            {"id": product.raw_principal.id},
        )
    await authorize()
