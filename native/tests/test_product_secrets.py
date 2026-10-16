"""Canonical secret values stay encrypted; these are local protocol tests."""

import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psycopg
import pytest
import pytest_asyncio
from app.user_roles import UserRoles
from fastapi import FastAPI, Response
from sqlalchemy.ext.asyncio import create_async_engine
from test_product_connections import bridge as canonical_connections

from moyai.contracts import NewSession
from moyai.product_history import ProductHistoryAdapter
from moyai.product_identity import ProductIdentityAdapter
from moyai.product_secrets import ProductSecrets

bridge = canonical_connections


@pytest_asyncio.fixture
async def secrets_api(runtime, bridge):
    bridge.security.user_roles = UserRoles(bridge.store, bridge.settings)
    source = create_async_engine(f"sqlite+aiosqlite:///file:{bridge.store.path}?mode=ro&uri=true")
    identity = ProductIdentityAdapter(
        bridge.settings, source, native_engine=runtime.database.engine, allow_shared_identities=True,
    )
    product = SimpleNamespace(
        store_proxy=bridge.store, security=bridge.security, settings=bridge.settings, identity=identity,
        history=ProductHistoryAdapter(identity, source),
    )
    runtime.product = product
    service = ProductSecrets(runtime)
    app = FastAPI()
    app.include_router(service.routes())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=bridge.settings.public_url,
    ) as client:
        response = Response()
        sid = bridge.security.new_session(response, identity={
            "sub": "owner", "email": "owner@example.test", "domain": "example.test", "name": "Owner",
        })
        cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        client.cookies.set("workspace_session", cookie)
        client.headers.update({"Origin": bridge.settings.public_url, "X-CSRF-Token": bridge.security.csrf(sid)})
        yield SimpleNamespace(client=client, service=service, runtime=runtime, bridge=bridge, identity=identity)
    runtime.product = None
    await source.dispose()


def secret(**overrides):
    return {
        "provider": "generic", "name": "example-service", "label": "Local test",
        "scope": "personal", "lifetime": "persistent", "format": "env",
        "client_id": "local-save-once",
        "value": json.dumps({"EXAMPLE_TOKEN": "synthetic-secret-value"}), **overrides,
    }


async def test_canonical_secret_crud_encryption_revision_replay_and_revocation(secrets_api):
    client = secrets_api.client
    response = await client.post("/api/credentials/secrets", json=secret())
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    again = await client.post("/api/credentials/secrets", json=secret())
    assert again.json()["id"] == identity
    conflict = await client.post("/api/credentials/secrets", json=secret(value='{"EXAMPLE_TOKEN":"different"}'))
    assert conflict.status_code == 409
    inventory = await client.get("/api/credentials")
    assert "synthetic-secret-value" not in inventory.text
    row = inventory.json()["secrets"][0]
    assert row["can_manage"] and row["scope"] == "personal"
    assert inventory.json()["capabilities"]["scopes"] == ["personal"]
    encrypted = secrets_api.bridge.store.rows("SELECT encrypted FROM provider_secrets WHERE id=?", (identity,))[0]["encrypted"]
    assert "synthetic-secret-value" not in encrypted
    updated = await client.patch("/api/credentials/secrets/" + identity, json={"revision": row["revision"], "label": "Changed"})
    assert updated.status_code == 200
    stale = await client.patch("/api/credentials/secrets/" + identity, json={"revision": row["revision"], "label": "Old"})
    assert stale.status_code == 409
    assert (await client.delete("/api/credentials/secrets/" + identity)).json() == {"revoked": True}
    saved = secrets_api.bridge.store.rows("SELECT encrypted,revoked_at FROM provider_secrets WHERE id=?", (identity,))[0]
    assert saved["encrypted"] == "" and saved["revoked_at"]
    assert (await client.get("/api/credentials")).json()["secrets"] == []


async def test_secret_mutations_require_csrf_and_org_admin_and_hide_invalid_value(secrets_api):
    client = secrets_api.client
    denied = await client.post("/api/credentials/secrets", json=secret(scope="organization"))
    assert denied.status_code == 403
    csrf = client.headers.pop("X-CSRF-Token")
    assert (await client.post("/api/credentials/secrets", json=secret())).status_code == 403
    client.headers["X-CSRF-Token"] = csrf
    invalid = await client.post("/api/credentials/secrets", json=secret(extra_secret="synthetic-sensitive-marker"))
    assert invalid.status_code == 422 and "synthetic-sensitive-marker" not in invalid.text
    reserved = await client.post("/api/credentials/secrets", json=secret(value='{"PATH":"synthetic-sensitive-marker"}'))
    assert reserved.status_code == 422 and "synthetic-sensitive-marker" not in reserved.text


