"""Explicit, read-only product identity authority for the Pydantic core.

Integration contract (the caller owns activation and reviewed schema migrations):

* Standalone mode keeps its current Security/Database path and no adapter.
* Existing-product mode supplies app.config.Settings with the ORIGINAL explicit
  SESSION_SECRET and raw Fernet ENCRYPTION_KEY, plus an existing AsyncEngine.
  No Store, login enrollment, schema initialization or agent runtime is imported.
* API actor dependency:
    principal = await identity.authenticate(request, mutation=is_mutation)
    Read requests first use read_native_user in a read session. Missing native
    users and mutations use project_native_user in a write transaction.
    return user
  Return principal.csrf_token from /me. The browser can send the same verified
  token as both x-csrf-token and X-Moyai-CSRF. Exact legacy Origin remains required.
  Recheck identity.require_request_current(request) before returning sensitive
  data after awaits; long-lived streams must recheck the saved grant repeatedly.
* At the START of Database._submit's existing transaction:
    grant = getattr(actor, "product_grant", None)
    await identity.authorize_admission(db, grant)
  Retain the native enabled/auth_version and session/turn fences. Persist grant
  with the turn and PreparedTurn/RunScope; never reconstruct it from a mirror.
* Database.authorize_scope, called by the existing native activity interceptor,
  compares scope.product_grant with the immutable turn binding, then calls
  identity.authorize_admission in the same native SQL session on EVERY business
  activity attempt, tool/model call and resumed approval. Missing grants fail
  closed in product mode. Existing evidence-only settlement may still record a
  confirmed result after revocation; it grants no authority to perform new work.
  This is a live activity-side check, not a workflow-replayed authorization result.
* Product IDs are not UUIDs: widen native User.id and all user-ID/FK columns to
  at least 512 characters before activation. Never truncate, hash or reassign IDs.
* Product role changes remain with UserRoles; do not expose native role/account
  onboarding as a second canonical user directory. Native disable/auth_version
  overrides are enforced as an additional fence and are never reset by projection.
  If shared-password identities are explicitly allowed, they remain nonindividual:
  private sessions still require an individual Google/Access principal. Historical
  sessions retain their original SessionPrivacy rules, including no admin bypass.

``legacy_security.decrypt`` reads explicitly legacy-tagged ciphertext only.
Native Security derives a different key and uses a different session salt.
Do not try both decoders or exchange cookies between those formats.

The legacy users schema currently has no enabled/auth_version columns. Existing
columns, if present, are honored; live user_roles revisions, account deletion,
login policy and Access bindings are checked regardless. Native local controls
are read on every check when present. Upstream IdP revocation still follows the
existing validated assertion/cookie policy; this is not an IdP introspection API.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .models import User

if TYPE_CHECKING:
    from app.config import Settings as ProductSettings


@dataclass(frozen=True)
class ProductGrant:
    user_id: str
    authority_version: str


class _GrantedUser(Protocol):
    product_grant: ProductGrant


@dataclass(frozen=True)
class ProductPrincipal:
    id: str
    kind: str
    method: str
    email: str
    name: str
    role: str
    enabled: bool
    authority_version: str
    linked_user_id: str | None = None
    csrf_token: str = field(default="", repr=False, compare=False)
    account_created_at: str = field(default="", repr=False)
    native_auth_version: int = field(default=1, repr=False)
    role_revision: int = field(default=0, repr=False)

    @property
    def individual(self) -> bool:
        return self.kind in {"google", "cloudflare"} and self.method in {"google", "cloudflare"}

    @property
    def grant(self) -> ProductGrant:
        return ProductGrant(self.id, self.authority_version)


class SlackAuthority(Protocol):
    async def current(
        self, db: AsyncSession, row: Mapping[str, Any], controls: tuple[bool, int],
    ) -> ProductPrincipal: ...


class _RoleConnection:
    """Adapt the existing pure UserRoles.role_in query to this async SQL session."""

    def __init__(self, session: Any, table: str):
        self.session, self.table = session, table

    def execute(self, statement: str, parameters: tuple[str, ...]):
        if statement != "SELECT role FROM user_roles WHERE email=?" or len(parameters) != 1:
            raise RuntimeError("The product role query changed; review its identity adapter.")
        return self.session.execute(
            text(f"SELECT role FROM {self.table} WHERE email=:email"), {"email": parameters[0]},
        ).mappings()


class ProductIdentityAdapter:
    session_cookie = "workspace_session"
    csrf_header = "x-csrf-token"

    def __init__(
        self,
        legacy_settings: ProductSettings,
        source_engine: AsyncEngine,
        *,
        schema: str | None = None,
        native_engine: AsyncEngine | None = None,
        allow_shared_identities: bool = False,
    ):
        # Lazy imports keep the standalone native package independent of app/.
        from app.cloudflare_access import CloudflareAccess
        from app.security import Security
        from app.user_roles import UserRoles

        if not legacy_settings.session_secret or not legacy_settings.encryption_key:
            raise ValueError("Product identity requires explicit legacy SESSION_SECRET and Fernet ENCRYPTION_KEY.")
        backend = source_engine.dialect.name
        if backend not in {"postgresql", "sqlite"}:
            raise ValueError("Unsupported product identity database.")
        uri = source_engine.url.query.get("uri")
        if backend == "sqlite" and (
            source_engine.url.query.get("mode") != "ro"
            or not isinstance(uri, str) or uri.lower() != "true"
        ):
            raise ValueError("SQLite product identity sources must use mode=ro; no empty-database fallback.")
        self.schema = schema or (legacy_settings.moyai_database_schema if backend == "postgresql" else None)
        if self.schema is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("Invalid product identity schema.")
        self.settings = legacy_settings
        self.source_engine = source_engine
        self.native_engine = native_engine or source_engine
        self.allow_shared_identities = allow_shared_identities
        self.slack_authority: SlackAuthority | None = None
        # No role service is attached here: Security handles signed-session and
        # CSRF validation without blocking SQL. Current roles are resolved below
        # through the unchanged UserRoles.role_in implementation.
        self.legacy_security = Security(legacy_settings)
        self.roles = UserRoles(SimpleNamespace(schema_updates=False), legacy_settings)
        self.access = CloudflareAccess(legacy_settings)

    def _table(self, name: str) -> str:
        return f'"{self.schema}"."{name}"' if self.schema else f'"{name}"'

    @asynccontextmanager
    async def _reader(self, engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
        async with engine.connect() as connection, connection.begin():
            if engine.dialect.name == "postgresql":
                await connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                await connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
            async with AsyncSession(bind=connection, expire_on_commit=False) as db:
                yield db

    async def _native_controls(self, user_id: str, db: AsyncSession | None = None) -> tuple[bool, int]:
        async def read(session: AsyncSession) -> tuple[bool, int]:
            connection = await session.connection()
            schema = User.__table__.schema
            if connection.dialect.name == "postgresql" and schema is None:
                schema = await connection.scalar(text("SELECT current_schema()"))
            exists = await connection.run_sync(
                lambda conn: inspect(conn).has_table(User.__tablename__, schema=schema)
            )
            if not exists:
                return True, 1
            row = (await session.execute(select(User.enabled, User.auth_version).where(User.id == user_id))).first()
            return (bool(row.enabled), row.auth_version) if row else (True, 1)

        if db is not None:
            return await read(db)
        async with self._reader(self.native_engine) as session:
            return await read(session)

    async def _current(self, db: AsyncSession, user_id: str, controls: tuple[bool, int]) -> ProductPrincipal:
        row = (await db.execute(
            text(f"SELECT * FROM {self._table('users')} WHERE id=:id"), {"id": user_id},
        )).mappings().first()
        if row is None:
            raise HTTPException(401, "The product account is no longer available.")
        kind, email = row["kind"], (row["email"] or "").strip().lower()
        enabled = controls[0]
        for key in ("enabled", "is_active"):
            if key in row:
                enabled = enabled and row[key] in (True, 1)
        for key in ("disabled", "disabled_at", "deleted_at", "revoked_at"):
            if row.get(key):
                enabled = False
        if kind == "slack":
            if self.slack_authority is None:
                raise HTTPException(401, "Signed Slack authority is not configured.")
            return await self.slack_authority.current(db, dict(row), (enabled, controls[1]))
        revision = 0
        binding = None
        if kind in {"google", "cloudflare"}:
            if not self.roles.eligible(email):
                raise HTTPException(401, "The product account is outside the allowed organization.")
            if self.settings.cloudflare_access_login:
                method = "cloudflare"
                binding = (await db.execute(text(
                    f"SELECT issuer,subject,email,user_id FROM {self._table('access_identities')} "
                    "WHERE issuer=:issuer AND user_id=:id"
                ), {"issuer": self.access.issuer, "id": user_id})).mappings().first()
                if binding is None or binding["email"] != email:
                    raise HTTPException(401, "The Access account mapping requires review.")
            elif kind == "google" and self.settings.google_enabled():
                method = "google"
            else:
                raise HTTPException(401, "This product sign-in method is no longer enabled.")
            role = await db.run_sync(
                lambda session: self.roles.role_in(_RoleConnection(session, self._table("user_roles")), email)
            )
            revision = (await db.execute(
                text(f"SELECT revision FROM {self._table('user_roles')} WHERE email=:email"), {"email": email},
            )).scalar_one_or_none() or 0
        elif kind == "shared" and self.allow_shared_identities:
            parts = user_id.split(":")
            if len(parts) != 3 or parts[0] != "shared":
                raise HTTPException(401, "Invalid shared product identity.")
            _, method, role = parts
            if method == "local":
                allowed = self.legacy_security.local_preview()
            elif method == "password":
                allowed = self.settings.password_login_enabled and bool(
                    self.settings.workspace_member_password if role == "member" else self.settings.workspace_password
                )
            else:
                allowed = False
            if not allowed or self.settings.cloudflare_access_login:
                raise HTTPException(401, "This shared sign-in method is no longer enabled.")
        else:
            # Slack accounting links are not individual login identities.
            raise HTTPException(401, "An existing individual product account is required.")
        if role not in {"admin", "member"} or not enabled:
            raise HTTPException(401, "The product account is disabled or invalid.")
        authority = {
            "id": user_id, "kind": kind, "method": method, "email": email, "role": role,
            "account_created_at": row["created_at"],
            "role_revision": revision, "native_auth_version": controls[1],
            "source_versions": {key: row[key] for key in ("auth_version", "session_version") if key in row},
            "binding": dict(binding) if binding else None,
            "policy": {
                "google_client_id": self.settings.google_client_id,
                "domains": sorted(self.settings.google_domains()),
                "access_audience": self.settings.cloudflare_access_audience,
                "password": self.settings.workspace_member_password if role == "member" else self.settings.workspace_password,
            },
        }
        version = hmac.new(
            self.legacy_security.secret.encode(),
            json.dumps(authority, sort_keys=True, separators=(",", ":")).encode(),
            hashlib.sha256,
        ).hexdigest()
        return ProductPrincipal(
            id=user_id, kind=kind, method=method, email=email, name=row["name"] or email or user_id,
            role=role, enabled=True, authority_version=version, linked_user_id=row.get("linked_user_id"),
            account_created_at=row["created_at"], native_auth_version=controls[1],
            role_revision=revision,
        )

    async def current(self, user_id: str) -> ProductPrincipal:
        try:
            controls = await self._native_controls(user_id)
            async with self._reader(self.source_engine) as db:
                return await self._current(db, user_id, controls)
        except SQLAlchemyError:
            raise HTTPException(503, "Current product authority is unavailable.") from None

    @staticmethod
    def _grant(value: ProductGrant | ProductPrincipal | None) -> ProductGrant:
        grant = value.grant if isinstance(value, ProductPrincipal) else value
        if not isinstance(grant, ProductGrant) or not grant.user_id or not re.fullmatch(
            r"[0-9a-f]{64}", grant.authority_version,
        ):
            raise HTTPException(401, "A saved product authority grant is required.")
        return grant

    async def require_current(self, grant: ProductGrant | ProductPrincipal | None) -> ProductPrincipal:
        grant = self._grant(grant)
        principal = await self.current(grant.user_id)
        if not hmac.compare_digest(principal.authority_version, grant.authority_version):
            raise HTTPException(401, "Product authority changed; fresh authorization is required.")
        return principal

    async def personal_memory_owner(self, db: AsyncSession, actor_id: str) -> str | None:
        """Resolve a verified relationship, never issue an owner's authority.

        Caller retains its admitted Slack actor grant and canonical writer lock.
        Accounting/manual links alone are deliberately not an authentication path.
        """
        from app.identities import PROFILE_MAX_AGE_SECONDS

        if not self.settings.person_login_enabled():
            return None
        actor = (await db.execute(text(
            f"SELECT * FROM {self._table('users')} WHERE id=:id AND kind='slack'",
        ), {"id": actor_id})).mappings().first()
        if (
            actor is None or not actor.get("profile_eligible") or actor.get("profile_conflict")
            or not actor.get("email") or actor.get("link_status") in {"review", "email_changed", "ineligible"}
        ):
            return None
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(actor["profile_checked_at"])).total_seconds()
        except (TypeError, ValueError):
            return None
        if not 0 <= age < PROFILE_MAX_AGE_SECONDS or not self.roles.eligible(actor["email"]):
            return None
        owners = (await db.execute(text(
            f"SELECT id FROM {self._table('users')} WHERE kind IN ('google','cloudflare') AND email=:email",
        ), {"email": actor["email"]})).scalars().all()
        if len(owners) != 1 or not (await self._native_controls(owners[0], db))[0]:
            return None
        return owners[0]

    async def individual_for_admission(self, db: AsyncSession, raw_grant: ProductGrant) -> ProductPrincipal:
        """Capture individual authority during an explicit authenticated action.

        Retain the raw actor/scope/effect as provenance. This is never a schedule
        or replay-time resolver: future work must reuse the returned saved grant.
        No source account is created, and accounting links are not sufficient.
        """
        principal = await self.authorize_admission(db, raw_grant)
        if principal.individual:
            return principal
        if principal.kind != "slack" or principal.method != "slack":
            raise HTTPException(403, "An independently verified individual is required.")
        owner = await self.personal_memory_owner(db, principal.id)
        if owner is None:
            raise HTTPException(403, "A fresh, unambiguous verified individual relationship is required.")
        await db.scalar(select(User).where(User.id == owner).with_for_update(read=True))
        individual = await self._current(db, owner, await self._native_controls(owner, db))
        if not individual.individual:
            raise HTTPException(403, "The linked account is not an eligible individual.")
        return individual

    async def authenticate(self, request: Request, *, mutation: bool = False, admin: bool = False) -> ProductPrincipal:
        from dataclasses import replace

        from app.cloudflare_access import AccessUnavailable
        from jwt import PyJWTError

        # Ignore even prepopulated state: the assertion must be checked here.
        verified = Request({**request.scope, "state": {}})
        access_identity = None
        if self.access.enabled:
            try:
                access_identity = await self.access.check(
                    "/api/product/identity", "GET", request.headers.get("cf-access-jwt-assertion", ""),
                )
            except AccessUnavailable:
                raise HTTPException(503, "Access verification is temporarily unavailable.") from None
            except (PyJWTError, ValueError, TypeError):
                raise HTTPException(401, "A valid product Access assertion is required.") from None
        if self.settings.cloudflare_access_login:
            try:
                info = self.legacy_security.signer.loads(
                    request.cookies.get(self.session_cookie, ""), max_age=43200,
                )
            except (BadSignature, SignatureExpired):
                info = None
            if (
                not access_identity or not isinstance(info, dict) or info.get("method") != "cloudflare"
                or info.get("identity") != access_identity
                or info.get("audience") != self.settings.cloudflare_access_audience
                or not isinstance(info.get("sid"), str) or not info["sid"]
            ):
                raise HTTPException(401, "Use an existing verified product sign-in.")
            verified.state.access_session = info
        sid = self.legacy_security.require(verified, mutation=mutation)
        info = self.legacy_security.session_info(verified)
        if not info:
            raise HTTPException(401, "Sign in to the product.")
        method = info.get("method", "password")
        if method == "cloudflare":
            if access_identity is None:
                raise HTTPException(401, "A verified Access identity is required.")
            async with self._reader(self.source_engine) as db:
                binding = (await db.execute(text(
                    f"SELECT user_id FROM {self._table('access_identities')} "
                    "WHERE issuer=:issuer AND subject=:subject AND email=:email"
                ), {
                    "issuer": access_identity["issuer"], "subject": access_identity["sub"],
                    "email": access_identity["email"],
                })).mappings().first()
            if binding is None:
                raise HTTPException(401, "This Access identity must be enrolled by the product first.")
            user_id = binding["user_id"]
        elif method == "google":
            subject = info["identity"]["sub"]
            if not isinstance(subject, str) or not subject:
                raise HTTPException(401, "Invalid product identity subject.")
            user_id = "google:" + subject
        else:
            user_id = f"shared:{method}:{info['role']}"
        principal = await self.current(user_id)
        if method in {"google", "cloudflare"} and principal.email != info["identity"]["email"].strip().lower():
            raise HTTPException(401, "The product identity mapping changed.")
        if principal.method != method:
            raise HTTPException(401, "The product sign-in method changed.")
        if admin and principal.role != "admin":
            raise HTTPException(403, "An organization administrator must perform this action.")
        request.state.product_identity_grant = principal.grant
        return replace(principal, csrf_token=self.legacy_security.csrf(sid))

    async def require_request_current(self, request: Request) -> ProductPrincipal:
        return await self.require_current(getattr(request.state, "product_identity_grant", None))

    async def authorize_admission(
        self, db: AsyncSession, grant: ProductGrant | ProductPrincipal | None,
    ) -> ProductPrincipal:
        grant = self._grant(grant)
        bind = db.get_bind()
        source = self.source_engine.url
        target = bind.engine.url
        native = self.native_engine.url
        if (
            bind.dialect.name != "postgresql" or self.source_engine.dialect.name != "postgresql"
            or (source.host, source.port or 5432, source.database) !=
               (target.host, target.port or 5432, target.database)
            or (native.host, native.port or 5432, native.database) !=
               (target.host, target.port or 5432, target.database)
            or not db.in_transaction()
        ):
            raise HTTPException(503, "Product admission requires one explicit PostgreSQL transaction and database.")
        # This is the SAME lock namespace used by app.database.begin_write.
        # Shared read ownership allows native concurrency but serializes against
        # canonical role/mapping writers, including inserting an absent role row.
        await db.execute(text(
            "SELECT pg_advisory_xact_lock_shared(726941, CAST(CAST(:schema AS regnamespace) AS oid)::int)"
        ), {"schema": self.schema})
        principal = await self._current(db, grant.user_id, await self._native_controls(grant.user_id, db))
        if not hmac.compare_digest(principal.authority_version, grant.authority_version):
            raise HTTPException(401, "Product authority changed before admission.")
        return principal

    async def read_native_user(self, db: AsyncSession, principal: ProductPrincipal) -> User | None:
        """Use authenticated authority without updating the native profile cache."""
        user = await db.get(User, principal.id)
        if user is None:
            return None
        username = "product:" + hashlib.sha256(principal.id.encode()).hexdigest()
        if user.username != username:
            raise HTTPException(409, "Product identity conflicts with an existing native account.")
        if not user.enabled or user.auth_version != principal.native_auth_version:
            raise HTTPException(401, "Your access changed. Sign in again.")
        # Request-only values must never flush over a concurrent profile/revocation
        # write. The response middleware rechecks this original grant after awaits.
        db.expunge(user)
        user.display_name, user.role = principal.name[:200], principal.role
        cast(_GrantedUser, user).product_grant = principal.grant
        return user

    async def project_native_user(self, db: AsyncSession, principal: ProductPrincipal) -> User:
        """A native FK/profile cache, never the source of product authority."""
        backend = db.get_bind().dialect.name
        if backend not in {"postgresql", "sqlite"}:
            raise HTTPException(503, "Unsupported native identity projection database.")
        if backend == "postgresql":
            # Canonical writers already hold this transaction's source lock.
            # A second pool checkout can deadlock behind a competing writer.
            await db.connection()
            fresh = await self.authorize_admission(db, principal.grant)
        else:
            fresh = await self.require_current(principal.grant)
        username = "product:" + hashlib.sha256(fresh.id.encode()).hexdigest()
        insert = postgres_insert if backend == "postgresql" else sqlite_insert
        # Both ID and derived username are unique. Concurrent first projections
        # must arbitrate both indexes, then verify the exact canonical ID.
        await db.execute(insert(User).values(
            id=fresh.id, username=username, display_name=fresh.name[:200], role=fresh.role,
            password="!product-managed-no-native-password!", enabled=True, auth_version=1,
        ).on_conflict_do_nothing())
        user = await db.scalar(
            select(User).where(User.id == fresh.id).with_for_update().execution_options(populate_existing=True)
        )
        if user is None or user.username != username:
            raise HTTPException(409, "Product identity conflicts with an existing native account.")
        elif not user.enabled:
            raise HTTPException(401, "This native account is disabled.")
        else:
            if backend == "postgresql":
                # A local revocation could win while the projection waited for
                # this user row; recheck the epoch under the acquired row lock.
                fresh = await self.authorize_admission(db, principal.grant)
            user.display_name, user.role = fresh.name[:200], fresh.role
        # No preference, Slack identity, local disable or local epoch is reset.
        # This server-authored request context is intentionally not an ORM field.
        cast(_GrantedUser, user).product_grant = fresh.grant
        return user
