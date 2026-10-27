"""Authenticated, revocable noVNC streams to the existing Modal computer."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from app.db import drain
from app.public_url import origin
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter
from pydantic_ai.workspaces import WorkspaceRef, WorkspaceUnavailableError
from pydantic_ai_harness import ModalSandbox
from sqlalchemy import update
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .computer import DESKTOP_VERSION, authorize
from .models import ComputerDesktop, ComputerLease, ComputerOperation, Session, User, new_id

if TYPE_CHECKING:
    from .product_identity import ProductGrant
    from .runtime import Runtime

VIEW_PORT = 6081
CONTROL_PORT = 6080
RECHECK_SECONDS = 1
MAX_CLIENT_MESSAGE = 1024 * 1024
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamAccess:
    session_id: str
    actor: User
    client_id: str
    workspace: str
    epoch: str | None
    session_token: str = field(default="", repr=False)
    product_request: Request | None = field(default=None, repr=False, compare=False)
    product_grant: ProductGrant | None = field(default=None, repr=False)

    @property
    def controlling(self) -> bool:
        return self.epoch is not None


async def bind(
    runtime: Runtime, session_id: str, actor: User, client_id: str, *, controlling: bool,
    session_token: str = "",
    product_request: Request | None = None, product_grant: ProductGrant | None = None,
) -> StreamAccess:
    authorized = await authorize(runtime, session_id, actor)
    async with runtime.database.sessions.begin() as db:
        if controlling:
            await db.execute(update(Session).where(
                Session.id == session_id,
            ).values(revision=Session.revision + 1))
        session = await runtime.database.allowed_session(db, session_id, actor)
        desktop = await db.get(ComputerDesktop, session_id)
        if not session.workspace or session.workspace != authorized.workspace:
            raise HTTPException(409, "The computer changed. Reopen its panel.")
        ref = TypeAdapter(WorkspaceRef).validate_json(session.workspace)
        if not desktop or desktop.workspace_id != ref.id or desktop.version != DESKTOP_VERSION:
            raise HTTPException(409, "Open the computer before connecting.")
        epoch = None
        if controlling:
            operation = await db.get(ComputerOperation, session_id)
            if operation and operation.expires_at > time.time():
                raise HTTPException(409, "Wait for the current desktop action before reconnecting control.")
            lease = await db.get(ComputerLease, session_id)
            if (
                lease is None or lease.actor_id != actor.id or lease.expires_at <= time.time()
                or desktop.control_client != client_id
            ):
                raise HTTPException(403, "Take control in this browser before sending input.")
            # Reconnect fences the previous socket. Its delayed finalizer must
            # not release the new connection's lease.
            epoch = desktop.control_epoch = new_id()
            session.snapshot = None
        return StreamAccess(
            session_id, actor, client_id, session.workspace, epoch, session_token,
            product_request, product_grant,
        )


async def authenticate_socket(
    runtime: Runtime, socket: WebSocket, *, cookie_name: str,
) -> tuple[User, str, Request | None, ProductGrant | None]:
    """HTTP middleware does not protect upgrades; authenticate the original assertion here."""
    if runtime.settings.product_mode == "existing" and runtime.product_identity is None:
        raise HTTPException(503, "Canonical product authority is unavailable.")
    if runtime.product_identity is not None:
        request = Request({
            **socket.scope, "type": "http", "method": "GET",
            "scheme": "https" if socket.url.scheme == "wss" else "http", "state": {},
        })
        principal = await runtime.product_identity.authenticate(request, mutation=False)
        async with runtime.database.sessions.begin() as db:
            actor = await runtime.product_identity.project_native_user(db, principal)
        return actor, "", request, principal.grant
    token = socket.cookies.get(cookie_name, "")
    identity = runtime.security.read_session(token)
    if not identity:
        raise HTTPException(401, "Sign in to view this computer.")
    actor = await runtime.database.user(identity["user"])
    if actor.auth_version != identity["version"]:
        raise HTTPException(401, "Your sign-in expired.")
    return actor, token, None, None


async def validate(runtime: Runtime, access: StreamAccess) -> None:
    if access.product_grant is not None:
        identity = runtime.product_identity
        if identity is None or access.product_request is None:
            raise HTTPException(401, "The product sign-in configuration changed.")
        # Re-authenticate the original cookie/CF assertion, including their
        # expiry. Never replace the admitted grant with a later role/mapping.
        principal = await identity.authenticate(access.product_request, mutation=False)
        if principal.grant != access.product_grant:
            raise HTTPException(401, "Your product authority changed.")
    elif runtime.product_identity is not None:
        raise HTTPException(401, "A verified product sign-in is required.")
    elif access.session_token:
        identity = runtime.security.read_session(access.session_token)
        if not identity or identity["user"] != access.actor.id or identity["version"] != access.actor.auth_version:
            raise HTTPException(401, "Your sign-in expired.")
    # authorize also rechecks the actor's retained canonical grant and native
    # local enabled/auth version, for both streams and ordinary HTTP actions.
    session = await authorize(runtime, access.session_id, access.actor)
    if session.workspace != access.workspace:
        raise HTTPException(403, "This conversation's computer changed.")
    if access.controlling:
        async with runtime.database.sessions() as db:
            lease = await db.get(ComputerLease, access.session_id)
            desktop = await db.get(ComputerDesktop, access.session_id)
            if (
                lease is None or lease.actor_id != access.actor.id or lease.expires_at <= time.time()
                or desktop is None or desktop.control_epoch != access.epoch
                or desktop.control_client != access.client_id
            ):
                raise HTTPException(403, "Computer control was released or changed.")


async def release(runtime: Runtime, access: StreamAccess) -> None:
    if not access.controlling:
        return
    async with runtime.database.sessions.begin() as db:
        await db.execute(update(Session).where(
            Session.id == access.session_id,
        ).values(revision=Session.revision + 1))
        desktop = await db.get(ComputerDesktop, access.session_id)
        if desktop and desktop.control_epoch == access.epoch and desktop.control_client == access.client_id:
            # A hidden panel or network interruption suspends its stream, not
            # its human ownership. Explicit release/unmount relinquishes this
            # client's lease; abandoned clients expire normally. Keep pending
            # HTTP actions and the browser identity fenced as well.
            desktop.control_epoch = new_id()


@asynccontextmanager
async def upstream(access: StreamAccess) -> AsyncIterator[ClientConnection]:
    reference = TypeAdapter(WorkspaceRef).validate_json(access.workspace)
    if reference.provider != "modal":
        raise HTTPException(409, "Desktop streaming requires a Modal computer.")
    backend = ModalSandbox(working_dir="/workspace").backend(reference)
    async with asyncio.timeout(15):
        sandbox = await backend.get_sandbox()
        if await sandbox.poll.aio() is not None:
            raise WorkspaceUnavailableError("The computer is no longer running.")
        credentials = await sandbox.create_connect_token.aio(
            port=CONTROL_PORT if access.controlling else VIEW_PORT,
            user_metadata={"session_id": access.session_id, "view_only": not access.controlling},
        )
    parsed = urlsplit(credentials.url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Modal did not return a secure desktop endpoint.")
    url = urlunsplit(("wss", parsed.netloc, "/websockify", "", ""))
    async with connect(
        url, additional_headers={"Authorization": f"Bearer {credentials.token}"},
        compression=None, max_size=16 * 1024 * 1024, max_queue=8,
        open_timeout=15, close_timeout=3, ping_interval=20, ping_timeout=20, proxy=None,
    ) as connection:
        yield connection


async def relay(runtime: Runtime, socket: WebSocket, remote: ClientConnection, access: StreamAccess) -> None:
    pending: asyncio.Queue[bytes] = asyncio.Queue(maxsize=16)

    async def receive_input():
        while True:
            packet = await socket.receive()
            if packet["type"] == "websocket.disconnect":
                return
            data = packet.get("bytes")
            if data is None or len(data) > MAX_CLIENT_MESSAGE:
                raise HTTPException(400, "Invalid desktop input.")
            await pending.put(data)

    async def send_input():
        while True:
            packets = [await pending.get()]
            size = len(packets[0])
            # RFB is an ordered byte stream. Coalesce already-received packets
            # without introducing a timer, so key-up/pointer bursts do not
            # queue behind a separate DB check for every tiny WebSocket frame.
            while not pending.empty() and size < MAX_CLIENT_MESSAGE:
                value = pending.get_nowait()
                packets.append(value)
                size += len(value)
            # This is a local DB authorization check, not a Modal execution.
            # Read-only viewers are also enforced by the upstream VNC server.
            # Finish SQL/cursor cleanup before propagating cancellation. The
            # cancellation still prevents this input from being forwarded.
            await drain(asyncio.create_task(validate(runtime, access)))
            await remote.send(b"".join(packets))

    async def send_display():
        async for data in remote:
            if not isinstance(data, bytes):
                raise TypeError("Unexpected desktop stream data.")
            await socket.send_bytes(data)

    async def watch_access():
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            # Idle/view-only sockets must also close on revocation, expiry,
            # workspace changes, or release from another API worker.
            # A sibling ending the relay must not abandon this check's cursor.
            await drain(asyncio.create_task(validate(runtime, access)))

    tasks = [
        asyncio.create_task(receive_input()), asyncio.create_task(send_input()), asyncio.create_task(send_display()),
        asyncio.create_task(watch_access()),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def routes(runtime: Runtime, *, cookie_name: str) -> APIRouter:
    router = APIRouter()

    @router.websocket("/api/sessions/{session_id}/computer/stream")
    async def stream(
        socket: WebSocket, session_id: str, client_id: UUID, control: bool = False,
    ):
        access = None
        accepted = False
        code, reason = 1000, "Desktop disconnected."
        try:
            scheme = "https" if socket.url.scheme == "wss" else "http"
            expected = origin(runtime.settings.public_url) or f"{scheme}://{socket.url.netloc}"
            if socket.headers.get("origin") != expected:
                raise HTTPException(403, "This desktop connection has an invalid origin.")
            actor, token, request, grant = await authenticate_socket(runtime, socket, cookie_name=cookie_name)
            access = await bind(
                runtime, session_id, actor, str(client_id), controlling=control, session_token=token,
                product_request=request, product_grant=grant,
            )
            await socket.accept()
            accepted = True
            async with upstream(access) as remote:
                # Connection setup includes network awaits; recheck before any
                # framebuffer or client input can pass through it.
                await validate(runtime, access)
                await relay(runtime, socket, remote, access)
        except HTTPException:
            code, reason = 1008, "Computer access or control changed. Refresh the panel."
        except WorkspaceUnavailableError:
            code, reason = 1011, "This computer expired. Saved files are still available."
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        except Exception as error:  # noqa: BLE001 - sanitize provider failures at the WebSocket boundary
            # Upstream exceptions can contain connection details. Neither Modal
            # credentials nor upstream URLs are exposed to browsers or logs.
            logger.warning("Desktop stream unavailable (%s).", type(error).__name__)
            code, reason = 1011, "Desktop connection unavailable. Try reconnecting."
        finally:
            if access is not None:
                await asyncio.shield(release(runtime, access))
            try:
                await socket.close(code=code, reason=reason if accepted else "")
            except (RuntimeError, WebSocketDisconnect):
                pass

    return router
