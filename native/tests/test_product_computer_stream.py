"""Real canonical auth adapter + local stores; no cloud/browser transport claims."""
import asyncio
import time
from datetime import datetime, timedelta

import jwt
import pytest
from app.user_roles import RoleChange, UserRoles
from fastapi import HTTPException, WebSocket
from itsdangerous.timed import TimestampSigner
from test_computer_stream import Remote, Socket, ready_computer
from test_product_identity import (
    cloudflare_setup,
    google_cookie,
)
from test_product_identity import (
    product as canonical_product,
)

from moyai.computer import authorize
from moyai.computer_stream import authenticate_socket, bind, relay, validate
from moyai.models import User, new_id

product = canonical_product


def socket(cookie="", *, assertion="", native_cookie="", state=None):
    cookies = []
    if cookie:
        cookies.append(f"workspace_session={cookie}")
    if native_cookie:
        cookies.append(f"moyai_session={native_cookie}")
    headers = [(b"host", b"workspace.example.test"), (b"origin", b"https://workspace.example.test")]
    if cookies:
        headers.append((b"cookie", "; ".join(cookies).encode()))
    if assertion:
        headers.append((b"cf-access-jwt-assertion", assertion.encode()))
    return WebSocket({
        "type": "websocket", "scheme": "wss", "path": "/api/sessions/test/computer/stream",
        "query_string": b"", "server": ("workspace.example.test", 443), "client": ("127.0.0.1", 1234),
        "headers": headers, "state": state or {}, "subprotocols": [],
    }, receive=None, send=None)


async def product_access(runtime, product, *, transport=None):
    product.adapter.native_engine = runtime.database.engine
    runtime.product_identity = product.adapter
    if transport is None:
        cookie, _ = google_cookie(product.adapter)
        transport = socket(cookie)
    actor, token, request, grant = await authenticate_socket(runtime, transport, cookie_name="moyai_session")
    session = await ready_computer(runtime, actor)
    access = await bind(
        runtime, session.id, actor, new_id(), controlling=False, session_token=token,
        product_request=request, product_grant=grant,
    )
    return access


async def test_product_stream_uses_original_canonical_cookie_and_ignores_native_cookie(runtime, actor, product):
    product.adapter.native_engine = runtime.database.engine
    runtime.product_identity = product.adapter
    native, _ = runtime.security.issue_session(actor.id, actor.auth_version)
    with pytest.raises(HTTPException):
        await authenticate_socket(runtime, socket(native_cookie=native), cookie_name="moyai_session")
    cookie, _ = google_cookie(product.adapter)
    access = await product_access(runtime, product, transport=socket(cookie, native_cookie=native, state={
        "product_identity_grant": "untrusted", "access_session": {"role": "admin"},
    }))
    assert access.actor.id == "google:admin-sub" and not access.session_token
    assert access.product_request.scope["type"] == "http"
    assert access.product_request.url.scheme == "https"
    assert access.product_request.cookies["workspace_session"] == cookie
    assert cookie not in repr(access) and native not in repr(access)
    await validate(runtime, access)


async def test_missing_existing_product_adapter_cannot_fall_back_to_native_auth(runtime, actor):
    session = await ready_computer(runtime, actor)
    native, _ = runtime.security.issue_session(actor.id, actor.auth_version)
    runtime.settings.product_mode = "existing"
    assert runtime.product_identity is None
    with pytest.raises(HTTPException) as denied:
        await authenticate_socket(runtime, socket(native_cookie=native), cookie_name="moyai_session")
    assert denied.value.status_code == 503
    with pytest.raises(HTTPException):
        await authorize(runtime, session.id, actor)


async def test_product_cookie_expiry_closes_idle_viewer(runtime, product, monkeypatch):
    issued = int(time.time())
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: issued)
    access = await product_access(runtime, product)
    await validate(runtime, access)
    monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda _: issued + 43201)
    monkeypatch.setattr("moyai.computer_stream.RECHECK_SECONDS", .01)
    downstream, remote = Socket(), Remote()
    with pytest.raises(HTTPException):
        await asyncio.wait_for(relay(runtime, downstream, remote, access), 2)
    assert not downstream.sent and not remote.sent


async def test_source_role_change_cannot_replace_original_stream_grant(runtime, product):
    access = await product_access(runtime, product)
    original = access.product_grant
    UserRoles(product.store, product.settings).change(
        RoleChange(email="admin@example.test", role="member", revision=0),
        {"method": "google", "identity": {"email": "other@example.test"}},
    )
    with pytest.raises(HTTPException):
        await validate(runtime, access)
    with pytest.raises(HTTPException):
        await authorize(runtime, access.session_id, access.actor)
    assert access.product_grant == original


@pytest.mark.parametrize("change", ["disable", "epoch"])
async def test_product_stream_preserves_native_local_revocation(runtime, product, change):
    access = await product_access(runtime, product)
    async with runtime.database.sessions.begin() as db:
        user = await db.get(User, access.actor.id)
        if change == "disable":
            user.enabled = False
        else:
            user.auth_version += 1
    with pytest.raises(HTTPException):
        await validate(runtime, access)


async def test_websocket_checks_cloudflare_assertion_without_http_middleware(runtime, product, monkeypatch):
    adapter, cookie, _key, claims, token = await cloudflare_setup(product)
    product.adapter = adapter
    adapter.native_engine = runtime.database.engine
    runtime.product_identity = adapter
    with pytest.raises(HTTPException):
        await authenticate_socket(runtime, socket(cookie), cookie_name="moyai_session")
    forged = jwt.encode(claims, "not-the-authority-key" * 3, algorithm="HS256", headers={"kid": "local-key"})
    with pytest.raises(HTTPException):
        await authenticate_socket(runtime, socket(cookie, assertion=forged), cookie_name="moyai_session")
    access = await product_access(runtime, product, transport=socket(cookie, assertion=token))
    await validate(runtime, access)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=3)
    monkeypatch.setattr(jwt.api_jwt, "datetime", Later)
    with pytest.raises(HTTPException):
        await validate(runtime, access)
