"""Durable, run-scoped results for optional tool-free background compaction."""
import asyncio
import json
import re
from uuid import uuid4

from fastapi import HTTPException

from .context_compaction import compaction_payload


class ContextMaintenance:
    def __init__(self, gateway):
        self.gateway = gateway
        self.store = gateway.store
        self.tasks = {}

    def recover(self):
        # The old process cannot still own inference. Keep completed results;
        # interrupted work needs a fresh capability, never a saved bearer token.
        self.store.execute("UPDATE context_jobs SET status='interrupted' WHERE status='running'")

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def read(self, run_id):
        rows = self.store.rows('SELECT * FROM context_jobs WHERE run_id=?', (run_id,))
        if not rows:
            return None
        row = rows[0]
        return {'id': row['operation_id'], 'status': row['status'],
                'snapshot': json.loads(row['snapshot']), 'result': json.loads(row['result'])}

    async def exchange(self, run_id, request):
        run = self.gateway.require_run(run_id, request)
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
        job = self.read(run_id)
        if job and (job['id'] != ack or job['status'] == 'running'):
            return job
        if snapshot is None:
            return None
        serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
        operation_id = uuid4().hex
        # One row bounds retained work per session. BEGIN IMMEDIATE is the
        # storage admission owner, including simultaneous/retried submissions.
        with self.store.connect() as conn:
            conn.begin_write()
            current = conn.execute('SELECT operation_id,status FROM context_jobs WHERE run_id=?', (run_id,)).fetchone()
            if current and (current['operation_id'] != ack or current['status'] == 'running'):
                return self.read(run_id)
            conn.execute('''INSERT INTO context_jobs(run_id,operation_id,snapshot,result,status) VALUES(?,?,?,'null','running')
                ON CONFLICT(run_id) DO UPDATE SET operation_id=excluded.operation_id,
                snapshot=excluded.snapshot,result='null',status='running' ''', (run_id, operation_id, serialized))
        def settled(done):
            self.tasks.pop(operation_id, None)
            # Covers failed admission and cancellation before generate() starts.
            self.store.execute("UPDATE context_jobs SET status='interrupted' WHERE run_id=? AND operation_id=? AND status='running'",
                               (run_id, operation_id))

        try:
            await self.gateway.checkpoints.flush()
            task = self.gateway.model_slots.run_maintenance(self.generate(run_id, operation_id, snapshot, request, run))
        except BaseException:
            # A failed flush/cancel must not strand a durable running row. The
            # next exchange can acknowledge and retry without restarting us.
            settled(None)
            raise
        self.tasks[operation_id] = task
        task.add_done_callback(settled)
        return self.read(run_id)

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
            # Optional maintenance cannot fail the task or publish model text
            # as an error. The sandbox still owns all the original receipts.
            pass
        finally:
            self.store.execute('UPDATE context_jobs SET status=?,result=? WHERE run_id=? AND operation_id=?',
                               (status, json.dumps(result), run_id, operation_id))
            await self.gateway.checkpoints.flush()