async def test_native_session_secret_scope_is_namespaced_and_unusable_by_legacy_runs(secrets_api):
    principal = await secrets_api.identity.current("google:owner")
    async with secrets_api.runtime.database.sessions.begin() as db:
        actor = await secrets_api.identity.project_native_user(db, principal)
    session = await secrets_api.runtime.database.create_session(actor, NewSession())
    root = "native:" + session.id
    response = await secrets_api.client.post("/api/credentials/secrets", json=secret(lifetime="session", root_id=root))
    assert response.status_code == 201, response.text
    row = (await secrets_api.client.get("/api/credentials", params={"run_id": root})).json()["secrets"][0]
    assert row["root_id"] == root and row["lifetime"] == "session"
    replacement = await secrets_api.client.patch("/api/credentials/secrets/" + row["id"], json={
        "revision": row["revision"], "value": '{"EXAMPLE_TOKEN":"replacement-local-only"}',
    })
    assert replacement.status_code == 200, replacement.text
    # Legacy scope checks compare source root IDs (32 hex), never the native
    # namespace. No row was copied into canonical runs.
    assert not secrets_api.bridge.store.rows("SELECT id FROM runs WHERE id=?", (root,))
    assert (await secrets_api.client.get("/api/credentials", params={"run_id": "0" * 32})).status_code == 404


async def test_postgres_pool_one_secret_mutations_reuse_live_role_transaction(tmp_path):
    from app.db import Store, database
    from app.security import Security
    from test_product_identity import settings_for

    url = os.environ.get("MOYAI_TEST_POSTGRES")
    if not url:
        pytest.skip("Set a disposable local PostgreSQL URL.")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}
    schema = "moyai_secret_probe_" + uuid4().hex
    store = None
    engine = create_async_engine(
        "postgresql+asyncpg://" + url.split("://", 1)[1],
        connect_args={"server_settings": {"search_path": f'"{schema}",pg_catalog'}},
    )
    try:
        def setup():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'CREATE SCHEMA "{schema}"')
            return Store(tmp_path / "source", database_url=url, database_schema=schema,
                         database_initialize=True, database_pool_size=1)

        store = await database(setup)
        settings = settings_for(tmp_path)
        roles = await database(UserRoles, store, settings)
        security = Security(settings, roles)
        await database(store.execute,
                       "INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       ("google:admin", "google", "admin@example.test", "Admin", "c", "u"))
        await database(store.execute, "CREATE TABLE moyai_users(id TEXT PRIMARY KEY,enabled BOOLEAN,auth_version INTEGER)")
        identity = ProductIdentityAdapter(settings, engine, native_engine=engine, schema=schema)
        runtime = SimpleNamespace(
            product=SimpleNamespace(store_proxy=store, security=security, settings=settings, identity=identity),
            settings=SimpleNamespace(postgres_url=url), database=SimpleNamespace(execution_schema=schema),
        )
        service = await database(ProductSecrets, runtime)
        app = FastAPI()
        app.include_router(service.routes())
        response = Response()
        sid = security.new_session(response, identity={
            "sub": "admin", "email": "admin@example.test", "domain": "example.test", "name": "Admin",
        })
        cookie = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.public_url) as client:
            client.cookies.set("workspace_session", cookie)
            client.headers.update({"Origin": settings.public_url, "X-CSRF-Token": security.csrf(sid)})
            created = await client.post("/api/credentials/secrets", json=secret(scope="organization"))
            assert created.status_code == 201, created.text
            saved = (await client.get("/api/credentials")).json()["secrets"][0]
            updated = await client.patch("/api/credentials/secrets/" + saved["id"],
                                         json={"revision": saved["revision"], "label": "Updated with one source slot"})
            assert updated.status_code == 200, updated.text
            assert (await client.delete("/api/credentials/secrets/" + saved["id"])).status_code == 200
    finally:
        await engine.dispose()
        if store is not None:
            await database(store.close)

        def cleanup():
            with psycopg.connect(url, autocommit=True) as connection:
                connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await database(cleanup)
