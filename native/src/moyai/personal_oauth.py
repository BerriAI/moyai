"""Owner-scoped grants for the deployment's existing OAuth apps."""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from app.connector_errors import ConnectorError
from app.public_url import app_path, cookie_name
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .connection_policy import ConnectionPolicy
from .models import Connection, User
from .oauth_models import OAuthAuthorization, OAuthCredential
from .oauth_providers import PROVIDERS, OAuthProvider, authorization_url, client_identity, exchange

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from .contracts import Grant, RunScope
    from .database import Database
    from .runtime import Runtime

STATE_PREFIX = "personal-app:"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def connection_id(owner: str, provider: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"moyai-personal-oauth:{owner}:{provider}"))


def native_connections():
    """Only reviewed native accounts join the canonical catalog in product mode."""
    return Connection.kind.in_(["mcp", "litellm"]) | select(OAuthCredential.connection_id).where(
        OAuthCredential.connection_id == Connection.id,
    ).exists()


async def connection_owner(
    database: Database, db: AsyncSession, actor_id: str, provider: str, surface: str,
) -> str | None:
    """Keep the sender's authority; resolve only a verified credential owner."""
    if surface == "web":
        return actor_id
    if surface != "slack" or provider != "github":
        return None
    if not actor_id.startswith("slack:"):
        return actor_id
    if database.product_identity is None:
        return None
    return await database.product_identity.personal_memory_owner(db, actor_id)


def personal_callback(request: Request) -> bool:
    """Dispatch a personal state at the app's already registered callback URL."""
    return request.method == "GET" and request.url.path in {
        f"/oauth/{provider}/callback" for provider in PROVIDERS
    } and request.query_params.get("state", "").startswith(STATE_PREFIX)


def public_oauth_landing(request: Request) -> bool:
    return request.method == "GET" and (
        request.url.path == "/oauth/personal/continue.js" or personal_callback(request)
    )


