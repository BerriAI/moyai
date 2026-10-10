import asyncio
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .connectors import Connectors, ConnectorError, TOOLS, RETRY_SAFE_READS
from .db import Store, now, database
from .blob_storage import ObjectStorage
from .runner import RunManager, TERMINAL, completed_response, response_status
from .persistence import Checkpoints, restore_checkpoint
from .security import Security, digest
from .cloudflare_access import CloudflareAccess, CloudflareAccessMiddleware
from .google_sso import GoogleSignIn
from .user_roles import UserRoles
from .user_preferences import UserPreferences
from .model_preferences import preferred_model, save_model
from .model_selection import ASTRA_ULTRAFAST, gateway_model
from .access_logging import configure_access_logging
from .broker_diagnostics import BrokerDiagnosticsMiddleware, model_gateway_error, upstream_headers, model_error, model_stage
from .workspace_diagnostics import DIAGNOSTIC_TOOL, DiagnosticArgs, inspect_workspace
from .slack import SlackSessions
from .spend import Spend, UsageCapture, completion_events
from .identities import SlackIdentities
from .agents import AgentCoordinator, TOOLS as AGENT_TOOLS
from .github_setup import routes as github_routes
from .credentials import Credentials, Invoke, Materialize, TOOLS as CREDENTIAL_TOOLS
from .skills import Skills, TOOL_NAMES as SKILL_TOOLS
from .memory import Memory, TOOL_NAMES as MEMORY_TOOLS
from .session_folders import SessionFolders
from .session_lifecycle import SessionLifecycle
from .session_metadata import is_session_id_request
from .session_pull_requests import SessionPullRequests
from sandbox.memory_history import scrub_memory_history
from .environments import Environments
from .tracing import AgentTracing
from .lens_feedback import FeedbackNotConfigured, LensFeedback
from .attachments import MAX_FILES, upload_limit
from .artifact_files import routes as artifact_file_routes
from .automations import Automations
from .computer import Computer
from . import captures
from .media_shares import MediaShares, TOOLS as MEDIA_TOOLS
from sandbox.broker_transport import CONTENT_TYPE, MAX_BODY, body_limit, wire_limit, unseal

STATIC = Path(__file__).parent / "static"
Provider = Literal["linear", "slack", "notion", "github"]
AttachmentId = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]


class NewRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    harness: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str = Field(default="", max_length=16000)
    repo_url: str = Field(default="", max_length=500)
    github_repository_id: int | None = Field(default=None, gt=0, strict=True)
    mode: Literal["demo", "modal"] = "demo"
    plugins: list[Provider] = Field(default_factory=list, max_length=4)
    environment_id: str = Field(default="auto", pattern=r"^(auto|none|[0-9a-f]{32})$")
    chat_enabled: bool = True
    model: str | None = Field(default=None, max_length=120)
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=MAX_FILES)
    client_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{8,80}$')
    side_chat_of: str = Field(default='', pattern=r'^([0-9a-f]{32})?$')

    @model_validator(mode='after')
    def require_input(self):
        if not self.prompt.strip() and self.attachment_ids:
            self.prompt = 'Please respond to the attached files and audio transcripts.'
        if len(self.prompt.strip()) < 3:
            raise ValueError('Enter a message or attach a file or recording.')
        return self

    @field_validator("repo_url")
    @classmethod
    def repository(cls, value):
        value = value.strip().removesuffix("/")
        if value and not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            raise ValueError("Use a https://github.com/owner/repository URL.")
        return value

    @field_validator("prompt")
    @classmethod
    def prompt_text(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("Enter a task of at least three characters.")
        return value.strip()


class TokenConnection(BaseModel):
    token: str = Field(min_length=8, max_length=4096)


class Decision(BaseModel):
    decision: Literal["approve", "deny"]


class ToolCall(BaseModel):
    name: str = Field(max_length=80)
    arguments: dict


class Login(BaseModel):
    password: str = Field(max_length=4096)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    content: str = Field(default="", max_length=16000)
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    model: str | None = Field(default=None, max_length=120)
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=MAX_FILES)
    send_now: bool = False

    @model_validator(mode='after')
    def require_input(self):
        if not self.content and self.attachment_ids:
            self.content = 'Please respond to the attached files and audio transcripts.'
        if not self.content:
            raise ValueError('Enter a message or attach a file or recording.')
        return self


