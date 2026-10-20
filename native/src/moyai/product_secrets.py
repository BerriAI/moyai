"""Canonical credential catalog and native secure-request routes.

Native session lifetimes use ``native:<uuid>`` in the existing root_id text field.
The old executor can never match that root against a legacy run. Native execution
retains handles; its trusted credential capability materializes command values.
"""

from __future__ import annotations

import copy
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NoReturn

from app.credentials import PROVIDERS, Credentials, Provider, Resolve, SaveSecret, UpdateSecret
from app.db import database as source_database
from fastapi import APIRouter, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from .product_connections import CommittedSourceWrites
from .product_identity import ProductPrincipal

if TYPE_CHECKING:
    from .runtime import Runtime

ROOT = r"^(?:[0-9a-f]{32}|native:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})?$"


class ProductSaveSecret(SaveSecret):
    root_id: str = Field(default="", pattern=ROOT)


class ProductUpdateSecret(UpdateSecret):
    root_id: str | None = Field(default=None, pattern=ROOT)


class CredentialResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class CredentialProvider(CredentialResponse):
    id: Provider
    name: str
    setup_url: str


class CredentialSecretMetadata(CredentialResponse):
    """Saved credential metadata; the encrypted value is never returned."""

    id: str
    provider: str
    label: str
    scope: str
    created_at: str
    name: str
    format: str
    env_var: str
    lifetime: str
    root_id: str
    expires_at: str
    invalid_reason: str
    revision: int
    status: str
    can_manage: bool


class CredentialCapabilities(CredentialResponse):
    create: bool
    update: bool
    delete: bool
    scopes: list[str]
    lifetimes: list[str]
    organization: bool
    native_execution: bool
    native_formats: list[str]
    native_provider_http: bool
    native_aws_login: bool


class CredentialInventory(CredentialResponse):
    providers: list[CredentialProvider]
    root_id: str
    secrets: list[CredentialSecretMetadata]
    capabilities: CredentialCapabilities
    note: str


class CredentialSaveResult(CredentialResponse):
    id: str
    saved: bool


class CredentialRevokeResult(CredentialResponse):
    revoked: bool


class CredentialAccessInputField(CredentialResponse):
    name: str
    label: str
    secret: bool
    required: bool


class CredentialAccessRequest(CredentialResponse):
    request_id: str
    generation: int
    status: str
    revision: int
    session_id: str
    provider: str
    name: str
    format: str
    env_var: str
    reason: str
    setup_url: str
    setup_instructions: str
    input_fields: list[CredentialAccessInputField]


class CredentialAccessRequestList(CredentialResponse):
    requests: list[CredentialAccessRequest]


class CredentialAccessRequestDetail(CredentialAccessRequest):
    credentials: list[CredentialSecretMetadata]
    scopes: list[str]
    lifetimes: list[str]
    can_resolve: bool
    note: str


class CredentialResolveResult(CredentialResponse):
    resolved: bool
    resumed: bool
    session_id: str
    turn_id: str | None = None
    note: str | None = None


class SecretRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as error:
                return JSONResponse(status_code=422, content={"detail": [
                    {key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()
                ]})
        return safe


class ProductSecrets:
    def __init__(self, runtime: Runtime):
        product = runtime.product
        if product is None:
            raise ValueError("Canonical Secrets requires existing-product mode.")
        self.runtime, self.product = runtime, product
        self.store, self.security = product.store_proxy, product.security
        self.credentials = Credentials(
            self.store, self.security, product.settings, None, CommittedSourceWrites(),
        )
        from .product_credential_execution import ProductCredentialExecution
        self.execution = ProductCredentialExecution(self)

    async def principal(self, request: Request, *, mutation: bool = False) -> ProductPrincipal:
        return await self.product.identity.authenticate(request, mutation=mutation)

    async def require_root(self, principal: ProductPrincipal, root: str, *, personal: bool = False) -> str | None:
        if not root or not re.fullmatch(ROOT, root):
            raise HTTPException(422, "Choose an existing root session.")
        if root.startswith("native:"):
            async with self.runtime.database.sessions.begin() as db:
                actor = await self.product.identity.project_native_user(db, principal)
            await self.runtime.database.prefetch_session_audiences(actor, [root.removeprefix("native:")])
            async with self.runtime.database.sessions.begin() as db:
                await self.product.identity.authorize_admission(db, principal.grant)
                session = await self.runtime.database.allowed_session(db, root.removeprefix("native:"), actor)
                if personal and session.owner_id != principal.id:
                    raise HTTPException(403, "Choose your own session for personal credentials.")
            return root
        def canonical_root():
            from app.session_privacy import SessionPrivacy

            run = self.store.run(root)
            if (
                not run or run["deleted_at"] or run["deletion_requested_at"]
                or not SessionPrivacy.can_access(run, principal.id if principal.individual else "")
            ):
                raise HTTPException(404, "Session not found.")
            if self.credentials.root(run) != root:
                raise HTTPException(422, "Choose an existing root session.")
            if personal and not self.credentials.same_requester(principal.id, run["active_user_id"]):
                raise HTTPException(403, "Choose your own session for personal credentials.")

        await source_database(canonical_root)
        return None

    async def inventory(self, principal: ProductPrincipal, root: str = "") -> dict[str, Any]:
        if root:
            await self.require_root(principal, root)
        rows = await source_database(
            self.credentials.list_secrets, principal.id, principal.role == "admin", root,
        )
        visible = []
        for row in rows:
            if row["lifetime"] == "session":
                try:
                    await self.require_root(principal, row["root_id"])
                except HTTPException as error:
                    if error.status_code >= 500:
                        raise
                    continue
            visible.append(row)
        await self.product.identity.require_current(principal.grant)
        scopes = []
        if principal.individual or self.security.local_preview():
            scopes.append("personal")
        if principal.role == "admin":
            scopes.append("organization")
        return {
            "providers": [{"id": key, "name": value["name"], "setup_url": value["setup"]}
                          for key, value in PROVIDERS.items()],
            "root_id": root, "secrets": visible,
            "capabilities": {
                "create": bool(scopes), "update": True, "delete": True,
                "scopes": scopes, "lifetimes": ["persistent", "session"],
                "organization": principal.role == "admin", "native_execution": self.execution.enabled,
                "native_formats": ["env", "file"] if self.execution.enabled else [],
                "native_provider_http": False, "native_aws_login": False,
            },
            "note": (
                "Generic env/file credentials use the native secure-request flow. Personal agent credentials are "
                "restricted to private web sessions; shared and Slack tasks require organization access. "
                "Interactive AWS login and separate provider-key inference are not enabled by this capability."
                if self.execution.enabled else
                "Saved canonical access. Native credential execution and secure request resumption are not registered."
            ),
        }

    def require_mutation(
        self, request: Request, principal: ProductPrincipal, connection: Any, native_root: str | None = None,
        *, personal: bool = False,
    ):
        # Use the canonical role helper through THIS writer connection. Calling
        # Security.role with its ordinary Store here would nest a pool checkout.
        security: Any = copy.copy(self.security)
        if security.user_roles is not None:
            roles = security.user_roles
            security.user_roles = SimpleNamespace(role=lambda email: roles.role_in(connection, email.strip().lower()))
        # This service checks source/native root privacy explicitly. The old
        # route-path guard understands legacy run IDs only.
        security.session_privacy = None
        security.require(request, mutation=True)
        # Re-read the canonical role at the actual synchronous write boundary.
        # A stale request snapshot cannot retain administrator privileges.
        admin = security.role(request) == "admin"
        if (principal.role == "admin") != admin:
            raise HTTPException(403, "Your product role changed. Refresh before saving.")
        if principal.individual:
            saved_role = connection.execute(
                "SELECT revision FROM user_roles WHERE email=?", (principal.email,),
            ).fetchone()
            if (saved_role["revision"] if saved_role else 0) != principal.role_revision:
                raise HTTPException(403, "Your product role grant changed. Refresh before saving.")
        row = connection.execute("SELECT * FROM users WHERE id=?", (principal.id,)).fetchone()
        row = dict(row) if row is not None else None
        if (
            not row or row["kind"] != principal.kind or row["email"] != principal.email
            or (principal.account_created_at and row["created_at"] != principal.account_created_at)
            or any(row.get(key) for key in ("disabled", "disabled_at", "deleted_at", "revoked_at"))
            or any(key in row and not row[key] for key in ("enabled", "is_active"))
        ):
            raise HTTPException(401, "The product account changed before saving.")
        if self.runtime.settings.postgres_url:
            schema = self.runtime.database.execution_schema
            if not schema or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
                raise HTTPException(503, "The native account schema is unavailable.")
            local = connection.execute(
                f'SELECT enabled,auth_version FROM "{schema}".moyai_users WHERE id=?', (principal.id,),
            ).fetchone()
            controls = (bool(local["enabled"]), local["auth_version"]) if local else (True, 1)
            if not controls[0] or controls[1] != principal.native_auth_version:
                raise HTTPException(401, "Native access changed before saving.")
            if native_root:
                session = connection.execute(
                    f'SELECT owner_id,visibility,deleted FROM "{schema}".moyai_sessions WHERE id=?',
                    (native_root.removeprefix("native:"),),
                ).fetchone()
                if (
                    not session or session["deleted"]
                    or ((personal or session["visibility"] == "private") and session["owner_id"] != principal.id)
                ):
                    raise HTTPException(404, "The credential session is no longer available.")
        if principal.method == "cloudflare":
            info = security.session_info(request) or {}
            identity = info.get("identity") or {}
            binding = connection.execute(
                "SELECT subject FROM access_identities WHERE issuer=? AND user_id=? AND email=?",
                (self.product.identity.access.issuer, principal.id, principal.email),
            ).fetchone()
            if not binding or binding["subject"] != identity.get("sub"):
                raise HTTPException(401, "The Access identity mapping changed before saving.")
        return admin

    async def create(self, request: Request, principal: ProductPrincipal, body: ProductSaveSecret):
        native = None
        if body.lifetime == "session":
            native = await self.require_root(principal, body.root_id, personal=body.scope == "personal")
        await self.product.identity.require_current(principal.grant)

        def save():
            with self.store.connect() as conn:
                conn.begin_write()
                admin = self.require_mutation(request, principal, conn, native, personal=body.scope == "personal")
                return self.credentials.insert_secret(
                    conn, body, principal.id, admin, authorized_native_root=native,
                )

        identity = await source_database(save)
        return {"id": identity, "saved": True}

    async def update(self, request: Request, principal: ProductPrincipal, identity: str, body: ProductUpdateSecret):
        def current():
            rows = self.store.rows(
                """SELECT id,scope,owner_id,root_id,lifetime FROM provider_secrets
                   WHERE id=? AND revoked_at=''""", (identity,),
            )
            if not rows or not (
                rows[0]["owner_id"] == principal.id or
                (rows[0]["scope"] == "organization" and principal.role == "admin")
            ):
                raise HTTPException(404, "Credentials not found.")
            return rows[0]

        row = await source_database(current)
        lifetime = body.lifetime if "lifetime" in body.model_fields_set else row["lifetime"]
        root = body.root_id if "root_id" in body.model_fields_set else row["root_id"]
        scope = body.scope if "scope" in body.model_fields_set else row["scope"]
        native = await self.require_root(principal, root or "", personal=scope == "personal") if lifetime == "session" else None
        await self.product.identity.require_current(principal.grant)

        def save():
            return self.credentials.update_secret(
                identity, body, principal.id, principal.role == "admin", authorized_native_root=native,
                authorize=lambda conn: self.require_mutation(
                    request, principal, conn, native, personal=scope == "personal",
                ),
            )

        return await source_database(save)

    async def revoke(self, request: Request, principal: ProductPrincipal, identity: str):
        from app.db import now

        await self.product.identity.require_current(principal.grant)

        def save():
            with self.store.connect() as conn:
                conn.begin_write()
                admin = self.require_mutation(request, principal, conn)
                row = conn.execute("SELECT * FROM provider_secrets WHERE id=?", (identity,)).fetchone()
                if not row or not (row["owner_id"] == principal.id or row["scope"] == "organization" and admin):
                    raise HTTPException(404, "Credentials not found.")
                if row["scope"] == "organization" and not admin:
                    raise HTTPException(403, "Only an administrator can revoke organization credentials.")
                conn.execute(
                    "UPDATE provider_secrets SET encrypted='',revoked_at=?,revision=revision+1 WHERE id=?",
                    (now(), identity),
                )
                self.credentials.audit_in(conn, principal.id, identity, "revoked")

        await source_database(save)
        return {"revoked": True}

    def routes(self) -> APIRouter:
        router = APIRouter(route_class=SecretRoute)

        @router.get("/api/credentials", operation_id="listCredentials")
        async def inventory(request: Request, run_id: str = "") -> CredentialInventory:
            return CredentialInventory.model_validate(await self.inventory(await self.principal(request), run_id))

        @router.post("/api/credentials/secrets", status_code=201, operation_id="createCredential")
        async def create(body: ProductSaveSecret, request: Request) -> CredentialSaveResult:
            return CredentialSaveResult.model_validate(
                await self.create(request, await self.principal(request, mutation=True), body),
            )

        @router.patch("/api/credentials/secrets/{identity}", operation_id="updateCredential")
        async def update(identity: str, body: ProductUpdateSecret, request: Request) -> CredentialSecretMetadata:
            return CredentialSecretMetadata.model_validate(
                await self.update(request, await self.principal(request, mutation=True), identity, body),
            )

        @router.delete("/api/credentials/secrets/{identity}", operation_id="revokeCredential")
        async def revoke(identity: str, request: Request) -> CredentialRevokeResult:
            return CredentialRevokeResult.model_validate(
                await self.revoke(request, await self.principal(request, mutation=True), identity),
            )

        @router.post("/api/credentials/requests/{identity}", operation_id="resolveCredentialRequest")
        async def resolve_request(identity: str, body: Resolve, request: Request) -> CredentialResolveResult:
            return CredentialResolveResult.model_validate(await self.execution.resolve(
                request, await self.principal(request, mutation=True), identity, body,
            ))

        @router.get("/api/credentials/requests", operation_id="listCredentialRequests")
        async def pending_requests(session_id: str, request: Request) -> CredentialAccessRequestList:
            return CredentialAccessRequestList.model_validate(
                await self.execution.pending(await self.principal(request), session_id),
            )

        @router.get("/api/credentials/requests/{identity}", operation_id="getCredentialRequest")
        async def describe_request(identity: str, request: Request) -> CredentialAccessRequestDetail:
            return CredentialAccessRequestDetail.model_validate(
                await self.execution.describe(await self.principal(request), identity),
            )

        return router

    async def settings_inventory(self, request: Request, run_id: str) -> CredentialInventory:
        """GET /api/settings/secrets in product mode; settings_api owns the route."""
        return CredentialInventory.model_validate(await self.inventory(await self.principal(request), run_id))

    async def reject_settings_mutation(self, request: Request) -> NoReturn:
        """Settings secret writes in product mode: canonical credential routes own scope and revision."""
        await self.principal(request, mutation=True)
        raise HTTPException(409, "Use canonical credential routes to preserve scope and revision.")
