"""Personal archiving and retained deletion for shared workspace sessions."""
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool

from .db import now
from .runner import TERMINAL


class ArchiveSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    archived: StrictBool


class SessionLifecycle:
    def __init__(self, store, security, manager, checkpoints):
        self.store, self.security = store, security
        self.manager, self.checkpoints = manager, checkpoints

    async def require_live_api(self, request: Request):
        """Guard the actual dispatched run routes, including files and computer."""
        route = request.scope.get('route')
        if not getattr(route, 'path', '').startswith('/api/runs/{run_id}'):
            return
        self.security.require(request, mutation=request.method in {'POST', 'PUT', 'PATCH', 'DELETE'})
        run = self.store.run(request.path_params['run_id'])
        deleting = request.method == 'DELETE' and route.path == '/api/runs/{run_id}'
        # A fresh/reconnecting stream must receive its terminal marker. The
        # events handler checks deletion before reading any retained history.
        events = request.method == 'GET' and route.path == '/api/runs/{run_id}/events'
        if not run or (run['deleted_at'] and not (deleting or events)):
            raise HTTPException(404, 'Session not found.')

    def archives(self, actor):
        return {row['run_id'] for row in self.store.rows('SELECT run_id FROM session_archives WHERE owner_id=?', (actor,))}

    @staticmethod
    def can_delete(run, actor, admin):
        return not run['parent_run_id'] and (admin or bool(actor and run['owner_id'] == actor))

    def metadata(self, run, actor, admin, archives=None):
        archives = self.archives(actor) if archives is None else archives
        return {'archived': (run['parent_run_id'] or run['id']) in archives,
                'can_delete': self.can_delete(run, actor, admin)}

    def archive(self, run_id, actor, archived):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['deleted_at']:
                raise HTTPException(404, 'Session not found.')
            if run['parent_run_id']:
                raise HTTPException(422, 'Archive the parent session to keep its agents together.')
            if archived:
                conn.execute('INSERT OR IGNORE INTO session_archives VALUES(?,?,?)', (actor, run_id, now()))
            else:
                conn.execute('DELETE FROM session_archives WHERE owner_id=? AND run_id=?', (actor, run_id))
        return {'id': run_id, 'archived': archived}

    def delete(self, run_id, actor, admin):
        # Shared with enqueue's BEGIN IMMEDIATE: one commits first, and the
        # other observes either queued work or the retained deletion marker.
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                raise HTTPException(404, 'Session not found.')
            if run['parent_run_id']:
                raise HTTPException(422, 'Delete the parent session to keep its agents together.')
            if not self.can_delete(run, actor, admin):
                raise HTTPException(403, 'Only the session creator or an administrator can delete this session.')
            if not run['deleted_at']:
                family = conn.execute('SELECT id,status FROM runs WHERE id=? OR parent_run_id=?', (run_id, run_id)).fetchall()
                durable = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='durable_sessions'").fetchone()
                for member in family:
                    pending = conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running','injected') LIMIT 1", (member['id'],)).fetchone()
                    sending = conn.execute("SELECT 1 FROM slack_outbox WHERE run_id=? AND status='sending' LIMIT 1", (member['id'],)).fetchone()
                    state = conn.execute('SELECT state FROM durable_sessions WHERE run_id=?', (member['id'],)).fetchone() if durable else None
                    if (member['status'] not in TERMINAL or pending or sending or self.manager.is_active(member['id'])
                            or (state and json.loads(state['state']).get('phase', 'idle') != 'idle')):
                        raise HTTPException(409, 'Stop this session and its agents, wait for cleanup to finish, then delete it.')
                conn.execute("UPDATE runs SET deleted_at=?,token_hash='' WHERE id=? OR parent_run_id=?", (now(), run_id, run_id))
                conn.execute("UPDATE slack_outbox SET status='skipped' WHERE status='pending' AND run_id IN (SELECT id FROM runs WHERE id=? OR parent_run_id=?)", (run_id, run_id))
        return {'id': run_id, 'deleted': True}

    def routes(self):
        router = APIRouter()

        @router.post('/api/runs/{run_id}/archive')
        async def archive(run_id: str, body: ArchiveSession, request: Request):
            self.security.require(request, mutation=True)
            actor = self.store.identity(self.security.session_info(request))
            result = self.archive(run_id, actor, body.archived)
            await self.checkpoints.flush()
            return result

        @router.delete('/api/runs/{run_id}')
        async def delete(run_id: str, request: Request):
            self.security.require(request, mutation=True)
            actor = self.store.identity(self.security.session_info(request))
            result = self.delete(run_id, actor, self.security.role(request) == 'admin')
            await self.checkpoints.flush()
            return result

        return router
