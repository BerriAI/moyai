"""Read-path regressions; actual-app before/after evidence is separate."""
import asyncio
import hashlib
from types import SimpleNamespace

import httpx
import test_product_identity as identity_fixtures
from sqlalchemy import event, text
from test_application_boundaries import sign_in
from test_sidebar import member_client

from moyai.api import create_app
from moyai.contracts import NewMessage, NewSession
from moyai.models import Turn, User

canonical_product = identity_fixtures.product


async def test_combined_reference_read_uses_real_current_product_identity(runtime, canonical_product):
    identity = canonical_product.adapter
    identity.native_engine = runtime.database.engine
    runtime.product_identity = identity
    user = User(
        id="google:admin-sub", username="product:" + hashlib.sha256(b"google:admin-sub").hexdigest(),
        display_name="Synthetic product user", password="", role="admin",
    )
    async with runtime.database.sessions.begin() as db:
        db.add(user)
    session = await runtime.database.create_session(user, NewSession(visibility="private"))
    cookie, _ = identity_fixtures.google_cookie(identity)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
        base_url="http://testserver", cookies={"workspace_session": cookie},
    ) as client:
        path = f"/api/sessions/{session.id}?resolve=true&include_activity=false&include_messages=true"
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["reference"]["id"] == response.json()["session"]["id"] == session.id
        assert response.json()["reference"]["source"] == "native"
        assert response.json()["messages"] == []
        assert response.headers["cache-control"] == "no-store"
        member_cookie, _ = identity_fixtures.google_cookie(identity, sub="member-sub", email="member@example.test")
        client.cookies.set("workspace_session", member_cookie)
        assert (await client.get(path)).status_code == 404
        client.cookies.set("workspace_session", cookie)
        async with canonical_product.writer.begin() as db:
            await db.execute(text("DELETE FROM users WHERE id='google:admin-sub'"))
        assert (await client.get(path)).status_code == 401


async def test_session_bootstrap_returns_the_authorized_transcript(runtime, actor):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    await runtime.database.submit(
        actor, session.id, NewMessage(prompt="Keep this saved message.", request_key="bootstrap"),
    )
    path = f"/api/sessions/{session.id}?include_activity=false"
    async with sign_in(runtime) as client:
        assert "messages" not in (await client.get(path)).json()
        response = await client.get(path + "&include_messages=true")
        assert response.status_code == 200
        expected = (await client.get(f"/api/sessions/{session.id}/messages")).json()
        assert response.json()["messages"] == expected
        assert expected[0]["content"] == "Keep this saved message."
        assert response.headers["cache-control"] == "no-store"
    async with member_client(runtime) as (other, _):
        denied = await other.get(path + "&include_messages=true")
        assert denied.status_code == 404
        assert "Keep this saved message." not in denied.text


async def test_transcript_snapshot_skips_optional_store_and_heavy_turn_columns(runtime, actor, monkeypatch):
    session = await runtime.database.create_session(actor, NewSession(visibility="private"))
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Saved prompt", request_key="one"))
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(Turn, turn.id)
        saved.status = "completed"
        saved.conversation = "large-private-working-snapshot"
    entered, release = asyncio.Event(), asyncio.Event()

    async def snapshot(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(messages=[])

    monkeypatch.setattr(runtime.stores.steps, "latest_snapshot", snapshot)
    statements = []
    def observed(_conn, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(runtime.database.engine.sync_engine, "before_cursor_execute", observed)
    try:
        async with sign_in(runtime) as client:
            response = await client.get(f"/api/sessions/{session.id}?include_activity=false")
            assert response.status_code == 200
            assert response.json()["turns"][0]["id"] == turn.id
            assert not entered.is_set()
            assert not any("moyai_turns.conversation" in sql or "moyai_turns.prompt" in sql for sql in statements)
            pending = asyncio.create_task(client.get(f"/api/sessions/{session.id}/activity"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert not pending.done()
            finally:
                release.set()
            assert (await pending).json() == []
        async with member_client(runtime) as (other, _):
            assert (await other.get(f"/api/sessions/{session.id}/activity")).status_code == 404
    finally:
        event.remove(runtime.database.engine.sync_engine, "before_cursor_execute", observed)


async def test_only_successful_hashed_build_assets_can_be_cached(runtime, tmp_path, monkeypatch):
    from moyai import api
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index-1234abcd.js").write_text("/* public build */")
    (assets / "licenses.md").write_text("licenses")
    (tmp_path / "index.html").write_text("<!doctype html><title>Moyai</title>")
    monkeypatch.setattr(api, "STATIC", tmp_path)
    async with sign_in(runtime) as client:
        built = await client.get("/assets/index-1234abcd.js")
        assert built.status_code == 200
        assert built.headers["cache-control"] == "public, max-age=31536000, immutable"
        for path in ["/", "/api/me", "/assets/licenses.md", "/assets/missing-1234abcd.js"]:
            response = await client.get(path)
            assert response.headers["cache-control"] == "no-store"
