"""Local product auth/library boundaries; no production identities or provider calls."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import jwt
import pytest
import pytest_asyncio
from app.config import Settings as ProductSettings
from app.database import sqlite_connection
from app.user_roles import RoleChange, UserRoles
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request, Response
from pydantic import TypeAdapter
from pydantic_ai import RunContext, RunUsage
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness import Memory
from pydantic_ai_harness.memory import InMemoryStore
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from temporalio import activity
from temporalio.testing import ActivityEnvironment

from moyai.contracts import NewMessage, NewSession, PreparedTurn, RunScope
from moyai.models import User
from moyai.product_identity import ProductGrant, ProductIdentityAdapter
from moyai.security import Security, check_password
from moyai.settings import Settings


@pytest.mark.parametrize("actor_id", [
    "shared:password:admin", "google:synthetic-subject", "cloudflare:" + "a" * 64,
    "google:" + "用户" * 220, "google:../private", "simple-non-uuid",
])
async def test_canonical_identity_memory_path_works_with_real_harness_and_without_grant(actor_id):
    scope = RunScope(
        session_id=str(uuid4()), turn_id=str(uuid4()), actor_id=actor_id, model="test",
        product_grant=ProductGrant(actor_id, "a" * 64),
    )
    segment = scope.memory_namespace.split("/")[1]
    assert segment.startswith("identity-") and len(segment) == 73
    assert scope.actor_id == actor_id
    assert replace(scope, product_grant=None).memory_namespace == scope.memory_namespace
    assert replace(scope, actor_id=actor_id + "x").memory_namespace != scope.memory_namespace
    prepared = PreparedTurn(scope, "Local serialization boundary")
    assert TypeAdapter(PreparedTurn).validate_json(TypeAdapter(PreparedTurn).dump_json(prepared)) == prepared
    memory = Memory(store=InMemoryStore(), namespace=lambda ctx: ctx.deps.memory_namespace)
    for selected in (scope, replace(scope, surface="slack")):
        context = RunContext(deps=selected, model=TestModel(), usage=RunUsage())
        bound = await memory.for_run(context)
        await bound.before_run(context)
        assert bound.resolve_scope(context)[1] == selected.memory_namespace + "/main"


@pytest.mark.parametrize("actor_id", [
    "067f4f0e-ec3a-4d81-8ef5-e294b9677104", "067F4F0E-EC3A-4D81-8EF5-E294B9677104",
])
def test_native_uuid_memory_namespaces_remain_unchanged(actor_id):
    scope = RunScope(str(uuid4()), str(uuid4()), actor_id, "test")
    assert scope.memory_namespace == f"private/{actor_id}/{scope.grants_key}"
    assert replace(scope, visibility="shared").memory_namespace == f"shared/{actor_id}/{scope.grants_key}"
    assert replace(scope, surface="slack").memory_namespace == f"slack/{scope.session_id}/{actor_id}/{scope.grants_key}"


def settings_for(tmp_path, **overrides):
    values = {
        "_env_file": None, "data_dir": tmp_path / "unused-legacy-state",
        "public_url": "http://127.0.0.1:8787", "moyai_database_url": "",
        "moyai_runtime_role": "standalone", "moyai_schema_mode": "auto",
        "moyai_separate_broker": False, "moyai_coordinator_handoff": False,
        "temporal_enabled": False, "checkpoint_dir": None,
        "session_secret": "synthetic-product-session-secret-only",
        "encryption_key": Fernet.generate_key().decode(),
        "workspace_password": "synthetic-shared-password-only", "workspace_member_password": "",
        "password_login_enabled": True, "google_client_id": "synthetic-client",
        "google_client_secret": "synthetic-client-secret",
        "google_allowed_domains": "example.test", "google_admin_emails": "admin@example.test,other@example.test",
        "cloudflare_access_team_domain": "", "cloudflare_access_audience": "",
        "cloudflare_access_broker_audience": "", "cloudflare_access_client_id": "",
        "cloudflare_access_client_secret": "", "cloudflare_access_login": False,
    }
    values.update(overrides)
    return ProductSettings(**values)


SCHEMA = (
    ("CREATE TABLE users (id TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL DEFAULT '', "
    "name TEXT NOT NULL, linked_user_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"),
    ("CREATE TABLE user_roles (email TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('admin','member')), "
    "revision INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)"),
    ("CREATE TABLE user_role_audit (id INTEGER PRIMARY KEY, email TEXT NOT NULL, previous_role TEXT NOT NULL, "
    "role TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)"),
    ("CREATE TABLE access_identities (issuer TEXT NOT NULL, subject TEXT NOT NULL, email TEXT NOT NULL, "
    "user_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, PRIMARY KEY(issuer,subject), "
    "UNIQUE(issuer,user_id), UNIQUE(issuer,email))"),
)


class TestRoleStore:
    __test__ = False
    schema_updates = False

    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite_connection(self.path)
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()


@pytest_asyncio.fixture
async def product(tmp_path):
    path = tmp_path / "canonical.db"
    writer = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with writer.begin() as connection:
        for statement in SCHEMA:
            await connection.exec_driver_sql(statement)
        await connection.execute(text(
            "INSERT INTO users VALUES (:id,'google',:email,:name,NULL,'created','updated')"
        ), [
            {"id": "google:admin-sub", "email": "admin@example.test", "name": "Product admin"},
            {"id": "google:member-sub", "email": "member@example.test", "name": "Product member"},
        ])
    source = create_async_engine(f"sqlite+aiosqlite:///file:{path}?mode=ro&uri=true")
    settings = settings_for(tmp_path)
    adapter = ProductIdentityAdapter(settings, source)
    try:
        yield SimpleNamespace(
            adapter=adapter, source=source, writer=writer, settings=settings, store=TestRoleStore(path),
        )
    finally:
        await source.dispose()
        await writer.dispose()


def google_cookie(adapter, *, sub="admin-sub", email="admin@example.test"):
    response = Response()
    sid = adapter.legacy_security.new_session(response, identity={
        "sub": sub, "email": email, "domain": email.rpartition("@")[2], "name": "Synthetic identity",
    })
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie["workspace_session"].value, adapter.legacy_security.csrf(sid)


def request(cookie="", *, method="GET", headers=None, state=None):
    values = {"cookie": f"workspace_session={cookie}"} if cookie else {}
    values.update(headers or {})
    return Request({
        "type": "http", "method": method, "path": "/api/product/sessions", "query_string": b"",
        "scheme": "http", "server": ("127.0.0.1", 8787), "client": ("127.0.0.1", 1234),
        "headers": [(name.lower().encode(), value.encode()) for name, value in values.items()],
        "state": state or {},
    })


async def test_existing_signed_google_identity_exact_id_and_live_roles(product):
    cookie, csrf = google_cookie(product.adapter)
    principal = await product.adapter.authenticate(request(cookie), admin=True)
    assert principal.id == "google:admin-sub" and principal.role == "admin" and principal.individual
    assert principal.method == "google" and principal.csrf_token == csrf
    assert set(asdict(principal.grant)) == {"user_id", "authority_version"}
    assert cookie not in repr(principal) and csrf not in repr(principal)
    UserRoles(product.store, product.settings).change(
        RoleChange(email=principal.email, role="member", revision=0),
        {"method": "google", "identity": {"email": "other@example.test"}},
    )
    with pytest.raises(HTTPException) as denied:
        await product.adapter.require_current(principal.grant)
    assert denied.value.status_code == 401
    assert (await product.adapter.authenticate(request(cookie))).role == "member"
    with pytest.raises(HTTPException) as denied:
        await product.adapter.authenticate(request(cookie), admin=True)
    assert denied.value.status_code == 403
    # Returning to the same role must not revive an older grant (revision ABA).
    UserRoles(product.store, product.settings).change(
        RoleChange(email=principal.email, role="admin", revision=1),
        {"method": "google", "identity": {"email": "other@example.test"}},
    )
    with pytest.raises(HTTPException):
        await product.adapter.require_current(principal.grant)


async def test_positive_schema_cache_never_caches_account_authority(product):
    async with product.writer.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.execute(User.__table__.insert().values(
            id="google:member-sub", username="member", display_name="Member",
            password="", role="member", enabled=True, auth_version=1,
        ))
    statements = []
    def observed(_conn, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(product.source.sync_engine, "before_cursor_execute", observed)
    try:
        principal = await product.adapter.current("google:member-sub")
        await product.adapter.require_current(principal.grant)
        assert sum("table_info" in sql for sql in statements) == 1
        assert sum("SELECT role,revision" in sql for sql in statements) == 2
        async with product.writer.begin() as conn:
            await conn.execute(text("UPDATE moyai_users SET enabled=false"))
        with pytest.raises(HTTPException) as denied:
            await product.adapter.require_current(principal.grant)
        assert denied.value.status_code == 401
        async with product.writer.begin() as conn:
            await conn.execute(text("DROP TABLE moyai_users"))
        with pytest.raises(HTTPException) as missing:
            await product.adapter.require_current(principal.grant)
        assert missing.value.status_code == 503
    finally:
        event.remove(product.source.sync_engine, "before_cursor_execute", observed)


@pytest.mark.parametrize("headers", [
    {"x-user-id": "google:admin-sub", "x-user-role": "admin"},
    {"cf-access-authenticated-user-email": "admin@example.test"},
])
async def test_unsigned_identity_headers_and_prepopulated_state_never_authenticate(product, headers):
    with pytest.raises(HTTPException) as denied:
        await product.adapter.authenticate(request(headers=headers, state={
            "access_actor": "google:admin-sub",
            "access_session": {"sid": "forged", "method": "google", "role": "admin"},
        }))
    assert denied.value.status_code == 401


@pytest.mark.parametrize("headers", [
    {},
    {"origin": "https://attacker.invalid"},
    {"origin": "http://127.0.0.1:8787", "x-moyai-csrf": "native-header-only"},
])
async def test_product_mutations_retain_exact_origin_and_legacy_csrf(product, headers):
    cookie, csrf = google_cookie(product.adapter)
    if "x-moyai-csrf" in headers:
        headers = {**headers, "x-moyai-csrf": csrf}
    with pytest.raises(HTTPException) as denied:
        await product.adapter.authenticate(request(cookie, method="POST", headers=headers), mutation=True)
    assert denied.value.status_code == 403
    principal = await product.adapter.authenticate(request(cookie, method="POST", headers={
        "origin": product.settings.public_url, "x-csrf-token": csrf, "x-moyai-csrf": csrf,
    }), mutation=True)
    assert principal.id == "google:admin-sub"


async def test_no_enrollment_bootstrap_or_source_writes(product):
    cookie, _ = google_cookie(product.adapter, sub="unenrolled")
    with pytest.raises(HTTPException) as denied:
        await product.adapter.authenticate(request(cookie))
    assert denied.value.status_code == 401
    async with product.writer.connect() as db:
        assert (await db.execute(text("SELECT count(*) FROM users"))).scalar_one() == 2
    async with product.source.connect() as db:
        with pytest.raises(SQLAlchemyError):
            await db.execute(text("DELETE FROM users"))
    assert not product.settings.data_dir.exists()
    assert "app.main" not in sys.modules and "app.durable_runner" not in sys.modules
    assert "app.deepagents_workflow" not in sys.modules


async def test_missing_explicit_keys_or_writable_sqlite_source_fail_closed(product):
    for key in ("session_secret", "encryption_key"):
        with pytest.raises(ValueError, match="explicit"):
            ProductIdentityAdapter(product.settings.model_copy(update={key: ""}), product.source)
    with pytest.raises(ValueError, match="mode=ro"):
        ProductIdentityAdapter(product.settings, product.writer)


async def test_native_cookie_and_cipher_are_not_legacy_formats(product):
    key = Settings(
        _env_file=None, encryption_key=product.settings.encryption_key, database_url="",
    ).master_key
    native = Security(key)
    token, _ = native.issue_session("google:admin-sub", 1)
    with pytest.raises(HTTPException):
        await product.adapter.authenticate(request(token))
    with pytest.raises(InvalidToken):
        native.unseal(product.adapter.legacy_security.encrypt("synthetic legacy secret"))
    with pytest.raises(InvalidToken):
        product.adapter.legacy_security.decrypt(native.seal("synthetic native secret"))


async def test_optional_disabled_flag_deletion_and_allowed_domains_revoke_immediately(product):
    principal = await product.adapter.current("google:member-sub")
    async with product.writer.begin() as db:
        await db.execute(text("ALTER TABLE users ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"))
        await db.execute(text("UPDATE users SET enabled=0 WHERE id='google:member-sub'"))
    with pytest.raises(HTTPException):
        await product.adapter.require_current(principal.grant)
    async with product.writer.begin() as db:
        await db.execute(text("UPDATE users SET enabled=1 WHERE id='google:member-sub'"))
    product.settings.google_allowed_domains = "another.test"
    with pytest.raises(HTTPException):
        await product.adapter.require_current(principal.grant)
    product.settings.google_allowed_domains = "example.test"
    async with product.writer.begin() as db:
        await db.execute(text("DELETE FROM users WHERE id='google:member-sub'"))
    with pytest.raises(HTTPException):
        await product.adapter.require_current(principal.grant)


async def test_native_projection_preserves_id_disabled_epoch_and_preferences(product, runtime):
    adapter = ProductIdentityAdapter(product.settings, product.source, native_engine=runtime.database.engine)
    principal = await adapter.current("google:member-sub")
    async with runtime.database.sessions.begin() as db:
        projected = await adapter.project_native_user(db, principal)
        assert projected.id == principal.id
        assert not check_password("synthetic-shared-password-only", projected.password)
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(User, principal.id)
        saved.enabled = False
        saved.auth_version += 1
        saved.preferences = '{"keep":"personal settings"}'
    with pytest.raises(HTTPException):
        await adapter.require_current(principal.grant)
    async with runtime.database.sessions.begin() as db:
        with pytest.raises(HTTPException):
            await adapter.project_native_user(db, principal)
    async with runtime.database.sessions.begin() as db:
        saved = await db.get(User, principal.id)
        assert not saved.enabled and saved.auth_version == 2
        assert saved.preferences == '{"keep":"personal settings"}'
        saved.enabled = True
    with pytest.raises(HTTPException):
        await adapter.require_current(principal.grant)


async def test_long_legacy_ids_and_concurrent_projection_are_preserved(product, runtime):
    subject = "subject-" + "x" * 80
    identity = "google:" + subject
    async with product.writer.begin() as db:
        await db.execute(text(
            "INSERT INTO users VALUES (:id,'google','long@example.test','Long ID',NULL,'c','u')"
        ), {"id": identity})
    adapter = ProductIdentityAdapter(product.settings, product.source, native_engine=runtime.database.engine)
    cookie, _ = google_cookie(adapter, sub=subject, email="long@example.test")
    principal = await adapter.authenticate(request(cookie))
    assert principal.id == identity and len(principal.id) > 36

    async def project():
        async with runtime.database.sessions.begin() as db:
            return await adapter.project_native_user(db, principal)

    first, second = await asyncio.gather(project(), project())
    assert first.id == second.id == identity
    async with runtime.database.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(User).where(User.id == identity)) == 1


async def test_grant_cannot_be_transferred_to_another_account_or_omitted(product):
    principal = await product.adapter.current("google:admin-sub")
    for grant in (None, ProductGrant("google:member-sub", principal.authority_version)):
        with pytest.raises(HTTPException) as denied:
            await product.adapter.require_current(grant)
        assert denied.value.status_code == 401


async def test_slack_links_are_not_a_private_or_admin_login(product):
    async with product.writer.begin() as db:
        await db.execute(text(
            "INSERT INTO users VALUES ('slack:T:U','slack','admin@example.test','Slack','google:admin-sub','c','u')"
        ))
    with pytest.raises(HTTPException):
        await product.adapter.current("slack:T:U")


async def test_shared_login_is_explicit_and_does_not_gain_individual_privacy(product):
    async with product.writer.begin() as db:
        await db.execute(text(
            "INSERT INTO users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    cookie = product.adapter.legacy_security.signer.dumps({"sid": "old-password-session"})
    with pytest.raises(HTTPException):
        await product.adapter.authenticate(request(cookie))
    adapter = ProductIdentityAdapter(product.settings, product.source, allow_shared_identities=True)
    principal = await adapter.authenticate(request(cookie))
    assert principal.id == "shared:password:admin" and not principal.individual


async def cloudflare_setup(product):
    settings = product.settings.model_copy(update={
        "public_url": "https://workspace.example.test", "cloudflare_access_login": True,
        "cloudflare_access_team_domain": "synthetic.cloudflareaccess.com",
        "cloudflare_access_audience": "employee-audience", "cloudflare_access_broker_audience": "broker-audience",
    })
    adapter = ProductIdentityAdapter(settings, product.source)
    identity = {
        "issuer": adapter.access.issuer, "sub": "pinned-subject",
        "email": "admin@example.test", "domain": "example.test",
    }
    async with product.writer.begin() as db:
        await db.execute(text(
            "INSERT INTO access_identities VALUES (:issuer,:subject,:email,'google:admin-sub','created')"
        ), {"issuer": identity["issuer"], "subject": identity["sub"], "email": identity["email"]})
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    adapter.access.keys = {"local-key": key.public_key()}
    adapter.access.expires = time.monotonic() + 3600
    cookie = adapter.legacy_security.signer.dumps({
        "sid": "signed-access-session", "method": "cloudflare",
        "audience": settings.cloudflare_access_audience, "identity": identity,
    })
    claims = {
        "iss": identity["issuer"], "sub": identity["sub"], "email": identity["email"],
        "type": "app", "aud": settings.cloudflare_access_audience, "iat": int(time.time()),
        "exp": int(time.time()) + 120,
    }
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "local-key"})
    return adapter, cookie, key, claims, token


async def test_cloudflare_requires_real_signed_assertion_and_existing_pinned_account(product):
    adapter, cookie, key, claims, token = await cloudflare_setup(product)
    principal = await adapter.authenticate(request(cookie, headers={"cf-access-jwt-assertion": token}))
    assert principal.id == "google:admin-sub" and principal.method == "cloudflare"
    assert principal.individual and principal.role == "admin"
    with pytest.raises(HTTPException):
        await adapter.authenticate(request(cookie, headers={"cf-access-authenticated-user-email": principal.email}))
    for change in ({"aud": "broker-audience"}, {"sub": "unbound-subject"}, {"common_name": "machine"}):
        other = jwt.encode({**claims, **change}, key, algorithm="RS256", headers={"kid": "local-key"})
        with pytest.raises(HTTPException):
            await adapter.authenticate(request(cookie, headers={"cf-access-jwt-assertion": other}))
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, wrong_key, algorithm="RS256", headers={"kid": "local-key"})
    with pytest.raises(HTTPException):
        await adapter.authenticate(request(cookie, headers={"cf-access-jwt-assertion": forged}))
    async with product.writer.begin() as db:
        await db.execute(text("DELETE FROM access_identities"))
    with pytest.raises(HTTPException):
        await adapter.require_current(principal.grant)
    with pytest.raises(HTTPException):
        await adapter.authenticate(request(cookie, headers={"cf-access-jwt-assertion": token}))


async def test_each_activity_attempt_checks_source_even_when_mirror_is_unchanged(product):
    principal = await product.adapter.current("google:admin-sub")
    mirror = {"id": principal.id, "role": "admin", "enabled": True}

    @activity.defn
    async def guarded_business_activity(grant: ProductGrant) -> str:
        await product.adapter.require_current(grant)
        return "authorized"

    environment = ActivityEnvironment()
    assert await environment.run(guarded_business_activity, principal.grant) == "authorized"
    UserRoles(product.store, product.settings).change(
        RoleChange(email=principal.email, role="member", revision=0),
        {"method": "google", "identity": {"email": "other@example.test"}},
    )
    assert mirror["role"] == "admin"
    with pytest.raises(HTTPException):
        await environment.run(guarded_business_activity, principal.grant)


async def test_standalone_native_admission_remains_usable_and_product_sqlite_admission_is_explicit(product, runtime, actor):
    session = await runtime.database.create_session(actor, NewSession())
    turn = await runtime.database.submit(actor, session.id, NewMessage(prompt="Standalone", request_key="local"))
    assert (await runtime.database.claim_next()).id == turn.id
    principal = await product.adapter.current("google:admin-sub")
    async with runtime.database.sessions.begin() as db:
        with pytest.raises(HTTPException) as unavailable:
            await product.adapter.authorize_admission(db, principal.grant)
        assert unavailable.value.status_code == 503


@pytest.mark.skipif(not os.getenv("MOYAI_TEST_POSTGRES"), reason="Set a disposable local PostgreSQL URL.")
async def test_postgres_admission_serializes_with_canonical_role_writes_and_rejects_stale_grants(tmp_path):
    base = urlsplit(os.environ["MOYAI_TEST_POSTGRES"])
    assert base.hostname in {"localhost", "127.0.0.1", "::1"}
    name = f"moyai_identity_{uuid4().hex}"
    admin = await asyncpg.connect(urlunsplit(base))
    engine = None
    created = False
    try:
        await admin.execute(f"CREATE DATABASE {name}")
        created = True
        url = urlunsplit(base._replace(path=f"/{name}"))
        engine = create_async_engine("postgresql+asyncpg://" + url.split("://", 1)[1])
        async with engine.begin() as db:
            await db.execute(text("CREATE SCHEMA moyai"))
            for statement in SCHEMA[:2]:
                await db.execute(text(statement.replace("CREATE TABLE ", "CREATE TABLE moyai.", 1)))
            await db.execute(text(
                "INSERT INTO moyai.users VALUES ('google:admin-sub','google','admin@example.test','Admin',NULL,'c','u')"
            ))
            await db.execute(text("CREATE TABLE admissions (id TEXT PRIMARY KEY)"))
        adapter = ProductIdentityAdapter(settings_for(tmp_path), engine)
        principal = await adapter.current("google:admin-sub")
        started = asyncio.Event()

        async def demote():
            connection = await asyncpg.connect(url)
            try:
                async with connection.transaction():
                    started.set()
                    # Same canonical writer protocol as app.database.begin_write.
                    await connection.execute("SELECT pg_advisory_xact_lock(726941, 'moyai'::regnamespace::oid::int)")
                    await connection.execute(
                        "INSERT INTO moyai.user_roles VALUES ('admin@example.test','member',1,'other@example.test','now')"
                    )
            finally:
                await connection.close()

        changing = None
        try:
            async with AsyncSession(engine) as db, db.begin():
                checked = await adapter.authorize_admission(db, principal.grant)
                assert checked.id == principal.id
                changing = asyncio.create_task(demote())
                await asyncio.wait_for(started.wait(), 5)
                done, _ = await asyncio.wait({changing}, timeout=0.1)
                assert not done
                await db.execute(text("INSERT INTO admissions VALUES ('accepted-before-demotion')"))
            await asyncio.wait_for(changing, 5)
        finally:
            if changing:
                await asyncio.gather(changing, return_exceptions=True)
        with pytest.raises(HTTPException):
            await adapter.require_current(principal.grant)
        async with AsyncSession(engine) as db, db.begin():
            with pytest.raises(HTTPException) as denied:
                await adapter.authorize_admission(db, principal.grant)
            assert denied.value.status_code == 401
        async with engine.connect() as db:
            assert (await db.execute(text("SELECT count(*) FROM admissions"))).scalar_one() == 1
        async with adapter._reader(engine) as db:
            with pytest.raises(SQLAlchemyError):
                await db.execute(text("DELETE FROM moyai.users"))
    finally:
        if engine:
            await engine.dispose()
        if created:
            await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        await admin.close()
