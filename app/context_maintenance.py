"""Durable, run-scoped results for optional tool-free background compaction."""
import asyncio
import json
import logging
import re
from uuid import uuid4

from fastapi import HTTPException

from .context_compaction import compaction_payload
from .db import database, drain


log = logging.getLogger(__name__)


class ContextMaintenance:
    def __init__(self, gateway):
        self.gateway = gateway
        self.store = gateway.store
        self.tasks = {}
        self.admissions = {}
        self.cleanups = set()
        self.finalized = set()
        self.cleanup_errors = []
        self.closing = False
        self.close_task = None

    def recover(self):
        self.store.execute("UPDATE context_jobs SET status='interrupted' WHERE status='running'")

    async def close(self):
        self.closing = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.drain_owned())
        await drain(self.close_task)

    async def drain_owned(self):
        while self.admissions or self.tasks or self.cleanups:
            active = set(self.admissions.values()) | set(self.tasks.values())
            for task in active:
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(*active, *tuple(self.cleanups), return_exceptions=True)
            # Done callbacks register fallback cleanup before releasing ownership.
            await asyncio.sleep(0)
        if self.cleanup_errors:
            raise self.cleanup_errors[0]

    def read(self, run_id, *, connection=None):
        rows = self.store.rows('SELECT * FROM context_jobs WHERE run_id=?', (run_id,), connection=connection)
        if not rows:
            return None
        row = rows[0]
        return {'id': row['operation_id'], 'status': row['status'],
                'snapshot': json.loads(row['snapshot']), 'result': json.loads(row['result'])}

    def claim(self, run_id, operation_id, serialized, ack, run, request):
        self.gateway.require_run(run_id, request)
        with self.store.connect(write_scope=run_id) as conn:
            conn.begin_write()
            current_run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if (not current_run or current_run['deleted_at'] or current_run['deletion_requested_at'] or
                    any(current_run[key] != run[key] for key in
                        ('token_hash', 'active_message_id', 'active_user_id', 'active_model'))):
                raise HTTPException(409, 'The originating response changed before context admission.')
            current = self.read(run_id, connection=conn)
            if current and (current['id'] != ack or current['status'] == 'running'):
                return current, False
            conn.execute('''INSERT INTO context_jobs(run_id,operation_id,snapshot,result,status) VALUES(?,?,?,'null','running')
                ON CONFLICT(run_id) DO UPDATE SET operation_id=excluded.operation_id,
                snapshot=excluded.snapshot,result='null',status='running' ''', (run_id, operation_id, serialized))
            if owner := getattr(self.store, 'broker_work', None):
                owner.claim_in(conn, 'context', operation_id)
            return self.read(run_id, connection=conn), True

    def settle(self, run_id, operation_id, *, status=None, result=None):
        with self.store.connect(write_scope=run_id) as conn:
            if status is None:
                conn.execute("UPDATE context_jobs SET status='interrupted' WHERE run_id=? AND operation_id=? AND status='running'",
                             (run_id, operation_id))
            else:
                conn.execute('UPDATE context_jobs SET status=?,result=? WHERE run_id=? AND operation_id=?',
                             (status, json.dumps(result), run_id, operation_id))
            if owner := getattr(self.store, 'broker_work', None):
                owner.release_in(conn, 'context', operation_id)

    async def cleanup(self, run_id, operation_id):
        await database(self.settle, run_id, operation_id)
        await self.gateway.checkpoints.flush()

    def start_cleanup(self, run_id, operation_id):
        task = asyncio.create_task(self.cleanup(run_id, operation_id))
        self.cleanups.add(task)
        def done(completed):
            self.cleanups.discard(completed)
            if completed.cancelled():
                self.cleanup_errors.append(asyncio.CancelledError())
            elif completed.exception() is not None:
                self.cleanup_errors.append(completed.exception())
        task.add_done_callback(done)
        return task

    def generated(self, run_id, operation_id, task):
        if operation_id in self.finalized:
            self.finalized.discard(operation_id)
        else:
            # Cancellation can close an unstarted model coroutine without ever
            # entering generate()'s finally. Retain an owned fallback cleanup.
            self.start_cleanup(run_id, operation_id)
        if self.tasks.get(operation_id) is task:
            self.tasks.pop(operation_id)
        if not task.cancelled() and task.exception() is not None:
            log.warning('Context maintenance stopped (%s)', type(task.exception()).__name__)

    async def start_job(self, run_id, operation_id, serialized, ack, snapshot, request, run):
        generator = None
        try:
            job, claimed = await database(self.claim, run_id, operation_id, serialized, ack, run, request)
            if not claimed:
                return job
            await self.gateway.checkpoints.flush()
            if self.closing:
                raise asyncio.CancelledError()
            generator = self.gateway.model_slots.run_maintenance(
                self.generate(run_id, operation_id, snapshot, request, run))
            self.tasks[operation_id] = generator
            generator.add_done_callback(lambda done: self.generated(run_id, operation_id, done))
            return await database(self.read, run_id)
        except BaseException as exc:
            if generator is None:
                try:
                    await drain(self.start_cleanup(run_id, operation_id))
                except BaseException as cleanup_error:
                    raise exc from cleanup_error
            raise

    async def exchange(self, run_id, request):
        if self.closing:
            raise HTTPException(503, 'Context maintenance is shutting down.')
        run = await database(self.gateway.require_run, run_id, request)
        body = await self.gateway.read_body(request, '/context/maintenance')
        if not isinstance(body, dict) or set(body) - {'snapshot', 'ack'}:
            raise HTTPException(422, 'Expected a context snapshot and acknowledgement.')
        snapshot, ack = body.get('snapshot'), body.get('ack', '')
        if not isinstance(ack, str) or len(ack) > 64:
            raise HTTPException(422, 'Invalid context acknowledgement.')
        if snapshot is not None:
            if (not isinstance(snapshot, dict) or set(snapshot) != {'epoch', 'cursor', 'summary', 'entries'}
                    or not isinstance(snapshot['epoch'], str) or not re.fullmatch(r'[0-9a-f]{32}', snapshot['epoch'])
                    or type(snapshot['cursor']) is not int or snapshot['cursor'] < 0):
                raise HTTPException(422, 'Invalid context snapshot.')
            compaction_payload(snapshot, run['active_model'] or run['model'])
            if snapshot['entries'][0]['seq'] != snapshot['cursor'] + 1:
                raise HTTPException(422, 'Context snapshot must begin immediately after its cursor.')
            if any(right['seq'] != left['seq'] + 1 for left, right in zip(snapshot['entries'], snapshot['entries'][1:])):
                raise HTTPException(422, 'Context snapshot must cover a contiguous prefix.')
        job = await database(self.read, run_id)
        if job and (job['id'] != ack or job['status'] == 'running'):
            return job
        if snapshot is None:
            return None
        if self.closing:
            raise HTTPException(503, 'Context maintenance is shutting down.')
        operation_id = uuid4().hex
        task = asyncio.create_task(self.start_job(run_id, operation_id,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True), ack, snapshot, request, run))
        self.admissions[operation_id] = task
        def admitted(done):
            self.admissions.pop(operation_id, None)
            if not done.cancelled():
                done.exception()  # The caller still receives the same exception.
        task.add_done_callback(admitted)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            if not task.done() and not task.cancelling():
                task.cancel()
            try:
                await drain(task)
            except BaseException as exc:
                raise cancelled from exc
            raise

    async def generate(self, run_id, operation_id, snapshot, request, run):
        status, result = 'failed', None
        try:
            response = await self.gateway.summarize(run_id, request,
                {**snapshot, 'cursor_protocol': 1}, expected_run=run)
            result = json.loads(response.body)
            status = 'completed'
        except asyncio.CancelledError:
            status = 'interrupted'
            raise
        except Exception:
            pass  # Optional failure never replaces the original task history.
        finally:
            await database(self.settle, run_id, operation_id, status=status, result=result)
            await self.gateway.checkpoints.flush()
            self.finalized.add(operation_id)
