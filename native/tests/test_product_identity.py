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
    ("CREATE TABLE oidc_identities (provider TEXT NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL, "
    "email TEXT NOT NULL, user_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(provider,issuer,subject), "
    "UNIQUE(provider,issuer,user_id), UNIQUE(provider,issuer,email))"),
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
    assert principal.method == "oidc" and principal.csrf_token == csrf
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


async def test_private_network_ui_keeps_session_csrf_authority_and_broker_checks(product):
    settings = settings_for(
        product.settings.data_dir.parent,
        public_url="https://private.example.test", broker_url="https://broker.example.test",
        cloudflare_access_team_domain="synthetic.cloudflareaccess.com",
        cloudflare_access_audience="employee-audience", cloudflare_access_broker_audience="broker-audience",
        cloudflare_access_ui_mode="private-network",
    )
    adapter = ProductIdentityAdapter(settings, product.source, allow_shared_identities=True)
    async with product.writer.begin() as db:
        await db.execute(text(
            "INSERT INTO users VALUES ('shared:password:admin','shared','','Shared',NULL,'c','u')"
        ))
    cookie = adapter.legacy_security.signer.dumps({"sid": "private-network-session"})
    with pytest.raises(HTTPException) as error:
        await adapter.authenticate(request())
    assert error.value.status_code == 401
    principal = await adapter.authenticate(request(cookie))
    assert principal.id == "shared:password:admin" and not principal.individual
    with pytest.raises(HTTPException) as error:
        await adapter.authenticate(request(cookie, method="POST", headers={"origin": settings.public_url}), mutation=True)
    assert error.value.status_code == 403
    headers = {"origin": settings.public_url, "x-csrf-token": adapter.legacy_security.csrf("private-network-session")}
    assert (await adapter.authenticate(request(cookie, method="POST", headers=headers), mutation=True)).id == principal.id
    worker = ProductIdentityAdapter(settings, product.source, allow_shared_identities=True)
    assert (await worker.require_current(principal.grant)).authority_version == principal.authority_version
    strict = ProductIdentityAdapter(
        settings.model_copy(update={"cloudflare_access_ui_mode": "jwt"}), product.source,
        allow_shared_identities=True,
    )
    with pytest.raises(HTTPException) as error:
        await strict.authenticate(request(cookie))
    assert error.value.status_code == 401

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    adapter.access.keys = {"local-key": key.public_key()}
    adapter.access.expires = time.monotonic() + 3600
    claims = {
        "iss": adapter.access.issuer, "type": "app", "aud": "broker-audience",
        "iat": int(time.time()), "exp": int(time.time()) + 120,
    }
    valid = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "local-key"})
    await adapter.access.check("/broker/inference", "POST", valid)
    for token in (
        "",
        jwt.encode({**claims, "aud": "employee-audience"}, key, algorithm="RS256", headers={"kid": "local-key"}),
        jwt.encode({**claims, "aud": ["employee-audience", "broker-audience"]}, key,
                   algorithm="RS256", headers={"kid": "local-key"}),
        jwt.encode({**claims, "exp": int(time.time()) - 1}, key, algorithm="RS256", headers={"kid": "local-key"}),
    ):
        with pytest.raises(jwt.PyJWTError):
            await adapter.access.check("/broker/inference", "POST", token)


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
            for statement in (SCHEMA[:2] + (SCHEMA[-1],)):
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


