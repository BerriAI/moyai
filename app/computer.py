"""Private Modal exec proxy. No exposed CDP/VNC endpoint or browser tokens."""
import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager

import modal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from . import captures


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['claim', 'release', 'input', 'open', 'click', 'type', 'key', 'scroll', 'back', 'screenshot', 'record_start', 'record_stop']
    args: dict = Field(default_factory=dict)


class DesktopConnection:
    """Private, ordered command channel; a failed write is never replayed."""
    def __init__(self, sandbox, identity):
        self.sandbox, self.identity = sandbox, identity
        self.process = self.reader = None
        self.created = self.used = time.monotonic()
        self.active = self.failed = False

    async def close(self):
        process, self.process = self.process, None
        self.reader = None
        if process:
            try:
                process.stdin.write_eof()
                await asyncio.wait_for(process.stdin.drain.aio(), 2)
            except Exception:
                pass  # The private bridge also exits on EOF, idle or exec timeout.

    async def request(self, body):
        try:
            async with asyncio.timeout(530):
                native = getattr(self.sandbox, 'computer_request', None)
                if native:
                    return await native(body)
                if self.process is None:
                    self.process = await self.sandbox.exec.aio('/usr/local/bin/python',
                        '/opt/workspace-runner/computer.py', 'bridge', timeout=3600, bufsize=1)
                    self.reader = self.process.stdout.__aiter__()
                self.process.stdin.write((json.dumps(body)+'\n').encode())
                await self.process.stdin.drain.aio()
                line = await self.reader.__anext__()
                if len(line) > 2 * 1024 * 1024:
                    raise ValueError('Computer response too large.')
                result = json.loads(line)
                if not isinstance(result, dict):
                    raise ValueError('Invalid Computer response.')
                return result
        except BaseException as exc:
            self.failed = True
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise HTTPException(503, 'Computer connection interrupted. Check the desktop before resuming control. Restart the workspace if it is running an older version.') from None


