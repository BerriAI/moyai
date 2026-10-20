"""Activate an already staged container through its owner-only local socket.

The release controller still owns draining, migrations and activation order.
This transport replaces only the second provider deployment. Durable receipts
belong to a unique staging attempt; private local copies avoid repeated DB reads.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import socket
import stat
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL = "moyai-maintenance-activation-v1"
RECEIPT_TABLE = "moyai_maintenance_activation_v1"
DEFAULT_SOCKET = "/tmp/moyai-maintenance/control.sock"
MAX_BYTES = 16384


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StageIdentity(StrictModel):
    protocol: Literal["moyai-maintenance-activation-v1"] = PROTOCOL
    build: str = Field(pattern=r"^[0-9a-f]{40}$")
    role: Literal["api", "coordinator", "broker", "worker"]
    service: str = Field(min_length=1, max_length=200)
    instance: str = Field(pattern=r"^[0-9a-f]{32}$")
    token: str = Field(pattern=r"^[0-9a-f]{32}$")


class ActivationGuard(StrictModel):
    product_schema: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    native_schema: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    generation: int = Field(ge=0)
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ActivationRequest(StrictModel):
    command: Literal["activate"] = "activate"
    expected: StageIdentity
    request_id: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,200}$")
    guard: ActivationGuard


class ActivationReceipt(StrictModel):
    identity: StageIdentity
    request_id: str = Field(pattern=r"^[A-Za-z0-9:._-]{1,200}$")
    guard: ActivationGuard


def socket_path() -> Path:
    configured = os.environ.get("MOYAI_MAINTENANCE_SOCKET")
    return Path(configured) if configured else Path(DEFAULT_SOCKET).resolve()


def service_id() -> str:
    """Local identities are confined by the existing loopback rehearsal mode."""
    if os.environ.get("MOYAI_PRODUCT_LOCAL_REHEARSAL") == "true":
        from .product_config import load_product_config
        config = load_product_config()
        if config is None or not config.local_rehearsal:
            raise ValueError("Invalid local maintenance rehearsal.")
        return os.environ.get("MOYAI_LOCAL_FLEET_SERVICE_ID", "")
    return os.environ.get("RENDER_SERVICE_ID", "")


def private_directory(path: Path, *, create: bool = False) -> None:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Maintenance control requires an absolute private directory.")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    node = path.lstat()
    if not stat.S_ISDIR(node.st_mode) or node.st_uid != os.geteuid() or stat.S_IMODE(node.st_mode) & 0o077:
        raise ValueError("Maintenance control requires an owner-only directory.")


def read_private(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        node = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(node.st_mode) or node.st_uid != os.geteuid()
            or stat.S_IMODE(node.st_mode) & 0o077 or node.st_size > MAX_BYTES
        ):
            raise ValueError("Maintenance control record is not private.")
        return stream.read(MAX_BYTES + 1)


def write_private(path: Path, body: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def matches_environment(identity: StageIdentity) -> bool:
    return (
        identity.build == os.environ.get("MOYAI_BUILD_SHA")
        and identity.role == os.environ.get("MOYAI_RUNTIME_ROLE")
        and identity.service == service_id()
        and identity.token == os.environ.get("MOYAI_MAINTENANCE_TOKEN")
    )


def accepted_receipt() -> ActivationReceipt | None:
    """Read a receipt without changing it; unknown or mismatched state fails closed."""
    path = socket_path().parent
    try:
        private_directory(path)
        raw = read_private(path / "activated.json")
    except FileNotFoundError:
        # Provider restarts reuse the deployed environment but replace /tmp.
        # A new staging attempt always gets a new token before its deployment.
        if os.environ.get("RENDER_MIGRATION_STAGE", "true").lower() != "true":
            return None
        receipt = durable_receipt()
        if receipt is None:
            return None
        private_directory(path, create=True)
        write_private(path / "activated.json", receipt.model_dump_json())
        raw = receipt.model_dump_json()
    receipt = ActivationReceipt.model_validate_json(raw)
    if not matches_environment(receipt.identity):
        raise ValueError("Maintenance activation belongs to a different container source.")
    return receipt


def durable_receipt() -> ActivationReceipt | None:
    import psycopg
    from psycopg import sql

    token = os.environ.get("MOYAI_MAINTENANCE_TOKEN")
    if not token:
        return None
    if not re.fullmatch("[0-9a-f]{32}", token):
        raise ValueError("Invalid maintenance staging token.")
    from .product_config import load_product_config

    config = load_product_config()
    if config is None or config.product_mode != "existing" or not config.native_schema:
        raise ValueError("Maintenance activation requires the existing product.")
    with psycopg.connect(config.database_url, connect_timeout=5) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        table = sql.Identifier(config.native_schema, RECEIPT_TABLE)
        # A first staging listener starts before explicit receipt provisioning.
        exists = connection.execute("SELECT to_regclass(%s)", (table.as_string(connection),)).fetchone()
        if not exists or exists[0] is None:
            return None
        verify_receipt_table(connection, config.native_schema)
        row = connection.execute(sql.SQL("SELECT receipt FROM {} WHERE token=%s").format(table), (token,)).fetchone()
    if row is None:
        return None
    receipt = ActivationReceipt.model_validate_json(row[0])
    if not matches_environment(receipt.identity):
        raise ValueError("Durable activation belongs to another staging attempt.")
    return receipt


def apply_accepted_activation() -> ActivationReceipt | None:
    """Also used by read-only probes whose shell inherits the staging environment."""
    receipt = accepted_receipt()
    if receipt is not None:
        os.environ["RENDER_MIGRATION_STAGE"] = "false"
    return receipt


def verify_guard(guard: ActivationGuard) -> None:
    """Recheck the installed schema and exact durable pause before accepting once."""
    import psycopg
    from psycopg import sql

    from .product_config import load_product_config
    from .product_deployment import verify_product_schema

    config = load_product_config()
    if (
        config is None or config.product_mode != "existing"
        or config.product_schema != guard.product_schema or config.native_schema != guard.native_schema
    ):
        raise ValueError("Maintenance activation scope changed.")
    verify_product_schema(config)
    with psycopg.connect(config.database_url, connect_timeout=5) as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        connection.execute("SET LOCAL statement_timeout = '5s'")
        row = connection.execute(sql.SQL(
            "SELECT generation,paused,request_fingerprint FROM {}.moyai_product_drain_state WHERE id=1"
        ).format(sql.Identifier(guard.native_schema))).fetchone()
        if row != (guard.generation, True, guard.request_fingerprint):
            raise ValueError("The verified maintenance pause changed.")


def verify_receipt_table(connection, schema: str) -> None:
    """A frozen, independently versioned release store, outside app migrations."""
    from psycopg import sql

    table = sql.Identifier(schema, RECEIPT_TABLE)
    relation = connection.execute(
        "SELECT relkind,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid=to_regclass(%s)",
        (table.as_string(connection),),
    ).fetchone()
    columns = connection.execute(
        "SELECT column_name,data_type,character_maximum_length,is_nullable,column_default "
        "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (schema, RECEIPT_TABLE),
    ).fetchall()
    constraints = connection.execute(
        "SELECT contype,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid=to_regclass(%s)",
        (table.as_string(connection),),
    ).fetchall()
    triggers = connection.execute(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid=to_regclass(%s) AND NOT tgisinternal",
        (table.as_string(connection),),
    ).fetchone()
    if (
        relation != ("r", False, False)
        or columns != [
            ("token", "character varying", 32, "NO", None),
            ("receipt", "text", None, "NO", None),
            ("created_at", "character varying", 40, "NO", None),
        ]
        or constraints != [("p", "PRIMARY KEY (token)")] or triggers != (0,)
    ):
        raise ValueError("Maintenance receipt storage differs from its protocol.")


def provision(guard: ActivationGuard) -> None:
    """Explicit DDL under the canonical zero-owner fence and exact native pause."""
    import psycopg
    from psycopg import sql

    from .product_config import load_product_config

    verify_guard(guard)
    config = load_product_config()
    assert config is not None
    with psycopg.connect(config.database_url, connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout = '5s'")
        connection.execute("SET LOCAL lock_timeout = '5s'")
        locked = connection.execute(
            "SELECT pg_try_advisory_xact_lock(726940,%s::regnamespace::oid::int)",
            (guard.product_schema,),
        ).fetchone()
        if locked != (True,):
            raise ValueError("A runtime still owns the product schema.")
        row = connection.execute(sql.SQL(
            "SELECT generation,paused,request_fingerprint FROM {}.moyai_product_drain_state WHERE id=1 FOR SHARE"
        ).format(sql.Identifier(guard.native_schema))).fetchone()
        if row != (guard.generation, True, guard.request_fingerprint):
            raise ValueError("The verified maintenance pause changed.")
        connection.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} "
            "(token varchar(32) PRIMARY KEY, receipt text NOT NULL, created_at varchar(40) NOT NULL)"
        ).format(sql.Identifier(guard.native_schema, RECEIPT_TABLE)))
        verify_receipt_table(connection, guard.native_schema)


def accept_durable(receipt: ActivationReceipt) -> None:
    """Commit once while locking the exact pause; a conflicting token never wins."""
    import psycopg
    from psycopg import sql

    from .models import now
    from .product_config import load_product_config

    verify_guard(receipt.guard)
    config = load_product_config()
    assert config is not None
    with psycopg.connect(config.database_url, connect_timeout=5) as connection:
        connection.execute("SET LOCAL statement_timeout = '5s'")
        connection.execute("SET LOCAL lock_timeout = '5s'")
        guard = receipt.guard
        row = connection.execute(sql.SQL(
            "SELECT generation,paused,request_fingerprint FROM {}.moyai_product_drain_state WHERE id=1 FOR SHARE"
        ).format(sql.Identifier(guard.native_schema))).fetchone()
        if row != (guard.generation, True, guard.request_fingerprint):
            raise ValueError("The verified maintenance pause changed.")
        verify_receipt_table(connection, guard.native_schema)
        table = sql.Identifier(guard.native_schema, RECEIPT_TABLE)
        connection.execute(sql.SQL(
            "INSERT INTO {} (token,receipt,created_at) VALUES (%s,%s,%s) ON CONFLICT (token) DO NOTHING"
        ).format(table), (receipt.identity.token, receipt.model_dump_json(), now()))
        saved = connection.execute(sql.SQL("SELECT receipt FROM {} WHERE token=%s").format(table),
                                   (receipt.identity.token,)).fetchone()
        if saved is None or ActivationReceipt.model_validate_json(saved[0]) != receipt:
            raise ValueError("Another activation owns this staging attempt.")


class StagedActivation:
    def __init__(self, stop: Callable[[], None], *, path: Path | None = None):
        self.path = path or socket_path()
        self.stop = stop
        self.identity = StageIdentity(
            build=os.environ.get("MOYAI_BUILD_SHA", ""),
            role=os.environ.get("MOYAI_RUNTIME_ROLE", ""),  # type: ignore[arg-type]
            service=service_id(),
            instance=uuid4().hex,
            token=os.environ.get("MOYAI_MAINTENANCE_TOKEN", ""),
        )
        self.receipt: ActivationReceipt | None = None
        self.lock = asyncio.Lock()
        self.server: asyncio.AbstractServer | None = None
        self.inode: tuple[int, int] | None = None
        self.clients: set[asyncio.Task] = set()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.clients.add(task)
        accepted = False
        try:
            try:
                async with asyncio.timeout(5):
                    raw = await reader.readline()
                if not raw.endswith(b"\n") or len(raw) > MAX_BYTES:
                    raise ValueError("Invalid maintenance command.")
                body = json.loads(raw)
                if body == {"command": "status"}:
                    result = {"ok": True, "state": "staged", "identity": self.identity.model_dump()}
                else:
                    request = ActivationRequest.model_validate(body)
                    async with self.lock:
                        if request.expected != self.identity:
                            raise ValueError("The staged process changed.")
                        receipt = ActivationReceipt(
                            identity=self.identity, request_id=request.request_id, guard=request.guard,
                        )
                        if self.receipt is not None and self.receipt != receipt:
                            raise ValueError("Another activation owns this process.")
                        if self.receipt is None:
                            # DB operations have bounded connect/statement/lock timeouts.
                            # Drain a write on cancellation: a committed receipt must
                            # still trigger normal startup even if its caller disappeared.
                            write = asyncio.create_task(asyncio.to_thread(accept_durable, receipt))
                            try:
                                await asyncio.shield(write)
                            except asyncio.CancelledError:
                                await write
                                self.receipt = receipt
                                accepted = True
                                raise
                            self.receipt = receipt
                        accepted = True
                        write_private(self.path.parent / "activated.json", receipt.model_dump_json())
                        result = {"ok": True, "state": "accepted", "receipt": receipt.model_dump()}
            except Exception:  # noqa: BLE001 - never expose credentials or SQL diagnostics
                result = {"ok": False, "error": "Maintenance activation was not confirmed."}
            writer.write(json.dumps(result).encode() + b"\n")
            async with asyncio.timeout(5):
                await writer.drain()
        except (ConnectionError, TimeoutError):
            pass
        finally:
            # Acceptance survives a lost reply. The caller must observe, never
            # assume a disconnected write did not happen or start a second one.
            if accepted:
                self.stop()
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.clients.discard(task)

    @asynccontextmanager
    async def lifespan(self, _application) -> AsyncIterator[None]:
        private_directory(self.path.parent, create=True)
        descriptor = os.open(self.path.parent / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            node = os.fstat(descriptor)
            if not stat.S_ISREG(node.st_mode) or node.st_uid != os.geteuid() or stat.S_IMODE(node.st_mode) & 0o077:
                raise ValueError("Invalid maintenance control lock.")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                node = self.path.lstat()
                recorded = json.loads(read_private(self.path.parent / "socket.json"))
                if (
                    not stat.S_ISSOCK(node.st_mode) or node.st_uid != os.geteuid()
                    or recorded != {"device": node.st_dev, "inode": node.st_ino}
                ):
                    raise ValueError("An unrecorded control socket occupies this path.")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as check:
                    try:
                        check.connect(str(self.path))
                    except ConnectionRefusedError:
                        pass
                    else:
                        raise ValueError("A live control socket occupies this path.")
                self.path.unlink()
            listener.bind(str(self.path))
            os.chmod(self.path, 0o600)
            node = self.path.lstat()
            self.inode = node.st_dev, node.st_ino
            write_private(self.path.parent / "socket.json", json.dumps({
                "device": node.st_dev, "inode": node.st_ino,
            }))
            self.server = await asyncio.start_unix_server(self.handle, sock=listener, limit=MAX_BYTES)
            try:
                yield
            finally:
                self.server.close()
                await self.server.wait_closed()
                if self.clients:
                    await asyncio.gather(*self.clients, return_exceptions=True)
        finally:
            listener.close()
            if self.inode is not None:
                with suppress(FileNotFoundError):
                    node = self.path.lstat()
                    if (node.st_dev, node.st_ino) == self.inode:
                        self.path.unlink()
            os.close(descriptor)


class ActivationHeaders:
    """Bind real health responses to the accepted local activation, without a new HTTP route."""

    def __init__(self, app, receipt: ActivationReceipt):
        self.app, self.receipt = app, receipt

    async def __call__(self, scope, receive, send):
        async def reply(message):
            if scope["type"] == "http" and scope.get("path") == "/health" and message["type"] == "http.response.start":
                message = {**message, "headers": [
                    *message.get("headers", []),
                    (b"x-moyai-maintenance-activation", self.receipt.request_id.encode("ascii")),
                    (b"x-moyai-maintenance-instance", self.receipt.identity.instance.encode("ascii")),
                ]}
            await send(message)
        await self.app(scope, receive, reply)


async def request(body: dict, *, path: Path | None = None) -> dict:
    path = path or socket_path()
    private_directory(path.parent)
    node = path.lstat()
    if not stat.S_ISSOCK(node.st_mode) or node.st_uid != os.geteuid() or stat.S_IMODE(node.st_mode) & 0o077:
        raise ValueError("Invalid maintenance control socket.")
    async with asyncio.timeout(30):
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_BYTES)
        try:
            writer.write(json.dumps(body).encode() + b"\n")
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "activate", "provision"])
    arguments = parser.parse_args()
    try:
        body: dict = {"command": "status"}
        if arguments.command in {"activate", "provision"}:
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Oversized maintenance activation.")
            if arguments.command == "provision":
                provision(ActivationGuard.model_validate_json(raw))
                print(json.dumps({"ok": True, "state": "provisioned", "protocol": PROTOCOL}))
                return
            body = ActivationRequest.model_validate_json(raw).model_dump()
        value = asyncio.run(request(body))
        if value.get("ok") is not True:
            raise ValueError("Activation was not confirmed.")
    except Exception:  # noqa: BLE001 - preserve ambiguous outcomes, redact diagnostics
        parser.exit(1, "Maintenance control did not confirm the operation; inspect before continuing.\n")
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
