"""Browser authorization and current-user token renewal for personal connected tools."""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from app.public_url import app_path, cookie_name
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .connection_policy import ConnectionPolicy
from .models import Connection, User
from .oauth_models import OAuthAuthorization, OAuthCredential
from .oauth_providers import (
    PROVIDERS,
    OAuthClient,
    OAuthFailure,
    OAuthProvider,
    OAuthTokens,
    authorization_url,
    configured,
    exchange,
    register,
)

if TYPE_CHECKING:
    from .contracts import Grant, RunScope
    from .database import Database
    from .runtime import Runtime


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def connection_id(owner: str, provider: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"moyai-personal-oauth:{owner}:{provider}"))


def native_connections():
    """Only reviewed native accounts join the canonical catalog in product mode."""
    return Connection.kind.in_(["mcp", "litellm"]) | select(OAuthCredential.connection_id).where(
        OAuthCredential.connection_id == Connection.id,
    ).exists()


def public_oauth_landing(path: str) -> bool:
    """These pages contain no user data and never redeem an authorization code."""
    return path == "/oauth/personal/continue.js" or path in {
        f"/oauth/personal/{provider}/callback" for provider in PROVIDERS
    }


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


async def access_token(database: Database, scope: RunScope, grant: Grant) -> str:
    """Resolve secrets on the trusted worker, outside durable turn/checkpoint payloads."""
    await database.authorize_scope(scope)
    async with database.sessions.begin() as db:
        # A no-op write obtains the same cross-process lock on PostgreSQL and
        # SQLite. Refreshes serialize before reading a rotating refresh token.
        await db.execute(update(Connection).where(
            Connection.id == grant.id, Connection.owner_id == scope.actor_id,
            Connection.enabled.is_(True), Connection.shared.is_(False), Connection.revision == grant.revision,
        ).values(revision=Connection.revision))
        account = await db.get(Connection, grant.id, populate_existing=True)
        saved = await db.get(OAuthCredential, grant.id, populate_existing=True)
        if (
            account is None or saved is None or not account.enabled or account.shared
            or account.owner_id != scope.actor_id or account.revision != grant.revision
            or account.kind != grant.kind or not saved.encrypted or grant.kind not in PROVIDERS
        ):
            raise PermissionError("This personal connection changed. Connect it again in Settings.")
        tokens = OAuthTokens.model_validate_json(database.security.unseal(saved.encrypted))
        if tokens.expires_at and tokens.expires_at < time.time() + 90:
            try:
                tokens = await exchange(grant.kind, tokens.client, previous=tokens)
            except OAuthFailure:
                raise PermissionError("Your personal connection could not be renewed. Reconnect it in Settings.") from None
            saved.encrypted = database.security.seal(tokens.stored())
            saved.revision += 1
        token = tokens.access_token.get_secret_value()
    await database.authorize_scope(scope)
    return token