# OIDC-provider doubles are protocol regressions, not live Okta E2E proof.
@pytest.fixture
def okta_provider(tmp_path, monkeypatch):
    import json

    import httpx
    from app.okta_sso import OktaSignIn
    from app.security import Security as ProductSecurity

    settings = settings_for(tmp_path, okta_issuer="https://company.okta.com",
        okta_client_id="okta-client", okta_client_secret="okta-secret",
        okta_allowed_domains="example.test", okta_admin_emails="admin@example.test")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())) | {"kid": "current", "alg": "RS256", "use": "sig"}
    metadata = {"issuer": settings.okta_issuer,
        "authorization_endpoint": settings.okta_issuer + "/oauth2/v1/authorize",
        "token_endpoint": settings.okta_issuer + "/oauth2/v1/token",
        "userinfo_endpoint": settings.okta_issuer + "/oauth2/v1/userinfo",
        "jwks_uri": settings.okta_issuer + "/oauth2/v1/keys",
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic"]}
    claims = {"iss": settings.okta_issuer, "aud": "okta-client", "sub": "employee-1",
              "iat": int(time.time()), "exp": int(time.time()) + 300, "nonce": "nonce"}
    state = SimpleNamespace(settings=settings, key=key, jwk=jwk, metadata=metadata, claims=claims,
        profile={"sub": "employee-1", "email": "Admin@Example.Test", "email_verified": True, "name": "Employee"},
        requests=[])
    def respond(req):
        state.requests.append(req)
        if req.url.path.endswith("openid-configuration"):
            payload = state.metadata
        elif req.url.path.endswith(("/keys", "/certs")):
            payload = {"keys": [state.jwk]}
        elif req.url.path.endswith("/token"):
            payload = {"id_token": jwt.encode(state.claims, state.key, algorithm="RS256", headers={"kid": "current"}),
                       "access_token": "temporary-provider-access-token"}
        elif req.url.path.endswith("/userinfo"):
            payload = state.profile
        else:
            raise AssertionError("Unexpected provider endpoint")
        return httpx.Response(200, json=payload)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    state.flow = OktaSignIn(settings, ProductSecurity(settings), None)
    state.http_client = client
    return state


async def test_okta_uses_verified_id_token_and_matching_verified_profile(okta_provider):
    from urllib.parse import parse_qs
    state = okta_provider
    identity = await state.flow.exchange("code", "verifier", "nonce")
    assert identity == {"issuer": state.settings.okta_issuer, "sub": "employee-1",
                        "email": "admin@example.test", "name": "Employee"}
    token_request = next(req for req in state.requests if req.url.path.endswith("/token"))
    assert token_request.headers["authorization"].startswith("Basic ")
    body = parse_qs(token_request.content.decode())
    assert body["code_verifier"] == ["verifier"]
    assert body["redirect_uri"] == [state.flow.callback]
    assert "temporary-provider-access-token" not in repr(identity)


@pytest.mark.parametrize("change", [
    {"iss": "https://another.okta.com"}, {"aud": "another-client"}, {"nonce": "wrong"},
    {"exp": 1}, {"iat": 9999999999}, {"azp": "another-client"},
    {"aud": ["okta-client", "another-client"]}, {"sub": ""},
])
async def test_okta_rejects_invalid_id_token_claims(okta_provider, change):
    state = okta_provider
    state.claims.update(change)
    with pytest.raises((jwt.PyJWTError, ValueError)):
        await state.flow.exchange("code", "verifier", "nonce")
    assert not any(req.url.path.endswith("/userinfo") for req in state.requests)


@pytest.mark.parametrize("change", [
    {"sub": "another-person"}, {"email_verified": False}, {"email_verified": "true"},
    {"email": "admin@outside.test"}, {"email": "admin@example.test "},
])
async def test_okta_rejects_unverified_mismatched_or_outside_profile(okta_provider, change):
    okta_provider.profile.update(change)
    with pytest.raises(ValueError):
        await okta_provider.flow.exchange("code", "verifier", "nonce")


@pytest.mark.parametrize("change", [
    {"issuer": "https://another.okta.com"}, {"token_endpoint": "https://steal.example/token"},
    {"jwks_uri": "http://company.okta.com/keys"},
    {"userinfo_endpoint": "https://user:password@company.okta.com/userinfo"},
])
async def test_okta_discovery_cannot_redirect_credentials(okta_provider, change):
    okta_provider.metadata.update(change)
    with pytest.raises(ValueError):
        await okta_provider.flow.discovery()
    assert len(okta_provider.requests) == 1


async def test_okta_browser_binding_single_use_and_local_return_path(okta_provider, tmp_path):
    from urllib.parse import parse_qs, urlencode

    import httpx
    from app.db import Store
    from app.security import digest
    from fastapi import FastAPI

    state = okta_provider
    store = Store(tmp_path / "store")
    state.flow.store = store
    app = FastAPI()
    app.include_router(state.flow.routes())
    try:
        async with state.http_client(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as browser:
            assert (await browser.post("/api/auth/okta/start", json={})).status_code == 403
            started = await browser.post("/api/auth/okta/start", json={"return_to": "https://steal.example"},
                                         headers={"Origin": "http://127.0.0.1:8787"})
            assert started.status_code == 200
            params = parse_qs(urlsplit(started.json()["url"]).query)
            assert params["scope"] == ["openid email profile"] and params["code_challenge_method"] == ["S256"]
            state.claims["nonce"] = params["nonce"][0]
            callback = "/auth/okta/callback?" + urlencode(
                {"state": params["state"][0], "code": "code"})
            async with state.http_client(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8787") as other:
                rejected = await other.get(callback)
                assert "signin=failed" in rejected.headers["location"]
                assert "workspace_session" not in other.cookies
            accepted = await browser.get(callback)
            assert accepted.status_code == 303 and accepted.headers["location"] == "/#tasks"
            info = state.flow.security.signer.loads(browser.cookies["workspace_session"])
            assert info["method"] == "okta" and info["identity"]["email"] == "admin@example.test"
            assert store.rows("SELECT kind FROM users") == [{"kind": "okta"}]
            replay = await browser.get(callback)
            assert "signin=failed" in replay.headers["location"]
            assert sum(req.url.path.endswith("/token") for req in state.requests) == 1
            assert state.flow.state_hash(params["state"][0]) != digest(params["state"][0])
    finally:
        store.close()


async def test_okta_canonical_identity_preserves_email_but_not_google_ownership(product):
    from app.okta_sso import okta_user_id
    product.settings.okta_issuer = "https://company.okta.com"
    product.settings.okta_client_id = "okta-client"
    product.settings.okta_client_secret = "okta-secret"
    product.settings.okta_allowed_domains = "example.test"
    product.settings.okta_admin_emails = "admin@example.test"
    identity = {"issuer": product.settings.okta_issuer, "sub": "employee-1", "email": "admin@example.test"}
    user_id = okta_user_id(identity["issuer"], identity["sub"])
    assert user_id != "google:admin-sub"
    assert user_id != okta_user_id("https://another.okta.com", identity["sub"])
    async with product.writer.begin() as conn:
        await conn.execute(text("INSERT INTO users VALUES (:id,'okta','admin@example.test','Employee',NULL,'created','updated')"), {"id": user_id})
    response = Response()
    sid = product.adapter.legacy_security.new_session(response, identity=identity, method="okta")
    cookie = SimpleCookie(response.headers["set-cookie"])["workspace_session"].value
    principal = await product.adapter.authenticate(request(cookie), admin=True)
    assert principal.id == user_id and principal.email == "admin@example.test" and principal.individual
    assert principal.csrf_token == product.adapter.legacy_security.csrf(sid)
    UserRoles(product.store, product.settings).change(RoleChange(email=principal.email, role="member", revision=0),
        {"method": "google", "identity": {"email": "other@example.test"}})
    assert (await product.adapter.authenticate(request(cookie))).role == "member"
    with pytest.raises(HTTPException):
        await product.adapter.require_current(principal.grant)
    product.settings.okta_client_id = "changed-client"
    with pytest.raises(HTTPException):
        await product.adapter.authenticate(request(cookie))
    product.settings.okta_client_id = "okta-client"
    product.settings.okta_issuer = "https://another.okta.com"
    with pytest.raises(HTTPException):
        await product.adapter.authenticate(request(cookie))
    with pytest.raises(HTTPException):
        await product.adapter.current(user_id)


async def test_explicit_oidc_link_preserves_account_and_requires_same_session(okta_provider, tmp_path):
    """Real shared route handlers with a simulated IdP; not provider E2E proof."""
    from urllib.parse import parse_qs, urlencode

    import httpx
    from app.db import Store
    from app.oidc_identity import IdentityLinkRequired
    from fastapi import FastAPI

    state = okta_provider
    store = Store(tmp_path / "link-store")
    state.flow.store = store
    state.flow.security.user_roles = UserRoles(store, state.settings)
    google = {"sub": "original-subject", "email": "admin@example.test", "domain": "example.test", "name": "Original name"}
    owner = store.oidc_identity("google", google)
    snapshot = store.rows("SELECT * FROM users WHERE id=?", (owner,))[0]
    # Same email alone cannot enroll or merge the other provider.
    okta = {"issuer": state.settings.okta_issuer, "sub": "employee-1", "email": "admin@example.test"}
    with pytest.raises(IdentityLinkRequired):
        store.oidc_identity("okta", okta)
    response = Response()
    sid = state.flow.security.new_session(response, identity=google)
    cookie = SimpleCookie(response.headers['set-cookie'])['workspace_session'].value
    app = FastAPI()
    app.include_router(state.flow.routes())
    source = create_async_engine(f"sqlite+aiosqlite:///file:{store.path}?mode=ro&uri=true")
    adapter = ProductIdentityAdapter(state.settings, source)
    try:
        async with state.http_client(transport=httpx.ASGITransport(app=app), base_url=state.settings.public_url) as browser:
            browser.cookies.set('workspace_session', cookie)
            browser.headers['Origin'] = state.settings.public_url
            assert (await browser.post('/api/auth/okta/start', json={'link': True})).status_code == 403
            browser.headers['x-csrf-token'] = state.flow.security.csrf(sid)
            started = await browser.post('/api/auth/okta/start', json={'link': True})
            assert started.status_code == 200
            params = parse_qs(urlsplit(started.json()['url']).query)
            state.claims['nonce'] = params['nonce'][0]
            assert params['prompt'] == ['login']
            callback = '/auth/okta/callback?' + urlencode({'state': params['state'][0], 'code': 'code'})
            # The login cookie alone cannot finish a link in another Moyai session.
            saved_cookie = browser.cookies['workspace_session']
            browser.cookies.delete('workspace_session')
            denied = await browser.get(callback)
            assert 'signin=failed' in denied.headers['location']
            assert not store.rows("SELECT 1 FROM oidc_identities WHERE provider='okta'")
            browser.cookies.set('workspace_session', saved_cookie)
            started = await browser.post('/api/auth/okta/start', json={'link': True})
            params = parse_qs(urlsplit(started.json()['url']).query)
            state.claims['nonce'] = params['nonce'][0]
            callback = '/auth/okta/callback?' + urlencode({'state': params['state'][0], 'code': 'code'})
            accepted = await browser.get(callback)
            assert accepted.status_code == 303 and 'signin=linked#settings/sign-in' in accepted.headers['location']
            # The response's signed session authenticates to the original account.
            linked_cookie = next(value.value for name, value in SimpleCookie(accepted.headers.get_list('set-cookie')[-1]).items() if name == 'workspace_session')
            left = await adapter.authenticate(request(cookie))
            right = await adapter.authenticate(request(linked_cookie))
            assert left.id == right.id == owner
            assert left.role == right.role == 'admin'
            assert left.individual and right.individual
            assert left.authority_version == right.authority_version
            assert store.oidc_identity('okta', okta) == owner
            saved = store.rows('SELECT * FROM users WHERE id=?', (owner,))[0]
            assert {k:v for k,v in saved.items() if k != 'updated_at'} == {k:v for k,v in snapshot.items() if k != 'updated_at'}
            assert len(store.rows('SELECT * FROM users')) == 1
            assert len(store.rows('SELECT * FROM identity_audit')) == 1
            assert 'signin=failed' in (await browser.get(callback)).headers['location']
            store.execute("DELETE FROM oidc_identities WHERE provider='okta'")
            with pytest.raises(HTTPException) as missing_binding:
                await adapter.authenticate(request(linked_cookie))
            assert missing_binding.value.status_code == 401
            browser.headers['x-csrf-token'] = state.flow.security.csrf(
                state.flow.security.session_info(request(linked_cookie))['sid'])
            assert (await browser.post('/api/auth/okta/start', json={'link': True})).status_code == 401
    finally:
        await source.dispose()
        store.close()


@pytest.mark.parametrize('first', ['google', 'okta'])
def test_linking_is_symmetric_and_rejects_conflicting_or_changed_identities(tmp_path, first):
    from app.db import Store
    from app.oidc_identity import IdentityLinkRequired

    profiles = {
        'google': {'sub': 'google-person', 'email': 'owner@example.test', 'domain': 'example.test'},
        'okta': {'sub': 'okta-person', 'email': 'owner@example.test', 'issuer': 'https://company.okta.com'},
    }
    second = 'okta' if first == 'google' else 'google'
    store = Store(tmp_path / first)
    try:
        user_id = store.oidc_identity(first, profiles[first])
        original = {'method': first, 'identity': profiles[first]}
        with pytest.raises(IdentityLinkRequired):
            store.oidc_identity(second, profiles[second])
        with pytest.raises(IdentityLinkRequired):
            store.oidc_identity(second, {**profiles[second], 'email': 'someone@example.test'}, link_info=original)
        assert store.oidc_identity(second, profiles[second], link_info=original) == user_id
        assert store.oidc_actor({'method': second, 'identity': profiles[second]}) == user_id
        with pytest.raises(IdentityLinkRequired):
            store.oidc_identity(second, {**profiles[second], 'sub': 'replacement-person'}, link_info=original)
        with pytest.raises(ValueError):
            store.oidc_identity(second, {**profiles[second], 'email': 'changed@example.test'})
        store.execute('DELETE FROM users WHERE id=?', (user_id,))
        with pytest.raises(ValueError):
            store.oidc_identity(second, profiles[second])
    finally:
        store.close()


def test_legacy_duplicate_accounts_are_never_merged_or_reparented(tmp_path):
    from app.db import Store
    from app.oidc_identity import IdentityLinkRequired, legacy_user_id

    store = Store(tmp_path / 'duplicates')
    google = {'sub': 'google-person', 'email': 'owner@example.test', 'domain': 'example.test'}
    okta = {'sub': 'okta-person', 'email': 'owner@example.test', 'issuer': 'https://company.okta.com'}
    try:
        original = store.oidc_identity('google', google)
        duplicate = legacy_user_id('okta', okta)
        store.execute('INSERT INTO users(id,kind,email,name,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                      (duplicate, 'okta', okta['email'], 'Existing owner', 'created', 'updated'))
        before = store.rows('SELECT * FROM users ORDER BY id')
        with pytest.raises(IdentityLinkRequired):
            store.oidc_identity('okta', okta, link_info={'method': 'google', 'identity': google})
        assert store.rows('SELECT * FROM users ORDER BY id') == before
        assert store.oidc_identity('okta', okta) == duplicate
        assert store.oidc_identity('google', google) == original
    finally:
        store.close()


@pytest.mark.parametrize('kind', ['shared', 'stale'])
async def test_link_requires_recent_individual_sign_in(okta_provider, tmp_path, monkeypatch, kind):
    import httpx
    from app.db import Store
    from fastapi import FastAPI
    from itsdangerous import TimestampSigner

    state = okta_provider
    store = Store(tmp_path / kind)
    state.flow.store = store
    identity = {'sub': 'original', 'email': 'admin@example.test', 'domain': 'example.test'}
    store.oidc_identity('google', identity)
    response = Response()
    with monkeypatch.context() as clock:
        if kind == 'stale':
            clock.setattr(TimestampSigner, 'get_timestamp', lambda self: int(time.time()) - 601)
        sid = state.flow.security.new_session(response, identity=identity if kind == 'stale' else None)
    app = FastAPI()
    app.include_router(state.flow.routes())
    try:
        async with state.http_client(transport=httpx.ASGITransport(app=app), base_url=state.settings.public_url) as browser:
            browser.cookies.set('workspace_session', SimpleCookie(response.headers['set-cookie'])['workspace_session'].value)
            response = await browser.post('/api/auth/okta/start', json={'link': True}, headers={
                'Origin': state.settings.public_url, 'x-csrf-token': state.flow.security.csrf(sid),
            })
            assert response.status_code == 403
            assert not state.requests
            assert not store.rows('SELECT * FROM login_states')
    finally:
        store.close()


def test_simultaneous_links_cannot_replace_the_first_verified_subject(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from app.db import Store
    from app.oidc_identity import IdentityLinkRequired

    store = Store(tmp_path / 'concurrent')
    google = {'sub': 'original', 'email': 'owner@example.test', 'domain': 'example.test'}
    owner = store.oidc_identity('google', google)
    def link(subject):
        try:
            return store.oidc_identity('okta', {'issuer': 'https://company.okta.com', 'sub': subject, 'email': google['email']},
                                       link_info={'method': 'google', 'identity': google})
        except IdentityLinkRequired:
            return 'denied'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(link, ['first', 'second']))
        assert sorted(outcomes) == sorted([owner, 'denied'])
        assert len(store.rows("SELECT * FROM oidc_identities WHERE provider='okta'")) == 1
    finally:
        store.close()


@pytest.mark.parametrize('change', [{}, {'hd': 'wrong.test'}, {'email_verified': False}, {'email': 'admin@outside.test'}])
async def test_shared_flow_retains_google_workspace_validation(okta_provider, change):
    from urllib.parse import parse_qs

    from app.google_sso import GoogleSignIn

    state = okta_provider
    google = GoogleSignIn(state.settings, state.flow.security, None)
    state.claims.update(iss='https://accounts.google.com', aud=state.settings.google_client_id,
                        email='admin@example.test', email_verified=True, hd='example.test')
    state.claims.update(change)
    assert google.state_hash('same-state') != state.flow.state_hash('same-state')
    if change:
        with pytest.raises(ValueError):
            await google.exchange('code', 'verifier', 'nonce')
    else:
        identity = await google.exchange('code', 'verifier', 'nonce')
        assert identity['sub'] == 'employee-1' and identity['email'] == 'admin@example.test'
        assert identity['domain'] == 'example.test'
        token = next(req for req in state.requests if req.url.path.endswith('/token'))
        assert parse_qs(token.content.decode())['client_id'] == [state.settings.google_client_id]
        assert not any(req.url.path.endswith('/userinfo') for req in state.requests)