class QueueChange(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    revision: int = Field(ge=0)
    action: Literal['edit', 'delete', 'steer']
    content: str | None = Field(default=None, min_length=1, max_length=16000)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    score: int = Field(ge=0, le=10)
    comment: str = Field(default='', max_length=10000)


class ConnectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    read_only: bool


class OrganizationName(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)


def create_app(settings: Settings | None = None):
    configure_access_logging()
    settings = settings or Settings()
    restore_checkpoint(settings)
    store = Store(settings.data_dir, default_model=settings.resolve_model(), auto_link_identities=settings.slack_identity_linking_enabled,
                  max_pending_runs=settings.max_pending_runs, object_storage=ObjectStorage(settings),
                  database_url=settings.moyai_database_url, database_schema=settings.moyai_database_schema,
                  database_initialize=settings.moyai_database_initialize, application_instance=True,
                  database_pool_size=settings.moyai_database_pool_size, runtime_role=settings.moyai_runtime_role,
                  runtime_settings=settings, schema_mode=settings.moyai_schema_mode)
    try:
        if settings.moyai_runtime_role != 'standalone':
            from .runtime_coordination import require_shared_artifacts
            require_shared_artifacts(store)
        app = _create_app(settings, store)
        if store.database:
            # Publish a changed policy only after every component has completed
            # schema setup; joining workers remain fenced until this succeeds.
            store.database.configure_runtime(settings)
        return app
    except BaseException:
        store.close()
        raise


def _create_app(settings, store):
    user_roles = UserRoles(store, settings)
    security = Security(settings, user_roles)
    from .sandbox_settings import SandboxSettings
    sandbox_settings = SandboxSettings(store, settings, security)
    connectors = Connectors(store, security, settings)
    session_pull_requests = SessionPullRequests(connectors.github)
    if settings.temporal_enabled:
        from .temporal_runtime import TemporalRunManager
        manager = TemporalRunManager(store, settings)
    else:
        manager = RunManager(store, settings)
    checkpoints = Checkpoints(store, settings)
    user_preferences = UserPreferences(store, security, checkpoints)
    environments = Environments(store, settings, security, manager, connectors, checkpoints)
    manager.environments = environments
    spend = Spend(store, settings, security, checkpoints)
    infrastructure = spend.infrastructure
    sandbox_settings.modal_clients = infrastructure.modal_clients = manager.modal_clients
    coordinator = AgentCoordinator(store, settings, manager)
    tracing = AgentTracing(store, settings, preferences=user_preferences)
    store.tracing = tracing
    manager.coordinator = coordinator
    credentials = Credentials(store, security, settings, manager, checkpoints)
    manager.credentials = credentials
    connectors.github_requester_match = credentials.same_requester
    skills = Skills(store, security, credentials.same_requester)
    memory = Memory(store, security, credentials.same_requester, checkpoints)
    from .model_slots import ModelSlots
    model_slots = ModelSlots(settings.max_concurrent_model_requests)
    from .memory_review import MemoryReview
    memory_review = MemoryReview(memory, settings, spend, model_slots)
    memory.reviewer = store.memory_review = memory_review
    from .context_budget import ContextBudget, ContextPressure, provider_context_rejection
    context_budget = ContextBudget(settings)
    lens_feedback = LensFeedback(store, settings)
    credentials.slots = model_slots
    manager.persist = checkpoints.flush
    if store.schema_updates:
        store.execute("INSERT INTO organization(id,name) VALUES(1,?) ON CONFLICT DO NOTHING", (settings.organization_name,))
    slack = SlackSessions(store, connectors, manager, checkpoints, settings, lens_feedback=lens_feedback)
    from .session_titles import SessionTitles
    session_titles = SessionTitles(store, settings, checkpoints)
    slack.session_titles = session_titles
    from .message_queue import MessageQueue
    message_queue = MessageQueue(store, slack.chat.change_queued_in)
    identities = SlackIdentities(store, connectors, settings, security, checkpoints, credentials.same_requester)
    connectors.slack_identities = identities
    slack.identities = identities
    manager.prepare_context = slack.prepare
    automations = Automations(store, settings, security, manager, connectors, environments, checkpoints)
    from .automation_tools import AutomationTools, TOOL_NAMES as AUTOMATION_TOOLS
    automation_tools = AutomationTools(automations, credentials.same_requester, credentials)
    from .model_tools import ModelTools, TOOL_NAMES as MODEL_TOOLS
    model_tools = ModelTools(store, settings)
    manager.automations = automations
    slack.automation_events = automations.events
    login_attempts = []
    owns_inference = settings.moyai_runtime_role == 'broker' or (
        settings.moyai_runtime_role in {'standalone', 'coordinator'} and not settings.moyai_separate_broker)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if settings.moyai_runtime_role == 'api':
                # Replicas serve requests and persist intents. The singleton
                # coordinator owns recovery, dispatch and background delivery.
                try:
                    await manager.recover()
                    await manager.wait_ready()
                    yield
                finally:
                    await session_lifecycle.close()
                    await session_titles.close()
                    await memory_review.close()
                    await spend.recovery.close()
                    await infrastructure.close()
                    await automations.close()
                    await environments.close()
                    await identities.close()
                    await slack.shutdown()
                    await manager.shutdown()
                    await manager.modal_clients.close()
                    await session_pull_requests.close()
                    await computer.close()
                    await tracing.close()
                    await lens_feedback.close()
                return
            if settings.moyai_runtime_role == 'worker':
                # Workers consume Temporal work only. They must not interrupt
                # gateway streams or take over singleton background recovery.
                await manager.recover()
                try:
                    yield
                finally:
                    await manager.shutdown()
                    await manager.modal_clients.close()
                    await session_pull_requests.close()
                    await computer.close()
                    await tracing.close()
                return
            # Only the process holding the inference fence may recover model
            # attempts or compaction. An API restart must leave live calls alone.
            if owns_inference:
                store.execute("UPDATE model_requests SET status='interrupted' WHERE status='pending'")
                harness_gateway.maintenance.recover()
            if settings.moyai_runtime_role == 'broker':
                try:
                    # External tool writes also live on /broker/*; an API restart
                    # must not mark an in-flight broker action ambiguous.
                    store.execute("UPDATE approvals SET status='uncertain' WHERE status='executing'")
                    await manager.recover()
                    await manager.wait_ready()
                    spend.recovery.start()
                    memory_review.start()
                    yield
                finally:
                    await memory_review.close()
                    await spend.recovery.close()
                    await harness_gateway.maintenance.close()
                    await harness_gateway.live_context.close()
                    await manager.shutdown()
                    await manager.modal_clients.close()
                    await environments.close()
                    await session_lifecycle.close()
                    await computer.close()
                    await session_pull_requests.close()
                    await tracing.close()
                return
            await manager.recover()
            session_lifecycle.start()
            await checkpoints.flush()
            slack.recover()
            identities.start()
            environments.start()
            automations.start()
            if owns_inference:
                spend.recovery.start()
                memory_review.start()
            watcher = asyncio.create_task(checkpoints.watch()) if settings.checkpoint_dir else None
            tracing.start()
            lens_feedback.start()
            infrastructure.start()
            session_titles.start()
            try:
                yield
            finally:
                await session_lifecycle.close()
                await memory_review.close()
                await spend.recovery.close()
                await harness_gateway.maintenance.close()
                await harness_gateway.live_context.close()
                await computer.close()
                await session_pull_requests.close()
                await session_titles.close()
                await infrastructure.close()
                await automations.close()
                await environments.close()
                await identities.close()
                await slack.shutdown()
                try:
                    await manager.shutdown()
                finally:
                    await manager.modal_clients.close()
                await tracing.close()
                await lens_feedback.close()
                if watcher:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                await checkpoints.flush()
        finally:
            await asyncio.to_thread(store.close)

    session_lifecycle = SessionLifecycle(store, security, manager, checkpoints)
    app = FastAPI(title="Moyai", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(session_lifecycle.require_live_api)])
    if settings.moyai_runtime_role == 'worker':
        from .runtime_coordination import WorkerRoleMiddleware
        app.add_middleware(WorkerRoleMiddleware)
    elif settings.moyai_separate_broker:
        from .runtime_coordination import BrokerRoleMiddleware
        app.add_middleware(BrokerRoleMiddleware, broker_only=settings.moyai_runtime_role == 'broker')
    if settings.moyai_runtime_role == 'api':
        from .runtime_coordination import ApiRoleMiddleware
        app.add_middleware(ApiRoleMiddleware)
    app.state.session_lifecycle = session_lifecycle
    app.state.lens_feedback = lens_feedback
    app.state.session_pull_requests = session_pull_requests
    app.include_router(session_lifecycle.routes())
    app.state.sandbox_settings = sandbox_settings
    app.include_router(sandbox_settings.routes())
    google = GoogleSignIn(settings, security, store)
    app.state.google_signin = google
    app.state.context_budget = context_budget
    app.state.session_titles = session_titles
    app.include_router(session_titles.routes(security))
    app.include_router(google.routes())
    app.include_router(user_roles.routes(security))
    app.state.user_roles = user_roles
    app.include_router(user_preferences.routes())
    app.state.user_preferences = user_preferences
    app.include_router(spend.routes())
    app.include_router(infrastructure.routes())
    app.include_router(identities.routes())
    app.include_router(credentials.routes())
    app.state.credentials = credentials
    app.include_router(skills.routes())
    app.include_router(memory.routes())
    app.state.memory = memory
    app.state.memory_review = memory_review
    app.state.model_slots = model_slots
    session_folders = SessionFolders(store, security, checkpoints)
    app.state.session_folders = session_folders
    app.include_router(session_folders.routes())
    media_shares = MediaShares(store, settings, security)
    app.state.media_shares = media_shares
    app.include_router(media_shares.routes())
    app.include_router(store.attachments.routes(security, settings))
    app.include_router(artifact_file_routes(settings, store, security))
    computer = Computer(settings, store, security, manager, credentials.same_requester)
    manager.computer = computer
    app.state.computer = computer
    app.include_router(computer.routes())
    app.state.automations = automations
    app.state.automation_tools = automation_tools
    app.state.model_tools = model_tools
    app.include_router(automations.routes())
    app.state.skills = skills
    app.state.environments = environments
    app.include_router(environments.routes())
    app.state.tracing = tracing
    app.include_router(github_routes(connectors, security, store, settings))
    app.state.identities = identities
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlparse(settings.public_url).hostname])
    app.add_middleware(BrokerDiagnosticsMiddleware, store=store, tracing=tracing)
    app.state.cloudflare_access = CloudflareAccess(settings)
    app.add_middleware(CloudflareAccessMiddleware, access=app.state.cloudflare_access, security=security)
    for key, value in {"store": store, "settings": settings, "security": security, "connectors": connectors, "manager": manager, "slack": slack, "spend": spend, "coordinator": coordinator}.items():
        setattr(app.state, key, value)

    @app.middleware("http")
    async def boundaries(request, call_next):
        if settings.trust_modal_proxy:
            # Modal Server rewrites Host to the container's private IP and
            # supplies the original host separately. Accept exactly our origin.
            expected = urlparse(settings.public_url).netloc
            if request.headers.get("x-forwarded-host") != expected:
                return JSONResponse({"detail": "Invalid workspace host"}, status_code=400)
            request.scope["headers"] = [(key, value) for key, value in request.scope["headers"] if key != b"host"] + [(b"host", expected.encode())]
        if security.local_preview() and request.client and request.client.host not in {"127.0.0.1", "::1"}:
            return JSONResponse({"detail": "Local preview only. Configure WORKSPACE_PASSWORD and PUBLIC_URL for remote access."}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return JSONResponse({"detail": "Invalid request length"}, status_code=400)
        route = '/' + request.url.path.split('/', 3)[-1] if request.url.path.startswith('/broker/') else ''
        limit = wire_limit(route) if route and request.headers.get('content-type') == CONTENT_TYPE else body_limit(route)
        if request.method == 'PUT' and request.url.path.startswith('/api/attachments/'):
            limit = upload_limit(request.headers.get('content-type', ''))
        if length < 0 or length > limit:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        if (request.method in {"POST", "DELETE", "PUT", "PATCH"} or request.url.path.startswith(("/oauth/", "/auth/"))) and not getattr(
            request.state, 'defer_activity_checkpoint', False
        ):
            try:
                await checkpoints.flush()
            except Exception:
                return JSONResponse({"detail": "Cloud persistence could not be confirmed. Refresh before retrying an action."}, status_code=503)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path == '/auth/github/register':
            response.headers['Content-Security-Policy'] += ' https://github.com'
        if request.url.path.startswith(("/api/", "/oauth/", "/auth/", "/broker/", "/media/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ConnectorError)
    async def connector_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if request.url.path.startswith('/api/settings/sandboxes'):
            return JSONResponse({'detail': 'Invalid sandbox connection form.'}, status_code=422)
        if request.url.path.startswith('/api/credentials'):
            return JSONResponse({'detail':'Invalid credential form. Choose who can use it and when it can be reused, then check the required fields.'},status_code=422)
        if request.url.path.startswith('/api/skills'):
            return JSONResponse({'detail':'Invalid skill. Choose Personal or Organization, use a lowercase-hyphenated name, a description up to 320 characters, and Markdown instructions up to 32,000 characters.'},status_code=422)
        return await request_validation_exception_handler(request,exc)

    @app.get("/health")
    async def health():
        if settings.moyai_runtime_role in {'worker', 'broker', 'api'} and not manager.ready.is_set():
            raise HTTPException(503, 'Runtime is connecting to Temporal.')
        if store.database:
            from .database import DatabaseError
            try:
                from .db import database
                await database(store.rows, 'SELECT 1')
            except DatabaseError:
                raise HTTPException(503, 'Database is unavailable.') from None
        return {"status": "ok"}

    @app.get("/api/session")
    async def session(request: Request):
        sid = security.session(request)
        response = JSONResponse({})
        if not sid and security.local_preview():
            sid = security.new_session(response, local=True)
        role = security.role(request) or ("admin" if sid else None)
        info = security.session_info(request) or {}
        user_id = None
        if info:
            user_id = store.identity(info)
        response.body = json.dumps({"authenticated": bool(sid), "csrf": security.csrf(sid) if sid else "", "local": security.local, "role": role,
                                   "identity": info.get("identity"), "user_id": user_id, "preferences": user_preferences.get(user_id), "google_enabled": settings.google_enabled(),
                                   "google_domains": sorted(settings.google_domains()) if settings.google_enabled() else [],
                                   "password_enabled": settings.password_login_enabled and bool(settings.workspace_password or settings.workspace_member_password)}).encode()
        response.headers["content-length"] = str(len(response.body))
        return response

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        security.check_origin(request)
        if not settings.password_login_enabled:
            raise HTTPException(403, "Use Google to sign in to this workspace.")
        login_attempts[:] = [stamp for stamp in login_attempts if stamp > time.monotonic() - 60]
        if len(login_attempts) >= 10:
            raise HTTPException(429, "Too many sign-in attempts. Wait a minute.")
        login_attempts.append(time.monotonic())
        if settings.workspace_password and hmac.compare_digest(body.password, settings.workspace_password):
            role = "admin"
        elif settings.workspace_member_password and hmac.compare_digest(body.password, settings.workspace_member_password):
            role = "member"
        else:
            raise HTTPException(401, "Incorrect workspace password.")
        response = JSONResponse({"authenticated": True, "role": role})
        security.new_session(response, role)
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        security.require(request, mutation=True)
        response = JSONResponse({"ok": True, **({'logout_url': '/cdn-cgi/access/logout'} if settings.cloudflare_access_login else {})})
        response.delete_cookie("workspace_session", path="/")
        return response

    def missing_cloud(provider=None):
        missing = settings.missing_cloud(provider)
        if security.local:
            missing.append("PUBLIC_URL (reachable HTTPS address)")
        return missing

    class ModelPreference(BaseModel):
        model_config = ConfigDict(extra='forbid')
        model: str = Field(min_length=1, max_length=120)

    @app.put('/api/settings/model-preference')
    async def set_model_preference(body: ModelPreference, request: Request):
        security.require(request, mutation=True)
        try:
            selected = settings.resolve_model(body.model)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        actor = store.identity(security.session_info(request))
        with store.connect() as conn:
            save_model(conn, actor, selected)
        await checkpoints.flush()
        return {'model': selected}

    def user_model(request):
        actor = store.identity(security.session_info(request))
        with store.connect() as conn:
            return preferred_model(conn, settings, actor)

    @app.get("/api/config")
    async def config(request: Request):
        security.require(request)
        missing = missing_cloud()
        from .harnesses import choices
        return {"harnesses": choices(), "harness": settings.default_harness(), "cloud_ready": not missing, "missing": missing,
                "evaluation": {"environment": settings.trace_environment, "agent_version": settings.moyai_build_sha,
                               "tracing_enabled": bool(settings.litellm_trace_endpoint and settings.litellm_trace_api_key)},
                "model": user_model(request), "models": [{**model, "default_harness": settings.default_harness(model["id"])} for model in settings.model_choices()],
                "sandbox_provider": settings.sandbox_provider, "sandbox_providers": sandbox_settings.view(False)["providers"],
                "public_url": settings.public_url, "max_concurrent_runs": settings.max_concurrent_runs,
                "max_parallel_agents": settings.max_parallel_agents, "parallel_agents_enabled": settings.temporal_enabled,
                "max_concurrent_model_requests": settings.max_concurrent_model_requests,
                "run_timeout_seconds": settings.run_timeout_seconds, "max_agent_iterations": settings.max_agent_iterations,
                "sandbox_rotation_seconds": settings.sandbox_rotation_seconds,
                "sandbox_idle_seconds": settings.sandbox_idle_seconds if settings.temporal_enabled else 0,
                "execution_engine": "Temporal" if settings.temporal_enabled else "Local worker",
                "execution_connected": manager.ready.is_set() if settings.temporal_enabled else True,
                "checkpoint_interval_seconds": settings.temporal_checkpoint_seconds if settings.temporal_enabled else None,
                "hermes_revision": settings.hermes_revision, "auth": "Cloudflare Access" if settings.cloudflare_access_login else "Google Workspace" if settings.google_enabled() else "Workspace password" if settings.workspace_password else "Local access only"}

    @app.get('/api/admin/pull-requests')
    async def pull_request_analytics(request: Request, start: date | None = None, end: date | None = None):
        security.require(request, admin=True)
        from .pr_analytics import report
        return report(session_pull_requests, start, end)

    @app.get('/api/admin/capacity')
    async def capacity(request: Request):
        security.require(request, admin=True)
        from .capacity import snapshot
        from .db import database
        model = model_slots.snapshot()
        if settings.moyai_separate_broker:
            # A local zero would falsely report the other process as idle.
            model = {key: value if key == 'capacity' else None for key, value in model.items()}
            model['source'] = 'separate_broker'
        return {**await database(snapshot, store, settings), 'model': model}

    @app.get("/api/organization")
    async def organization(request: Request):
        security.require(request)
        return {"name": store.rows("SELECT name FROM organization WHERE id=1")[0]["name"],
                "role": security.role(request), "member_access_configured": bool(settings.workspace_member_password) or settings.person_login_enabled(),
                "google_signin": settings.google_enabled(),
                "slack_sessions": slack.status(),
                "activity": store.rows("SELECT provider,action,actor,created_at FROM connection_audit ORDER BY id DESC LIMIT 15")}

    @app.patch("/api/organization")
    async def rename_organization(body: OrganizationName, request: Request):
        security.require(request, mutation=True, admin=True)
        store.execute("UPDATE organization SET name=? WHERE id=1", (body.name,))
        connectors.audit("organization", "Updated organization name")
        return {"name": body.name}

    def load_run_list(request, focus, scope, archived, search, view):
        owner = session_folders.actor(request)
        if scope == 'all':
            security.require(request, admin=True)
        if scope is None:
            scope = 'all' if security.role(request) == 'admin' else 'mine'
        memberships = session_folders.memberships(owner)
        pins = session_folders.pins(owner)
        selected = store.run(focus) if re.fullmatch(r'[0-9a-f]{32}', focus) else None
        parent_id = store.root_id(selected['id']) if selected else ''
        search = search.strip().lower()
        ids = store.sidebar_run_ids(owner if scope == 'mine' else None, [*memberships, *pins, parent_id],
                                    archive_owner=owner, archived=archived, pin_owner=owner,
                                    search=(search,) if search else (), search_folders=True)
        matches = store.sidebar_search_matches(search, ids) if search else {}
        sidebar_metadata = store.sidebar_metadata(owner, ids)
        receipts = session_pull_requests.receipts(ids)
        archives = session_lifecycle.archives(owner)
        admin = security.role(request) == 'admin'
        runs = {}
        for run in store.runs_by_ids(ids, sidebar=view == 'sidebar'):
            run_id = run['id']
            if run['deleted_at']:
                continue
            runs[run_id] = {**public_run(run), **session_lifecycle.metadata(run, owner, admin, archives), **sidebar_metadata.get(run_id, {}),
                           'folder_id': memberships.get(run_id), 'children': []}
        nodes = dict(runs)
        for child in store.subtrees(list(runs), sidebar=True):
            if child['id'] not in runs and not child['deleted_at']:
                visible = public_run(child)
                nodes[child['id']] = {**{key: visible[key] for key in ('id', 'parent_run_id', 'agent_label', 'status', 'mode', 'created_at', 'updated_at', 'active_message_id')},
                                     'archived': runs[child['ancestor_id']]['archived'], 'can_delete': False, 'children': []}
        for child in nodes.values():
            if child['id'] not in runs and child['parent_run_id'] in nodes:
                nodes[child['parent_run_id']]['children'].append(child)
            if search:
                child.update(search_query=search, search_match=child['id'] in matches, search_snippet=matches.get(child['id'], ''))
        return runs, receipts

    @app.get("/api/runs")
    async def runs(request: Request, focus: str = '', scope: Literal['all', 'mine'] | None = None, archived: bool = False,
                   search: str = Query(default='', max_length=200), view: Literal['full', 'sidebar'] = 'full'):
        # SQLite reads/identity writes can wait on disk or a writer. Keep the
        # bounded worker pool responsible for those waits, not the event loop.
        rows, receipts = await run_in_threadpool(load_run_list, request, focus, scope, archived, search, view)
        # The PR cache and refresh tasks belong to the event loop. Recheck live
        # GitHub access here; do not pass connection credentials across threads.
        summaries = session_pull_requests.summaries(rows, receipts=receipts)
        for run_id, row in rows.items():
            row['pr_summary'] = summaries[run_id]
        return list(rows.values())

    @app.post("/api/runs", status_code=201)
    async def create(body: NewRun, request: Request):
        await database(security.require, request, mutation=True)
        metadata_request = ((body.chat_enabled or settings.temporal_enabled)
                            and not body.attachment_ids and is_session_id_request(body.prompt))
        if body.side_chat_of:
            parent = await database(store.run, body.side_chat_of)
            if not parent or parent['deleted_at']:
                raise HTTPException(404, 'Original session not found.')
            if not body.chat_enabled:
                raise HTTPException(422, 'Side chats require a chat session.')
        try:
            requested_model = body.model if body.model is not None else await database(user_model, request)
            harness = body.harness or (parent['harness'] if body.side_chat_of else settings.default_harness(requested_model))
            model = settings.harness_model(harness, requested_model)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if body.mode == "modal" and not metadata_request:
            if missing_cloud():
                raise HTTPException(503, "Cloud setup is incomplete. See Runtime for the missing settings.")
            connected = {item["id"] for item in await database(connectors.list) if item["connected"] and item["enabled"]}
            if not set(body.plugins) <= connected:
                raise HTTPException(422, "Connect the selected apps before starting the task.")
        if body.mode == 'modal' and not metadata_request and (body.repo_url or body.github_repository_id):
            if 'github' in body.plugins:
                identity = await connectors.github.selected_target({}, body.repo_url.removeprefix('https://github.com/').removesuffix('.git'), body.github_repository_id)
                body.repo_url = 'https://github.com/' + connectors.github.repository_name(identity)
            else:
                repo = await connectors.github.public_repository(body.repo_url.removeprefix('https://github.com/').removesuffix('.git'), body.github_repository_id)
                identity, body.repo_url = repo['id'], 'https://github.com/' + repo['full_name']
            body.github_repository_id = identity
        if body.mode == "modal" and not metadata_request:
            await database(environments.choose, body.environment_id, body.repo_url, body.github_repository_id)
        session = await database(security.session_info, request)
        user_id = await database(store.identity, session)
        try:
            run = await database(store.create_run, body.prompt, body.repo_url, body.mode, sorted(set(body.plugins)), chat_enabled=body.chat_enabled or settings.temporal_enabled, model=model, user_id=user_id,
                                   attachment_ids=body.attachment_ids, client_id=body.client_id, environment_id=body.environment_id, side_chat_of=body.side_chat_of, harness=harness, github_repository_id=body.github_repository_id, metadata_request=metadata_request)
        except ValueError as exc:
            raise HTTPException(429 if 'queue' in str(exc) else 409, str(exc))
        await checkpoints.flush()
        if not metadata_request:
            if settings.temporal_enabled:
                await database(manager.submit, run)
            else:
                manager.submit(run)
            await session_titles.request(run['id'])
        return public_run(run)

    @app.get('/api/runs/{run_id}/side-chats')
    async def side_chats(run_id: str, request: Request):
        security.require(request)
        if not store.run(run_id):
            raise HTTPException(404, 'Session not found.')
        actor = store.identity(security.session_info(request))
        archives = session_lifecycle.archives(actor)
        rows = store.rows("SELECT id,owner_id,parent_run_id,prompt,agent_label,display_title,status,model,created_at,updated_at,active_message_id,pending_result,deletion_requested_at,deleted_at FROM runs WHERE side_chat_of=? AND deleted_at='' ORDER BY created_at,id", (run_id,))
        return [{**public_run(row), **session_lifecycle.metadata(row, actor, security.role(request) == 'admin', archives)} for row in rows]

    def load_run_details(run_id, request, activity):
        security.require(request)
        from .activity_history import projection
        # Keep message status and the activity cursor in one read snapshot. A
        # response completing during this request must be included or replayed
        # by SSE, never skipped between independent database reads.
        with store.connect() as connection:
            connection.begin_read()
            run = store.run(run_id, connection=connection)
            if not run or run['deleted_at']:
                raise HTTPException(404, "Task not found")
            messages = public_messages(run, store.messages(run_id, connection=connection))
            activity_data = projection(connection, run, messages) if activity == 'summary' and run['chat_enabled'] else {
                'events': store.events(run_id, limit=10000, connection=connection)}
        owners = store.rows('SELECT id,email,name FROM users WHERE id=?', (run['owner_id'],))
        project = environments.context(run)
        actor = store.identity(security.session_info(request))
        messages = skills.message_mentions(messages, actor)
        feedback_rows = lens_feedback.for_messages(run_id, feedback_author(request))
        messages = [{**message, 'feedback': feedback_rows.get(message['id'])}
                    if message['role'] == 'assistant' else message for message in messages]
        sidebar_id = store.root_id(run_id)
        run['workflow_root_id'] = sidebar_id
        return {**public_run(run), **session_lifecycle.metadata(run, actor, security.role(request) == 'admin'),
                **store.sidebar_metadata(actor, [sidebar_id]).get(sidebar_id, {}),
                'feedback_enabled': lens_feedback.enabled,
                **activity_data, "approvals": store.approvals(run_id), "messages": messages,
                'pr_write_access': connectors.github.access_requests(run, actor),
                'project_environment': {key: project[key] for key in ('name', 'repository', 'build_id', 'commit_sha') if key in project},
                "owner": owners[0] if owners else None, "goal": store.goal(run_id),
                "agents": coordinator.view(run_id, include_costs=security.role(request) == 'admin'),
                "credential_requests": credentials.pending(run,store.identity(security.session_info(request)),security.role(request)=='admin'),
                "slack_mirroring": slack.chat.mirroring(run_id),
                "active": manager.is_active(run_id), "has_artifact": store.artifacts.info(run_id + '.zip') is not None, "has_captures": bool(captures.listing(settings, run_id, store=store)), "slack_source": store.slack_source(run_id)}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request, activity: Literal['full', 'summary'] = 'full'):
        result = await run_in_threadpool(load_run_details, run_id, request, activity)
        identities.wake.set()
        # PR refresh tasks are event-loop owned; database and object-store reads
        # above keep their complete transactions in the thread pool.
        summary = session_pull_requests.summaries([run_id])[run_id]
        return {**result, 'pr_summary': summary, 'pull_requests': summary['pull_requests']}

    def feedback_author(request):
        actor = store.identity(security.session_info(request))
        rows = store.rows('''
            SELECT COALESCE(NULLIF(linked.email,''),NULLIF(users.email,''),linked.name,users.name,'') AS author
            FROM users
            LEFT JOIN users AS linked ON linked.id=users.linked_user_id
            WHERE users.id=?
        ''', (actor,))
        return rows[0]['author'] or actor if rows else actor

    @app.post('/api/runs/{run_id}/messages/{message_id}/feedback')
    async def submit_feedback(run_id: str, message_id: int, body: FeedbackBody, request: Request):
        security.require(request, mutation=True)
        run = store.run(run_id)
        if not run or run['deleted_at']:
            raise HTTPException(404, 'Session not found.')
        try:
            result = lens_feedback.submit(run_id, message_id, feedback_author(request),
                                          body.score, body.comment, 'web')
        except FeedbackNotConfigured:
            raise HTTPException(409, 'Lens feedback is not configured.') from None
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        await checkpoints.flush()
        return {'message_id': message_id, **result}

    @app.get('/api/runs/{run_id}/activity')
    async def activity_history(run_id: str, request: Request, message_id: int = Query(gt=0, le=2**63-1),
                               after: int = Query(default=0, ge=0, le=2**63-1),
                               until: int | None = Query(default=None, ge=0, le=2**63-1)):
        security.require(request)
        if not store.run(run_id):
            raise HTTPException(404, 'Session not found.')
        from .activity_history import history_page
        return history_page(store, run_id, message_id, after=after, until=until)

    from .github_write_access import WriteDecision

    @app.post('/api/runs/{run_id}/pr-write-access/{access_id}')
    async def decide_pr_write_access(run_id: str, access_id: str, body: WriteDecision, request: Request):
        security.require(request, mutation=True)
        actor = store.identity(security.session_info(request))
        result = await connectors.github.decide_write_access(run_id, access_id, actor, body.decision)
        await checkpoints.flush()
        return result

    @app.get('/api/runs/{run_id}/pull-request')
    async def read_pull_request(run_id: str, request: Request, url: str = Query(max_length=512)):
        security.require(request)
        result = await session_pull_requests.read(run_id, url)
        security.require(request)
        return result

    @app.post("/api/runs/{run_id}/messages", status_code=202)
    async def send_message(run_id: str, body: ChatMessage, request: Request):
        security.require(request, mutation=True)
        run = store.run(run_id)
        if not run:
            raise HTTPException(404, "Session not found")
        metadata_request = not body.attachment_ids and is_session_id_request(body.content)
        if run["mode"] == "modal" and not metadata_request and missing_cloud(run.get("sandbox_provider")):
            raise HTTPException(503, "Cloud setup is incomplete. See Runtime.")
        try:
            # Omitted models use the session preference inside the enqueue
            # transaction; retries retain their originally selected model.
            selected_model = settings.resolve_model(body.model) if body.model is not None else None
            if selected_model is None:
                settings.resolve_model(fallback=run['model'])
            from .harnesses import validate_harness
            validate_harness(run['harness'], selected_model or run['model'])
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            user_id = store.identity(security.session_info(request))
            enqueue = coordinator.enqueue_child if run['parent_run_id'] and not metadata_request else slack.chat.enqueue_web
            # Resolve on every send so the saved choice also applies to other
            # tabs and older clients. Explicit Send now remains available.
            send_immediately = user_preferences.get(user_id)['send_immediately']
            message, created = enqueue(run_id, body.content, body.client_id, selected_model, user_id,
                                       body.attachment_ids, body.send_now, send_immediately=send_immediately,
                                       **({'metadata_request': True} if metadata_request else {}))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        await checkpoints.flush()
        # Resume even for a duplicate whose first acknowledgement was lost.
        if not metadata_request:
            await session_titles.request(run_id)
            if store.has_queued_messages(run_id):
                manager.submit(store.run(run_id))
        return {"id": message["id"], "status": message["status"], "model": message['model'], "created": created}

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        security.require(request, mutation=True)
        if not store.run(run_id):
            raise HTTPException(404, "Task not found")
        await manager.cancel(run_id)
        return public_run(store.run(run_id))

    @app.patch('/api/runs/{run_id}/messages/{message_id}')
    async def change_queued_message(run_id: str, message_id: int, body: QueueChange, request: Request):
        security.require(request, mutation=True)
        if body.action == 'edit' and body.content is None:
            raise HTTPException(422, 'Enter the updated message.')
        actor = store.identity(security.session_info(request))
        result = message_queue.change(run_id, message_id, actor, security.role(request) == 'admin',
                                      body.revision, body.action, body.content)
        await checkpoints.flush()
        slack.chat.wake.set()
        if store.has_queued_messages(run_id):
            manager.submit(store.run(run_id))
        return {'id': result['id'], 'status': result['status'], 'revision': result['revision']}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = 0):
        security.require(request)
        if not store.run(run_id):
            raise HTTPException(404, "Task not found")
        try:
            last_id = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            last_id = 0

        async def stream():
            cursor = max(after, last_id, 0)
            while not await request.is_disconnected():
                row = store.run(run_id)
                if not row or row['deleted_at']:
                    yield "event: deleted\ndata: {}\n\n"
                    break
                batch = store.events(run_id, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                if not row["chat_enabled"] and row["status"] in TERMINAL and not manager.is_active(run_id) and len(batch) < 200:
                    yield "event: settled\ndata: {}\n\n"
                    break
                yield f"event: run-status\ndata: {json.dumps({'status': response_status(row), 'active': manager.is_active(run_id), 'model': row['model'], 'active_model': row['active_model'], 'updated_at': row['updated_at'], 'active_message_id': row['active_message_id'], 'checkpoint_error': row['checkpoint_error'], 'deletion_error': session_lifecycle.deletion_error(run_id), 'slack_mirroring': slack.chat.mirroring(run_id)})}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/artifact")
    def artifact(run_id: str, request: Request):
        security.require(request)
        if not re.fullmatch(r"[0-9a-f]{32}", run_id) or not store.run(run_id):
            raise HTTPException(404, "No result archive is available.")
        try:
            return store.artifacts.download(run_id + '.zip', f'moyai-{run_id[:8]}.zip')
        except FileNotFoundError:
            raise HTTPException(404, 'No result archive is available.') from None

    @app.get("/api/connections")
    async def connections(request: Request):
        security.require(request)
        if connectors.github.saved_credentials():
            try:
                await connectors.github.ensure_connection()
            except ConnectorError:
                connectors.record_check('github', 'needs_attention')
        return connectors.list()

    @app.post("/api/connections/{provider}")
    async def connect(provider: Provider, body: TokenConnection, request: Request):
        security.require(request, mutation=True, admin=True)
        if provider == 'github':
            raise HTTPException(400, 'Install the organization GitHub App; personal or raw access tokens are not accepted.')
        credentials = {"access_token": body.token.strip(), "kind": "personal"}
        label = await connectors.verify(provider, credentials)
        connectors.save(provider, credentials, label)
        connectors.record_check(provider, "healthy")
        connectors.audit(provider, "Connected for the organization")
        return {"connected": True, "label": label}

    @app.delete("/api/connections/{provider}")
    async def disconnect(provider: Provider, request: Request):
        security.require(request, mutation=True, admin=True)
        async with connectors.locks[provider]:
            store.execute("DELETE FROM connections WHERE provider=?", (provider,))
        connectors.expire_approvals(provider)
        connectors.audit(provider, "Disconnected from the organization")
        return {"connected": False}

    @app.patch("/api/connections/{provider}/policy")
    async def connection_policy(provider: Provider, body: ConnectionPolicy, request: Request):
        security.require(request, mutation=True, admin=True)
        store.execute("INSERT INTO connection_policies(provider,enabled,read_only) VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET enabled=excluded.enabled,read_only=excluded.read_only",
                      (provider, body.enabled, body.read_only))
        if not body.enabled or body.read_only:
            connectors.expire_approvals(provider)
        action = ("Paused for all sessions" if not body.enabled else "Enabled: read only" if body.read_only
                  else "Enabled: create and maintain Moyai pull requests across chats" if provider == 'github'
                  else "Enabled: read and write without approval")
        connectors.audit(provider, action)
        return connectors.policy(provider)

    @app.post("/api/connections/{provider}/check")
    async def check_connection(provider: Provider, request: Request):
        security.require(request, mutation=True, admin=True)
        try:
            if provider == 'github':
                await connectors.github.refresh_connection()
                label = ', '.join(connectors.github.targets())
            else:
                label = await connectors.verify(provider, await connectors.credentials(provider))
        except ConnectorError:
            connectors.record_check(provider, "needs_attention")
            raise
        connectors.record_check(provider, "healthy")
        return {"healthy": True, "label": label}

    @app.post("/api/connections/{provider}/oauth")
    async def oauth_start(provider: Provider, request: Request):
        sid = security.require(request, mutation=True, admin=True)
        if not connectors.configured_oauth(provider):
            raise HTTPException(409, "Configure this app's OAuth client ID and secret first, or connect using a token.")
        state = secrets.token_urlsafe(32)
        store.execute("DELETE FROM oauth_states WHERE expires<?", (time.time(),))
        store.execute("INSERT INTO oauth_states VALUES(?,?,?,?)", (digest(state), provider, digest(sid), time.time() + 600))
        return {"url": connectors.authorization_url(provider, state)}

    @app.get("/oauth/{provider}/callback")
    async def oauth_callback(provider: Provider, request: Request, state: str = "", code: str = "", error: str = ""):
        sid = security.require(request, admin=True)
        with store.connect() as conn:
            rows = conn.execute("DELETE FROM oauth_states WHERE state_hash=? AND provider=? AND session_id=? AND expires>? RETURNING state_hash",
                                (digest(state), provider, digest(sid), time.time())).fetchall()
        if not rows:
            raise HTTPException(400, "Expired or invalid connection request. Start again from Connections.")
        if error or not code:
            return RedirectResponse("/?connection=cancelled#connections", status_code=303)
        credentials = await connectors.exchange(provider, code=code)
        label = await connectors.verify(provider, credentials)
        connectors.save(provider, credentials, label)
        connectors.record_check(provider, "healthy")
        connectors.audit(provider, "Authorized an organization connection")
        return RedirectResponse("/?connection=success#connections", status_code=303)

    @app.post("/api/approvals/{approval_id}")
    async def approval(approval_id: str, body: Decision, request: Request):
        security.require(request, mutation=True, admin=True)
        status = "approved" if body.decision == "approve" else "denied"
        count = store.execute("UPDATE approvals SET status=? WHERE id=? AND status='pending' AND EXISTS(SELECT 1 FROM runs WHERE runs.id=approvals.run_id AND runs.status IN ('running','reconnecting','awaiting_approval'))", (status, approval_id))
        if not count:
            raise HTTPException(409, "This approval was already resolved or the run has ended.")
        row = store.rows("SELECT run_id,tool FROM approvals WHERE id=?", (approval_id,))[0]
        store.event(row["run_id"], "approval", f"{row['tool']}: {status}")
        return {"status": status}

    def require_run(run_id, request):
        run = store.run(run_id)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if (not run or run['deleted_at'] or run['deletion_requested_at'] or run["mode"] != "modal" or run["status"] not in {"running", "reconnecting", "awaiting_approval"}
                or not run["token_hash"] or not hmac.compare_digest(run["token_hash"], digest(token))):
            raise HTTPException(401, "Run capability expired or invalid.")
        observation = request.scope.get('moyai_model_diagnostic')
        if observation is not None:
            observation.setdefault('run', dict(run))
        return run

    async def broker_body(request, route):
        # Called only after require_run. The edge carries opaque authenticated
        # data; the application still checks identity, size, schema and tools.
        raw = await request.body()
        if request.headers.get('content-type') == CONTENT_TYPE:
            try:
                raw = unseal(request.headers.get('authorization', '').removeprefix('Bearer '), route, raw)
            except ValueError:
                raise HTTPException(400, 'Invalid or expired broker envelope.')
        if len(raw) > body_limit(route):
            raise HTTPException(413, 'Broker request too large.')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, 'Invalid broker request JSON.')

    from .github_git import routes as git_routes
    app.include_router(git_routes(connectors.github, require_run))

    @app.post('/broker/{run_id}/control')
    async def session_control(run_id: str, request: Request):
        from .db import database
        await database(require_run, run_id, request)
        body = await broker_body(request, '/control')
        result = await database(apply_session_control, run_id, request, body)
        await checkpoints.flush()
        return result

    def apply_session_control(run_id, request, body):
        # Revalidate after reading the body: a stop or capability rotation can
        # race a slow upload. Connections stay inside this worker thread, and
        # cancellation waits for any outstanding receipt write.
        run = require_run(run_id, request)
        if isinstance(body, dict) and body.get('version') == 2:
            if body.get('receipt_only') is True:
                message_queue.acknowledge(run_id, run['active_message_id'], body.get('applied', []))
                result = {'steer_message_id': None}
            else:
                result = message_queue.live_control(run_id, run['active_message_id'], body.get('applied', []))
            return {**result, 'receipt_only_supported': True}
        target = message_queue.accept_steer(run_id, run['active_message_id'])
        return {'steer_message_id': target}

    def workspace_catalog(run):
        from .tool_schema_cache import tool_schema
        return [DIAGNOSTIC_TOOL] + media_shares.tools() + session_lifecycle.tools(run) + model_tools.tools(run) + automation_tools.tools(run) + automations.tools(run) + memory.tools(run) + skills.tools(run) + credentials.tools(run) + coordinator.tools(run) + [{"name": name, "description": spec[3], "inputSchema": tool_schema(spec[2]), "annotations": {"readOnlyHint": not spec[1], "idempotentHint": name in RETRY_SAFE_READS}}
                for name, spec in TOOLS.items() if spec[0] in run["plugins"] and connectors.allowed(name)]

    @app.get("/broker/{run_id}/tools")
    def tool_list(run_id: str, request: Request):
        return workspace_catalog(require_run(run_id, request))

    @app.post("/broker/{run_id}/tools/call")
    async def tool_call(run_id: str, request: Request):
        run = require_run(run_id, request)
        try:
            body = ToolCall.model_validate(await broker_body(request, '/tools/call'))
        except ValidationError:
            raise HTTPException(422, 'Invalid tool request.')
        if body.name == 'workspace_diagnostics':
            try:
                DiagnosticArgs.model_validate(body.arguments)
            except ValidationError:
                raise HTTPException(422, 'Workspace diagnostics takes no arguments and only reads the current session.') from None
            return inspect_workspace(store, connectors, run, workspace_catalog(run))
        if body.name in MEDIA_TOOLS:
            try:
                result = await asyncio.to_thread(media_shares.call, run, body.name, body.arguments)
            except ValidationError:
                result = {'error': 'Invalid media arguments. Use media_list and the current tool schema.', 'status_code': 422}
            except HTTPException as exc:
                if exc.status_code == 401:
                    raise
                result = {'error': exc.detail, 'status_code': exc.status_code}
            await checkpoints.flush()
            return result
        if body.name == 'sessions_read':
            from .session_read import read_session
            try:
                return await asyncio.to_thread(read_session, session_lifecycle, run, body.arguments)
            except ValidationError:
                raise HTTPException(422, 'Use a session ID or URL, nonnegative cursors, and a limit from 1 to 20.') from None
        if body.name == 'sessions_search':
            try:
                return session_lifecycle.search(run, body.arguments)
            except ValidationError:
                raise HTTPException(422, 'Enter session search keywords (1-200 characters) and a limit from 1 to 20.') from None
        if body.name in MODEL_TOOLS:
            try:
                result = model_tools.call(run, body.name, body.arguments)
            except ValidationError:
                raise HTTPException(422, 'Invalid model arguments. Use model_list and the current tool schema.') from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            await checkpoints.flush()
            return result
        if body.name in AUTOMATION_TOOLS:
            try:
                return await automation_tools.call(run, body.name, body.arguments)
            except ValidationError:
                raise HTTPException(500, 'The saved automation could not be validated.') from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        if body.name in MEMORY_TOOLS:
            try:
                result = memory.call(run, body.name, body.arguments)
            except ValidationError:
                result = {'error': 'Invalid memory arguments. Check the current turn, text limits and tool schema.', 'status_code': 422}
            except HTTPException as exc:
                if exc.status_code not in {403, 404, 409, 422}:
                    raise
                # A definite rejection is a tool receipt, not an uncertain
                # transport failure that prevents the runtime from continuing.
                result = {'error': exc.detail, 'status_code': exc.status_code}
            await checkpoints.flush()
            return result
        if body.name == 'automation_claim_item':
            try:
                result = automations.claim(run, body.arguments)
            except ValidationError:
                raise HTTPException(422, 'Invalid item key.') from None
            await checkpoints.flush()
            return result
        if body.name in SKILL_TOOLS:
            try:
                result = await asyncio.to_thread(skills.call, run, body.name, body.arguments)
            except ValidationError:
                result = {'error': 'Invalid skill arguments. Check the tool schema, text limits and relative file paths.', 'status_code': 422}
            except HTTPException as exc:
                if exc.status_code not in {403, 404, 409, 413, 422}:
                    raise
                # Definite skill rejections are recoverable tool receipts.
                # A missing skill or revision conflict must not abort a sync.
                result = {'error': exc.detail, 'status_code': exc.status_code}
            await checkpoints.flush()
            return result
        if body.name in CREDENTIAL_TOOLS:
            if not credentials.tools(run):
                raise HTTPException(403,'Credential requests require a durable chat session.')
            try:
                result = await credentials.call(run, body.name, body.arguments)
                await checkpoints.flush()
                return result
            except ValidationError:
                return {'error':'Invalid credential tool arguments.'}
            except ValueError as exc:
                return {'error':str(exc)}
        if body.name in AGENT_TOOLS:
            if not coordinator.available(run):
                raise HTTPException(403, 'Only a Temporal chat session can coordinate agents.')
            try:
                result = await coordinator.call(run_id, body.name, body.arguments)
                await checkpoints.flush()
                return result
            except (ValueError, ValidationError) as exc:
                # Do not include validation payloads or private provider errors.
                return {'error': 'Invalid agent arguments.' if isinstance(exc, ValidationError) else str(exc)}
        if body.name not in TOOLS or TOOLS[body.name][0] not in run["plugins"]:
            raise HTTPException(403, "This tool is not enabled for this task.")
        provider, write, schema, _ = TOOLS[body.name]
        try:
            arguments = schema.model_validate(body.arguments).model_dump()
        except ValidationError:
            raise HTTPException(422, "Invalid tool arguments.")
        if not connectors.allowed(body.name):
            raise HTTPException(403, "This operation is disabled by the organization's connection policy.")
        if provider == 'github':
            try:
                await connectors.github.ensure_connection()
            except ConnectorError as exc:
                return {'error': str(exc)}
            run = {**store.run(run_id), 'github_connection_version': connectors.github.connection_version()}
        # Enabled tools, including newly registered writes, execute directly.
        # Connection policies and the live session capability still apply.
        require_run(run_id, request)
        if not connectors.allowed(body.name):
            return {"error": "The organization connection changed. No action was sent."}
        try:
            result = (await connectors.github.call(run, body.name, arguments) if provider == 'github'
                      else await connectors.my_linear_issues(run) if body.name == 'linear_my_issues'
                      else await connectors.call(body.name, arguments, run=run) if body.name in {'slack_send', 'slack_me'}
                      else await connectors.call(body.name, arguments))
            store.event(run_id, "tool", f"{body.name} completed")
            if body.name == 'github_request_pull_request_write_access':
                await checkpoints.flush()
            return result
        except Exception as exc:
            message = str(exc) if isinstance(exc, ConnectorError) else f"App operation could not be confirmed ({type(exc).__name__})."
            store.event(run_id, "error", f"{body.name}: {message}")
            return {"error": message, "outcome_uncertain": write, "instruction": "Verify the destination before retrying a write."}

    @app.post('/broker/{run_id}/credentials/materialize')
    async def credential_materialize(run_id: str, request: Request):
        run = require_run(run_id, request)
        if not credentials.tools(run):
            raise HTTPException(403, 'Credential access requires a durable chat session.')
        try:
            args = Materialize.model_validate(await broker_body(request, '/credentials/materialize'))
            result = credentials.materialize(run, args)
        except (ValueError, ValidationError):
            raise HTTPException(422, 'Invalid credential access request.') from None
        await checkpoints.flush()
        # This route is consumed only by the sandbox executor, never returned
        # as an agent tool result or saved in runner/Temporal state.
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/broker/{run_id}/credentials/invoke')
    async def credential_invoke(run_id: str, request: Request):
        run = require_run(run_id,request)
        try:
            args = Invoke.model_validate(await broker_body(request,'/credentials/invoke'))
            status,result = await credentials.invoke(run,args)
        except (ValueError,ValidationError):
            raise HTTPException(422,'Invalid provider request.') from None
        return JSONResponse(result,status_code=status)

    @app.get("/broker/{run_id}/v1/models")
    async def models(run_id: str, request: Request):
        run = require_run(run_id, request)
        return {"object": "list", "data": [{"id": gateway_model(run['active_model'] or run['model'] or settings.agent_model), "object": "model", "owned_by": "workspace"}]}

    @app.get('/broker/{run_id}/attachments/{attachment_id}')
    async def broker_attachment(run_id: str, attachment_id: str, request: Request):
        return await asyncio.to_thread(store.attachments.broker_file, require_run(run_id, request), attachment_id)

    @app.post('/hooks/slack/interactions')
    async def slack_interactions(request: Request):
        return await slack.feedback.receive(request)

    @app.post("/hooks/slack/events")
    async def slack_events(request: Request):
        return await slack.receive(request, missing_cloud())

    from .harness_gateway import HarnessGateway
    harness_gateway = HarnessGateway(settings=settings, store=store, spend=spend,
        checkpoints=checkpoints, require_run=require_run, read_body=broker_body,
        model_slots=model_slots, memory=memory, skills=skills, tracing=tracing, context_budget=context_budget)

    from .native_sessions import NativeSessions
    native_sessions = NativeSessions(settings, store, security, checkpoints, require_run, broker_body)
    harness_gateway.native_sessions = native_sessions

    @app.post('/broker/{run_id}/context/native')
    async def native_session(run_id: str, request: Request):
        return await native_sessions.exchange(run_id, request)

    @app.post('/broker/{run_id}/context/compact')
    async def compact_context(run_id: str, request: Request):
        return await harness_gateway.forward(run_id, request, '/context/compact')

    @app.post('/broker/{run_id}/context/maintenance')
    async def maintain_context(run_id: str, request: Request):
        return await harness_gateway.maintenance.exchange(run_id, request)

    @app.get('/broker/{run_id}/context/window')
    async def native_context_window(run_id: str, request: Request):
        return await harness_gateway.context_window(run_id, request)

    @app.post('/broker/{run_id}/v1/messages')
    async def messages_proxy(run_id: str, request: Request):
        return await harness_gateway.forward(run_id, request, '/v1/messages')

    @app.post('/broker/{run_id}/v1/responses')
    async def responses_proxy(run_id: str, request: Request):
        return await harness_gateway.forward(run_id, request, '/v1/responses')

    @app.post("/broker/{run_id}/v1/chat/completions")
    async def model_proxy(run_id: str, request: Request):
        require_run(run_id, request)
        model_stage(request, 'admission')
        # Reject before reading/charging an inference. Waiting on this server
        # would retain large request bodies and could expire sealed envelopes.
        # The sandbox retries only this explicit, unbilled admission response.
        if model_slots.locked():
            model_slots.record_queue()
            raise HTTPException(429, 'Waiting for a model request slot.',
                                headers={'X-Moyai-Model-Queue': '1', 'Retry-After': '3'})
        model_stage(request, 'validation')
        prepared = await prepare_model(run_id, request)
        model_stage(request, 'admission')
        async with model_slots:
            return await forward_model(run_id, request, *prepared)

    async def prepare_model(run_id: str, request: Request):
        run = require_run(run_id, request)
        try:
            selected_model = settings.resolve_model(fallback=run['active_model'] or run['model'])
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        body = await broker_body(request, '/v1/chat/completions')
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            raise HTTPException(422, "messages must be an array")
        if 'steering_applied' in body:
            message_queue.acknowledge(run_id, run['active_message_id'], body['steering_applied'])
        allowed = {"messages", "tools", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "stop", "stream", "stream_options", "response_format", "reasoning_effort", "max_tokens", "max_completion_tokens", "seed"}
        payload = {key: value for key, value in body.items() if key in allowed}
        native_sessions.observe_scope(run)
        payload['messages'] = await asyncio.to_thread(store.attachments.with_images, run, scrub_memory_history(payload['messages']))
        memory_context = memory.context(run)
        if memory_context:
            # Resolve references for the current requester on every inference.
            # No retrieved note body goes into sandbox history or tool output.
            payload['messages'] = [{'role':'system','content':memory_context}, *payload['messages']]
        skill_context = skills.context(run)
        if skill_context:
            # The original platform system instructions stay last and take
            # precedence. Skill definitions never enter sandbox tool results,
            # conversation snapshots, event logs or Temporal workflow history.
            payload['messages'] = [{'role':'system','content':skill_context}, *payload['messages']]
        if selected_model == ASTRA_ULTRAFAST:
            raise HTTPException(422, 'GPT-6 Astra Ultrafast requires the Responses API. Use Codex or regular GPT-6 Astra.')
        payload["model"] = selected_model
        for field in ("max_tokens", "max_completion_tokens"):
            if field in payload:
                if type(payload[field]) is not int or payload[field] < 1:
                    raise HTTPException(422, "Invalid output limit")
        model_stage(request, 'context_preparation')
        try:
            payload, checked_budget = await harness_gateway.live_context.prepare(
                run, request, payload, '/v1/chat/completions')
        except ContextPressure as exc:
            store.event(run_id, 'context', 'Compacting before the next model request.', exc.budget)
            await checkpoints.flush()
            raise
        return run, body, payload, checked_budget

    async def forward_model(run_id: str, request: Request, run, body, payload, checked_budget):
        selected_model = payload['model']
        harness_gateway.live_context.require_current(run, request, selected_model)
        admitted = store.execute("UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1 "
            "WHERE id=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?) "
            "AND status IN ('running','reconnecting','awaiting_approval') AND token_hash=? AND active_message_id IS NOT DISTINCT FROM ? "
            "AND active_user_id IS NOT DISTINCT FROM ? AND active_model IS NOT DISTINCT FROM ? AND (coalesce(active_model,'')!='' OR model IS NOT DISTINCT FROM ?)",
            (run_id, settings.max_agent_iterations, settings.max_agent_iterations * 3,
             run['token_hash'], run['active_message_id'], run['active_user_id'], run['active_model'], run['model']))
        if not admitted:
            raise HTTPException(429, "This run reached its model request limit.")
        request_id = spend.begin(run, selected_model)
        model_stage(request, 'upstream', request_id=request_id)
        # Keep user/session accounting local. The existing virtual key remains
        # the sole billing credential; sandbox-supplied attribution is ignored.
        payload['metadata'] = {'moyai_request_id': request_id}
        wants_stream = bool(payload.get('stream'))
        # Streaming headers precede generation and cannot contain its final
        # charge. Ask for a completed response, then adapt it to Hermes' SSE
        # protocol. Tool/activity events remain live throughout the session.
        payload['stream'] = False
        payload.pop('stream_options', None)
        await checkpoints.flush()
        capture = UsageCapture(False)
        status = 'failed'
        trace_started = time.time_ns()
        trace_response = {}
        gateway_id = ''
        error = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
            try:
                harness_gateway.live_context.require_current(run, request, selected_model)
                status = 'unknown'
                async with client.stream('POST', settings.litellm_api_base.rstrip('/') + '/chat/completions',
                                         json=payload, headers={'Authorization': f'Bearer {settings.litellm_api_key}', 'x-litellm-call-id': request_id}) as upstream:
                    gateway_id = spend.headers(request_id, upstream, False)
                    if upstream.status_code >= 400:
                        status = 'failed'
                        raw_error = bytearray()
                        async for chunk in upstream.aiter_bytes():
                            raw_error.extend(chunk[:8192 - len(raw_error)])
                            if len(raw_error) >= 8192:
                                break
                        if provider_context_rejection(upstream.status_code, raw_error):
                            store.event(run_id, 'context', 'The provider requested further context reduction.', checked_budget.public())
                            raise ContextPressure(checked_budget.public())
                        raise model_gateway_error(request_id, upstream, raw_error)
                    # Bound transport memory independently of model token limits.
                    raw_response = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        raw_response.extend(chunk)
                        if len(raw_response) > 8 * 1024 * 1024:
                            raise HTTPException(502, 'Model gateway response exceeded the size limit.')
                    capture.feed(bytes(raw_response))
                    capture.finish()
                    if not capture.done:
                        raise HTTPException(502, 'Model gateway returned an invalid completion.')
                    status = 'completed'
                    trace_response = json.loads(raw_response)
            except httpx.HTTPError as exc:
                error = model_error(exc)
                raise HTTPException(502, 'Model gateway could not be reached.',
                                    headers=upstream_headers(request_id, error=exc)) from None
            except BaseException as exc:
                error = model_error(exc)
                if isinstance(exc, asyncio.CancelledError):
                    status = 'interrupted'
                raise
            finally:
                if error:
                    model_stage(request, 'upstream', error=error)
                # Account before returning any data, including if the sandbox
                # stopped while the already-submitted inference was completing.
                spend.finish(request_id, capture, status)
                if status == 'completed':
                    context_budget.remember(payload, capture.usage, run_id + '/v1/chat/completions')
                tracing.model(run, request_id, trace_started, body['messages'],
                              {**trace_response, **capture.response}, status, gateway_id=gateway_id, error=error)
                await checkpoints.flush()
        value = json.loads(raw_response)
        if wants_stream:
            async def relay():
                for chunk in completion_events(value):
                    if store.run(run_id)['status'] not in {'running', 'reconnecting', 'awaiting_approval'}:
                        break
                    yield chunk
            return StreamingResponse(relay(), media_type='text/event-stream', headers=upstream_headers(request_id, upstream))
        return JSONResponse(value, headers=upstream_headers(request_id, upstream))

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def public_messages(run: dict[str, object], messages: list[dict[str, object]]) -> list[dict[str, object]]:
    """Show a durable answer receipt while its canonical turn is still saving."""
    raw = run.get('pending_result')
    if not isinstance(raw, str) or not raw:
        return messages
    try:
        result = json.loads(raw)
    except ValueError:
        return messages
    message_id = run.get('active_message_id')
    active = next((m for m in messages if m['id'] == message_id and m['role'] == 'user' and m['status'] == 'running'), None)
    if not active or not completed_response(run, result):
        return messages
    if any(m['role'] == 'assistant' and m.get('response_to_id') == message_id for m in messages):
        return messages
    # Compatibility for receipts written before canonical answer publication.
    reply = {'id': -message_id, 'role': 'assistant', 'content': run.get('summary') or result['message'],
             'status': 'save_failed' if result.get('save_failed') else 'saving',
             'model': run.get('active_model') or active.get('model', ''),
             'created_at': run['updated_at'], 'attachments': []}
    index = next((i for i, m in enumerate(messages) if m['role'] == 'user' and m['status'] == 'queued'), len(messages))
    return messages[:index] + [reply] + messages[index:]


def public_run(run):
    return {**{key: value for key, value in run.items() if key not in {"token_hash", "pending_result", "side_chat_context", "title_attempted_at"}},
            "status": response_status(run)}


app = create_app()
