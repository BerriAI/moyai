"""Durable model job inbox, independent result storage, idempotent accounting."""
import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from uuid import uuid4

import modal
from cryptography.fernet import Fernet
from fastapi import HTTPException

from inference.worker import MAX_ENVELOPE, RECOVER_WINDOW, SUBMIT_WINDOW, decode, encode, money
from .db import now


class ModalInference:
    def __init__(self, settings):
        self.settings = settings
        self._client = None
        self._client_lock = asyncio.Lock()

    async def client(self):
        async with self._client_lock:
            if self._client is None:
                self._client = await modal.Client.from_credentials.aio(self.settings.modal_token_id, self.settings.modal_token_secret)
            return self._client

    async def spawn(self, envelope):
        function = modal.Function.from_name(self.settings.inference_app_name, 'complete', client=await self.client())
        call = await function.spawn.aio(envelope)
        return call.object_id

    async def ledger(self):
        return modal.Dict.from_name(self.settings.inference_ledger_name, client=await self.client())

    async def wait(self, job_id, call_id, seconds):
        # The durable receipt becomes readable before the file archive finishes.
        # A direct function completion also wakes large-response callers.
        call = modal.FunctionCall.from_id(call_id, client=await self.client())
        ledger = await self.ledger()
        completion = asyncio.create_task(call.get.aio(timeout=seconds))
        deadline = time.monotonic() + seconds
        try:
            while not completion.done() and time.monotonic() < deadline:
                if await ledger.contains.aio(job_id + '.result'):
                    return
                await asyncio.sleep(0.25)
        finally:
            completion.cancel()  # Cancels local observation only, not the Modal job.
            await asyncio.gather(completion, return_exceptions=True)

    async def read(self, job_id, suffix):
        ledger = await self.ledger()
        value = await ledger.get.aio(job_id + suffix, None)
        if value is not None:
            if not isinstance(value, bytes) or len(value) > MAX_ENVELOPE:
                raise ValueError('Invalid inference receipt')
            return value
        if suffix == '.headers':
            return None
        volume = modal.Volume.from_name(self.settings.inference_volume_name, client=await self.client())
        content = bytearray()
        try:
            async for chunk in volume.read_file.aio(job_id + suffix):
                content.extend(chunk)
                if len(content) > MAX_ENVELOPE:
                    raise ValueError('Inference receipt too large')
        except FileNotFoundError:
            return None
        return bytes(content)


