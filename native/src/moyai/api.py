"""Moyai's authenticated product API around native conversations and workflows."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, TypeAdapter, field_validator
from pydantic_ai import ConversationTypeAdapter, DeferredToolResults
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse, ask_user_result
from sqlalchemy import func, select, update
from sqlalchemy.orm import load_only
from temporalio.service import RPCError

from .api_views import (
    AuthConfig,
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
)
from .computer import routes as computer_routes
from .connection_policy import ConnectionPolicy
from .contracts import ApprovalReply, EffectResolution, NewMessage, NewSession, QueuedEdit, SessionEdit
from .debug_trace import debug_trace
from .failed_action_attempts import failed_action_attempt
from .failure_diagnostics import failure_details
from .file_preview import routes as file_preview_routes
from .general_preferences import GeneralPreferences, general_preferences
from .history_handoff import is_turn_run, latest_turn_snapshot
from .images import routes as image_routes
from .live_events import PUBLIC_TOPIC
from .live_stream import authorized_events
from .models import (
    Artifact,
    Connection,
    Effect,
    Event,
    Message,
    Session,
    SessionPreference,
    SessionRead,
    Turn,
    User,
    new_id,
    now,
)
from .presentation import public_event, redact, saved_activity
from .product_identity import ProductAuthorityBusy
from .product_requester import answer_actor, can_answer
from .pull_requests import routes as pull_request_routes
from .runtime import Runtime
from .security import check_password
from .settings import Settings
from .settings_api import routes as settings_routes
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
    kind: Literal["github", "linear", "notion", "slack"]
    credential: SecretStr
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


SAVED_ACTIVITY = TypeAdapter(list[SavedActivity])
SESSION_MESSAGES = TypeAdapter(list[SessionMessage])


class EventStream(StreamingResponse):
    media_type = "text/event-stream"


def session_view(session: Session) -> dict[str, Any]:
    from .slack_messages import plain_slack_title

    return {
        "id": session.id, "title": plain_slack_title(session.title) if session.surface == "slack" else session.title, "owner_id": session.owner_id,
        "visibility": session.visibility, "surface": session.surface,
        "model": session.model, "status": session.status,
        "active_turn": session.active_turn, "archived": session.archived, "deleted": session.deleted,
        "workspace": json.loads(session.workspace) if session.workspace else None,
        "save_warning": session.save_warning, "updated_at": session.updated_at,
    }


def turn_view(turn: Turn) -> dict[str, Any]:
    return {
        "id": turn.id, "session_id": turn.session_id, "actor_id": turn.actor_id,
        "model": turn.model, "status": turn.status, "mode": turn.mode,
        "parent_turn": turn.parent_turn, "enqueue_id": turn.enqueue_id,
        "connections": json.loads(turn.connections),
        "accepted_at": turn.accepted_at, "usage": json.loads(turn.usage),
        "error": turn.error, "created_at": turn.created_at, "finished_at": turn.finished_at,
    }


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
    work_progress = runtime.work_progress

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
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            work_progress.clear()
            if owned:
                await runtime.close()

    app = FastAPI(title="Moyai", lifespan=lifespan)
    app.state.runtime = runtime
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
        token = request.cookies.get(COOKIE, "")
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

    app.include_router(image_routes(runtime, actor))
    app.include_router(computer_routes(runtime, actor, cookie_name=COOKIE))
    app.include_router(sidebar_routes(runtime, actor))
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
    if runtime.product_billing is not None:
        from .product_billing_routes import routes as product_billing_routes
        app.include_router(product_billing_routes(runtime.product_billing, runtime=runtime))
    if runtime.product_automations is not None:
        from .product_automations import routes as product_automation_routes
        app.include_router(product_automation_routes(runtime.product_automations))
    from .product_routes import routes as product_routes
    app.include_router(product_routes(runtime, actor))
    app.include_router(settings_routes(runtime, actor))
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
        if not hasattr(request.state, "moyai_received_at"):
            request.state.moyai_received_at = now()
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.startswith("/hooks/"):
            origin = request.headers.get("origin")
            expected = runtime.settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")
            if origin and origin.rstrip("/") != expected:
                return Response("Origin is not allowed.", status_code=403)
        if runtime.product is not None and (
            request.url.path not in {"/health", "/healthz"}
            and not request.url.path.startswith(("/hooks/", "/broker/native/"))
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
                        request.url.path + ("?" + request.url.query if request.url.query else ""),
                        status_code=303,
                    ) if request.method in {"GET", "HEAD"} else
                    JSONResponse({"detail": "Refresh your organization sign-in."}, status_code=401)
                )
                response.raw_headers.append((b"set-cookie", access_cookie))
                response.headers["Cache-Control"] = "no-store"
                return response
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
        response.headers["Referrer-Policy"] = "same-origin"
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
            COOKIE, token, httponly=True, samesite="strict",
            secure=runtime.settings.secure_cookies, max_age=7 * 86400,
        )
        return LoginResult(csrf=csrf)

    @app.post("/api/logout", operation_id="logout")
    async def logout(response: Response, user: User = Depends(actor)) -> LogoutResult:
        response.delete_cookie("workspace_session" if runtime.product is not None else COOKIE)
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
            access_enabled=bool(product and product.settings.cloudflare_access_login),
        )

    @app.get("/api/me", operation_id="getMe")
    async def me(request: Request, user: User = Depends(actor)) -> Me:
        principal = getattr(request.state, "product_principal", None)
        if principal is not None:
            csrf = principal.csrf_token
        else:
            identity = runtime.security.read_session(request.cookies[COOKIE])
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
            preferences=GeneralPreferences.model_validate(general_preferences(user.preferences)),
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
        reference = None
        if resolve and runtime.product_identity is not None:
            from .product_routes import ProductReadService, Reader
            # actor has just verified the product principal. The read adapter
            # still fences source/native ACLs and canonical continuation aliases.
            reference = (await ProductReadService(runtime).detail(
                Reader(user, request.state.product_principal), session_id, "native",
            ))["session"]
        await runtime.database.prefetch_session_audiences(user, [session_id])
        async with runtime.database.sessions() as db:
            row = await runtime.database.allowed_session(db, session_id, user, metadata_only=True)
            # Only a waiting conversation needs its private checkpoint to render
            # pending questions. Ordinary reads must not transfer megabytes of it.
            if row.status == "waiting":
                # Keep the state and active turn aligned with this checkpoint if
                # execution advanced while the source audience was checked.
                await db.refresh(row, ["status", "active_turn", "conversation"])
        turn_columns = load_only(
            Turn.id, Turn.session_id, Turn.actor_id, Turn.model, Turn.status, Turn.mode,
            Turn.parent_turn, Turn.enqueue_id, Turn.connections, Turn.accepted_at, Turn.usage,
            Turn.error, Turn.created_at, Turn.finished_at,
        )
        async with runtime.database.sessions() as db:
            turns = list((await db.scalars(select(Turn).where(
                Turn.session_id == session_id
            ).options(turn_columns).order_by(Turn.created_at.desc()).limit(100))).all())
            if row.active_turn and not any(turn.id == row.active_turn for turn in turns):
                active_turn = await db.get(Turn, row.active_turn, options=[turn_columns])
                if active_turn is not None:
                    turns.append(active_turn)
            guidance = dict((await db.execute(select(Event.turn_id, Event.created_at).where(
                Event.turn_id.in_([turn.id for turn in turns]), Event.kind == "steering_admitted",
            ).order_by(Event.id))).all())
            # Resolve each relationship once for this projection/transaction.
            # A linked Slack author may own many turns; repeating its admission
            # and identity reads per turn adds hundreds of database round trips.
            owners = {owner: await runtime.database.same_person(db, user, owner)
                      for owner in {row.owner_id, *(turn.actor_id for turn in turns)}}
            result: dict[str, Any] = {
                "session": {**session_view(row), "can_manage": owners[row.owner_id]}, "turns": [
                    {**turn_view(turn), "guidance_at": guidance.get(turn.id),
                     "is_self": owners[turn.actor_id]} for turn in turns
                ],
            }
            if include_messages:
                from .models import MessageSource
                version = (await db.execute(select(
                    func.coalesce(func.max(Message.id), 0), func.count(MessageSource.message_identity),
                    func.coalesce(func.sum(MessageSource.revision), 0),
                ).outerjoin(MessageSource, MessageSource.message_identity == Message.identity).where(
                    Message.session_id == session_id,
                ))).one()
                authors = (await db.execute(select(User.id, User.display_name, User.auth_version).join(
                    Message, Message.author_id == User.id,
                ).where(Message.session_id == session_id).distinct().order_by(User.id))).all()
                identities = [(author.id, author.display_name, author.auth_version,
                               await runtime.database.same_person(db, user, author.id)) for author in authors]
                identity_version = hashlib.sha256(json.dumps(identities).encode()).hexdigest()
                result["transcript_version"] = ":".join(str(part) for part in (*version, identity_version))
            if row.surface == "slack":
                from .slack_delivery_status import status as delivery_status
                result["slack_delivery"] = await delivery_status(runtime.database, db, session_id)
            swarm_member = await runtime.database.swarms.member(db, session_id)
            from .goal_models import Goal
            goal = await db.get(Goal, session_id)
            result["goal"] = runtime.database.goals.view(goal)
            if goal is not None and result["goal"] is not None:
                result["goal"]["elapsed_seconds"] = await runtime.database.goals.elapsed_seconds(db, goal)
            if swarm_member is not None:
                result["session"]["swarm_id"] = swarm_member.mission_id
            from .delegation_models import DelegatedSession
            delegation = await db.get(DelegatedSession, session_id)
            if delegation is not None:
                try:
                    await runtime.database.allowed_session(db, delegation.parent_session_id, user, metadata_only=True)
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
                else:
                    result["session"]["parent_session_id"] = delegation.parent_session_id
            if reference is not None:
                result["reference"] = reference
            preference = await db.get(SessionPreference, (user.id, session_id))
            result["session"]["pinned"] = bool(preference and preference.pinned)
            result["session"]["folder"] = preference.folder if preference else ""
            read = await db.get(SessionRead, (user.id, session_id))
            result["session"]["unread"] = bool(read and read.unread)
            active_turn = next((turn for turn in turns if turn.id == row.active_turn), None)
            unsettled = list((await db.scalars(select(Effect).where(
                Effect.session_id == session_id, Effect.state == "pending",
                ~failed_action_attempt(),
            ))).all())
            result["has_unsettled_effects"] = bool(unsettled)
            own_turns = {turn.id for turn, view in zip(turns, result["turns"]) if view["is_self"]}
            result["effects"] = [{
                "id": effect.id, "turn_id": effect.turn_id, "tool_name": effect.tool_name,
                "arguments": json.loads(runtime.security.unseal(effect.arguments)),
            } for effect in unsettled if await runtime.database.same_person(db, user, effect.actor_id)]
        if include_activity and turns and turns[0].id in own_turns and turns[0].status != "running":
            snapshot = await latest_turn_snapshot(runtime, turns[0].id)
            if snapshot is not None:
                result["activity"] = saved_activity(snapshot.messages, turns[0].id)
        if row.status == "waiting" and row.conversation and active_turn is not None:
            # Requester-link checks take their own transaction. Do not hold a
            # native connection while awaiting them (including one-slot pools).
            result["can_answer"] = await can_answer(runtime, user, active_turn)
            if result["can_answer"]:
                conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(row.conversation))
                result["pending"] = TypeAdapter(Any).dump_python(conversation.deferred_tool_requests, mode="json")
                from .questions import can_reply_with_text
                result["can_reply_with_text"] = can_reply_with_text(conversation.deferred_tool_requests)
                async with runtime.database.sessions() as db:
                    deadline = await db.scalar(select(Event).where(
                        Event.identity == f"optional-question:{active_turn.id}",
                    ))
                if deadline is not None:
                    result["question_deadline"] = json.loads(runtime.security.unseal(deadline.content))["deadline"]
            elif runtime.product_secrets is not None and user.role == "admin":
                # Organization access assistance does not confer permission to
                # answer another requester's questions or approve their tools.
                requests = await runtime.product_secrets.execution.pending(request.state.product_principal, session_id)
                result["credential_requests"] = [
                    {"request_id": item["request_id"]} for item in requests["requests"]
                ]
        if include_messages:
            result["messages"] = await runtime.database.messages(user, session_id)
            mention_names = [(message['id'], message.get('slack_mentions', {})) for message in result['messages']]
            result['transcript_version'] += ':' + hashlib.sha256(
                json.dumps(mention_names, sort_keys=True).encode(),
            ).hexdigest()
        return SessionDetail.model_validate(result)

    @app.get("/api/sessions/{session_id}/progress/{turn_id}", operation_id="getTurnProgress")
    async def progress(
        session_id: str, turn_id: str, request: Request,
        include_activity: bool = False, user: User = Depends(actor),
    ) -> TurnWorkProgress:
        from .progress import turn_progress

        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
            if not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(403, "Working activity is visible only to its requester.")
        result = (
            await work_progress.read(turn) if include_activity
            else {"messages": await turn_progress(runtime, turn)}
        )
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        if runtime.product_identity is not None:
            await runtime.product_identity.require_request_current(request)
        await runtime.database.session(user, session_id)
        return TurnWorkProgress.model_validate(result)

    @app.get("/api/sessions/{session_id}/progress/{turn_id}/actions/{action_id}", operation_id="getTurnAction")
    async def action_detail(
        session_id: str, turn_id: str, action_id: str, request: Request,
        agent_id: str | None = None, user: User = Depends(actor),
    ) -> WorkActionDetail:
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
            if not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(403, "Working activity is visible only to its requester.")
        result, source_session = await work_progress.action(turn, action_id, agent_id)
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        if runtime.product_identity is not None:
            await runtime.product_identity.require_request_current(request)
        await runtime.database.session(user, session_id)
        if source_session != session_id:
            await runtime.database.session(user, source_session)
        return WorkActionDetail.model_validate(result)

    @app.get("/api/sessions/{session_id}/turns/{turn_id}/failure", operation_id="getTurnFailureDetails")
    async def turn_failure_details(
        session_id: str, turn_id: str, request: Request, response: Response, user: User = Depends(actor),
    ) -> TurnFailureDetails:
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id or not turn.error:
                raise HTTPException(404, "Failure not found.")
            if not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(403, "Failure details are visible only to the requester.")
        details = await failure_details(runtime, turn)
        fresh = await runtime.database.user(user.id)
        if fresh.auth_version != user.auth_version:
            raise HTTPException(401, "Your session expired.")
        if runtime.product_identity is not None:
            await runtime.product_identity.require_request_current(request)
        await runtime.database.session(user, session_id)
        response.headers["Cache-Control"] = "no-store"
        return TurnFailureDetails.model_validate(details)

    @app.get("/api/sessions/{session_id}/activity", operation_id="getSessionActivity")
    async def session_activity(session_id: str, user: User = Depends(actor)) -> list[SavedActivity]:
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = (await db.execute(select(Turn.id, Turn.actor_id, Turn.status).where(
                Turn.session_id == session_id,
            ).order_by(Turn.created_at.desc()).limit(1))).first()
            own = turn is not None and await runtime.database.same_person(db, user, turn.actor_id)
        if turn is not None and own and turn.status != "running":
            snapshot = await latest_turn_snapshot(runtime, turn.id)
            if snapshot is not None:
                return SAVED_ACTIVITY.validate_python(saved_activity(snapshot.messages, turn.id))
        return []

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
        return TurnView.model_validate(turn_view(await runtime.database.submit(
            user, session_id, body, received_at=request.state.moyai_received_at,
        )))

    @app.patch("/api/turns/{turn_id}", operation_id="updateQueuedTurn")
    async def edit_queued(turn_id: str, body: QueuedEdit, user: User = Depends(actor)) -> QueuedTurnUpdateResult:
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(404, "Message not found.")
            session = await runtime.database.allowed_session(db, turn.session_id, user)
            await runtime.database.swarms.editable_input(db, turn)
            prompt = await runtime.skill_invocations.normalize(body.prompt, user, session)
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
            ).values(prompt=runtime.security.seal(execution_prompt)).returning(Turn.id))
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
                        db, session_id, (pending.metadata or {}).get(identity) or {}, outcome,
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
            if not await runtime.database.same_person(db, user, turn.actor_id):
                raise HTTPException(403, "Working activity is visible only to its requester.")
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
                runtime.database, runtime.product_identity, request, user, session_id, turn.actor_id,
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
            return [ConnectionView.model_validate(item) for item in await runtime.product_connections.catalog(
                user.id, shared_session=shared, surface=surface,
            )]
        async with runtime.database.sessions() as db:
            rows = await db.scalars(select(Connection).where(
                (Connection.owner_id == user.id) | Connection.shared.is_(True)
            ))
            return [ConnectionView.model_validate(connection_view(row)) for row in rows if not shared or row.shared]

    @app.post("/api/connections", operation_id="createConnection")
    async def create_connection(body: ConnectionInput, user: User = Depends(actor)) -> ConnectionView:
        if runtime.product_connections is not None:
            raise HTTPException(409, "Manage the existing product accounts in Connections.")
        if body.shared and user.role != "admin":
            raise HTTPException(403, "Only administrators can publish a shared connection.")
        if not body.credential.get_secret_value():
            raise HTTPException(422, "Provide a credential.")
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
        if runtime.product_connections is not None:
            raise HTTPException(409, "Manage the existing product accounts in Connections.")
        async with runtime.database.sessions.begin() as db:
            row = await db.get(Connection, connection_id)
            if row is None or row.owner_id != user.id:
                raise HTTPException(404, "Connection not found.")
            if body.kind != row.kind:
                raise HTTPException(422, "Create a new connection to change its service.")
            if (row.shared or body.shared) and user.role != "admin":
                raise HTTPException(403, "Only administrators can change a shared connection.")
            row.name, row.enabled = body.name, body.enabled
            row.shared, row.read_only = body.shared, body.read_only
            if body.credential.get_secret_value():
                row.credential = runtime.security.seal(body.credential.get_secret_value())
            row.policy = body.policy.model_dump_json()
            row.revision += 1
        return ConnectionView.model_validate(connection_view(row))

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
            return FileResponse(index)
        raise HTTPException(503, "Build the web application with npm run build in native/web.")

    return app
