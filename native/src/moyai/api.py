"""Moyai's authenticated product API around native conversations and workflows."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit
from uuid import UUID

from app.public_url import app_path, base_path, cookie_name, origin
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, TypeAdapter, field_validator, model_validator
from pydantic_ai import ConversationTypeAdapter, DeferredToolResults
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse, ask_user_result
from sqlalchemy import func, select, update
from temporalio.service import RPCError

from .api_views import (
    AuthConfig,
    ConnectionCheck,
    ConnectionToolPreview,
    ConnectionView,
    EffectResolveResult,
    GoalCommandResult,
    GoalStartedResult,
    HealthStatus,
    InvocableSkillPage,
    LiveEvent,
    LoginResult,
    LogoutResult,
    Me,
    MeUser,
    PendingEffect,
    PlanStep,
    QueuedTurnCancelResult,
    QueuedTurnUpdateResult,
    SavedActivity,
    SessionDeleteResult,
    SessionDetail,
    SessionFile,
    SessionListItem,
    SessionMessage,
    SessionRestoreResult,
    SessionStopResult,
    SessionSummary,
    SessionUpdateResult,
    SlackDeliveryRetry,
    TurnFailureDetails,
    TurnView,
    TurnWorkProgress,
    WorkActionDetail,
    session_view,
    turn_view,
)
from .computer import routes as computer_routes
from .connection_policy import ConnectionPolicy
from .contracts import ApprovalReply, EffectResolution, NewMessage, NewSession, QueuedEdit, SessionEdit
from .debug_trace import debug_trace
from .failed_action_attempts import failed_action_attempt
from .file_preview import routes as file_preview_routes
from .gateway_config import GatewayConfig, GatewayDefaults, GatewayDiscovery
from .history_handoff import is_turn_run
from .images import routes as image_routes
from .live_events import PUBLIC_TOPIC
from .live_stream import authorized_events
from .mcp_remote import remote_mcp_toolset, validate_mcp_endpoint
from .models import (
    Artifact,
    Connection,
    Effect,
    Message,
    Session,
    SessionPreference,
    SessionRead,
    Turn,
    User,
    new_id,
    now,
)
from .preferences.settings_api import routes as settings_routes
from .presentation import public_event, redact
from .private_instructions import preferences_for_user
from .product_identity import ProductAuthorityBusy
from .product_requester import answer_actor
from .public_path import PublicPathMiddleware
from .pull_requests import routes as pull_request_routes
from .runtime import Runtime
from .security import check_password
from .session_reads import SessionReads
from .settings import Settings
from .sidebar import folders_for, lock_sidebar
from .sidebar import routes as sidebar_routes
from .slack import dispatcher as slack_dispatcher
from .slack import routes as slack_routes
from .worker import dispatcher, reconcile_workflows
from .workspace import routes as workspace_routes

COOKIE = "moyai_session"
STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class Login(BaseModel):
    username: str = Field(default="", max_length=200)
    password: SecretStr = Field(max_length=4096)


class ConnectionInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    kind: Literal["github", "linear", "notion", "slack", "mcp", "litellm"]
    credential: SecretStr
    clear_credential: bool = False
    shared: bool = False
    read_only: bool = True
    enabled: bool = True
    policy: ConnectionPolicy = Field(default_factory=ConnectionPolicy)

    @field_validator("credential")
    @classmethod
    def token_only(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value().strip().lower() == "oauth":
            raise ValueError("Enter an access token; interactive OAuth is not supported on the worker.")
        return value

    @model_validator(mode="after")
    def service_configuration(self) -> ConnectionInput:
        if self.policy.oauth:
            raise ValueError("Manage personal OAuth accounts through Connect and Disconnect.")
        if self.kind == "litellm":
            if self.shared:
                raise ValueError("LiteLLM gateway connections are personal and cannot be shared.")
            if self.policy.gateway is None or self.policy.mcp is not None or self.policy.product is not None:
                raise ValueError("Provide a LiteLLM gateway URL and capability selections.")
            if self.clear_credential:
                raise ValueError("A LiteLLM gateway requires a key; disable it to disconnect.")
        elif self.policy.gateway is not None or self.policy.gateway_discovery is not None:
            raise ValueError("Gateway configuration requires a LiteLLM connection.")
        if self.kind in {"mcp", "litellm"}:
            credential = self.credential.get_secret_value()
            if len(credential) > 8192 or any(not 33 <= ord(char) <= 126 for char in credential):
                raise ValueError("Use a bearer token of at most 8192 printable ASCII characters without whitespace.")
            if self.kind == "mcp" and self.policy.mcp is None:
                raise ValueError("Provide the remote MCP endpoint configuration.")
            if self.policy.product is not None:
                raise ValueError("Remote MCP connections cannot contain product account references.")
        elif self.policy.mcp is not None or self.clear_credential:
            raise ValueError("MCP configuration and credential clearing require an MCP connection.")
        if self.clear_credential and self.credential.get_secret_value():
            raise ValueError("Choose either a replacement credential or clearing the saved credential.")
        return self


SESSION_MESSAGES = TypeAdapter(list[SessionMessage])


class EventStream(StreamingResponse):
    media_type = "text/event-stream"


def connection_view(connection: Connection) -> dict[str, Any]:
    return {
        "id": connection.id, "name": connection.name, "kind": connection.kind,
        "owner_id": connection.owner_id, "shared": connection.shared,
        "read_only": connection.read_only, "enabled": connection.enabled,
        "revision": connection.revision, "policy": json.loads(connection.policy),
    }


def create_app(runtime: Runtime | None = None, *, run_dispatcher: bool = True) -> FastAPI:
    owned = runtime is None
    runtime = runtime or Runtime(Settings())
    session_cookie = cookie_name(COOKIE, runtime.settings.public_url)
    session_reads = SessionReads(runtime)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owned:
            await runtime.start()
        tasks = [
            asyncio.create_task(dispatcher(runtime)),
            asyncio.create_task(reconcile_workflows(runtime)),
        ] if run_dispatcher else []
        if run_dispatcher and runtime.product is None and runtime.settings.slack_bot_token.get_secret_value():
            tasks.append(asyncio.create_task(slack_dispatcher(runtime)))
            tasks.append(asyncio.create_task(runtime.slack_activity.watch()))
        # Distributed product roles start Slack under CoordinatorHandoff.
        # The explicit loopback-only combined rehearsal owns its local product
        # store exclusively and must run the same inbox/outbox service too.
        local_slack = runtime.product_slack if (
            run_dispatcher and runtime.product_config is not None
            and runtime.product_config.local_rehearsal and runtime.product_config.role == "standalone"
        ) else None
        try:
            if local_slack is not None:
                local_slack.start()
            yield
        finally:
            if local_slack is not None:
                await local_slack.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            runtime.work_progress.clear()
            if owned:
                await runtime.close()

    app = FastAPI(title="Moyai", lifespan=lifespan)
    app.state.runtime = runtime
    if tracing := runtime.configure_tracing():
        tracing.instrument_app(app)
    if runtime.product_slack is not None:
        app.include_router(runtime.product_slack.routes())
    else:
        app.include_router(slack_routes(runtime))

    @app.exception_handler(RequestValidationError)
    async def safe_validation(_request: Request, error: RequestValidationError):
        # Credentials and uploaded content must not be reflected into error
        # responses consumed by clients, proxies, or diagnostic tooling.
        return JSONResponse(status_code=422, content={"detail": [
            {key: item[key] for key in ("type", "loc", "msg")} for item in error.errors()
        ]})

    @app.exception_handler(ProductAuthorityBusy)
    async def authority_busy(_request: Request, _error: ProductAuthorityBusy):
        return JSONResponse(
            status_code=503, content={"detail": "Account access is busy. Try again shortly."},
            headers={"Retry-After": "1", "Cache-Control": "no-store"},
        )

    async def actor(request: Request) -> User:
        if runtime.product_identity is not None:
            principal = await runtime.product_identity.authenticate(
                request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"},
            )
            user: User | None = None
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                async with runtime.database.sessions() as db:
                    user = await runtime.product_identity.read_native_user(db, principal)
            if user is None:
                async with runtime.database.sessions.begin() as db:
                    user = await runtime.product_identity.project_native_user(db, principal)
            request.state.product_principal = principal
            # Share membership reads only within this authenticated HTTP request.
            # Live streams explicitly discard this snapshot before each recheck.
            cast(Any, user).slack_read_cache = {}
            await prefetch_request_audience(request, user)
            return user
        token = request.cookies.get(session_cookie, "")
        identity = runtime.security.read_session(token)
        if not identity:
            raise HTTPException(401, "Sign in to continue.")
        user = await runtime.database.user(identity["user"])
        if user.auth_version != identity["version"]:
            raise HTTPException(401, "Your session expired.")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-moyai-csrf", "")
            if not hmac.compare_digest(csrf, identity["csrf"]):
                raise HTTPException(403, "Refresh the page before making changes.")
        return user

    async def prefetch_request_audience(request: Request, user: User) -> None:
        """HTTP mutations must not discover provider work after taking write locks."""
        if runtime.product_slack is None:
            return
        session_id = request.path_params.get("session_id")
        if session_id is None:
            turn_id = request.path_params.get("turn_id")
            artifact_id = request.path_params.get("artifact_id")
            async with runtime.database.sessions() as db:
                if turn_id:
                    session_id = await db.scalar(select(Turn.session_id).where(Turn.id == turn_id))
                elif artifact_id:
                    session_id = await db.scalar(select(Artifact.session_id).where(Artifact.id == artifact_id))
        if session_id is not None:
            await runtime.database.prefetch_session_audiences(
                user, [session_id], deleted=request.url.path.endswith("/restore"),
            )

    if runtime.product_slack is not None:
        app.include_router(runtime.product_slack.web.routes(actor))

    # UUID matching keeps this route distinct from the canonical provider check
    # registered below, while OpenAPI retains the ordinary connection_id path.
    @app.post("/api/connections/{connection_id:uuid}/check", operation_id="checkConnection")
    async def check_connection(
        connection_id: UUID, request: Request, user: User = Depends(actor),
    ) -> ConnectionCheck:
        identity = str(connection_id)
        async with runtime.database.sessions() as db:
            row = await db.get(Connection, identity)
            if row is None or (row.owner_id != user.id and not row.shared):
                raise HTTPException(404, "Connection not found.")
            if row.kind != "mcp":
                raise HTTPException(422, "Only remote MCP connections support this check.")
            grant = (await runtime.database.native_grants(
                db, user.id, [identity], kinds=frozenset({"mcp"}),
            ))[0]

        async def authorize() -> None:
            if runtime.product_identity is not None:
                await runtime.product_identity.require_request_current(request)
            current_user = await runtime.database.user(user.id)
            if current_user.auth_version != user.auth_version or current_user.role != user.role:
                raise HTTPException(401, "Your account changed. Sign in again before checking the connection.")
            async with runtime.database.sessions() as db:
                current = await runtime.database.native_grants(
                    db, user.id, [identity], kinds=frozenset({"mcp"}),
                )
                if current != [grant]:
                    raise HTTPException(409, "The connection changed while checking. Try again.")

        try:
            policy = ConnectionPolicy.model_validate(grant.policy)
            if policy.mcp is None or policy.product is not None:
                raise ValueError("Invalid remote MCP configuration.")
            credential = runtime.security.unseal(grant.credential).decode()
            async with remote_mcp_toolset(
                policy.mcp, credential, runtime.settings.mcp_allowed_origins,
                identity=f"connection_check_{identity}", authorize=authorize,
            ) as toolset:
                tools = await toolset.list_tools()
            preview = [
                ConnectionToolPreview(
                    name=str(redact(tool.name, [credential])),
                    description=str(redact(tool.description, [credential])) if tool.description is not None else None,
                )
                for tool in tools if policy.allows(tool.name) and (
                    not grant.read_only or (
                        tool.annotations is not None
                        and tool.annotations.model_dump(by_alias=True).get("readOnlyHint") is True
                    )
                )
            ]
        except Exception:  # noqa: BLE001 - remote errors may contain credentials or provider payloads
            # SDKs can wrap an authorization failure. Resolve its current,
            # trusted status without exposing any provider exception content.
            await authorize()
            raise HTTPException(502, "The MCP connection could not be checked. Verify its endpoint and access.") from None
        # The transport rechecks after DNS, before each send; the same authority
        # gates release after discovery and teardown. Provider I/O holds no DB transaction.
        await authorize()
        return ConnectionCheck(tool_count=len(preview), tools=preview)

    app.include_router(image_routes(runtime, actor))
    app.include_router(computer_routes(runtime, actor, cookie_name=session_cookie))
    app.include_router(sidebar_routes(runtime, actor))
    from .responsibility_routes import routes as responsibility_routes
    app.include_router(responsibility_routes(runtime, actor))
    from .dot_routes import routes as dot_routes
    app.include_router(dot_routes(runtime, actor))
    app.include_router(workspace_routes(runtime, actor))
    app.include_router(file_preview_routes(runtime, actor))
    app.include_router(pull_request_routes(runtime, actor))
    if runtime.product is not None:
        from .product_auth import routes as product_auth_routes
        app.include_router(product_auth_routes(runtime, actor))
    if runtime.product_connections is not None:
        from .product_connections import routes as product_connection_routes
        app.include_router(product_connection_routes(runtime.product_connections))
    if runtime.product_secrets is not None:
        app.include_router(runtime.product_secrets.routes())
    if runtime.product_inference is not None:
        from .product_inference_routes import product_inference_routes
        app.include_router(product_inference_routes(runtime.product_inference))
    if runtime.product_memory is not None:
        from .product_memory import routes as product_memory_routes
        app.include_router(product_memory_routes(runtime.product_memory))
    if runtime.product_skill_catalog is not None:
        from .product_skill_catalog import routes as product_skill_routes
        app.include_router(product_skill_routes(runtime.product_skill_catalog))
    if runtime.product_skill_learning is not None:
        from .product_skill_learning import routes as skill_learning_routes
        app.include_router(skill_learning_routes(runtime.product_skill_learning))
    if runtime.product_billing is not None:
        from .billing.product_billing_routes import routes as product_billing_routes
        app.include_router(product_billing_routes(runtime.product_billing, runtime=runtime))
    if runtime.product_automations is not None:
        from .automations.product_automations import routes as product_automation_routes
        app.include_router(product_automation_routes(runtime.product_automations))
    from .product_routes import routes as product_routes
    app.include_router(product_routes(runtime, actor))
    app.include_router(settings_routes(runtime, actor))
    from .personal_oauth import routes as personal_oauth_routes
    app.include_router(personal_oauth_routes(runtime, actor))
    from .swarm_routes import routes as swarm_routes
    app.include_router(swarm_routes(runtime, actor))

    @app.get("/api/skills", operation_id="listInvocableSkills")
    async def invocable_skills(
        shared: bool = False, offset: int = Query(0, ge=0, le=10000),
        limit: int = Query(100, ge=1, le=100), user: User = Depends(actor), session_id: str | None = None,
    ) -> InvocableSkillPage:
        if session_id is not None:
            await runtime.database.prefetch_session_audiences(user, [session_id])
            async with runtime.database.sessions() as db:
                session = await runtime.database.allowed_session(db, session_id, user)
                context = await runtime.database.execution_context(db, session)
                if context.surface != "web":
                    return InvocableSkillPage(skills=[], has_more=False, next_offset=None)
                shared = context.visibility != "private"
        return InvocableSkillPage.model_validate(
            await runtime.skill_invocations.listing(user, shared=shared, offset=offset, limit=limit)
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        from .personal_oauth import landing, personal_callback, public_oauth_landing

        if not hasattr(request.state, "moyai_received_at"):
            request.state.moyai_received_at = now()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.startswith("/hooks/"):
            request_origin = request.headers.get("origin")
            expected = origin(runtime.settings.public_url or str(request.base_url))
            if request_origin and request_origin.rstrip("/") != expected:
                return Response("Origin is not allowed.", status_code=403)
        if runtime.product is not None and (
            request.url.path not in {"/health", "/healthz"}
            and not request.url.path.startswith(("/hooks/", "/broker/native/"))
            and not public_oauth_landing(request)
        ):
            from .product_access import access_session
            try:
                access_cookie = await access_session(runtime.product, request)
            except HTTPException as error:
                return JSONResponse(
                    {"detail": error.detail}, status_code=error.status_code,
                    headers={"Cache-Control": "no-store"},
                )
            if access_cookie is not None:
                # Replay a read once with the newly issued, verified product
                # cookie. Writes need a fresh CSRF token from /api/me first.
                response = (
                    RedirectResponse(
                        app_path(runtime.settings.public_url, request.url.path) + ("?" + request.url.query if request.url.query else ""),
                        status_code=303,
                    ) if request.method in {"GET", "HEAD"} else
                    JSONResponse({"detail": "Refresh your organization sign-in."}, status_code=401)
                )
                response.raw_headers.append((b"set-cookie", access_cookie))
                response.headers["Cache-Control"] = "no-store"
                return response
        if personal_callback(request):
            try:
                response = landing(request, runtime.settings.public_url)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        else:
            response = await call_next(request)
        if (
            runtime.product_identity is not None
            and getattr(request.state, "product_identity_grant", None) is not None
            and response.status_code < 400
        ):
            try:
                await runtime.product_identity.require_request_current(request)
            except HTTPException as error:
                response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers["X-Frame-Options"] = "DENY"
        origin_url = urlsplit(runtime.settings.public_url or str(request.base_url))
        stream_origin = f"{'wss' if origin_url.scheme == 'https' else 'ws'}://{origin_url.netloc}"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://api.fontshare.com; "
            f"img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' {stream_origin}; "
            "font-src 'self' https://cdn.fontshare.com; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        )
        # Only content-addressed build assets are public/cacheable. HTML,
        # authenticated API data, unversioned files and errors remain no-store.
        immutable = response.status_code in {200, 304} and re.fullmatch(
            r"/assets/[^/]+-[A-Za-z0-9_-]{8,}\.(?:js|css|woff2?|png|svg|jpe?g|webp|ico)", request.url.path,
        )
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-store"
        return response

    @app.get("/health", operation_id="getHealth")
    async def health() -> HealthStatus:
        async with runtime.database.sessions() as db:
            await db.execute(select(1))
        return HealthStatus(status="ok", engine="pydantic-ai", durability="temporal")

    @app.post("/api/login", operation_id="loginWithPassword")
    async def login(body: Login, request: Request, response: Response) -> LoginResult:
        if runtime.product is not None:
            from .product_auth import password_login
            return LoginResult.model_validate(
                await password_login(runtime, body.password.get_secret_value(), request, response)
            )
        await runtime.database.limit_login(
            request.client.host if request.client else "unknown", body.username
        )
        async with runtime.database.sessions() as db:
            user = await db.scalar(select(User).where(User.username == body.username))
        valid = (
            user is not None and user.enabled
            and await asyncio.to_thread(check_password, body.password.get_secret_value(), user.password)
        )
        if not valid or user is None:
            raise HTTPException(401, "Username or password is incorrect.")
        token, csrf = runtime.security.issue_session(user.id, user.auth_version)
        response.set_cookie(
            session_cookie, token, httponly=True, samesite="strict",
            secure=runtime.settings.secure_cookies, max_age=7 * 86400,
            path=base_path(runtime.settings.public_url) or "/",
        )
        return LoginResult(csrf=csrf)

    @app.post("/api/logout", operation_id="logout")
    async def logout(response: Response, user: User = Depends(actor)) -> LogoutResult:
        response.delete_cookie(runtime.product.security.session_cookie if runtime.product is not None else session_cookie,
                               path=base_path(runtime.settings.public_url) or "/")
        if runtime.product is not None and runtime.product.settings.cloudflare_access_login:
            return LogoutResult(signed_out=True, logout_url="/cdn-cgi/access/logout")
        return LogoutResult(signed_out=True)

    @app.get("/api/auth/config", operation_id="getAuthConfig")
    async def auth_config() -> AuthConfig:
        product = runtime.product
        return AuthConfig(
            mode="existing" if product is not None else "standalone",
            password_enabled=bool(
                product.settings.password_login_enabled
                and (product.settings.workspace_password or product.settings.workspace_member_password)
                and not product.settings.cloudflare_access_login
            ) if product is not None else True,
            google_enabled=bool(product and product.settings.google_enabled()
                                and not product.settings.cloudflare_access_login),
            okta_enabled=bool(product and product.settings.okta_enabled()
                              and not product.settings.cloudflare_access_login),
            access_enabled=bool(product and product.settings.cloudflare_access_login),
        )

    @app.get("/api/me", operation_id="getMe")
    async def me(request: Request, user: User = Depends(actor)) -> Me:
        principal = getattr(request.state, "product_principal", None)
        if principal is not None:
            csrf = principal.csrf_token
        else:
            identity = runtime.security.read_session(request.cookies[session_cookie])
            if identity is None:
                raise HTTPException(401, "Your session expired.")
            csrf = identity["csrf"]
        return Me(
            user=MeUser(
                id=user.id, name=user.display_name, role=user.role,
                individual=principal.individual if principal is not None else True,
            ),
            csrf=csrf, models=runtime.settings.models,
            default_model=runtime.settings.default_model,
            preferences=await preferences_for_user(runtime.database, user),
            product_mode="existing" if runtime.product_identity is not None else "standalone",
        )

    @app.get("/api/sessions", operation_id="listSessions")
    async def sessions(
        archived: bool = False, deleted: bool = False, user: User = Depends(actor),
    ) -> list[SessionListItem]:
        rows = await runtime.database.list_sessions(user, archived=archived, deleted=deleted)
        async with runtime.database.sessions() as db:
            ids = [row.id for row in rows]
            preferences = {
                item.session_id: item for item in await db.scalars(
                    select(SessionPreference).where(SessionPreference.user_id == user.id, SessionPreference.session_id.in_(ids))
                )
            }
            participated = set(await db.scalars(select(Turn.session_id).where(
                Turn.actor_id == user.id, Turn.session_id.in_(ids),
            ).distinct()))
            latest = dict((await db.execute(select(Message.session_id, func.max(Message.id)).where(
                Message.session_id.in_(ids),
            ).group_by(Message.session_id))).all())
            reads = {item.session_id: item for item in await db.scalars(select(SessionRead).where(
                SessionRead.user_id == user.id, SessionRead.session_id.in_(ids),
            ))}
            owners = {row.id: await runtime.database.same_person(db, user, row.owner_id) for row in rows}
        return [SessionListItem.model_validate({
            **session_view(row), "can_manage": owners[row.id],
            "pinned": preferences[row.id].pinned if row.id in preferences else False,
            "folder": preferences[row.id].folder if row.id in preferences else "",
            "participated": row.owner_id != user.id and row.id in participated,
            "unread": bool(reads[row.id].unread or latest.get(row.id, 0) > reads[row.id].message_id)
            if row.id in reads else False,
        }) for row in rows]

    @app.post("/api/sessions", operation_id="createSession")
    async def create_session(body: NewSession, user: User = Depends(actor)) -> SessionSummary:
        return SessionSummary.model_validate(session_view(await runtime.database.create_session(user, body)))

    @app.get("/api/sessions/{session_id}", operation_id="getSession")
    async def session(
        session_id: str, request: Request, resolve: bool = False, include_activity: bool = True,
        include_messages: bool = False,
        user: User = Depends(actor),
    ) -> SessionDetail:
        return await session_reads.detail(
            session_id, request, user, resolve=resolve, include_activity=include_activity,
            include_messages=include_messages,
        )

    @app.get("/api/sessions/{session_id}/progress/{turn_id}", operation_id="getTurnProgress")
    async def progress(
        session_id: str, turn_id: str, request: Request,
        include_activity: bool = False, user: User = Depends(actor),
    ) -> TurnWorkProgress:
        return await session_reads.progress(
            session_id, turn_id, request, user, include_activity=include_activity,
        )

    @app.get("/api/sessions/{session_id}/progress/{turn_id}/actions/{action_id}", operation_id="getTurnAction")
    async def action_detail(
        session_id: str, turn_id: str, action_id: str, request: Request,
        agent_id: str | None = None, user: User = Depends(actor),
    ) -> WorkActionDetail:
        return await session_reads.action(
            session_id, turn_id, action_id, request, user, agent_id=agent_id,
        )

    @app.get("/api/sessions/{session_id}/turns/{turn_id}/failure", operation_id="getTurnFailureDetails")
    async def turn_failure_details(
        session_id: str, turn_id: str, request: Request, response: Response, user: User = Depends(actor),
    ) -> TurnFailureDetails:
        result = await session_reads.failure(session_id, turn_id, request, user)
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.get("/api/sessions/{session_id}/activity", operation_id="getSessionActivity")
    async def session_activity(session_id: str, request: Request, user: User = Depends(actor)) -> list[SavedActivity]:
        return await session_reads.activity(session_id, request, user)

    @app.get("/api/sessions/{session_id}/debug-trace")
    async def session_debug_trace(session_id: str, user: User = Depends(actor)):
        return JSONResponse(
            content=await debug_trace(runtime, user, session_id),
            headers={
                "Content-Disposition": f'attachment; filename="moyai-trace-{session_id}.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/sessions/{session_id}/messages/{message_id}/source")
    async def message_source(session_id: str, message_id: int, user: User = Depends(actor)):
        from .slack_permalink import permalink
        return RedirectResponse(await permalink(runtime, user, session_id, message_id),
                                status_code=302, headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions/{session_id}/slack-delivery/retry", operation_id="retrySlackDelivery")
    async def retry_slack_delivery(session_id: str, user: User = Depends(actor)) -> SlackDeliveryRetry:
        from .slack_delivery_status import retry
        return SlackDeliveryRetry.model_validate(await retry(runtime, user, session_id))

    @app.patch("/api/sessions/{session_id}", operation_id="updateSession")
    async def edit_session(session_id: str, body: SessionEdit, user: User = Depends(actor)) -> SessionUpdateResult:
        async with runtime.database.sessions.begin() as db:
            if body.archived is True:
                # Keep the same source/user/session lock order as goal admission.
                # Archive and goal suspension commit together before dispatch can claim work.
                await runtime.database.goals.lock(db, user, session_id)
            await lock_sidebar(db, user.id)
            row = await runtime.database.allowed_session(db, session_id, user)
            if body.title is not None or body.archived is not None:
                if not await runtime.database.same_person(db, user, row.owner_id):
                    raise HTTPException(403, "Only the owner can change this conversation.")
                if body.title is not None:
                    row.title = body.title
                if body.archived is not None:
                    row.archived = body.archived
                    if body.archived:
                        from .goal_models import Goal
                        goal = await db.get(Goal, session_id)
                        if goal is not None and goal.status == "active":
                            await runtime.database.goals.pause_continuation(
                                db, goal, "The conversation was archived. Unarchive it before resuming the goal.",
                            )
                row.updated_at = now()
            if body.pinned is not None or body.folder is not None:
                pref = await db.get(SessionPreference, (user.id, session_id))
                if pref is None:
                    pref = SessionPreference(user_id=user.id, session_id=session_id)
                    db.add(pref)
                if body.pinned is not None:
                    pref.pinned = body.pinned
                if body.folder is not None:
                    if body.folder and not any(item.name == body.folder for item in await folders_for(db, user.id)):
                        raise HTTPException(404, "Folder not found.")
                    pref.folder = body.folder
            if body.unread is not None:
                read = await db.get(SessionRead, (user.id, session_id))
                if read is None:
                    read = SessionRead(user_id=user.id, session_id=session_id, message_id=0)
                    db.add(read)
                read.unread = body.unread
                if not body.unread:
                    read.message_id = await db.scalar(select(func.max(Message.id)).where(Message.session_id == session_id)) or 0
        return SessionUpdateResult(updated=True)

    @app.delete("/api/sessions/{session_id}", operation_id="deleteSession")
    async def delete_session(session_id: str, user: User = Depends(actor)) -> SessionDeleteResult:
        await runtime.database.delete_session(user, session_id)
        return SessionDeleteResult(deleted=True)

    @app.post("/api/sessions/{session_id}/restore", operation_id="restoreSession")
    async def restore_session(session_id: str, user: User = Depends(actor)) -> SessionRestoreResult:
        await runtime.database.restore_session(user, session_id)
        return SessionRestoreResult(restored=True)

    @app.get("/api/sessions/{session_id}/messages", operation_id="listSessionMessages")
    async def messages(session_id: str, after: int = 0, user: User = Depends(actor)) -> list[SessionMessage]:
        return SESSION_MESSAGES.validate_python(await runtime.database.messages(user, session_id, after=after))

    async def cancel_stopped_workflow(workflow_id: str) -> None:
        # Stop intent has committed. The dispatcher keeps ownership until
        # cancellation/terminal state is acknowledged, including lost RPCs.
        try:
            async with asyncio.timeout(2):
                client = await runtime.connect()
                await client.get_workflow_handle(workflow_id).cancel()
        except (RPCError, TimeoutError, RuntimeError, OSError):
            logging.getLogger(__name__).warning("Stop cancellation remains pending for dispatcher reconciliation.")

    @app.post("/api/sessions/{session_id}/messages", status_code=202, operation_id="createSessionMessage")
    async def send(
        session_id: str, body: NewMessage, request: Request, user: User = Depends(actor),
    ) -> GoalStartedResult | TurnView | GoalCommandResult:
        from .goals import is_goal_command
        from .skill_invocations import has_invocations
        if is_goal_command(body.prompt):
            result = await runtime.database.goals.command(user, session_id, body)
            async with runtime.database.sessions() as db:
                stopped_workflow = await db.scalar(select(Turn.workflow_id).join(
                    Session, Session.active_turn == Turn.id,
                ).where(Session.id == session_id, Turn.status == "stopped"))
            if stopped_workflow:
                await cancel_stopped_workflow(stopped_workflow)
            return (GoalStartedResult if "id" in result else GoalCommandResult).model_validate(result)
        if body.reply_to is not None:
            async with runtime.database.sessions() as db:
                await runtime.database.allowed_session(db, session_id, user)
                pending = await db.get(Turn, body.reply_to)
                if pending is None or pending.session_id != session_id:
                    raise HTTPException(409, "That question is no longer available.")
            user = await answer_actor(runtime, user, pending)
        elif has_invocations(body.prompt):
            async with runtime.database.sessions() as db:
                session = await runtime.database.allowed_session(db, session_id, user)
            body = body.model_copy(update={"prompt": await runtime.skill_invocations.normalize(body.prompt, user, session)})
        return TurnView.model_validate(turn_view(await runtime.admission.submit(
            user, session_id, body, received_at=request.state.moyai_received_at, dot_accounts=True,
        )))

    @app.patch("/api/turns/{turn_id}", operation_id="updateQueuedTurn")
    async def edit_queued(turn_id: str, body: QueuedEdit, user: User = Depends(actor)) -> QueuedTurnUpdateResult:
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
            session = await runtime.database.allowed_session(db, turn.session_id, user)
            await runtime.database.swarms.editable_input(db, turn)
            await runtime.database.responsibilities.editable_input(db, turn)
            prompt = await runtime.skill_invocations.normalize(body.prompt, user, session)
            from .contracts import RunScope
            from .skill_invocations import gateway_connection_ids
            connections = json.loads(turn.connections)
            selections = list(dict.fromkeys([*connections, *gateway_connection_ids(prompt)]))
            if selections != connections:
                context = await runtime.database.execution_context(db, session, turn_id=turn.id)
                grants = await runtime.database.resolve_grants(
                    db, turn.actor_id, selections,
                    shared_session=context.visibility == "shared", surface=context.surface,
                )
                proposed = RunScope(
                    session.id, turn.id, turn.actor_id, turn.model, grants, session.visibility, session.surface,
                )
                if session.workspace and session.workspace_scope != proposed.workspace_key:
                    raise HTTPException(409, "Start a new conversation for the changed gateway connection.")
            from .models import MessageSource
            message = await db.scalar(select(Message).where(Message.identity == f"user:{turn.id}"))
            source = await db.get(MessageSource, message.identity) if message else None
            execution_prompt = prompt
            if message is not None:
                from .message_sources import public_message
                original, provenance = await public_message(runtime.database, db, message)
                if provenance and provenance.get("legacy_review_required"):
                    raise HTTPException(409, "Review this older message in Slack and send a correction instead.")
                if source is None:
                    source = MessageSource(message_identity=message.identity, kind=(provenance or {}).get("kind", "web"),
                                           revision=0, data=runtime.security.seal(provenance or {"kind": "web"}))
                    db.add(source)
                frozen = runtime.security.unseal(turn.prompt).decode()
                if not frozen.startswith(original):
                    raise HTTPException(409, "The original input is unavailable. Send a correction instead.")
                execution_prompt += frozen[len(original):]
                provenance = json.loads(runtime.security.unseal(source.data))
                provenance["edited_in"] = "web"
                source.data = runtime.security.seal(provenance)
                source.revision += 1
                source.mirror = True
            changed = await db.scalar(update(Turn).where(
                Turn.id == turn.id, Turn.status == "queued"
            ).values(
                prompt=runtime.security.seal(execution_prompt), connections=json.dumps(selections),
            ).returning(Turn.id))
            if changed is None:
                raise HTTPException(409, "The message was already admitted. Send a correction instead.")
            await db.execute(update(Message).where(
                Message.turn_id == turn.id, Message.role == "user"
            ).values(content=runtime.security.seal(prompt)))
        return QueuedTurnUpdateResult(updated=True)

    @app.post("/api/turns/{turn_id}/steer", status_code=202, operation_id="steerQueuedTurn")
    async def steer_queued(turn_id: str, user: User = Depends(actor)) -> TurnView:
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
        original = await answer_actor(runtime, user, turn)
        return TurnView.model_validate(turn_view(await runtime.database.steer_queued(original, turn_id)))

    @app.delete("/api/turns/{turn_id}", operation_id="cancelQueuedTurn")
    async def cancel_queued(turn_id: str, user: User = Depends(actor)) -> QueuedTurnCancelResult:
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
            await runtime.database.allowed_session(db, turn.session_id, user)
            await runtime.database.swarms.editable_input(db, turn)
            changed = await db.scalar(update(Turn).where(
                Turn.id == turn.id, Turn.status == "queued"
            ).values(status="stopped", finished_at=now()).returning(Turn.id))
            if changed is None:
                raise HTTPException(409, "The message was already admitted. Stop the active turn instead.")
        return QueuedTurnCancelResult(cancelled=True)

    @app.post("/api/sessions/{session_id}/stop", status_code=202, operation_id="stopSession")
    async def stop(session_id: str, user: User = Depends(actor)) -> SessionStopResult:
        workflow_id = await runtime.database.stop(user, session_id)
        if workflow_id:
            await cancel_stopped_workflow(workflow_id)
        return SessionStopResult(status="stopping")

    @app.post("/api/sessions/{session_id}/answers", status_code=202, operation_id="createSessionAnswer")
    async def answer(
        session_id: str, body: ApprovalReply, request: Request, user: User = Depends(actor),
    ) -> TurnView:
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            turn = await db.get(Turn, body.turn_id)
            if not turn or turn.session_id != session_id or not turn.conversation:
                raise HTTPException(409, "This conversation is not waiting for an answer.")
            conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(turn.conversation))
            pending = conversation.deferred_tool_requests
            if pending is None:
                raise HTTPException(409, "There are no pending requests.")
        resumed_actor = await answer_actor(runtime, user, turn)
        async with runtime.database.sessions() as db:
            calls, approvals = {}, {}
            if runtime.product_secrets is not None:
                from .product_slack_controls import stored_replies
                calls, approvals = await stored_replies(runtime, db, turn)
                calls.update(await runtime.product_secrets.execution.resolved_calls(db, turn, resumed_actor))
            questions = {call.tool_call_id: call for call in pending.calls}
            if body.browser_logins:
                from .browser_login import TOOL_NAME, login_result
                for identity, outcome in body.browser_logins.items():
                    call = questions.get(identity)
                    if call is None or call.tool_name != TOOL_NAME:
                        raise HTTPException(422, "Unknown browser sign-in request.")
                    calls[identity] = await login_result(
                        db, session_id, (pending.metadata or {}).get(identity) or {}, outcome, turn_id=turn.id,
                    )
            for identity, value in body.answers.items():
                call = questions.get(identity)
                if call is None or call.tool_name != "ask_user_question":
                    raise HTTPException(422, "Unknown question.")
                try:
                    calls[identity] = ask_user_result(
                        AskUserRequest.from_tool_call(call), TypeAdapter(AskUserResponse).validate_python(value)
                    )
                except ValueError as error:
                    raise HTTPException(422, "Provide a valid response to each question.") from error
            try:
                results = pending.build_results(approvals={**approvals, **body.approvals}, calls=calls)
                if pending.remaining(results) is not None:
                    raise ValueError("Deferred requests are incomplete.")
            except (ValueError, KeyError) as error:
                raise HTTPException(422, "Complete every current question, approval, and secure access request.") from error
        resumed = await runtime.database.resume(
            resumed_actor, session_id, turn.id, body.request_key,
            TypeAdapter(DeferredToolResults).dump_python(results, mode="json"),
            received_at=request.state.moyai_received_at,
        )
        return TurnView.model_validate(turn_view(resumed))

    @app.get(
        "/api/sessions/{session_id}/events", operation_id="streamSessionEvents", response_class=EventStream,
        responses={200: {"model": LiveEvent, "description": "Server-sent events; each data frame is one LiveEvent."}},
    )
    async def events(
        request: Request, session_id: str, turn_id: str, offset: int = 0,
        user: User = Depends(actor),
    ):
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
        client = await runtime.connect()
        try:
            cursor = max(offset, int(request.headers.get("last-event-id", "-1")) + 1, 0)
        except ValueError:
            raise HTTPException(422, "Invalid event cursor.") from None
        secret_values = [runtime.settings.gateway_key.get_secret_value()]
        if turn.preparation:
            preparation = json.loads(runtime.security.unseal(turn.preparation))
            for grant in preparation["scope"]["grants"]:
                if grant.get("credential") and not grant.get("policy", {}).get("product"):
                    secret_values.append(runtime.security.unseal(grant["credential"]).decode())

        async def stream():
            yield "event: connected\ndata: {}\n\n"
            try:
                handle = client.get_workflow_handle(turn.workflow_id)
                next_offset = cursor
                finished = False
                while not finished and not await request.is_disconnected():
                    previous_offset = next_offset
                    async with runtime.durability.stream_agent_events(
                        client, handle, from_offset=next_offset, topic=PUBLIC_TOPIC,
                    ) as native:
                        async for event in native:
                            next_offset = native.offset + 1
                            if isinstance(event, AgentRunResultEvent):
                                finished = is_turn_run(event.result.run_id, turn.id)
                            item = public_event(event)
                            if item is not None:
                                yield f"id: {native.offset}\ndata: " + json.dumps(
                                    redact(item, secret_values), ensure_ascii=False, default=str
                                ) + "\n\n"
                    # Workflow Streams can end its iterator normally when its
                    # poll is cancelled. Preserve our reader's cancellation:
                    # reopening here would keep the old API alive during drain.
                    reader = asyncio.current_task()
                    if reader is not None and reader.cancelling():
                        raise asyncio.CancelledError
                    if next_offset == previous_offset:
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("The live stream ended; the client can refresh saved state.", exc_info=True)
                # A completed workflow no longer serves Workflow Streams. Saved app state wins.
                yield 'event: refresh\ndata: {}\n\n'
            yield 'event: finished\ndata: {}\n\n'

        async def authorize_viewer():
            from .stream_authority import authorize_stream_viewer
            await authorize_stream_viewer(
                runtime.database, runtime.product_identity, request, user, session_id,
            )

        return StreamingResponse(
            authorized_events(
                stream(), authorize_viewer, request.is_disconnected,
                reconnect=getattr(request.state, "moyai_reconnect", None),
            ),
            media_type="text/event-stream",
        )

    @app.get("/api/connections", operation_id="listConnections")
    async def connections(
        user: User = Depends(actor), session_id: str | None = None, shared: bool = False,
    ) -> list[ConnectionView]:
        surface = "web"
        if session_id is not None:
            await runtime.database.prefetch_session_audiences(user, [session_id])
            async with runtime.database.sessions() as db:
                session = await runtime.database.allowed_session(db, session_id, user)
                context = await runtime.database.execution_context(db, session)
                shared, surface = context.visibility == "shared", context.surface
        if runtime.product_connections is not None:
            canonical = [ConnectionView.model_validate(item) for item in await runtime.product_connections.catalog(
                user.id, shared_session=shared, surface=surface,
            )]
        else:
            canonical = []
        async with runtime.database.sessions() as db:
            query = select(Connection).where(
                (Connection.owner_id == user.id) | Connection.shared.is_(True)
            )
            if runtime.product_connections is not None:
                from .personal_oauth import native_connections
                query = query.where(native_connections())
            rows = await db.scalars(query.order_by(Connection.id))
            return canonical + [
                ConnectionView.model_validate(connection_view(row)) for row in rows if not shared or row.shared
            ]

    @app.post("/api/connections", operation_id="createConnection")
    async def create_connection(body: ConnectionInput, user: User = Depends(actor)) -> ConnectionView:
        if runtime.product_connections is not None and body.kind not in {"mcp", "litellm"}:
            raise HTTPException(409, "Manage the existing product accounts in Connections.")
        if body.shared and user.role != "admin":
            raise HTTPException(403, "Only administrators can publish a shared connection.")
        if body.kind != "mcp" and not body.credential.get_secret_value():
            raise HTTPException(422, "Provide a credential.")
        if body.policy.mcp is not None:
            try:
                validate_mcp_endpoint(body.policy.mcp, runtime.settings.mcp_allowed_origins)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        if body.kind == "litellm":
            body.policy.gateway_discovery = await discover_gateway(body, user)
        async with runtime.database.sessions.begin() as db:
            row = Connection(
                id=new_id(), owner_id=user.id, kind=body.kind, name=body.name,
                credential=runtime.security.seal(body.credential.get_secret_value()),
                shared=body.shared, read_only=body.read_only, enabled=body.enabled,
                policy=body.policy.model_dump_json(),
            )
            db.add(row)
        return ConnectionView.model_validate(connection_view(row))

    @app.put("/api/connections/{connection_id}", operation_id="updateConnection")
    async def edit_connection(
        connection_id: str, body: ConnectionInput, user: User = Depends(actor),
    ) -> ConnectionView:
        if runtime.product_connections is not None and body.kind not in {"mcp", "litellm"}:
            raise HTTPException(409, "Manage the existing product accounts in Connections.")
        async with runtime.database.sessions.begin() as db:
            row = await db.get(Connection, connection_id)
            if row is None or row.owner_id != user.id:
                raise HTTPException(404, "Connection not found.")
            if ConnectionPolicy.model_validate_json(row.policy).oauth:
                raise HTTPException(409, "Manage this account through personal Connect and Disconnect.")
            if runtime.product_connections is not None and row.kind not in {"mcp", "litellm"}:
                raise HTTPException(409, "Manage the existing product accounts in Connections.")
            if body.kind != row.kind:
                raise HTTPException(422, "Create a new connection to change its service.")
            if (row.shared or body.shared) and user.role != "admin":
                raise HTTPException(403, "Only administrators can change a shared connection.")
            if body.kind == "litellm":
                previous = ConnectionPolicy.model_validate_json(row.policy)
                if (
                    previous.gateway and body.policy.gateway
                    and previous.gateway.url != body.policy.gateway.url
                    and not body.credential.get_secret_value()
                ):
                    raise HTTPException(422, "Enter a replacement key when changing the gateway URL.")
                # Discovery is server-authored; never accept a client-supplied catalog.
                body.policy.gateway_discovery = previous.gateway_discovery
                if body.enabled:
                    body.policy.gateway_discovery = await discover_gateway(
                        body, user, runtime.security.unseal(row.credential).decode(),
                    )
            if body.policy.mcp is not None:
                try:
                    validate_mcp_endpoint(body.policy.mcp, runtime.settings.mcp_allowed_origins)
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
                previous = ConnectionPolicy.model_validate_json(row.policy)
                if (
                    previous.mcp is not None and previous.mcp.url != body.policy.mcp.url
                    and not body.credential.get_secret_value() and not body.clear_credential
                    and runtime.security.unseal(row.credential)
                ):
                    raise HTTPException(422, "Replace or clear the saved credential when changing the MCP endpoint.")
            values: dict[str, Any] = {
                "name": body.name, "enabled": body.enabled,
                "shared": body.shared, "read_only": body.read_only,
                "policy": body.policy.model_dump_json(), "revision": Connection.revision + 1,
            }
            if body.clear_credential:
                values["credential"] = runtime.security.seal("")
            elif body.credential.get_secret_value():
                values["credential"] = runtime.security.seal(body.credential.get_secret_value())
            # Concurrent edits must not share a revision or invalidate the
            # endpoint/retained-token decision made against this exact row.
            changed = await db.scalar(update(Connection).where(
                Connection.id == row.id, Connection.revision == row.revision,
            ).values(**values).returning(Connection.id).execution_options(synchronize_session=False))
            if changed is None:
                raise HTTPException(409, "The connection changed while saving. Refresh before trying again.")
            await db.refresh(row)
        return ConnectionView.model_validate(connection_view(row))

    @app.get("/api/gateway/defaults", operation_id="getGatewayDefaults")
    async def gateway_defaults(user: User = Depends(actor)) -> GatewayDefaults:
        # Only prefill the URL. The deployment's inference key never authorizes
        # personal tools or skills, and is never sent to the browser.
        try:
            url = GatewayConfig(url=runtime.settings.gateway_url).url
        except ValueError:
            url = ""
        return GatewayDefaults(url=url)

    async def discover_gateway(body: ConnectionInput, user: User, previous_key: str = "") -> GatewayDiscovery:
        from .gateway import GatewayClient

        assert body.policy.gateway is not None
        try:
            validate_mcp_endpoint(body.policy.gateway.endpoint_config(), runtime.settings.mcp_allowed_origins)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

        async def authorize() -> None:
            current = await runtime.database.user(user.id)
            if current.auth_version != user.auth_version or current.role != user.role:
                raise HTTPException(401, "Your account changed. Sign in again.")
            if runtime.product_identity is not None:
                await runtime.product_identity.require_current(getattr(user, "product_grant", None))

        client = GatewayClient(
            body.policy.gateway, body.credential.get_secret_value() or previous_key,
            runtime.settings.mcp_allowed_origins, authorize,
        )
        result = await client.discover()
        await authorize()
        return result

    @app.get("/api/sessions/{session_id}/files", operation_id="listSessionFiles")
    async def files(session_id: str, user: User = Depends(actor)) -> list[SessionFile]:
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            rows = await db.scalars(select(Artifact).where(Artifact.session_id == session_id))
            return [SessionFile(
                id=row.id, name=row.name, media_type=row.media_type,
                size=row.size, created_at=row.created_at,
            ) for row in rows]

    @app.get("/api/sessions/{session_id}/plan", operation_id="getSessionPlan")
    async def plan(session_id: str, user: User = Depends(actor)) -> list[PlanStep]:
        await runtime.database.session(user, session_id)
        return [
            PlanStep.model_validate(item.model_dump(mode="json"))
            for item in await runtime.stores.plan(session_id).get_items()
        ]

    @app.get("/api/sessions/{session_id}/effects", operation_id="listSessionEffects")
    async def effects(session_id: str, user: User = Depends(actor)) -> list[PendingEffect]:
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            rows = await db.scalars(select(Effect).where(
                Effect.session_id == session_id,
                Effect.state == "pending",
                ~failed_action_attempt(),
            ).order_by(Effect.created_at))
            return [PendingEffect(
                id=row.id, turn_id=row.turn_id, tool_name=row.tool_name,
                arguments=json.loads(runtime.security.unseal(row.arguments)),
                created_at=row.created_at,
            ) for row in rows if await runtime.database.same_person(db, user, row.actor_id)]

    @app.post("/api/effects/{effect_id}/resolve", operation_id="resolveEffect")
    async def resolve_effect(
        effect_id: str, body: EffectResolution, user: User = Depends(actor),
    ) -> EffectResolveResult:
        async with runtime.database.sessions() as db:
            session_id = await db.scalar(select(Effect.session_id).where(Effect.id == effect_id))
        if session_id is not None:
            await runtime.database.prefetch_session_audiences(user, [session_id])
        async with runtime.database.sessions.begin() as db:
            row = await db.get(Effect, effect_id)
            if row is None or not await runtime.database.same_person(db, user, row.actor_id):
                raise HTTPException(404, "Action not found.")
            session = await runtime.database.allowed_session(db, row.session_id, user)
            if session.active_turn:
                raise HTTPException(409, "Stop or finish the active task before reconciling its actions.")
            if row.state != "pending":
                raise HTTPException(409, "This action already has a recorded outcome.")
            row.state = f"reviewed_{body.outcome}"
            row.resolution = runtime.security.seal({
                "reviewer": user.id, "outcome": body.outcome, "evidence": body.evidence,
            })
            row.settled_at = now()
            db.add(Message(
                session_id=row.session_id, turn_id=row.turn_id, author_id=user.id,
                role="user", identity=f"effect-resolution:{row.id}",
                content=runtime.security.seal(
                    f"Action reconciliation by the requester: {row.tool_name} was "
                    f"{body.outcome.replace('_', ' ')}. Evidence: {body.evidence}"
                ),
            ))
            # Rebuild from the public conversation so the next run sees this evidence.
            session.last_grants = None
        return EffectResolveResult(recorded=True)

    @app.get("/api/files/{artifact_id}")
    async def download(artifact_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            row = await db.get(Artifact, artifact_id)
            if row is None:
                raise HTTPException(404, "File not found.")
            await runtime.database.allowed_session(db, row.session_id, user)
        content = await runtime.objects.get(row.digest)
        current = await runtime.database.user(user.id)
        if current.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        await runtime.database.session(user, row.session_id)
        return Response(
            content, media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(Path(row.name).name)}"},
        )

    if STATIC.is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/static/models/moyai.glb")
    async def sculpture():
        return FileResponse(STATIC / "moyai.glb", media_type="model/gltf-binary")

    @app.get("/{path:path}")
    async def frontend(path: str):
        index = STATIC / "index.html"
        if index.is_file():
            html = index.read_text().replace("<!--moyai-base-->",
                f'<base href="{app_path(runtime.settings.public_url)}">')
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        raise HTTPException(503, "Build the web application with npm run build in native/web.")

    app.add_middleware(PublicPathMiddleware, public_url=runtime.settings.public_url)
    return app
