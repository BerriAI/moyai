"""Private Modal exec proxy. No exposed CDP/VNC endpoint or browser tokens."""
import asyncio
import base64
import json
import time

import modal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from . import captures


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['claim', 'release', 'open', 'click', 'type', 'key', 'scroll', 'back', 'screenshot', 'record_start', 'record_stop']
    args: dict = Field(default_factory=dict)


class Computer:
    def __init__(self, settings, store, security, manager, same_requester):
        self.settings, self.store, self.security = settings, store, security
        self.manager, self.same_requester = manager, same_requester
        self.cache = {}
        self.locks = {}
        self.slots = asyncio.Semaphore(4)
        self.capture_locks = {}
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
        sandbox = await modal.Sandbox.from_id.aio(run['sandbox_id'], client=await self.manager.client())
        return sandbox if await sandbox.poll.aio() is None else None

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
            await self.execute(sandbox, 'request', json.dumps({'action': 'finish'}))
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
                if time.monotonic() - stamp >= 1 or sid != run.get('sandbox_id'):
                    try:
                        sandbox = await self.sandbox(run)
                        value = await self.execute(sandbox, 'request', '{"action":"state"}') if sandbox else {'available': False}
                        if sandbox:
                            try:
                                await self.sync(sandbox, run_id, value.get('media', []))
                            except Exception:
                                value['notice'] = 'A capture could not be saved to the app yet. Keeping the workspace copy and retrying.'
                    except Exception:
                        value = {'available': False, 'notice': 'Computer is reconnecting or this sandbox has shut down. Saved captures are still available.'}
                    if len(self.cache) >= 32:
                        self.cache.pop(min(self.cache, key=lambda key: self.cache[key][0]), None)
                    self.cache[run_id] = (time.monotonic(), run.get('sandbox_id'), value)
                return {**value, 'has_sandbox': bool(run.get('sandbox_id')), 'actor': actor,
                        'captures': captures.listing(self.settings, run_id)}

        @router.post('/api/runs/{run_id}/computer')
        async def command(run_id: str, body: Command, request: Request):
            run, actor = self.authorize(request, run_id, mutation=True)
            if len(json.dumps(body.args)) > 16000:
                raise HTTPException(413, 'Computer command too large.')
            async with self.locks.setdefault(run_id, asyncio.Lock()):
                sandbox = await self.sandbox(run)
                if not sandbox:
                    raise HTTPException(409, 'This workspace is asleep. Send a message to Moyai to start it again.')
                try:
                    result = await self.execute(sandbox, 'request', json.dumps({**body.model_dump(), 'actor': actor}), timeout=530)
                    if result.get('error'):
                        raise HTTPException(409, result['error'])
                    if body.action != 'release':
                        self.store.execute('INSERT INTO computer_activity VALUES(?,?) ON CONFLICT(run_id) DO UPDATE SET touched=excluded.touched', (run_id, time.time()))
                    if body.action in {'screenshot', 'record_stop'}:
                        await self.sync(sandbox, run_id)
                    return {'ok': True, 'captures': captures.listing(self.settings, run_id)}
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