def landing(request: Request, public_url: str) -> Response:
    """A first-party page restores Strict cookies; only /complete redeems the code."""
    provider = request.url.path.split("/")[2]
    params = {key: request.query_params.get(key, "") for key in ("state", "code", "error")}
    if len(params["state"]) > 200 or len(params["code"]) > 8192 or len(params["error"]) > 200:
        raise HTTPException(400, "Invalid authorization response.")
    destination = app_path(public_url, f"/oauth/personal/{provider}/complete") + "?" + urlencode(params)
    script = app_path(public_url, "/oauth/personal/continue.js")
    return HTMLResponse(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer"><title>Connecting your app · Moyai</title>'
        f'<script src="{html.escape(script, quote=True)}" defer></script></head>'
        '<body><h1>Completing your connection…</h1>'
        f'<a id="continue" href="{html.escape(destination, quote=True)}">Continue to Moyai</a></body></html>',
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


class PersonalOAuthView(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    provider: OAuthProvider
    name: str
    configured: bool
    connected: bool
    connection_id: str | None


class PersonalOAuthStart(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    url: str


class PersonalOAuthDisconnected(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    connected: bool = False


async def credentials(database: Database, scope: RunScope, grant: Grant) -> dict:
    """Resolve and renew secrets on the worker, outside durable turn payloads."""
    await database.authorize_scope(scope)
    service = database.product_connections
    if service is None:
        raise PermissionError("The deployment's app connections are unavailable.")
    provider = TypeAdapter(OAuthProvider).validate_python(grant.kind)
    async with database.sessions.begin() as db:
        owner = await connection_owner(database, db, scope.actor_id, provider, scope.authority.surface)
        if owner is None:
            raise PermissionError("Link your work account and connect GitHub in Settings > Connections.")
        # Serialize refreshes across processes before reading a rotating token.
        await db.execute(update(Connection).where(
            Connection.id == grant.id, Connection.owner_id == owner,
            Connection.enabled.is_(True), Connection.shared.is_(False), Connection.revision == grant.revision,
        ).values(revision=Connection.revision))
        account = await db.get(Connection, grant.id, populate_existing=True)
        saved = await db.get(OAuthCredential, grant.id, populate_existing=True)
        if (
            account is None or saved is None or not account.enabled or account.shared
            or account.owner_id != owner or account.revision != grant.revision
            or account.kind != grant.kind or not saved.encrypted
        ):
            raise PermissionError("This personal connection changed. Connect it again in Settings.")
        tokens = json.loads(database.security.unseal(saved.encrypted))
        if not tokens.get("client_id") or await client_identity(service, provider) != tokens["client_id"]:
            raise PermissionError("The app configuration changed. Connect your account again.")
        if tokens.get("expires_at") and tokens["expires_at"] < time.time() + 90:
            try:
                tokens = await exchange(service, provider, tokens["client_id"], previous=tokens)
            except ConnectorError:
                raise PermissionError("Your personal connection could not be renewed. Reconnect it in Settings.") from None
            saved.encrypted = database.security.seal(tokens)
            saved.revision += 1
    await database.authorize_scope(scope)
    return tokens


async def access_token(database: Database, scope: RunScope, grant: Grant) -> str:
    return (await credentials(database, scope, grant))["access_token"]


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()
    settings, database, security = runtime.settings, runtime.database, runtime.security

    def service():
        if runtime.product_connections is None:
            raise HTTPException(409, "This installation does not have deployment-managed app connections.")
        return runtime.product_connections

    def session_cookie() -> str:
        # Resolve lazily; constructing unrelated routers needs no product session.
        return cookie_name("workspace_session" if runtime.product_identity is not None else "moyai_session", settings.public_url)

    def require_individual(request: Request) -> None:
        principal = getattr(request.state, "product_principal", None)
        if runtime.product_identity is not None and (principal is None or not principal.individual):
            raise HTTPException(403, "Sign in with your individual work account to connect personal apps.")

    async def current(request: Request, user: User) -> None:
        require_individual(request)
        if runtime.product_identity is not None:
            await runtime.product_identity.require_request_current(request)
        fresh = await database.user(user.id)
        if not fresh.enabled or fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your account changed. Sign in again.")

    @router.get("/api/personal-connections", operation_id="listPersonalConnections")
    async def inventory(request: Request, user: User = Depends(actor)) -> list[PersonalOAuthView]:
        require_individual(request)
        if runtime.product_connections is None:
            return []
        async with database.sessions() as db:
            rows = {row.kind: row for row in await db.scalars(select(Connection).join(
                OAuthCredential, OAuthCredential.connection_id == Connection.id,
            ).where(Connection.owner_id == user.id, Connection.shared.is_(False)))}
        return [
            PersonalOAuthView(
                provider=provider, name=name, configured=bool(await client_identity(service(), provider)),
                connected=bool(rows.get(provider) and rows[provider].enabled),
                connection_id=rows[provider].id if rows.get(provider) and rows[provider].enabled else None,
            ) for provider, name in PROVIDERS.items()
        ]

    @router.post("/api/personal-connections/{provider}/oauth", operation_id="startPersonalConnection")
    async def start(provider: OAuthProvider, request: Request, user: User = Depends(actor)) -> PersonalOAuthStart:
        require_individual(request)
        client = await client_identity(service(), provider)
        if not client:
            raise HTTPException(409, f"{PROVIDERS[provider]} is not enabled on this Moyai deployment yet.")
        session = request.cookies.get(session_cookie(), "")
        if not session:
            raise HTTPException(401, "Sign in again before connecting an account.")
        identity = connection_id(user.id, provider)
        state, verifier = STATE_PREFIX + secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        await current(request, user)
        async with database.sessions.begin() as db:
            insert = postgres_insert if settings.postgres_url else sqlite_insert
            await db.execute(insert(Connection).values(
                id=identity, owner_id=user.id, kind=provider, name=f"Personal {PROVIDERS[provider]}",
                credential=security.seal(""), shared=False, read_only=False, enabled=False,
                revision=1, policy=ConnectionPolicy(oauth=True).model_dump_json(),
            ).on_conflict_do_nothing(index_elements=["id"]))
            await db.execute(insert(OAuthCredential).values(
                connection_id=identity, encrypted="", revision=1,
            ).on_conflict_do_nothing(index_elements=["connection_id"]))
            revision = await db.scalar(update(Connection).where(Connection.id == identity).values(
                revision=Connection.revision + 1,
            ).returning(Connection.revision))
            await db.execute(delete(OAuthAuthorization).where(
                (OAuthAuthorization.expires_at < int(time.time()))
                | ((OAuthAuthorization.owner_id == user.id) & (OAuthAuthorization.provider == provider)),
            ))
            db.add(OAuthAuthorization(
                state_hash=digest(state), owner_id=user.id, provider=provider,
                session_hash=digest(session), auth_version=user.auth_version,
                connection_id=identity, connection_revision=revision,
                expires_at=int(time.time()) + 600,
                encrypted=security.seal({"verifier": verifier, "client_id": client}),
            ))
        return PersonalOAuthStart(url=await authorization_url(service(), provider, state, verifier))

    def result(provider: OAuthProvider, status: str) -> RedirectResponse:
        return RedirectResponse(
            app_path(settings.public_url, f"/?personal_connection={status}&provider={provider}#settings/connections"),
            status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.get("/oauth/personal/continue.js", include_in_schema=False)
    async def continue_script() -> Response:
        return Response(
            "window.location.replace(document.getElementById('continue').href);",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.get("/oauth/personal/{provider}/complete", include_in_schema=False)
    async def complete(
        provider: OAuthProvider, request: Request, user: User = Depends(actor),
        state: str = "", code: str = "", error: str = "",
    ) -> Response:
        require_individual(request)
        if len(state) > 200 or len(code) > 8192 or len(error) > 200:
            return result(provider, "failed")
        async with database.sessions.begin() as db:
            pending = (await db.execute(delete(OAuthAuthorization).where(
                OAuthAuthorization.state_hash == digest(state),
                OAuthAuthorization.owner_id == user.id, OAuthAuthorization.provider == provider,
                OAuthAuthorization.auth_version == user.auth_version,
                OAuthAuthorization.session_hash == digest(request.cookies.get(session_cookie(), "")),
                OAuthAuthorization.expires_at > int(time.time()),
            ).returning(OAuthAuthorization))).scalar_one_or_none()
        if pending is None:
            return result(provider, "expired")
        if error or not code:
            return result(provider, "cancelled")
        material = json.loads(security.unseal(pending.encrypted))
        try:
            tokens = await exchange(
                service(), provider, material["client_id"], code=code, verifier=material["verifier"],
            )
            await current(request, user)
        except ConnectorError:
            return result(provider, "failed")
        async with database.sessions.begin() as db:
            changed = await db.scalar(update(Connection).where(
                Connection.id == pending.connection_id, Connection.owner_id == user.id,
                Connection.revision == pending.connection_revision, Connection.shared.is_(False),
            ).values(enabled=True, revision=Connection.revision + 1).returning(Connection.id))
            if changed is None:
                return result(provider, "expired")
            await db.execute(update(OAuthCredential).where(
                OAuthCredential.connection_id == changed,
            ).values(encrypted=security.seal(tokens), revision=OAuthCredential.revision + 1))
        return result(provider, "success")

    @router.delete("/api/personal-connections/{provider}", operation_id="disconnectPersonalConnection")
    async def disconnect(
        provider: OAuthProvider, request: Request, user: User = Depends(actor),
    ) -> PersonalOAuthDisconnected:
        require_individual(request)
        identity = connection_id(user.id, provider)
        async with database.sessions.begin() as db:
            await db.execute(update(Connection).where(
                Connection.id == identity, Connection.owner_id == user.id,
            ).values(enabled=False, revision=Connection.revision + 1))
            await db.execute(update(OAuthCredential).where(
                OAuthCredential.connection_id == identity,
            ).values(encrypted="", revision=OAuthCredential.revision + 1))
            await db.execute(delete(OAuthAuthorization).where(
                OAuthAuthorization.owner_id == user.id, OAuthAuthorization.provider == provider,
            ))
        return PersonalOAuthDisconnected()

    return router