class InferenceJobs:
    def __init__(self, store, settings, spend, checkpoints, backend=None):
        self.store, self.settings, self.spend, self.checkpoints = store, settings, spend, checkpoints
        self.backend = backend or ModalInference(settings)
        self.locks = {}
        with store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS inference_jobs (
                    id TEXT PRIMARY KEY REFERENCES model_requests(id),
                    run_id TEXT NOT NULL, capability_hash TEXT NOT NULL, client_id TEXT NOT NULL,
                    input_hash TEXT NOT NULL, envelope BLOB NOT NULL,
                    wants_stream INTEGER NOT NULL, created REAL NOT NULL,
                    call_id TEXT NOT NULL DEFAULT '', dispatched REAL NOT NULL DEFAULT 0,
                    workflow_sent INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                    result BLOB, recovered INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_id,capability_hash,client_id)
                );
                CREATE INDEX IF NOT EXISTS idx_inference_recovery ON inference_jobs(recovered,workflow_sent);
            ''')
        if settings.durable_inference_enabled and (not settings.temporal_enabled or not settings.inference_encryption_key):
            raise ValueError('Durable inference requires Temporal and INFERENCE_ENCRYPTION_KEY')
        if store.rows('SELECT 1 FROM inference_jobs WHERE recovered=0 LIMIT 1') and (
                not settings.temporal_enabled or not settings.inference_encryption_key):
            raise ValueError('Keep Temporal and the inference key configured until pending jobs recover')
        if not settings.inference_encryption_key and store.rows('SELECT 1 FROM inference_jobs LIMIT 1'):
            raise ValueError('Keep the inference encryption key to read saved receipts')
        self.cipher = Fernet(settings.inference_encryption_key.encode()) if settings.inference_encryption_key else None

    def get(self, job_id):
        rows = self.store.rows('SELECT * FROM inference_jobs WHERE id=?', (job_id,))
        return rows[0] if rows else None

    def existing(self, run, body):
        client_id = body.get('inference_id')
        if not isinstance(client_id, str) or not re.fullmatch(r'[0-9a-f]{32}', client_id):
            raise HTTPException(422, 'A durable inference ID is required.')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        rows = self.store.rows('SELECT * FROM inference_jobs WHERE run_id=? AND capability_hash=? AND client_id=?',
                               (run['id'], run['token_hash'], client_id))
        if rows and rows[0]['input_hash'] != fingerprint:
            raise HTTPException(409, 'Inference ID was already used for different input.')
        return (rows[0] if rows else None), fingerprint

    def admit(self, run, body, payload, selected_model):
        existing, fingerprint = self.existing(run, body)
        if existing:
            return existing
        if not self.settings.durable_inference_enabled:
            raise HTTPException(409, 'Durable inference admissions are paused. Existing jobs remain recoverable.')
        job_id, created = uuid4().hex, time.time()
        wants_stream = bool(payload.get('stream'))
        payload = {**payload, 'metadata': {'moyai_request_id': job_id}, 'stream': False}
        payload.pop('stream_options', None)
        envelope = encode(self.cipher, {'id': job_id, 'created': created, 'payload': payload,
                                      'base': self.settings.litellm_api_base, 'key_hash': self.spend.key_hash})
        if len(envelope) > MAX_ENVELOPE:
            raise HTTPException(413, 'Model input exceeds the durable job size limit.')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Admission, attribution and dispatch inbox commit together. Never
            # increment counters or regenerate private context for a reconnect.
            rows = conn.execute('SELECT * FROM inference_jobs WHERE run_id=? AND capability_hash=? AND client_id=?',
                                (run['id'], run['token_hash'], body['inference_id'])).fetchall()
            if rows:
                if rows[0]['input_hash'] != fingerprint:
                    raise HTTPException(409, 'Inference ID was already used for different input.')
                return dict(rows[0])
            if conn.execute("SELECT COUNT(*) FROM inference_jobs WHERE recovered=0 AND status='pending'").fetchone()[0] >= self.settings.max_concurrent_model_requests:
                raise HTTPException(429, 'Waiting for a model request slot.',
                                    headers={'X-Moyai-Model-Queue': '1', 'Retry-After': '3'})
            limit = self.settings.max_agent_iterations
            updated = conn.execute("""UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1
                WHERE id=? AND token_hash=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?)
                AND status IN ('running','reconnecting','awaiting_approval')""",
                                   (run['id'], run['token_hash'], limit, limit * 3)).rowcount
            if not updated:
                raise HTTPException(409, 'Session ended or reached its model request limit.')
            user = run['active_user_id'] if run['chat_enabled'] else run['owner_id']
            conn.execute('INSERT INTO model_requests(id,key_hash,run_id,message_id,user_id,model,created_at) VALUES(?,?,?,?,?,?,?)',
                         (job_id, self.spend.key_hash, run['id'], run['active_message_id'], user, selected_model, now()))
            conn.execute('''INSERT INTO inference_jobs(id,run_id,capability_hash,client_id,input_hash,envelope,wants_stream,created)
                VALUES(?,?,?,?,?,?,?,?)''', (job_id, run['id'], run['token_hash'], body['inference_id'], fingerprint,
                                           envelope, wants_stream, created))
        return self.get(job_id)

    def import_receipt(self, job, encrypted, *, final):
        receipt = decode(self.cipher, encrypted)
        expected = self.store.rows('SELECT key_hash FROM model_requests WHERE id=?', (job['id'],))[0]['key_hash']
        if receipt['id'] != job['id'] or receipt['key_hash'] != expected:
            raise ValueError('Inference receipt identity mismatch')
        cost = money(receipt.get('cost'))
        status = receipt['status'] if final else 'pending'
        if status not in {'completed', 'failed', 'unknown', 'rejected', 'pending'}:
            raise ValueError('Invalid inference receipt state')
        usage = receipt.get('usage') or {}
        tokens = [usage.get(k) if type(usage.get(k)) is int and usage[k] >= 0 else None
                  for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        finished = datetime.fromtimestamp(receipt['finished'], timezone.utc).isoformat() if final else None
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # A receipt updates the original row. It never inserts an additional
            # charge, changes key/user attribution or clears already-known cost.
            conn.execute('''UPDATE model_requests SET cost=COALESCE(cost,?),
                cost_source=CASE WHEN cost IS NULL AND ? IS NOT NULL THEN ? ELSE cost_source END,
                gateway_id=CASE WHEN gateway_id='' THEN ? ELSE gateway_id END,
                status=CASE WHEN finished_at IS NULL OR ? THEN ? ELSE status END,
                finished_at=COALESCE(?,finished_at), prompt_tokens=COALESCE(?,prompt_tokens),
                completion_tokens=COALESCE(?,completion_tokens),total_tokens=COALESCE(?,total_tokens) WHERE id=?''',
                         (cost, cost, receipt.get('cost_source', ''), receipt.get('gateway_id', ''), final,
                          status, finished, *tokens, job['id']))
            if final:
                conn.execute("UPDATE inference_jobs SET status=?,result=?,recovered=1,envelope=X'' WHERE id=?",
                             (status, encrypted, job['id']))

    async def advance(self, job_id, wait=0):
        lock = self.locks.setdefault(job_id, asyncio.Lock())
        try:
            async with lock:
                job = self.get(job_id)
                if not job or job['recovered']:
                    return True
                if job['dispatched'] or job['call_id']:
                    result = await self.backend.read(job_id, '.result')
                    if result:
                        self.import_receipt(job, result, final=True)
                        await self.checkpoints.flush()
                        return True
                    headers = await self.backend.read(job_id, '.headers')
                    if headers:
                        self.import_receipt(job, headers, final=False)
                age = time.time() - job['created']
                if not job['call_id'] and age < SUBMIT_WINDOW and time.time() - job['dispatched'] > 30:
                    self.store.execute('UPDATE inference_jobs SET dispatched=? WHERE id=?', (time.time(), job_id))
                    await self.checkpoints.flush()
                    # An acknowledgment can be lost. Re-spawning is safe because
                    # Modal atomically claims the logical ID before inference.
                    call_id = await self.backend.spawn(job['envelope'])
                    self.store.execute('UPDATE inference_jobs SET call_id=? WHERE id=?', (call_id, job_id))
                    job['call_id'] = call_id
                if job['call_id'] and wait:
                    await self.backend.wait(job_id, job['call_id'], wait)
                    result = await self.backend.read(job_id, '.result')
                    if result:
                        self.import_receipt(job, result, final=True)
                        await self.checkpoints.flush()
                        return True
                if age > SUBMIT_WINDOW + 900:
                    # Do not pretend an unresolved request cost zero. Retain
                    # recovery for late receipts, even after the session stops.
                    self.store.execute("UPDATE inference_jobs SET status='unknown' WHERE id=?", (job_id,))
                    self.store.execute("UPDATE model_requests SET status='unknown' WHERE id=? AND finished_at IS NULL", (job_id,))
                await self.checkpoints.flush()
                if age > RECOVER_WINDOW:
                    self.store.execute('UPDATE inference_jobs SET recovered=1 WHERE id=?', (job_id,))
                    return True
                return {'retry_seconds': 3600 if age > SUBMIT_WINDOW + 900 else 10}
        finally:
            # No unbounded lock dictionary after many model calls. A waiter
            # keeps its reference; only remove when nobody is awaiting the lock.
            if not lock.locked() and not getattr(lock, '_waiters', None):
                self.locks.pop(job_id, None)

    async def response(self, job):
        try:
            await self.advance(job['id'], wait=45)
        except Exception:
            # Stable logical ID makes retry safe despite Modal/network failures.
            # Never expose provider diagnostics in a broker response.
            pass
        job = self.get(job['id'])
        if job['result']:
            receipt = decode(self.cipher, job['result'])
            if receipt['status'] == 'completed':
                return receipt['response'], bool(job['wants_stream'])
            raise HTTPException(400, 'Model request could not finish. Any received cost was saved; this request was not replayed.')
        if job['status'] == 'unknown':
            raise HTTPException(400, 'Model outcome is unconfirmed. Cost recovery continues; this request was not replayed.')
        raise HTTPException(503, 'Model request is still being recovered.', headers={'X-Moyai-Inference-Pending': '1'})
