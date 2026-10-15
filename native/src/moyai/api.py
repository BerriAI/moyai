"""Moyai's authenticated product API around native conversations and workflows."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, TypeAdapter, field_validator
from pydantic_ai import ConversationTypeAdapter, DeferredToolResults
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai_harness.ask_user import AskUserRequest, AskUserResponse, ask_user_result
from sqlalchemy import func, select, update
from temporalio.service import RPCError

from .computer import routes as computer_routes
from .connections import ConnectionPolicy
from .contracts import ApprovalReply, EffectResolution, NewMessage, NewSession, QueuedEdit, SessionEdit
from .images import routes as image_routes
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
from .presentation import public_event, redact, saved_activity
from .runtime import Runtime
from .security import check_password
from .settings import Settings
from .sidebar import folders_for, lock_sidebar
from .sidebar import routes as sidebar_routes
from .slack import dispatcher as slack_dispatcher
from .slack import routes as slack_routes
from .worker import dispatcher, reconcile_workflows

COOKIE = "moyai_session"
STATIC = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=200)
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


def session_view(session: Session) -> dict[str, Any]:
    return {
        "id": session.id, "title": session.title, "owner_id": session.owner_id,
        "visibility": session.visibility, "surface": session.surface,
        "model": session.model, "status": session.status,
        "active_turn": session.active_turn, "archived": session.archived,
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if owned:
            await runtime.start()
        tasks = [
            asyncio.create_task(dispatcher(runtime)),
            asyncio.create_task(reconcile_workflows(runtime)),
        ] if run_dispatcher else []
        if run_dispatcher and runtime.settings.slack_bot_token.get_secret_value():
            tasks.append(asyncio.create_task(slack_dispatcher(runtime)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if owned:
                await runtime.close()

    app = FastAPI(title="Moyai", lifespan=lifespan)
    app.state.runtime = runtime
    app.include_router(slack_routes(runtime))

    async def actor(request: Request) -> User:
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

    app.include_router(image_routes(runtime, actor))
    app.include_router(computer_routes(runtime, actor))
    app.include_router(sidebar_routes(runtime, actor))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not request.url.path.startswith("/hooks/"):
            origin = request.headers.get("origin")
            expected = runtime.settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")
            if origin and origin.rstrip("/") != expected:
                return Response("Origin is not allowed.", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health():
        async with runtime.database.sessions() as db:
            await db.execute(select(1))
        return {"status": "ok", "engine": "pydantic-ai", "durability": "temporal"}

    @app.post("/api/login")
    async def login(body: Login, request: Request, response: Response):
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
        return {"csrf": csrf}

    @app.post("/api/logout")
    async def logout(response: Response, user: User = Depends(actor)):
        response.delete_cookie(COOKIE)
        return {"signed_out": True}

    @app.get("/api/me")
    async def me(request: Request, user: User = Depends(actor)):
        identity = runtime.security.read_session(request.cookies[COOKIE])
        if identity is None:
            raise HTTPException(401, "Your session expired.")
        return {
            "user": {"id": user.id, "name": user.display_name, "role": user.role},
            "csrf": identity["csrf"], "models": runtime.settings.models,
            "default_model": runtime.settings.default_model,
        }

    @app.get("/api/sessions")
    async def sessions(archived: bool = False, user: User = Depends(actor)):
        rows = await runtime.database.list_sessions(user, archived=archived)
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
        return [{
            **session_view(row),
            "pinned": preferences[row.id].pinned if row.id in preferences else False,
            "folder": preferences[row.id].folder if row.id in preferences else "",
            "participated": row.owner_id != user.id and row.id in participated,
            "unread": bool(reads[row.id].unread or latest.get(row.id, 0) > reads[row.id].message_id)
            if row.id in reads else False,
        } for row in rows]

    @app.post("/api/sessions")
    async def create_session(body: NewSession, user: User = Depends(actor)):
        return session_view(await runtime.database.create_session(user, body))

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str, user: User = Depends(actor)):
        row = await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turns = list((await db.scalars(select(Turn).where(
                Turn.session_id == session_id
            ).order_by(Turn.created_at.desc()).limit(100))).all())
            if row.active_turn and not any(turn.id == row.active_turn for turn in turns):
                active_turn = await db.get(Turn, row.active_turn)
                if active_turn is not None:
                    turns.append(active_turn)
            result: dict[str, Any] = {
                "session": session_view(row), "turns": [turn_view(turn) for turn in turns],
            }
            preference = await db.get(SessionPreference, (user.id, session_id))
            result["session"]["pinned"] = bool(preference and preference.pinned)
            active_turn = next((turn for turn in turns if turn.id == row.active_turn), None)
            unsettled = list((await db.scalars(select(Effect).where(
                Effect.session_id == session_id, Effect.state == "pending"
            ))).all())
            result["has_unsettled_effects"] = bool(unsettled)
            result["effects"] = [{
                "id": effect.id, "turn_id": effect.turn_id, "tool_name": effect.tool_name,
                "arguments": json.loads(runtime.security.unseal(effect.arguments)),
            } for effect in unsettled if effect.actor_id == user.id]
        if turns and turns[0].actor_id == user.id and turns[0].status != "running":
            snapshot = await runtime.stores.steps.latest_snapshot(run_id=turns[0].id)
            if snapshot is not None:
                result["activity"] = saved_activity(snapshot.messages, turns[0].id)
        if (
            row.status == "waiting" and row.conversation
            and active_turn is not None and active_turn.actor_id == user.id
        ):
            conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(row.conversation))
            result["pending"] = TypeAdapter(Any).dump_python(conversation.deferred_tool_requests, mode="json")
        return result

    @app.patch("/api/sessions/{session_id}")
    async def edit_session(session_id: str, body: SessionEdit, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            await lock_sidebar(db, user.id)
            row = await runtime.database.allowed_session(db, session_id, user)
            if body.title is not None or body.archived is not None:
                if row.owner_id != user.id:
                    raise HTTPException(403, "Only the owner can change this conversation.")
                if body.title is not None:
                    row.title = body.title
                if body.archived is not None:
                    row.archived = body.archived
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
        return {"updated": True}

    @app.get("/api/sessions/{session_id}/messages")
    async def messages(session_id: str, after: int = 0, user: User = Depends(actor)):
        return await runtime.database.messages(user, session_id, after=after)

    @app.post("/api/sessions/{session_id}/messages", status_code=202)
    async def send(session_id: str, body: NewMessage, user: User = Depends(actor)):
        return turn_view(await runtime.database.submit(user, session_id, body))

    @app.patch("/api/turns/{turn_id}")
    async def edit_queued(turn_id: str, body: QueuedEdit, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.actor_id != user.id:
                raise HTTPException(404, "Message not found.")
            await runtime.database.allowed_session(db, turn.session_id, user)
            changed = await db.scalar(update(Turn).where(
                Turn.id == turn.id, Turn.status == "queued"
            ).values(prompt=runtime.security.seal(body.prompt)).returning(Turn.id))
            if changed is None:
                raise HTTPException(409, "The message was already admitted. Send a correction instead.")
            await db.execute(update(Message).where(
                Message.turn_id == turn.id, Message.role == "user"
            ).values(content=runtime.security.seal(body.prompt)))
        return {"updated": True}

    @app.post("/api/turns/{turn_id}/steer", status_code=202)
    async def steer_queued(turn_id: str, user: User = Depends(actor)):
        return turn_view(await runtime.database.steer_queued(user, turn_id))

    @app.delete("/api/turns/{turn_id}")
    async def cancel_queued(turn_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.actor_id != user.id:
                raise HTTPException(404, "Message not found.")
            await runtime.database.allowed_session(db, turn.session_id, user)
            changed = await db.scalar(update(Turn).where(
                Turn.id == turn.id, Turn.status == "queued"
            ).values(status="stopped", finished_at=now()).returning(Turn.id))
            if changed is None:
                raise HTTPException(409, "The message was already admitted. Stop the active turn instead.")
        return {"cancelled": True}

    @app.post("/api/sessions/{session_id}/stop", status_code=202)
    async def stop(session_id: str, user: User = Depends(actor)):
        workflow_id = await runtime.database.stop(user, session_id)
        if workflow_id:
            try:
                client = await runtime.connect()
                await client.get_workflow_handle(workflow_id).cancel()
            except RPCError:
                # The persisted Stop already revoked authority. Dispatcher reconciliation follows.
                pass
        return {"status": "stopping"}

    @app.post("/api/sessions/{session_id}/answers", status_code=202)
    async def answer(session_id: str, body: ApprovalReply, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            turn = await db.get(Turn, body.turn_id)
            if not turn or turn.session_id != session_id or not turn.conversation:
                raise HTTPException(409, "This conversation is not waiting for an answer.")
            if turn.actor_id != user.id:
                raise HTTPException(403, "Only the original requester may answer this request.")
            conversation = ConversationTypeAdapter.validate_json(runtime.security.unseal(turn.conversation))
            pending = conversation.deferred_tool_requests
            if pending is None:
                raise HTTPException(409, "There are no pending requests.")
            questions = {call.tool_call_id: call for call in pending.calls}
            calls = {}
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
                results = pending.build_results(approvals=body.approvals, calls=calls)
            except (ValueError, KeyError) as error:
                raise HTTPException(422, "Answer every current question or approval.") from error
        resumed = await runtime.database.resume(
            user, session_id, turn.id, body.request_key,
            TypeAdapter(DeferredToolResults).dump_python(results, mode="json"),
        )
        return turn_view(resumed)

    @app.get("/api/sessions/{session_id}/events")
    async def events(
        request: Request, session_id: str, turn_id: str, offset: int = 0,
        user: User = Depends(actor),
    ):
        await runtime.database.session(user, session_id)
        async with runtime.database.sessions() as db:
            turn = await db.get(Turn, turn_id)
            if turn is None or turn.session_id != session_id:
                raise HTTPException(404, "Turn not found.")
            if turn.actor_id != user.id:
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
                        client, handle, from_offset=next_offset
                    ) as native:
                        async for event in native:
                            next_offset = native.offset + 1
                            if isinstance(event, AgentRunResultEvent):
                                finished = event.result.run_id == turn.id
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

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/connections")
    async def connections(user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            rows = await db.scalars(select(Connection).where(
                (Connection.owner_id == user.id) | Connection.shared.is_(True)
            ))
            return [connection_view(row) for row in rows]

    @app.post("/api/connections")
    async def create_connection(body: ConnectionInput, user: User = Depends(actor)):
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
        return connection_view(row)

    @app.put("/api/connections/{connection_id}")
    async def edit_connection(connection_id: str, body: ConnectionInput, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            row = await db.get(Connection, connection_id)
            if row is None or row.owner_id != user.id:
                raise HTTPException(404, "Connection not found.")
            if body.kind != row.kind:
                raise HTTPException(422, "Create a new connection to change its service.")
            if body.shared and user.role != "admin":
                raise HTTPException(403, "Only administrators can publish a shared connection.")
            row.name, row.enabled = body.name, body.enabled
            row.shared, row.read_only = body.shared, body.read_only
            if body.credential.get_secret_value():
                row.credential = runtime.security.seal(body.credential.get_secret_value())
            row.policy = body.policy.model_dump_json()
            row.revision += 1
        return connection_view(row)

    @app.get("/api/sessions/{session_id}/files")
    async def files(session_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            rows = await db.scalars(select(Artifact).where(Artifact.session_id == session_id))
            return [{
                "id": row.id, "name": row.name, "media_type": row.media_type,
                "size": row.size, "created_at": row.created_at,
            } for row in rows]

    @app.get("/api/sessions/{session_id}/plan")
    async def plan(session_id: str, user: User = Depends(actor)):
        await runtime.database.session(user, session_id)
        return TypeAdapter(Any).dump_python(
            await runtime.stores.plan(session_id).get_items(), mode="json"
        )

    @app.get("/api/sessions/{session_id}/effects")
    async def effects(session_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            await runtime.database.allowed_session(db, session_id, user)
            rows = await db.scalars(select(Effect).where(
                Effect.session_id == session_id, Effect.actor_id == user.id,
                Effect.state == "pending",
            ).order_by(Effect.created_at))
            return [{
                "id": row.id, "turn_id": row.turn_id, "tool_name": row.tool_name,
                "arguments": json.loads(runtime.security.unseal(row.arguments)),
                "created_at": row.created_at,
            } for row in rows]

    @app.post("/api/effects/{effect_id}/resolve")
    async def resolve_effect(effect_id: str, body: EffectResolution, user: User = Depends(actor)):
        async with runtime.database.sessions.begin() as db:
            row = await db.get(Effect, effect_id)
            if row is None or row.actor_id != user.id:
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
        return {"recorded": True}

    @app.get("/api/files/{artifact_id}")
    async def download(artifact_id: str, user: User = Depends(actor)):
        async with runtime.database.sessions() as db:
            row = await db.get(Artifact, artifact_id)
            if row is None:
                raise HTTPException(404, "File not found.")
            await runtime.database.allowed_session(db, row.session_id, user)
            content = await runtime.objects.get(row.digest)
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
