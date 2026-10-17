"""Real PostgreSQL read/pool regressions; supplementary to deployed-app proof."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import test_product_authority as authority_fixtures
from fastapi import Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from test_product_identity import google_cookie

from moyai.api import create_app
from moyai.contracts import NewMessage, NewSession
from moyai.database import Database
from moyai.models import Session, User
from moyai.product_continuation import ProductContinuationService
from moyai.product_continuation_models import ProductContinuation
from moyai.product_history import ProductHistoryAdapter
from moyai.product_identity import ProductIdentityAdapter, ProductPrincipal
from moyai.product_routes import ProductReadService, Reader
from moyai.runtime import Runtime
from moyai.settings import Settings

pytestmark = pytest.mark.skipif(
    not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.",
)
product_database = authority_fixtures.product_database
admitted_product = authority_fixtures.admitted_product


class AdmittedProduct(Protocol):
    database: Database
    adapter: ProductIdentityAdapter
    source: AsyncEngine
    schema: str
    settings: Settings
    subject: str
    actor: User
    principal: ProductPrincipal


@dataclass(frozen=True)
class ReadProduct:
    runtime: Runtime
    identity: ProductIdentityAdapter
    writer: AsyncEngine
    client: httpx.AsyncClient
    actor: User
    principal: ProductPrincipal
    session_id: str
    app: FastAPI


@pytest_asyncio.fixture
async def read_product(admitted_product: AdmittedProduct) -> AsyncIterator[ReadProduct]:
    product = admitted_product
    session = await product.database.create_session(product.actor, NewSession())
    await product.database.engine.dispose()
    native = create_async_engine(
        product.settings.sqlalchemy_url, pool_size=1, max_overflow=0, pool_timeout=0.3,
        connect_args={"server_settings": {"search_path": f'"{product.schema}",pg_catalog'}},
    )
    source = create_async_engine(
        product.source.url, pool_size=1, max_overflow=0, pool_timeout=0.3,
    )
    product.database.engine = native
    product.database.sessions = async_sessionmaker(native, expire_on_commit=False)
    product.adapter.native_engine = native
    product.adapter.source_engine = source
    runtime = Runtime(product.settings.model_copy(update={"product_mode": "standalone"}))
    await runtime.database.close()
    runtime.database = product.database
    runtime.product_identity = product.adapter
    runtime.product_history = ProductHistoryAdapter(product.adapter, source, schema="moyai")
    runtime.product_continuations = ProductContinuationService(runtime)
    cookie, _ = google_cookie(product.adapter, sub=product.subject)
    app = create_app(runtime, run_dispatcher=False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8787", cookies={"workspace_session": cookie},
        ) as client:
            yield ReadProduct(
                runtime, product.adapter, product.source, client, product.actor, product.principal, session.id,
                app,
            )
    finally:
        await runtime.close()
        await source.dispose()


@pytest.mark.parametrize("writer_kind", ["canonical", "identity"])
async def test_existing_user_gets_complete_while_writer_still_owns_lock(
    read_product: ReadProduct, writer_kind: Literal["canonical", "identity"],
) -> None:
    product = read_product
    # Finish lazy application setup before measuring the one-connection pools.
    # Health does not authenticate or project this user: the first authenticated
    # reads must still complete while the writer owns its lock.
    assert (await product.client.get("/health")).status_code == 200
    async with product.writer.begin() as writer:
        if writer_kind == "canonical":
            await writer.execute(text(
                "SELECT pg_advisory_xact_lock(726941, 'moyai'::regnamespace::oid::int)"
            ))
        else:
            await writer.execute(text(
                f'UPDATE "{product.runtime.settings.database_schema}".moyai_users '
                "SET display_name=display_name WHERE id=:id"
            ), {"id": product.actor.id})
        replies = await asyncio.wait_for(asyncio.gather(
            product.client.get("/api/me"),
            product.client.get("/api/sessions"),
            product.client.get(f"/api/sessions/{product.session_id}"),
            product.client.get(f"/api/sessions/{product.session_id}/messages"),
        ), timeout=2)
        assert [reply.status_code for reply in replies] == [200, 200, 200, 200]
        assert writer.in_transaction()


async def test_get_uses_current_role_and_profile_without_writing_native_cache(
    read_product: ReadProduct,
) -> None:
    product = read_product
    async with product.writer.begin() as writer:
        await writer.execute(text(
            "INSERT INTO moyai.user_roles VALUES "
            "('admin@example.test','member',1,'other@example.test','now')"
        ))
        await writer.execute(text(
            "UPDATE moyai.users SET name='Current product name' WHERE id=:id"
        ), {"id": product.actor.id})
    response = await product.client.get("/api/me")
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "member"
    assert response.json()["user"]["name"] == "Current product name"
    async with product.runtime.database.sessions() as db:
        cached = await db.get(User, product.actor.id)
        assert cached is not None
        assert cached.role == "admin"
        assert cached.display_name == "Product admin"


async def test_missing_read_projection_still_creates_the_canonical_identity(
    admitted_product: AdmittedProduct,
) -> None:
    product = admitted_product
    async with product.database.sessions.begin() as db:
        await db.execute(delete(User))
    runtime = Runtime(product.settings.model_copy(update={"product_mode": "standalone"}))
    await runtime.database.close()
    runtime.database = product.database
    runtime.product_identity = product.adapter
    cookie, _ = google_cookie(product.adapter, sub=product.subject)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(runtime, run_dispatcher=False)),
            base_url="http://127.0.0.1:8787", cookies={"workspace_session": cookie},
        ) as client:
            response = await client.get("/api/me")
        assert response.status_code == 200
        assert response.json()["user"]["id"] == product.principal.id
        assert (await product.database.user(product.principal.id)).enabled
    finally:
        await runtime.close()


async def test_native_history_recheck_completes_with_one_connection(
    read_product: ReadProduct,
) -> None:
    product = read_product
    service = ProductReadService(product.runtime)
    reader = Reader(product.actor, product.principal)
    results = await asyncio.wait_for(asyncio.gather(*(
        service.native_recheck(reader, [product.session_id]) for _ in range(4)
    )), timeout=2)
    assert results == [{product.session_id}] * 4


async def test_mapped_history_recheck_preserves_source_and_native_privacy(
    read_product: ReadProduct,
) -> None:
    product = read_product
    source_id = uuid4().hex
    async with product.writer.begin() as writer:
        await writer.execute(text(
            "CREATE TABLE moyai.runs (id TEXT PRIMARY KEY, owner_id TEXT, private_owner_id TEXT, "
            "parent_run_id TEXT, deleted_at TEXT, deletion_requested_at TEXT)"
        ))
        await writer.execute(text("CREATE TABLE moyai.run_roots (run_id TEXT, root_id TEXT)"))
        await writer.execute(text("CREATE TABLE moyai.run_ancestry (run_id TEXT, ancestor_id TEXT)"))
        await writer.execute(text("CREATE TABLE moyai.messages (run_id TEXT, role TEXT, user_id TEXT)"))
        await writer.execute(text(
            "INSERT INTO moyai.runs VALUES (:id,:owner,'','','','')"
        ), {"id": source_id, "owner": product.actor.id})
        await writer.execute(text("INSERT INTO moyai.run_roots VALUES (:id,:id)"), {"id": source_id})
        await writer.execute(text("INSERT INTO moyai.run_ancestry VALUES (:id,:id)"), {"id": source_id})
    turn = await product.runtime.database.submit(
        product.actor, product.session_id, NewMessage(prompt="Local input", request_key="local"),
    )
    async with product.runtime.database.sessions.begin() as db:
        db.add(ProductContinuation(
            source_schema="moyai", source_session_id=source_id, native_session_id=product.session_id,
            source_owner_id=product.actor.id, initiated_by=product.actor.id,
            source_fingerprint="a" * 64, history="[]", history_count=0, imported_count=0,
            workspace="{}", workspace_fingerprint="b" * 64, first_turn_id=turn.id,
        ))
    service = ProductReadService(product.runtime)
    reader = Reader(product.actor, product.principal)
    assert await asyncio.wait_for(service.native_recheck(reader, [product.session_id]), 2) == {
        product.session_id,
    }
    async with product.writer.begin() as writer:
        await writer.execute(text("UPDATE moyai.runs SET private_owner_id='other' WHERE id=:id"), {"id": source_id})
    assert await service.native_recheck(reader, [product.session_id]) == set()
    async with product.writer.begin() as writer:
        await writer.execute(text("UPDATE moyai.runs SET private_owner_id='' WHERE id=:id"), {"id": source_id})
    async with product.runtime.database.sessions.begin() as db:
        await db.execute(update(Session).where(Session.id == product.session_id).values(deleted=True))
    assert await service.native_recheck(reader, [product.session_id]) == set()


@pytest.mark.parametrize("change", ["disable", "epoch", "username"])
async def test_read_projection_rejects_changed_native_controls(
    read_product: ReadProduct, change: Literal["disable", "epoch", "username"],
) -> None:
    product = read_product
    async with product.runtime.database.sessions.begin() as db:
        row = await db.get(User, product.actor.id)
        assert row is not None
        if change == "disable":
            row.enabled = False
        elif change == "epoch":
            row.auth_version += 1
        else:
            row.username = "unrelated-local-account"
    async with product.runtime.database.sessions() as db:
        with pytest.raises(HTTPException) as denied:
            await product.identity.read_native_user(db, product.principal)
    assert denied.value.status_code == (409 if change == "username" else 401)


async def test_source_revocation_after_read_still_invalidates_original_grant(
    read_product: ReadProduct,
) -> None:
    product = read_product
    async with product.runtime.database.sessions() as db:
        user = await product.identity.read_native_user(db, product.principal)
    assert user is not None
    async with product.writer.begin() as writer:
        await writer.execute(text("DELETE FROM moyai.users WHERE id=:id"), {"id": product.actor.id})
    response = await product.client.get("/api/me")
    assert response.status_code == 401
    with pytest.raises(HTTPException):
        await product.identity.require_current(product.principal.grant)
    async with product.runtime.database.sessions() as db:
        assert await db.scalar(select(User.id).where(User.id == product.actor.id)) == product.actor.id


@pytest.mark.parametrize("change", ["role", "deleted", "disabled", "epoch"])
async def test_response_rechecks_revocation_after_read_only_actor(
    read_product: ReadProduct, change: Literal["role", "deleted", "disabled", "epoch"],
) -> None:
    product = read_product
    me = next(route for route in product.app.routes if isinstance(route, APIRoute) and route.path == "/api/me")
    actor = me.dependant.dependencies[0].call
    assert actor is not None

    @product.app.get("/read-then-revoke")
    async def revoke_after_authorization(user: User = Depends(actor)) -> dict[str, str]:
        assert user.id == product.actor.id
        if change in {"role", "deleted"}:
            async with product.writer.begin() as writer:
                if change == "role":
                    await writer.execute(text(
                        "INSERT INTO moyai.user_roles VALUES "
                        "('admin@example.test','member',1,'other@example.test','now')"
                    ))
                else:
                    await writer.execute(text("DELETE FROM moyai.users WHERE id=:id"), {"id": user.id})
        else:
            async with product.runtime.database.sessions.begin() as db:
                saved = await db.get(User, user.id)
                assert saved is not None
                if change == "disabled":
                    saved.enabled = False
                else:
                    saved.auth_version += 1
        return {"private_result": "must not leave the middleware"}

    # Keep the probe ahead of the application's catch-all frontend route.
    product.app.router.routes.insert(0, product.app.router.routes.pop())
    response = await product.client.get("/read-then-revoke")
    assert response.status_code == 401
    assert "private_result" not in response.json()
