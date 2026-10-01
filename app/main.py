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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .connectors import Connectors, ConnectorError, TOOLS
from .db import Store, now
from .runner import RunManager, TERMINAL
from .persistence import Checkpoints, restore_checkpoint
from .security import Security, digest
from .google_sso import GoogleSignIn
from .access_logging import configure_access_logging
from .slack import SlackSessions
from .spend import Spend, UsageCapture, completion_events
from .identities import SlackIdentities
from .agents import AgentCoordinator, TOOLS as AGENT_TOOLS
from .github_setup import routes as github_routes
from .credentials import Credentials, CredentialRequest, Invoke, TOOLS as CREDENTIAL_TOOLS
from .skills import Skills
from .attachments import upload_limit
from sandbox.broker_transport import CONTENT_TYPE, MAX_BODY, MAX_WIRE, unseal

STATIC = Path(__file__).parent / "static"
Provider = Literal["linear", "slack", "notion", "github"]
AttachmentId = Annotated[str, Field(pattern=r'^[0-9a-f]{32}$')]


class NewRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=3, max_length=16000)
    repo_url: str = Field(default="", max_length=500)
    mode: Literal["demo", "modal"] = "demo"
    plugins: list[Provider] = Field(default_factory=list, max_length=4)
    chat_enabled: bool = True
    model: str | None = Field(default=None, max_length=120)
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)
    client_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{8,80}$')

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
    content: str = Field(min_length=1, max_length=16000)
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")
    model: str | None = Field(default=None, max_length=120)
    attachment_ids: list[AttachmentId] = Field(default_factory=list, max_length=5)


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
    security = Security(settings)
    connectors = Connectors(store, security, settings)
    if settings.temporal_enabled:
        from .temporal_runtime import TemporalRunManager
        manager = TemporalRunManager(store, settings)
    else:
        manager = RunManager(store, settings)
    checkpoints = Checkpoints(store, settings)
    spend = Spend(store, settings, security, checkpoints)
    coordinator = AgentCoordinator(store, settings, manager)
    manager.coordinator = coordinator
    credentials = Credentials(store, security, settings, manager, checkpoints)
    manager.credentials = credentials
    skills = Skills(store, security, credentials.same_requester)
    model_slots = asyncio.Semaphore(settings.max_concurrent_model_requests)
    credentials.slots = model_slots
    manager.persist = checkpoints.flush
    store.execute("INSERT OR IGNORE INTO organization(id,name) VALUES(1,?)", (settings.organization_name,))
    slack = SlackSessions(store, connectors, manager, checkpoints, settings)
    identities = SlackIdentities(store, connectors, settings, security, checkpoints)
    slack.identities = identities
    manager.prepare_context = slack.prepare
    login_attempts = []

    @asynccontextmanager
    async def lifespan(app):
        await manager.recover()
        await checkpoints.flush()
        slack.recover()
        identities.start()
        store.execute("UPDATE model_requests SET status='interrupted' WHERE status='pending'")
        watcher = asyncio.create_task(checkpoints.watch()) if settings.checkpoint_dir else None
        try:
            yield
        finally:
            await identities.close()
            await slack.shutdown()
            await manager.shutdown()
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            await checkpoints.flush()

    app = FastAPI(title="Moyai Devin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    google = GoogleSignIn(settings, security, store)
    app.state.google_signin = google
    app.include_router(google.routes())
    app.include_router(spend.routes())
    app.include_router(identities.routes())
    app.include_router(credentials.routes())
    app.state.credentials = credentials
    app.include_router(skills.routes())
    app.include_router(store.attachments.routes(security, settings))
    app.state.skills = skills
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
        limit = MAX_WIRE if request.url.path.startswith('/broker/') and request.headers.get('content-type') == CONTENT_TYPE else MAX_BODY
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
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
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
            return JSONResponse({'detail':'Invalid credential form. Choose who can use a new key and check the provider and required fields.'},status_code=422)
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
        return {"cloud_ready": not missing, "missing": missing, "model": settings.resolve_model(), "models": settings.model_choices(),
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
    async def runs(request: Request, focus: str = ''):
        security.require(request)
        ids = [row['id'] for row in store.rows("SELECT id FROM runs WHERE parent_run_id='' ORDER BY created_at DESC LIMIT 100")]
        selected = store.run(focus) if re.fullmatch(r'[0-9a-f]{32}', focus) else None
        parent_id = (selected['parent_run_id'] or selected['id']) if selected else ''
        if parent_id and parent_id not in ids and store.run(parent_id):
            ids.append(parent_id)
        runs = {run_id: {**public_run(store.run(run_id)), 'children': []} for run_id in ids}
        if ids:
            children = store.rows('SELECT id,parent_run_id,agent_label,status,mode,created_at,updated_at FROM runs WHERE parent_run_id IN (' + ','.join('?' for _ in ids) + ') ORDER BY created_at,id', ids)
            for child in children:
                runs[child['parent_run_id']]['children'].append(child)
        return list(runs.values())

    @app.post("/api/runs", status_code=201)
    async def create(body: NewRun, request: Request):
        security.require(request, mutation=True)
        try:
            model = settings.resolve_model(body.model)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if body.mode == "modal":
            if missing_cloud():
                raise HTTPException(503, "Cloud setup is incomplete. See Runtime for the missing settings.")
            connected = {item["id"] for item in connectors.list() if item["connected"] and item["enabled"]}
            if not set(body.plugins) <= connected:
                raise HTTPException(422, "Connect the selected apps before starting the task.")
        user_id = store.identity(security.session_info(request))
        try:
            run = store.create_run(body.prompt, body.repo_url, body.mode, sorted(set(body.plugins)), chat_enabled=body.chat_enabled or settings.temporal_enabled, model=model, user_id=user_id,
                                   attachment_ids=body.attachment_ids, client_id=body.client_id)
        except ValueError as exc:
            raise HTTPException(429 if 'queue' in str(exc) else 409, str(exc))
        await checkpoints.flush()
        manager.submit(run)
        return public_run(run)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        security.require(request)
        run = store.run(run_id)
        if not run:
            raise HTTPException(404, "Task not found")
        owners = store.rows('SELECT id,email,name FROM users WHERE id=?', (run['owner_id'],))
        return {**public_run(run), "events": store.events(run_id, limit=10000), "approvals": store.approvals(run_id), "messages": store.messages(run_id),
                "owner": owners[0] if owners else None,
                "agents": coordinator.view(run_id, include_costs=security.role(request) == 'admin'),
                "credential_requests": credentials.pending(run,store.identity(security.session_info(request)),security.role(request)=='admin'),
                "slack_mirroring": slack.chat.mirroring(run_id),
                "active": manager.is_active(run_id), "has_artifact": artifact_path(run_id).exists(), "slack_source": store.slack_source(run_id)}

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
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        try:
            user_id = store.identity(security.session_info(request))
            enqueue = coordinator.enqueue_child if run['parent_run_id'] else slack.chat.enqueue_web
            message, created = enqueue(run_id, body.content, body.client_id, selected_model, user_id, body.attachment_ids)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        await checkpoints.flush()
        # Resume even for a duplicate whose first acknowledgement was lost.
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
                yield f"event: run-status\ndata: {json.dumps({'status': row['status'], 'active': manager.is_active(run_id), 'model': row['model'], 'active_model': row['active_model'], 'active_message_id': row['active_message_id'], 'checkpoint_error': row['checkpoint_error'], 'slack_mirroring': slack.chat.mirroring(run_id)})}\n\n"
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
        return FileResponse(path, media_type="application/zip", filename=f"moyai-devin-{run_id[:8]}.zip")

    @app.get("/api/connections")
    async def connections(request: Request):
        security.require(request)
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
        action = "Paused for all sessions" if not body.enabled else "Enabled: read only" if body.read_only else "Enabled: writes require admin approval"
        connectors.audit(provider, action)
        return connectors.policy(provider)

    @app.post("/api/connections/{provider}/check")
    async def check_connection(provider: Provider, request: Request):
        security.require(request, mutation=True, admin=True)
        try:
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
        count = store.execute("UPDATE approvals SET status=? WHERE id=? AND status='pending' AND EXISTS(SELECT 1 FROM runs WHERE runs.id=approvals.run_id AND runs.status IN ('running','awaiting_approval'))", (status, approval_id))
        if not count:
            raise HTTPException(409, "This approval was already resolved or the run has ended.")
        row = store.rows("SELECT run_id,tool FROM approvals WHERE id=?", (approval_id,))[0]
        store.event(row["run_id"], "approval", f"{row['tool']}: {status}")
        return {"status": status}

    def require_run(run_id, request):
        run = store.run(run_id)
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if (not run or run["mode"] != "modal" or run["status"] not in {"running", "awaiting_approval"}
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
        if len(raw) > MAX_BODY:
            raise HTTPException(413, 'Broker request too large.')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, 'Invalid broker request JSON.')

    from .github_git import routes as git_routes
    app.include_router(git_routes(connectors.github, require_run))

    @app.get("/broker/{run_id}/tools")
    async def tool_list(run_id: str, request: Request):
        run = require_run(run_id, request)
        return skills.tools(run) + credentials.tools(run) + coordinator.tools(run) + [{"name": name, "description": spec[3], "inputSchema": spec[2].model_json_schema(), "annotations": {"readOnlyHint": not spec[1]}}
                for name, spec in TOOLS.items() if spec[0] in run["plugins"] and connectors.allowed(name)]

    @app.post("/broker/{run_id}/tools/call")
    async def tool_call(run_id: str, request: Request):
        run = require_run(run_id, request)
        try:
            body = ToolCall.model_validate(await broker_body(request, '/tools/call'))
        except ValidationError:
            raise HTTPException(422, 'Invalid tool request.')
        if body.name in {'skills_load','skills_save','skills_read_file'}:
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
                if body.name == 'credentials_request':
                    result = credentials.request(run,CredentialRequest.model_validate(body.arguments))
                    await checkpoints.flush()
                    return result
                status,result = await credentials.invoke(run,Invoke.model_validate(body.arguments))
                return {'status_code':status,'response':result}
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
            run = {**run, 'github_connection_version': connectors.github.connection_version()}
        approval_id = None
        if write:
            approval_id = uuid4().hex
            store.execute("INSERT INTO approvals(id,run_id,tool,arguments,status,created_at) VALUES(?,?,?,?,?,?)",
                          (approval_id, run_id, body.name, json.dumps(arguments), "pending", now()))
            store.update_run(run_id, status="awaiting_approval")
            store.event(run_id, "approval", f"Approval needed: {body.name}", {"approval_id": approval_id})
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if await request.is_disconnected():
                    store.execute("UPDATE approvals SET status='expired' WHERE id=? AND status IN ('pending','approved')", (approval_id,))
                    store.execute("UPDATE runs SET status='running' WHERE id=? AND status='awaiting_approval' AND NOT EXISTS(SELECT 1 FROM approvals WHERE run_id=? AND status='pending')", (run_id, run_id))
                    return {"error": "Tool connection closed. Action was not sent."}
                current = store.run(run_id)
                row = store.rows("SELECT status FROM approvals WHERE id=?", (approval_id,))[0]
                if current["status"] not in {"running", "awaiting_approval"}:
                    return {"error": "Run stopped. Action was not sent."}
                if row["status"] in {"approved", "denied", "expired"}:
                    break
                await asyncio.sleep(0.25)
            else:
                store.execute("UPDATE approvals SET status='expired' WHERE id=? AND status='pending'", (approval_id,))
            claim = store.execute("UPDATE approvals SET status='executing' WHERE id=? AND status='approved' AND EXISTS(SELECT 1 FROM runs WHERE runs.id=approvals.run_id AND runs.status IN ('running','awaiting_approval'))", (approval_id,))
            remaining = store.rows("SELECT COUNT(*) AS n FROM approvals WHERE run_id=? AND status='pending'", (run_id,))[0]["n"]
            store.execute("UPDATE runs SET status='running' WHERE id=? AND status='awaiting_approval' AND ?=0", (run_id, remaining))
            if not claim:
                store.event(run_id, "tool", f"{body.name} was not approved; no action sent")
                return {"error": "Action denied, expired, or cancelled."}
        require_run(run_id, request)
        if not connectors.allowed(body.name):
            if approval_id:
                store.execute("UPDATE approvals SET status='expired' WHERE id=?", (approval_id,))
            return {"error": "The organization connection changed. No action was sent."}
        # A durable executing record prevents an uncertain external write being
        # mistaken for a pending approval after a container restart.
        if approval_id:
            await checkpoints.flush()
        try:
            result = (await connectors.github.call(run, body.name, arguments) if provider == 'github'
                      else await connectors.call(body.name, arguments))
            if approval_id:
                store.execute("UPDATE approvals SET status='completed',result=? WHERE id=?", (json.dumps(result)[:12000], approval_id))
            store.event(run_id, "tool", f"{body.name} completed")
            return result
        except Exception as exc:
            message = str(exc) if isinstance(exc, ConnectorError) else f"App operation could not be confirmed ({type(exc).__name__})."
            if approval_id:
                store.execute("UPDATE approvals SET status='uncertain',result=? WHERE id=?", (message, approval_id))
            store.event(run_id, "error", f"{body.name}: {message}")
            return {"error": message, "outcome_uncertain": write, "instruction": "Verify the destination before retrying a write."}

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
        admitted = store.execute("UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1 WHERE id=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?) AND status IN ('running','awaiting_approval')",
                                 (run_id, settings.max_agent_iterations, settings.max_agent_iterations * 3))
        if not admitted:
            raise HTTPException(429, "This run reached its model request limit.")
        allowed = {"messages", "tools", "tool_choice", "parallel_tool_calls", "temperature", "top_p", "stop", "stream", "stream_options", "response_format", "reasoning_effort", "max_tokens", "max_completion_tokens", "seed"}
        payload = {key: value for key, value in body.items() if key in allowed}
        payload['messages'] = store.attachments.with_images(run, payload['messages'])
        skill_context = skills.context(run)
        if skill_context:
            # The original platform system instructions stay last and take
            # precedence. Skill definitions never enter sandbox tool results,
            # conversation snapshots, event logs or Temporal workflow history.
            payload['messages'] = [{'role':'system','content':skill_context}, *payload['messages']]
        payload["model"] = selected_model
        for field in ("max_tokens", "max_completion_tokens"):
            if field in payload:
                if not isinstance(payload[field], int) or payload[field] < 1:
                    raise HTTPException(422, "Invalid output limit")
                payload[field] = min(payload[field], 16000)
        if not ({"max_tokens", "max_completion_tokens"} & payload.keys()):
            payload["max_tokens"] = 8192
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
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
            try:
                async with client.stream('POST', settings.litellm_api_base.rstrip('/') + '/chat/completions',
                                         json=payload, headers={'Authorization': f'Bearer {settings.litellm_api_key}', 'x-litellm-call-id': request_id}) as upstream:
                    spend.headers(request_id, upstream, False)
                    if upstream.status_code >= 400:
                        status = 'failed'
                        raise HTTPException(502, f'Model gateway rejected the request ({upstream.status_code}). Check model access and gateway configuration.')
                    # Bound memory even if a provider ignores our output limit.
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
            except httpx.HTTPError:
                raise HTTPException(502, 'Model gateway could not be reached.')
            except asyncio.CancelledError:
                status = 'interrupted'
                raise
            finally:
                # Account before returning any data, including if the sandbox
                # stopped while the already-submitted inference was completing.
                spend.finish(request_id, capture, status)
                await checkpoints.flush()
        value = json.loads(raw_response)
        if wants_stream:
            async def relay():
                for chunk in completion_events(value):
                    if store.run(run_id)['status'] not in {'running', 'awaiting_approval'}:
                        break
                    yield chunk
            return StreamingResponse(relay(), media_type='text/event-stream')
        return JSONResponse(value)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def public_run(run):
    return {key: value for key, value in run.items() if key not in {"token_hash", "pending_result"}}


app = create_app()
