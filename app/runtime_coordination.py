"""Renewable execution ownership, fenced at every PostgreSQL write transaction.

Leases use the database clock and do not retain a connection while awaiting a
sandbox. Expired workers cannot commit after a replacement claims their lease.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Lease:
    schema: str
    name: str
    token: str
    task_id: int


held_leases: ContextVar[tuple[Lease, ...]] = ContextVar('moyai_held_leases', default=())


class LeaseLost(RuntimeError):
    pass


def require_shared_artifacts(store):
    """Refuse a cutover that would leave known local payloads on one machine."""
    root = store.artifacts.root
    if root.is_symlink():
        raise ValueError('Distributed roles require migrated artifact storage, without local symlinks.')
    manifested = {row['name'] for row in store.rows("SELECT name FROM artifact_objects WHERE reference!=''")}
    if root.exists():
        for path in root.rglob('*'):
            if path.is_symlink() or (path.is_file() and not path.name.startswith('.upload-')
                                     and path.relative_to(root).as_posix() not in manifested):
                raise ValueError('Migrate and verify legacy artifact files before enabling distributed roles. See docs/runtime-scaling.md.')


class WorkerRoleMiddleware:
    """Workers expose readiness only, including when a client attempts WebSocket."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        if scope['type'] == 'http' and scope['path'] != '/health':
            from starlette.responses import JSONResponse
            await JSONResponse({'detail': 'Execution workers do not serve workspace traffic.'}, status_code=404)(scope, receive, send)
            return
        await self.app(scope, receive, send)


@asynccontextmanager
async def lease(database, name: str, *, wait: bool = True, ttl: float = 30):
    owner = asyncio.current_task()
    if any(item.schema == database.schema and item.name == name and item.task_id == id(owner)
           for item in held_leases.get()):
        yield True
        return
    token = uuid4().hex
    # Each database attempt skips contention. Only this async loop may wait;
    # eviction passes wait=False while holding the incoming session's lease.
    while not await asyncio.to_thread(database.acquire_lease, name, token, ttl):
        if not wait:
            yield False
            return
        await asyncio.sleep(0.05)
    lost = False

    async def renew():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(ttl / 3)
                if not await asyncio.to_thread(database.renew_lease, name, token, ttl):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # Failure to confirm ownership is a fence, never permission.
        lost = True
        owner.cancel()

    pulse = asyncio.create_task(renew())
    context = held_leases.set((*held_leases.get(), Lease(database.schema, name, token, id(owner))))
    try:
        yield True
    except asyncio.CancelledError:
        if lost:
            raise LeaseLost('Execution ownership expired; retry on the current owner.') from None
        raise
    finally:
        held_leases.reset(context)
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        # Cleanup is conditional on this token, so it cannot delete a successor.
        # On database failure the bounded lease expires by itself.
        try:
            await asyncio.shield(asyncio.to_thread(database.release_lease, name, token))
        except Exception:
            pass


class AdmissionLock:
    def __init__(self, database=None):
        self.database = database
        self.local = asyncio.Lock()
        self.context = None

    async def __aenter__(self):
        await self.local.acquire()
        try:
            if self.database:
                self.context = lease(self.database, 'sandbox-admission')
                await self.context.__aenter__()
        except BaseException:
            self.local.release()
            raise

    async def __aexit__(self, *exc):
        try:
            if self.context:
                await self.context.__aexit__(*exc)
                self.context = None
        finally:
            self.local.release()
