"""Request-owned accounting across async admission, cancellation and cleanup."""
import asyncio
import logging
from datetime import datetime, timezone
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException

from .db import database, drain


class ModelRequest:
    def __init__(self, *, settings, store, spend, tracing, checkpoints, run, model, started):
        self.settings, self.store, self.spend = settings, store, spend
        self.tracing, self.checkpoints = tracing, checkpoints
        self.run, self.model, self.started = run, model, started
        # Allocate before the worker starts. Cancellation after its commit must
        # not lose the receipt merely because an await never returns its value.
        self.id = str(uuid4())
        self.gateway_id = ''
        self.admission_attempted = False
        self.finalization = None
        self.startup = False
        self.dispatch_observation = None

    async def admit(self, validate, *, startup):
        self.startup = startup
        self.admission_attempted = True
        await database(self.admit_in, validate, startup)

    def dispatched(self, request):
        if not self.startup or self.dispatch_observation is not None:
            return
        from .performance import model_dispatched
        stamp = datetime.now(timezone.utc).isoformat()
        received = request.scope.get('moyai_broker_received_at')

        async def record():
            try:
                await database(model_dispatched, self.store, self.run, received, stamp, self.id)
            except Exception as exc:
                # Observability must not change an inference outcome or replay it.
                logging.getLogger(__name__).warning('Dispatch timing unavailable (%s)', type(exc).__name__)

        self.dispatch_observation = asyncio.create_task(record())

    def admit_in(self, validate, startup):
        validate()
        run = self.run
        with self.store.connect(write_scope=run['id']) as conn:
            conn.begin_write()
            admitted = conn.execute(
                "UPDATE runs SET model_calls=model_calls+1,turn_model_calls=turn_model_calls+1 "
                "WHERE id=? AND (?=0 OR (CASE WHEN chat_enabled=1 THEN turn_model_calls ELSE model_calls END)<?) "
                "AND status IN ('running','reconnecting','awaiting_approval') AND token_hash=? AND active_message_id IS NOT DISTINCT FROM ? "
                "AND active_user_id IS NOT DISTINCT FROM ? AND active_model IS NOT DISTINCT FROM ? AND (coalesce(active_model,'')!='' OR model IS NOT DISTINCT FROM ?)",
                (run['id'], self.settings.max_agent_iterations, self.settings.max_agent_iterations * 3,
                 run['token_hash'], run['active_message_id'], run['active_user_id'], run['active_model'], run['model'])).rowcount
            if not admitted:
                raise HTTPException(429, 'This run reached its model request limit.')
            self.spend.begin(run, self.model, startup=startup, request_id=self.id, connection=conn)

    async def headers(self, upstream, streaming):
        self.gateway_id = upstream.headers.get('x-litellm-call-id', '')[:200]
        await database(self.spend.headers, self.id, upstream, streaming)
        return self.gateway_id

    async def finish(self, capture, status, messages, response, *, error, close=(), remember=None):
        if self.finalization is None:
            usage = SimpleNamespace(usage=deepcopy(capture.usage), cost=capture.cost) if capture else None
            self.finalization = asyncio.create_task(self.settle(
                usage, status, messages, deepcopy(response), deepcopy(error), tuple(close), remember))
        # All callers join the same outcome. Later cancellation cannot convert
        # a confirmed completion into an interruption or run accounting twice.
        await drain(self.finalization)

    def record(self, capture, status, messages, response, error):
        if not self.admission_attempted or not self.store.rows(
                'SELECT 1 FROM model_requests WHERE id=?', (self.id,)):
            return False
        self.spend.finish(self.id, capture, status)
        if not self.run.get('private_owner_id'):
            self.tracing.model(self.run, self.id, self.started, messages, response, status,
                               gateway_id=self.gateway_id, error=error)
        return True

    async def settle(self, capture, status, messages, response, error, close, remember):
        failures = []
        if self.dispatch_observation is not None:
            await drain(self.dispatch_observation)
        for operation in close:
            try:
                await operation()
            except BaseException as exc:
                failures.append(exc)
        recorded = False
        try:
            recorded = await database(self.record, capture, status, messages, response, error)
        except BaseException as exc:
            failures.append(exc)
        try:
            if recorded and status == 'completed' and remember is not None:
                remember()
        except BaseException as exc:
            failures.append(exc)
        try:
            if self.admission_attempted:
                await self.checkpoints.flush()
        except BaseException as exc:
            failures.append(exc)
        if failures:
            raise failures[0]
