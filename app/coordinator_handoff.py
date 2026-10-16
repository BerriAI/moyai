"""Inbox-ready coordinator replicas with one fenced background lifecycle.

The advisory lock is deliberately not a renewable lease: a new coordinator must
not recover another process's live Slack sends. The old process holds it through
shutdown, while either replica can durably acknowledge signed incoming events.
"""
import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re
import time

from fastapi import HTTPException

from .db import database, now

log = logging.getLogger(__name__)
coordinator_owner: ContextVar[tuple[str, int] | None] = ContextVar('coordinator_owner', default=None)
MAX_EVENT_BYTES = 262144
MAX_PENDING_EVENTS = 1000


async def bounded_body(request, limit):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(413, 'Slack payload is too large.')
    return bytes(body)


class HookInbox:
    def __init__(self, store, slack, missing_cloud):
        self.store, self.slack, self.missing_cloud = store, slack, missing_cloud
        self.wake = asyncio.Event()
        self.task = None

    def prune_in(self, conn):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        conn.execute("DELETE FROM coordinator_hooks WHERE status!='pending' AND created_at<?", (cutoff,))

    def enqueue(self, key, payload):
        with self.store.connect() as conn:
            conn.begin_write()
            if conn.execute('SELECT 1 FROM coordinator_hooks WHERE id=?', (key,)).fetchone():
                return
            self.prune_in(conn)
            if conn.execute("SELECT COUNT(*) FROM coordinator_hooks WHERE status='pending'").fetchone()[0] >= MAX_PENDING_EVENTS:
                raise HTTPException(503, 'The event inbox is full. Retry later.', headers={'Retry-After': '1'})
            conn.execute("INSERT INTO coordinator_hooks VALUES(?,'event',?,'pending','','',?,0)",
                         (key, json.dumps(payload, separators=(',', ':')), now()))

    async def receive_event(self, request):
        body = await bounded_body(request, MAX_EVENT_BYTES)
        self.slack.verify_signature(body, request.headers)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'Invalid Slack event.') from None
        if payload.get('type') == 'url_verification':
            return {'challenge': payload.get('challenge', '')}
        if payload.get('type') != 'event_callback':
            return {'ok': True}
        team, event_id = payload.get('team_id'), payload.get('event_id')
        if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value)
                   for value in (team, event_id)):
            raise HTTPException(400, 'Invalid Slack event identifier.')
        # Signature freshness is checked only at intake. A saved, authenticated
        # event may wait through a deploy or provider outage longer than 5 min.
        await database(self.enqueue, f'event:{team}:{event_id}', payload)
        self.wake.set()
        return {'ok': True}

    def claim_interaction(self, key):
        with self.store.connect() as conn:
            conn.begin_write()
            row = conn.execute('SELECT status,response FROM coordinator_hooks WHERE id=?', (key,)).fetchone()
            if row:
                if row['status'] == 'done':
                    return json.loads(row['response'])
                # A Slack modal trigger is single-use. Losing its response must
                # not open/send again, even after a process dies during the call.
                raise HTTPException(409, 'This Slack action is already being processed. Reopen the action if needed.')
            self.prune_in(conn)
            minute = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            if conn.execute("SELECT COUNT(*) FROM coordinator_hooks WHERE kind='interaction' AND created_at>?", (minute,)).fetchone()[0] >= 1000:
                raise HTTPException(429, 'Slack action limit reached. Retry later.', headers={'Retry-After': '60'})
            conn.execute("INSERT INTO coordinator_hooks VALUES(?,'interaction','','running','','',?,0)", (key, now()))
            return None

    async def receive_interaction(self, request):
        body = await bounded_body(request, 65536)
        self.slack.verify_signature(body, request.headers)
        payload = self.slack.feedback.parse_payload(body)
        key = 'interaction:' + hashlib.sha256(body).hexdigest()
        saved = await database(self.claim_interaction, key)
        if saved is not None:
            return saved
        try:
            # These must remain synchronous: modal trigger IDs expire in three
            # seconds and view submissions can return field validation errors.
            result = await self.slack.feedback.receive_payload(payload)
        except BaseException:
            await database(self.store.execute,
                "UPDATE coordinator_hooks SET status='uncertain' WHERE id=? AND status='running'", (key,))
            raise
        # Keep the confirmed response outside the ambiguous-send handler. The
        # drained write may commit before cancellation is propagated to caller.
        await database(self.store.execute,
            "UPDATE coordinator_hooks SET status='done',response=? WHERE id=? AND status='running'",
            (json.dumps(result), key))
        return result

    async def drain_one(self):
        rows = await database(self.store.rows,
            "SELECT id,payload FROM coordinator_hooks WHERE kind='event' AND status='pending' AND retry_at<=? ORDER BY created_at,id LIMIT 1",
            (time.time(),))
        if not rows:
            return False
        row = rows[0]
        try:
            # Only the lock holder calls this private path. The HTTP route has
            # no option to skip signature verification or enqueue raw headers.
            await self.slack.receive_payload(json.loads(row['payload']), self.missing_cloud())
        except HTTPException as exc:
            if exc.status_code >= 500 or exc.status_code == 429:
                await database(self.store.execute, 'UPDATE coordinator_hooks SET retry_at=? WHERE id=?',
                               (time.time() + 1, row['id']))
                return True
            await database(self.store.execute,
                "UPDATE coordinator_hooks SET status='rejected',payload='',detail=? WHERE id=?",
                ('Rejected event (' + str(exc.status_code) + ').', row['id']))
        except Exception as exc:
            await database(self.store.execute, 'UPDATE coordinator_hooks SET retry_at=? WHERE id=?',
                           (time.time() + 1, row['id']))
            log.warning('Coordinator event will retry (%s)', type(exc).__name__)
            return True
        else:
            await database(self.store.execute,
                "UPDATE coordinator_hooks SET status='done',payload='' WHERE id=?", (row['id'],))
        return True

    def start(self):
        self.task = asyncio.create_task(self.watch(), name='coordinator-hook-inbox')

    async def watch(self):
        while True:
            self.wake.clear()
            try:
                if await self.drain_one():
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Coordinator event inbox will retry (%s)', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class CoordinatorHandoff:
    def __init__(self, store, manager):
        self.store, self.manager = store, manager
        self.status = 'starting'
        self.stop = asyncio.Event()
        self.task = None
        self.started = False

    @property
    def healthy(self):
        return self.status in {'standby', 'leader'} and self.task is not None and not self.task.done()

    async def supervise(self, background):
        db = self.store.database
        acquired = False
        token = None
        try:
            while not self.stop.is_set():
                await database(self.store.rows, 'SELECT 1')
                if await database(db.acquire_coordinator):
                    acquired = True
                    break
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=.1)
                except TimeoutError:
                    pass
            if not acquired:
                return
            token = coordinator_owner.set((db.schema, db.owner_pid))
            self.started = True
            async with background():
                self.status = 'leader'
                while not self.stop.is_set():
                    # Verify the session lock and contract even when no work is
                    # queued. Losing ownership never starts a reconnecting owner.
                    await database(self.store.rows, 'SELECT 1')
                    try:
                        await asyncio.wait_for(self.stop.wait(), timeout=.25)
                    except TimeoutError:
                        pass
                self.status = 'draining'
        except BaseException:
            self.status = 'failed'
            raise
        finally:
            if token is not None:
                coordinator_owner.reset(token)
            if acquired:
                # background.__aexit__ has awaited every close, including the
                # old process's live outbox sends, before another may recover.
                await database(db.release_coordinator)

    @asynccontextmanager
    async def lifespan(self, background):
        # A standby must demonstrate dependency readiness before Render can
        # switch its TCP traffic. It does not dispatch or recover anything.
        async with asyncio.timeout(self.manager.settings.temporal_startup_timeout_seconds):
            await self.manager.connect_temporal()
            await database(self.store.rows, 'SELECT 1')
        self.status = 'standby'
        self.task = asyncio.create_task(self.supervise(background), name='coordinator-owner')
        try:
            yield
        finally:
            self.stop.set()
            # Do not cancel supervisor: it must finish each background close
            # before releasing the singleton lock and database owner session.
            cancelled = None
            while True:
                try:
                    await asyncio.shield(self.task)
                    break
                except asyncio.CancelledError as exc:
                    if self.task.cancelled():
                        raise
                    # A second cancellation must not reach the supervisor's
                    # background drain or release its ownership prematurely.
                    if cancelled is None:
                        cancelled = exc
                except BaseException as exc:
                    if cancelled is not None:
                        raise cancelled from exc
                    raise
            if cancelled is not None:
                raise cancelled


class CoordinatorIngressMiddleware:
    """A standby serves only durable hooks and health, never request-owned jobs."""
    def __init__(self, app, ownership):
        self.app, self.ownership = app, ownership

    async def __call__(self, scope, receive, send):
        if scope['type'] in {'http', 'websocket'}:
            path = scope.get('path', '')
            allowed = path == '/health' or path.startswith('/hooks/')
            if not allowed or not self.ownership.healthy:
                if scope['type'] == 'websocket':
                    await send({'type': 'websocket.close', 'code': 1008})
                else:
                    from starlette.responses import JSONResponse
                    await JSONResponse({'detail': 'Coordinator ingress is unavailable.'}, status_code=503,
                        headers={'Retry-After': '1',
                                 'X-Moyai-Coordinator-State': self.ownership.status,
                                 'X-Moyai-Build': self.ownership.manager.settings.moyai_build_sha,
                                 'X-Moyai-Owner': str(self.ownership.store.database.owner_pid),
                                 'X-Moyai-Role': 'coordinator', 'X-Moyai-Rolling-Release': '1'})(scope, receive, send)
                return
        await self.app(scope, receive, send)


from .schema_initializers import initialize_coordinator_handoff as initialize_schema
