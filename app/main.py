import asyncio
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .connectors import Connectors, ConnectorError, TOOLS
from .db import Store, now
from .runner import RunManager, TERMINAL
from .persistence import Checkpoints, restore_checkpoint
from .security import Security, digest
from .google_sso import GoogleSignIn
from .user_roles import UserRoles
from .access_logging import configure_access_logging
from .slack import SlackSessions
from .spend import Spend, UsageCapture, completion_events
from .identities import SlackIdentities
from .agents import AgentCoordinator, TOOLS as AGENT_TOOLS
from .github_setup import routes as github_routes
from .credentials import Credentials, Invoke, Materialize, TOOLS as CREDENTIAL_TOOLS
from .skills import Skills, TOOL_NAMES as SKILL_TOOLS
from .memory import Memory, TOOL_NAMES as MEMORY_TOOLS
from .session_folders import SessionFolders
from sandbox.memory_history import scrub_memory_history
from .environments import Environments
from .tracing import AgentTracing
from .attachments import upload_limit
from .artifact_files import routes as artifact_file_routes
from .automations import Automations
from .computer import Computer
from . import captures
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
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)
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
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)
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
                  max_pending_runs=settings.max_pending_runs)
    user_roles = UserRoles(store, settings)
    security = Security(settings, user_roles)
    connectors = Connectors(store, security, settings)
    if settings.temporal_enabled:
        from .temporal_runtime import TemporalRunManager
        manager = TemporalRunManager(store, settings)
    else:
        manager = RunManager(store, settings)
    checkpoints = Checkpoints(store, settings)
    environments = Environments(store, settings, security, manager, connectors, checkpoints)
    manager.environments = environments
    spend = Spend(store, settings, security, checkpoints)
    infrastructure = spend.infrastructure
    coordinator = AgentCoordinator(store, settings, manager)
    tracing = AgentTracing(store, settings)
    store.tracing = tracing
    manager.coordinator = coordinator
    credentials = Credentials(store, security, settings, manager, checkpoints)
    manager.credentials = credentials
    skills = Skills(store, security, credentials.same_requester)
    memory = Memory(store, security, credentials.same_requester, checkpoints)
    model_slots = asyncio.Semaphore(settings.max_concurrent_model_requests)
    from .context_budget import ContextBudget, ContextPressure, provider_context_rejection
    context_budget = ContextBudget(settings)
    credentials.slots = model_slots
    manager.persist = checkpoints.flush
    store.execute("INSERT OR IGNORE INTO organization(id,name) VALUES(1,?)", (settings.organization_name,))
    slack = SlackSessions(store, connectors, manager, checkpoints, settings)
    from .session_titles import SessionTitles
    session_titles = SessionTitles(store, settings, checkpoints)
    slack.session_titles = session_titles
    from .message_queue import MessageQueue
    message_queue = MessageQueue(store, slack.chat.change_queued_in)
    identities = SlackIdentities(store, connectors, settings, security, checkpoints)
    slack.identities = identities
    manager.prepare_context = slack.prepare
    automations = Automations(store, settings, security, manager, connectors, environments, checkpoints)
    from .automation_tools import AutomationTools, TOOL_NAMES as AUTOMATION_TOOLS
    automation_tools = AutomationTools(automations, credentials.same_requester)
    from .model_tools import ModelTools, TOOL_NAMES as MODEL_TOOLS
    model_tools = ModelTools(store, settings)
    manager.automations = automations
    slack.automation_events = automations.events
    login_attempts = []

    @asynccontextmanager
    async def lifespan(app):
        await manager.recover()
        await checkpoints.flush()
        slack.recover()
        identities.start()
        environments.start()
        automations.start()
        store.execute("UPDATE model_requests SET status='interrupted' WHERE status='pending'")
        watcher = asyncio.create_task(checkpoints.watch()) if settings.checkpoint_dir else None
        tracing.start()
        infrastructure.start()
        session_titles.start()
        try:
            yield
        finally:
            await session_titles.close()
            await infrastructure.close()
            await automations.close()
            await environments.close()
            await identities.close()
            await slack.shutdown()
            await manager.shutdown()
            await tracing.close()
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await checkpoints.flush()

    app = FastAPI(title="Moyai", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    google = GoogleSignIn(settings, security, store)
    app.state.google_signin = google
    app.state.context_budget = context_budget
    app.state.session_titles = session_titles
    app.include_router(session_titles.routes(security))
    app.include_router(google.routes())
    app.include_router(user_roles.routes(security))
    app.state.user_roles = user_roles
    app.include_router(spend.routes())
    app.include_router(infrastructure.routes())
    app.include_router(identities.routes())
    app.include_router(credentials.routes())
    app.state.credentials = credentials
    app.include_router(skills.routes())
    app.include_router(memory.routes())
    app.state.memory = memory
    session_folders = SessionFolders(store, security, checkpoints)
    app.state.session_folders = session_folders
    app.include_router(session_folders.routes())
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
        if request.method in {"POST", "DELETE", "PUT", "PATCH"} or request.url.path.startswith(("/oauth/", "/auth/")):
            try:
                await checkpoints.flush()
            except Exception:
                return JSONResponse({"detail": "Cloud persistence could not be confirmed. Refresh before retrying an action."}, status_code=503)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path == '/auth/github/register':
            response.headers['Content-Security-Policy'] += ' https://github.com'
        if request.url.path.startswith(("/api/", "/oauth/", "/auth/", "/broker/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ConnectorError)
    async def connector_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if request.url.path.startswith('/api/credentials'):
            return JSONResponse({'detail':'Invalid credential form. Choose who can use it and when it can be reused, then check the required fields.'},status_code=422)
        if request.url.path.startswith('/api/skills'):
            return JSONResponse({'detail':'Invalid skill. Choose Personal or Organization, use a lowercase-hyphenated name, a description up to 320 characters, and Markdown instructions up to 32,000 characters.'},status_code=422)
        return await request_validation_exception_handler(request,exc)

    @app.get("/health")
    async def health():
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
                                   "identity": info.get("identity"), "user_id": user_id, "google_enabled": settings.google_enabled(),
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
        response = JSONResponse({"ok": True})
        response.delete_cookie("workspace_session", path="/")
        return response

    def missing_cloud():
        missing = settings.missing_cloud()
        if security.local:
            missing.append("PUBLIC_URL (reachable HTTPS address)")
        return missing

    @app.get("/api/config")
    async def config(request: Request):
        security.require(request)
        missing = missing_cloud()
        from .harnesses import choices
        return {"harnesses": choices(), "harness": settings.agent_harness, "cloud_ready": not missing, "missing": missing, "model": settings.harness_model(settings.agent_harness), "models": settings.model_choices(),
                "public_url": settings.public_url, "max_concurrent_runs": settings.max_concurrent_runs,
                "max_parallel_agents": settings.max_parallel_agents, "parallel_agents_enabled": settings.temporal_enabled,
                "max_concurrent_model_requests": settings.max_concurrent_model_requests,
                "run_timeout_seconds": settings.run_timeout_seconds, "max_agent_iterations": settings.max_agent_iterations,
                "sandbox_rotation_seconds": settings.sandbox_rotation_seconds,
                "sandbox_idle_seconds": settings.sandbox_idle_seconds if settings.temporal_enabled else 0,
                "execution_engine": "Temporal" if settings.temporal_enabled else "Local worker",
                "execution_connected": manager.ready.is_set() if settings.temporal_enabled else True,
                "checkpoint_interval_seconds": settings.temporal_checkpoint_seconds if settings.temporal_enabled else None,
                "hermes_revision": settings.hermes_revision, "auth": "Google Workspace" if settings.google_enabled() else "Workspace password" if settings.workspace_password else "Local access only"}

    @app.get("/api/organization")
    async def organization(request: Request):
        security.require(request)
        return {"name": store.rows("SELECT name FROM organization WHERE id=1")[0]["name"],
                "role": security.role(request), "member_access_configured": bool(settings.workspace_member_password) or settings.google_enabled(),
                "google_signin": settings.google_enabled(),
                "slack_sessions": slack.status(),
                "activity": store.rows("SELECT provider,action,actor,created_at FROM connection_audit ORDER BY id DESC LIMIT 15")}

    @app.patch("/api/organization")
    async def rename_organization(body: OrganizationName, request: Request):
        security.require(request, mutation=True, admin=True)
        store.execute("UPDATE organization SET name=? WHERE id=1", (body.name,))
        connectors.audit("organization", "Updated organization name")
        return {"name": body.name}

    @app.get("/api/runs")
    async def runs(request: Request, focus: str = '', scope: Literal['all', 'mine'] | None = None):
        owner = session_folders.actor(request)
        if scope == 'all':
            security.require(request, admin=True)
        if scope is None:
            scope = 'all' if security.role(request) == 'admin' else 'mine'
        memberships = session_folders.memberships(owner)
        selected = store.run(focus) if re.fullmatch(r'[0-9a-f]{32}', focus) else None
        parent_id = (selected['parent_run_id'] or selected['id']) if selected else ''
        ids = store.sidebar_run_ids(owner if scope == 'mine' else None, [*memberships, parent_id])
        runs = {run_id: {**public_run(store.run(run_id)), 'folder_id': memberships.get(run_id), 'children': []} for run_id in ids}
        if ids:
            children = store.rows('SELECT id,parent_run_id,agent_label,status,mode,created_at,updated_at FROM runs WHERE parent_run_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY created_at,id', ids)
            for child in children:
                runs[child['parent_run_id']]['children'].append(child)
        return list(runs.values())

    @app.post("/api/runs", status_code=201)
    async def create(body: NewRun, request: Request):
        security.require(request, mutation=True)
        if body.side_chat_of:
            parent = store.run(body.side_chat_of)
            if not parent:
                raise HTTPException(404, 'Original session not found.')
            if not body.chat_enabled:
                raise HTTPException(422, 'Side chats require a chat session.')
        try:
            harness = body.harness or (parent['harness'] if body.side_chat_of else settings.agent_harness)
            model = settings.harness_model(harness, body.model)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if body.mode == "modal":
            if missing_cloud():
                raise HTTPException(503, "Cloud setup is incomplete. See Runtime for the missing settings.")
            connected = {item["id"] for item in connectors.list() if item["connected"] and item["enabled"]}
            if not set(body.plugins) <= connected:
                raise HTTPException(422, "Connect the selected apps before starting the task.")
        if body.mode == 'modal' and (body.repo_url or body.github_repository_id):
            if 'github' in body.plugins:
                identity = await connectors.github.selected_target({}, body.repo_url.removeprefix('https://github.com/').removesuffix('.git'), body.github_repository_id)
                body.repo_url = 'https://github.com/' + connectors.github.repository_name(identity)
            else:
                repo = await connectors.github.public_repository(body.repo_url.removeprefix('https://github.com/').removesuffix('.git'), body.github_repository_id)
                identity, body.repo_url = repo['id'], 'https://github.com/' + repo['full_name']
            body.github_repository_id = identity
        if body.mode == "modal":
            environments.choose(body.environment_id, body.repo_url, body.github_repository_id)
        user_id = store.identity(security.session_info(request))
        try:
            run = store.create_run(body.prompt, body.repo_url, body.mode, sorted(set(body.plugins)), chat_enabled=body.chat_enabled or settings.temporal_enabled, model=model, user_id=user_id,
                                   attachment_ids=body.attachment_ids, client_id=body.client_id, environment_id=body.environment_id, side_chat_of=body.side_chat_of, harness=harness, github_repository_id=body.github_repository_id)
        except ValueError as exc:
            raise HTTPException(429 if 'queue' in str(exc) else 409, str(exc))
        await checkpoints.flush()
        manager.submit(run)
        session_titles.schedule(run['id'])
        return public_run(run)

    @app.get('/api/runs/{run_id}/side-chats')
    async def side_chats(run_id: str, request: Request):
        security.require(request)
        if not store.run(run_id):
            raise HTTPException(404, 'Session not found.')
        return store.rows('SELECT id,prompt,agent_label,display_title,status,model,created_at,updated_at FROM runs WHERE side_chat_of=? ORDER BY created_at,id', (run_id,))

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        security.require(request)
        run = store.run(run_id)
        if not run:
            raise HTTPException(404, "Task not found")
        owners = store.rows('SELECT id,email,name FROM users WHERE id=?', (run['owner_id'],))
        project = environments.context(run)
        messages = public_messages(run, store.messages(run_id))
        identities.wake.set()  # Resolve newly discovered mentions in saved Slack history.
        return {**public_run(run), "events": store.events(run_id, limit=10000), "approvals": store.approvals(run_id), "messages": messages,
                'project_environment': {key: project[key] for key in ('name', 'repository', 'build_id', 'commit_sha') if key in project},
                "owner": owners[0] if owners else None, "goal": store.goal(run_id),
                "agents": coordinator.view(run_id, include_costs=security.role(request) == 'admin'),
                "credential_requests": credentials.pending(run,store.identity(security.session_info(request)),security.role(request)=='admin'),
                "slack_mirroring": slack.chat.mirroring(run_id),
                "active": manager.is_active(run_id), "has_artifact": artifact_path(run_id).exists(), "has_captures": bool(captures.listing(settings, run_id)), "slack_source": store.slack_source(run_id)}

    @app.post("/api/runs/{run_id}/messages", status_code=202)
    async def send_message(run_id: str, body: ChatMessage, request: Request):
        security.require(request, mutation=True)
        run = store.run(run_id)
        if not run:
            raise HTTPException(404, "Session not found")
        if run["mode"] == "modal" and missing_cloud():
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
            enqueue = coordinator.enqueue_child if run['parent_run_id'] else slack.chat.enqueue_web
            message, created = enqueue(run_id, body.content, body.client_id, selected_model, user_id, body.attachment_ids, body.send_now)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        await checkpoints.flush()
        # Resume even for a duplicate whose first acknowledgement was lost.
        session_titles.schedule(run_id)
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
                batch = store.events(run_id, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
                row = store.run(run_id)
                if not row["chat_enabled"] and row["status"] in TERMINAL and not manager.is_active(run_id) and len(batch) < 200:
                    yield "event: settled\ndata: {}\n\n"
                    break
                yield f"event: run-status\ndata: {json.dumps({'status': row['status'], 'active': manager.is_active(run_id), 'model': row['model'], 'active_model': row['active_model'], 'updated_at': row['updated_at'], 'active_message_id': row['active_message_id'], 'checkpoint_error': row['checkpoint_error'], 'slack_mirroring': slack.chat.mirroring(run_id)})}\n\n"
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def artifact_path(run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(404, "Task not found")
        return settings.data_dir / "artifacts" / f"{run_id}.zip"

    @app.get("/api/runs/{run_id}/artifact")
    async def artifact(run_id: str, request: Request):
        security.require(request)
        path = artifact_path(run_id)
        if not path.exists() or not store.run(run_id):
            raise HTTPException(404, "No result archive is available.")
        return FileResponse(path, media_type="application/zip", filename=f"moyai-{run_id[:8]}.zip")

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
                  else "Enabled: create and maintain session-owned pull requests" if provider == 'github'
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
        if (not run or run["mode"] != "modal" or run["status"] not in {"running", "reconnecting", "awaiting_approval"}
                or not run["token_hash"] or not hmac.compare_digest(run["token_hash"], digest(token))):
            raise HTTPException(401, "Run capability expired or invalid.")
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
        run = require_run(run_id, request)
        body = await broker_body(request, '/control')
        if isinstance(body, dict) and body.get('version') == 2:
            if body.get('receipt_only') is True:
                message_queue.acknowledge(run_id, run['active_message_id'], body.get('applied', []))
                result = {'steer_message_id': None}
            else:
                result = message_queue.live_control(run_id, run['active_message_id'], body.get('applied', []))
            await checkpoints.flush()
            return {**result, 'receipt_only_supported': True}
        target = message_queue.accept_steer(run_id, run['active_message_id'])
        if target:
            await checkpoints.flush()
        return {'steer_message_id': target}

    @app.get("/broker/{run_id}/tools")
    async def tool_list(run_id: str, request: Request):
        run = require_run(run_id, request)
        return model_tools.tools(run) + automation_tools.tools(run) + automations.tools(run) + memory.tools(run) + skills.tools(run) + credentials.tools(run) + coordinator.tools(run) + [{"name": name, "description": spec[3], "inputSchema": spec[2].model_json_schema(), "annotations": {"readOnlyHint": not spec[1]}}
                for name, spec in TOOLS.items() if spec[0] in run["plugins"] and connectors.allowed(name)]

    @app.post("/broker/{run_id}/tools/call")
    async def tool_call(run_id: str, request: Request):
        run = require_run(run_id, request)
        try:
            body = ToolCall.model_validate(await broker_body(request, '/tools/call'))
        except ValidationError:
            raise HTTPException(422, 'Invalid tool request.')
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
                raise HTTPException(422, 'Invalid automation arguments. Check the current revision, schedule and tool schema.') from None
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        if body.name in MEMORY_TOOLS:
            try:
                result = memory.call(run, body.name, body.arguments)
            except ValidationError:
                raise HTTPException(422, 'Invalid memory arguments. Check the current turn, text limits and tool schema.') from None
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
                result = skills.call(run,body.name,body.arguments)
            except ValidationError:
                raise HTTPException(422,'Invalid skill arguments. Check the tool schema, text limits and relative file paths.') from None
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
                raise HTTPException(403, 'Only a top-level Temporal session can coordinate agents.')
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
                      else await connectors.call(body.name, arguments, run=run) if body.name == 'slack_send'
                      else await connectors.call(body.name, arguments))
            store.event(run_id, "tool", f"{body.name} completed")
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
        return {"object": "list", "data": [{"id": run['active_model'] or run['model'] or settings.agent_model, "object": "model", "owned_by": "workspace"}]}

    @app.get('/broker/{run_id}/attachments/{attachment_id}')
    async def broker_attachment(run_id: str, attachment_id: str, request: Request):
        return store.attachments.broker_file(require_run(run_id, request), attachment_id)

    @app.post("/hooks/slack/events")
    async def slack_events(request: Request):
        return await slack.receive(request, missing_cloud())

    from .harness_gateway import HarnessGateway
    harness_gateway = HarnessGateway(settings=settings, store=store, spend=spend,
        checkpoints=checkpoints, require_run=require_run, read_body=broker_body,
        model_slots=model_slots, memory=memory, skills=skills, tracing=tracing, context_budget=context_budget)

    @app.post('/broker/{run_id}/context/compact')
    async def compact_context(run_id: str, request: Request):
        return await harness_gateway.forward(run_id, request, '/context/compact')

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
        # Reject before reading/charging an inference. Waiting on this server
        # would retain large request bodies and could expire sealed envelopes.
        # The sandbox retries only this explicit, unbilled admission response.
        if model_slots.locked():
            raise HTTPException(429, 'Waiting for a model request slot.',
                                headers={'X-Moyai-Model-Queue': '1', 'Retry-After': '3'})
        async with model_slots:
            return await forward_model(run_id, request)

    async def forward_model(run_id: str, request: Request):
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
        payload['messages'] = store.attachments.with_images(run, scrub_memory_history(payload['messages']))
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
        payload["model"] = selected_model
        for field in ("max_tokens", "max_completion_tokens"):
            if field in payload:
                if type(payload[field]) is not int or payload[field] < 1:
                    raise HTTPException(422, "Invalid output limit")
        try:
            checked_budget = await context_budget.check(payload, scope=run_id + '/v1/chat/completions')
        except ContextPressure as exc:
            store.event(run_id, 'context', 'Compacting before the next model request.', exc.budget)
            await checkpoints.flush()
            raise
        require_run(run_id, request)
        admitted = store.execute("UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1 WHERE id=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?) AND status IN ('running','reconnecting','awaiting_approval')",
                                 (run_id, settings.max_agent_iterations, settings.max_agent_iterations * 3))
        if not admitted:
            raise HTTPException(429, "This run reached its model request limit.")
        request_id = spend.begin(run, selected_model)
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
        status = 'unknown'
        trace_started = time.time_ns()
        trace_response = {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
            try:
                async with client.stream('POST', settings.litellm_api_base.rstrip('/') + '/chat/completions',
                                         json=payload, headers={'Authorization': f'Bearer {settings.litellm_api_key}', 'x-litellm-call-id': request_id}) as upstream:
                    spend.headers(request_id, upstream, False)
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
                        raise HTTPException(502, f'Model gateway rejected the request ({upstream.status_code}). Check model access and gateway configuration.')
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
            except httpx.HTTPError:
                raise HTTPException(502, 'Model gateway could not be reached.')
            except asyncio.CancelledError:
                status = 'interrupted'
                raise
            finally:
                # Account before returning any data, including if the sandbox
                # stopped while the already-submitted inference was completing.
                spend.finish(request_id, capture, status)
                if status == 'completed':
                    context_budget.remember(payload, capture.usage, run_id + '/v1/chat/completions')
                tracing.model(run, request_id, trace_started, body['messages'], trace_response, status)
                await checkpoints.flush()
        value = json.loads(raw_response)
        if wants_stream:
            async def relay():
                for chunk in completion_events(value):
                    if store.run(run_id)['status'] not in {'running', 'reconnecting', 'awaiting_approval'}:
                        break
                    yield chunk
            return StreamingResponse(relay(), media_type='text/event-stream')
        return JSONResponse(value)

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
    if (not active or not isinstance(message_id, int) or not isinstance(result, dict)
            or result.get('message_id') != message_id or result.get('completed') is not True
            or any(result.get(key) for key in ('continuation', 'steer_message_id', 'startup_retry', 'wait_group', 'wait_credential'))
            or not isinstance(result.get('message'), str) or not result['message'].strip()):
        return messages
    # Keep settlement, model history and Slack delivery owned by finish_message.
    # Its user-status update removes this projection, even across a stale run read.
    reply = {'id': -message_id, 'role': 'assistant', 'content': run.get('summary') or result['message'],
             'status': 'save_failed' if result.get('save_failed') else 'saving',
             'model': run.get('active_model') or active.get('model', ''),
             'created_at': run['updated_at'], 'attachments': []}
    index = next((i for i, m in enumerate(messages) if m['role'] == 'user' and m['status'] == 'queued'), len(messages))
    return messages[:index] + [reply] + messages[index:]


def public_run(run):
    return {key: value for key, value in run.items() if key not in {"token_hash", "pending_result", "side_chat_context", "title_attempted_at"}}


app = create_app()