def routes(runtime: Runtime, actor) -> APIRouter:
    router = APIRouter()
    settings, database, security = runtime.settings, runtime.database, runtime.security
    session_cookie = (
        runtime.product_identity.session_cookie if runtime.product_identity is not None
        else cookie_name("moyai_session", settings.public_url)
    )

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
        async with database.sessions() as db:
            rows = {row.kind: row for row in await db.scalars(select(Connection).join(
                OAuthCredential, OAuthCredential.connection_id == Connection.id,
            ).where(Connection.owner_id == user.id, Connection.shared.is_(False)))}
        return [
            PersonalOAuthView(
                provider=provider, name=definition.name, configured=configured(settings, provider),
                connected=bool(rows.get(provider) and rows[provider].enabled),
                connection_id=rows[provider].id if rows.get(provider) and rows[provider].enabled else None,
            ) for provider, definition in PROVIDERS.items()
        ]

    @router.post("/api/personal-connections/{provider}/oauth", operation_id="startPersonalConnection")
    async def start(provider: OAuthProvider, request: Request, user: User = Depends(actor)) -> PersonalOAuthStart:
        require_individual(request)
        if not configured(settings, provider):
            raise HTTPException(409, f"{PROVIDERS[provider].name} is not enabled on this Moyai deployment yet.")
        session = request.cookies.get(session_cookie, "")
        if not session:
            raise HTTPException(401, "Sign in again before connecting an account.")
        identity = connection_id(user.id, provider)
        # Persist the connection revision before network work. Disconnect and a
        # newer connect attempt must be able to supersede an in-flight response.
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        await current(request, user)
        async with database.sessions.begin() as db:
            insert = postgres_insert if settings.postgres_url else sqlite_insert
            await db.execute(insert(Connection).values(
                id=identity, owner_id=user.id, kind=provider, name=f"Personal {PROVIDERS[provider].name}",
                credential=security.seal(""), shared=False, read_only=False, enabled=False,
                revision=1, policy=ConnectionPolicy(oauth=True).model_dump_json(),
            ).on_conflict_do_nothing(index_elements=["id"]))
            await db.execute(insert(OAuthCredential).values(
                connection_id=identity, encrypted="", revision=1,
            ).on_conflict_do_nothing(index_elements=["connection_id"]))
            # Incrementing fences an older callback already exchanging its code.
            revision = await db.scalar(update(Connection).where(Connection.id == identity).values(
                revision=Connection.revision + 1,
            ).returning(Connection.revision))
            await db.execute(delete(OAuthAuthorization).where(
                (OAuthAuthorization.expires_at < int(time.time()))
                | ((OAuthAuthorization.owner_id == user.id) & (OAuthAuthorization.provider == provider)),
            ))
            pending = OAuthAuthorization(
                state_hash=digest(state), owner_id=user.id, provider=provider,
                session_hash=digest(session), auth_version=user.auth_version,
                connection_id=identity, connection_revision=revision,
                expires_at=int(time.time()) + 600, encrypted="",
            )
            db.add(pending)
        try:
            client = await register(settings, provider)
            await current(request, user)
        except OAuthFailure as error:
            async with database.sessions.begin() as db:
                await db.execute(delete(OAuthAuthorization).where(OAuthAuthorization.state_hash == digest(state)))
            raise HTTPException(502, str(error)) from None
        async with database.sessions.begin() as db:
            changed = await db.scalar(update(OAuthAuthorization).where(
                OAuthAuthorization.state_hash == digest(state),
                OAuthAuthorization.expires_at > int(time.time()),
            ).values(encrypted=security.seal({
                "verifier": verifier,
                "client": {**client.model_dump(exclude={"client_secret"}), "client_secret": client.client_secret.get_secret_value()},
            })).returning(OAuthAuthorization.state_hash))
            if changed is None:
                raise HTTPException(409, "This connection request was superseded. Try Connect again.")
        return PersonalOAuthStart(url=authorization_url(provider, client, state, verifier))

    def result(provider: OAuthProvider, status: str) -> RedirectResponse:
        return RedirectResponse(
            app_path(settings.public_url, f"/?personal_connection={status}&provider={provider}#settings/connections"),
            status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    # Loading this first-party document before navigating again makes Strict
    # login cookies available without weakening the application's cookie policy.
    @router.get("/oauth/personal/continue.js", include_in_schema=False)
    async def continue_script() -> Response:
        return Response(
            "window.location.replace(document.getElementById('continue').href);",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @router.get("/oauth/personal/{provider}/callback", include_in_schema=False)
    async def landing(
        provider: OAuthProvider, state: str = "", code: str = "", error: str = "", iss: str = "",
    ) -> Response:
        if len(state) > 200 or len(code) > 8192 or len(iss) > 200 or len(error) > 200:
            return result(provider, "failed")
        destination = app_path(settings.public_url, f"/oauth/personal/{provider}/complete") + "?" + urlencode(
            {"state": state, "code": code, "error": error, "iss": iss},
        )
        script = app_path(settings.public_url, "/oauth/personal/continue.js")
        return HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="referrer" content="no-referrer"><title>Connecting your app · Moyai</title>'
            f'<script src="{html.escape(script, quote=True)}" defer></script></head>'
            '<body><h1>Completing your connection…</h1>'
            f'<a id="continue" href="{html.escape(destination, quote=True)}">Continue to Moyai</a></body></html>',
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    # Only this authenticated same-site step can consume the browser-bound state
    # and redeem the code. Ordinary organization callbacks remain independent.
    @router.get("/oauth/personal/{provider}/complete", include_in_schema=False)
    async def complete(
        provider: OAuthProvider, request: Request, user: User = Depends(actor),
        state: str = "", code: str = "", error: str = "", iss: str = "",
    ) -> Response:
        require_individual(request)
        if len(state) > 200 or len(code) > 8192 or len(iss) > 200 or len(error) > 200:
            return result(provider, "failed")
        async with database.sessions.begin() as db:
            pending = (await db.execute(delete(OAuthAuthorization).where(
                OAuthAuthorization.state_hash == digest(state),
                OAuthAuthorization.owner_id == user.id, OAuthAuthorization.provider == provider,
                OAuthAuthorization.auth_version == user.auth_version,
                OAuthAuthorization.session_hash == digest(request.cookies.get(session_cookie, "")),
                OAuthAuthorization.expires_at > int(time.time()),
            ).returning(OAuthAuthorization))).scalar_one_or_none()
        if pending is None or not pending.encrypted:
            return result(provider, "expired")
        if error or not code:
            return result(provider, "cancelled")
        if iss and iss != PROVIDERS[provider].issuer:
            return result(provider, "failed")
        material = json.loads(security.unseal(pending.encrypted))
        try:
            tokens = await exchange(
                provider, OAuthClient.model_validate(material["client"]), code=code, verifier=material["verifier"],
            )
            await current(request, user)
        except OAuthFailure:
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
            ).values(encrypted=security.seal(tokens.stored()), revision=OAuthCredential.revision + 1))
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