class Computer:
    def __init__(self, settings, store, security, manager, same_requester):
        self.settings, self.store, self.security = settings, store, security
        self.manager, self.same_requester = manager, same_requester
        self.cache = {}
        self.locks = {}
        self.slots = asyncio.Semaphore(4)
        self.capture_locks = {}
        self.connections = {}
        self.connection_lock = asyncio.Lock()
        store.execute('CREATE TABLE IF NOT EXISTS computer_activity (run_id TEXT PRIMARY KEY, touched REAL NOT NULL)')

    def touched(self, run_id):
        rows = self.store.rows('SELECT touched FROM computer_activity WHERE run_id=?', (run_id,))
        return rows[0]['touched'] if rows else 0

    def authorize(self, request, run_id, mutation=False):
        self.security.require(request, mutation=mutation)
        run = self.store.run(run_id)
        if not run or run['mode'] != 'modal':
            raise HTTPException(404, 'Cloud session not found.')
        actor = self.store.identity(self.security.session_info(request))
        if self.security.role(request) != 'admin' and not any(
            self.same_requester(actor, owner) for owner in (run.get('owner_id'), run.get('active_user_id')) if owner
        ):
            raise HTTPException(403, 'Only the session requester or an administrator can view or control this browser.')
        return run, actor

    async def sandbox(self, run):
        if not run.get('sandbox_id'):
            return None
        sandbox = await self.manager.provider(run).get(run['sandbox_id'])
        return sandbox if await sandbox.poll.aio() is None else None

    @asynccontextmanager
    async def connection(self, run):
        async with self.connection_lock:
            key, identity = run['id'], run.get('sandbox_id')
            connection = self.connections.get(key)
            now = time.monotonic()
            if connection and (connection.identity != identity or connection.failed or
                               now-connection.used > 20 or now-connection.created > 3000):
                self.connections.pop(key)
                await connection.close()
                connection = None
            if connection is None:
                sandbox = await self.sandbox(run)
                if sandbox:
                    if len(self.connections) >= 32:
                        idle = [(entry.used, rid) for rid, entry in self.connections.items() if not entry.active]
                        if not idle:
                            raise HTTPException(503, 'Computer connections are busy. Try again shortly.')
                        await self.connections.pop(min(idle)[1]).close()
                    connection = self.connections[key] = DesktopConnection(sandbox, identity)
            if connection:
                connection.active = True
        try:
            yield connection
        finally:
            if connection:
                connection.active = False
                connection.used = time.monotonic()

    async def close(self):
        connections, self.connections = list(self.connections.values()), {}
        await asyncio.gather(*(connection.close() for connection in connections))

    async def execute(self, sandbox, *args, timeout=45):
        async with self.slots:
            proc = await sandbox.exec.aio('/usr/local/bin/python', '/opt/workspace-runner/computer.py', *args, timeout=timeout)
            stdout, _ = await asyncio.gather(proc.stdout.read.aio(), proc.stderr.read.aio())
            if await proc.wait.aio() != 0:
                raise HTTPException(503, 'Computer is not ready in this workspace. Start a new response to update it.')
            try:
                return json.loads(stdout)
            except (ValueError, TypeError):
                raise HTTPException(502, 'The sandbox did not return a valid Computer response.') from None

    async def sync(self, sandbox, run_id, records=None):
        async with self.capture_locks.setdefault(run_id, asyncio.Lock()):
            if records is None:
                records = await self.execute(sandbox, 'captures')
            root = captures.directory(self.settings, run_id)
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            used = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
            saved = False
            if not isinstance(records, list) or len(records) > 100:
                raise HTTPException(502, 'Invalid sandbox capture catalog.')
            for record in records:
                name, size = record.get('name'), record.get('size')
                if not captures.valid_name(name) or not isinstance(size, int) or not 0 < size <= captures.MAX_FILE:
                    continue
                target = root / name
                if target.exists():
                    continue  # Captures are immutable and uniquely named.
                if used + size > captures.MAX_TOTAL:
                    raise HTTPException(413, 'This session has reached its 64 MB saved capture budget.')
                data = await self.execute(sandbox, 'capture', name, timeout=90)
                encoded = data.get('data', '')
                if len(encoded) > (captures.MAX_FILE + 2) // 3 * 4:
                    raise HTTPException(413, 'Capture too large.')
                raw = base64.b64decode(encoded, validate=True)
                if len(raw) != size or len(raw) > captures.MAX_FILE:
                    raise HTTPException(502, 'Capture changed while saving. Refresh to retry.')
                captures.media_type(name, raw)
                staged = target.with_suffix('.next')
                staged.write_bytes(raw)
                staged.chmod(0o600)
                staged.replace(target)
                used += size
                saved = True
            if saved:
                self.store.event(run_id, 'artifact', 'Browser captures saved. Open Computer or Files to view them.')
                await self.manager.persist()

    async def save_before_release(self, sandbox, run_id):
        try:
            result = await self.execute(sandbox, 'request', json.dumps({'action': 'finish'}))
            if result.get('error'):
                self.store.event(run_id, 'error', 'Browser recording could not be finalized. Any partial capture remains in the workspace.')
            await self.sync(sandbox, run_id)
        except Exception:
            self.store.event(run_id, 'error', 'Browser captures could not be copied from the sandbox. Any completed captures remain in its workspace snapshot.')

    def routes(self):
        router = APIRouter()

        @router.get('/api/runs/{run_id}/computer')
        async def state(run_id: str, request: Request):
            run, actor = self.authorize(request, run_id)
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                stamp, sid, value = self.cache.get(run_id, (0, None, None))
                if time.monotonic() - stamp >= .08 or sid != run.get('sandbox_id'):
                    try:
                        async with self.connection(run) as connection:
                            value = await connection.request({'action':'state'}) if connection else {'available': False}
                            value['has_sandbox'] = connection is not None
                            if connection:
                                try:
                                    await self.sync(connection.sandbox, run_id, value.get('media', []))
                                except Exception:
                                    value['notice'] = 'A capture could not be saved to the app yet. Keeping the workspace copy and retrying.'
                    except Exception:
                        value = {'available': False, 'notice': 'Computer is reconnecting or this sandbox has shut down. Saved captures are still available.'}
                    if len(self.cache) >= 32:
                        self.cache.pop(min(self.cache, key=lambda key: self.cache[key][0]), None)
                    self.cache[run_id] = (time.monotonic(), run.get('sandbox_id'), value)
                return {**value, 'has_sandbox': value.get('has_sandbox', False), 'actor': actor,
                        'captures': captures.listing(self.settings, run_id)}

        @router.post('/api/runs/{run_id}/computer')
        async def command(run_id: str, body: Command, request: Request):
            run, actor = self.authorize(request, run_id, mutation=True)
            if len(json.dumps(body.args)) > 16000:
                raise HTTPException(413, 'Computer command too large.')
            async with self.locks.setdefault(run_id, asyncio.Lock()), self.connection(run) as connection:
                if not connection:
                    raise HTTPException(409, 'This workspace is asleep. Send a message to Moyai to start it again.')
                try:
                    result = await connection.request({**body.model_dump(), 'actor': actor})
                    if result.get('error'):
                        raise HTTPException(409, result['error'])
                    if body.action != 'release':
                        self.store.execute('INSERT INTO computer_activity VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET touched=excluded.touched', (run_id, time.time()))
                    if body.action in {'screenshot', 'record_stop'}:
                        await self.sync(connection.sandbox, run_id)
                    return {**result, 'ok': True, 'actor': actor, 'has_sandbox': True,
                            'captures': captures.listing(self.settings, run_id)}
                finally:
                    self.cache.pop(run_id, None)

        @router.get('/api/runs/{run_id}/computer/captures/{name}')
        def content(run_id: str, name: str, request: Request, download: bool = False):
            # Completed captures follow the same shared-session visibility as saved files.
            self.security.require(request)
            if not self.store.run(run_id) or not captures.valid_name(name):
                raise HTTPException(404, 'Capture not found.')
            return captures.response(captures.directory(self.settings, run_id) / name, request, download)

        return router
