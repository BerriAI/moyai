"""Temporal wake delivery and optional independently replicated execution workers."""
import asyncio
from datetime import timedelta
import json
import logging
import time
from uuid import uuid4

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from .db import database
from .durable_runner import DurableRunner
from .session_workflow import SessionWorkflow
from .automation_workflow import AutomationWorkflow
from .scheduling_diagnostics import elapsed_ms, record, watch_event_loop

log = logging.getLogger(__name__)


class TemporalRunManager(DurableRunner):
    def __init__(self, store, settings):
        super().__init__(store, settings)
        self.temporal = None
        self.worker = None
        self.worker_task = None
        self.dispatch_task = None
        self.diagnostics_task = None
        self.prepared_task = None
        self.dispatch_listener_task = None
        self.ready = asyncio.Event()
        self.dispatch_wake = asyncio.Event()
        self.dispatch_loop = None
        self.identity = 'moyai-' + settings.moyai_runtime_role + '-' + uuid4().hex[:12]

    def submit_in(self, conn, run):
        super().submit_in(conn, run)
        # Wake only after the outbox transaction commits. A rolled-back input
        # must not start a workflow, and thread-pool writers cannot set an
        # asyncio Event directly. Postgres delivers NOTIFY only on commit, so
        # inputs saved by another coordinator or worker also wake the dispatcher.
        if not run.get('deleted_at'):
            if self.store.database:
                conn.execute("SELECT pg_notify('moyai_dispatch', ?)", (self.store.database.schema,))
            conn.commit_callbacks.append(self.notify_dispatch)

    def notify_dispatch(self):
        loop = self.dispatch_loop
        if loop is not None and not loop.is_closed() and not self.closing:
            try:
                loop.call_soon_threadsafe(self.dispatch_wake.set)
            except RuntimeError:
                pass  # Shutdown may close the loop after the check; input is durable.

    async def listen_dispatch(self):
        import psycopg
        while not self.closing:
            try:
                async with await psycopg.AsyncConnection.connect(
                        self.store.database.pool.conninfo, autocommit=True, connect_timeout=10) as conn:
                    await conn.execute('LISTEN moyai_dispatch')
                    self.notify_dispatch()  # Drain inputs committed while disconnected.
                    async for notification in conn.notifies():
                        if notification.payload == self.store.database.schema:
                            self.notify_dispatch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning('Dispatch notifications reconnecting (%s)', type(exc).__name__)
                await asyncio.sleep(2)  # The durable two-second poll remains active.

    async def wait_for_dispatch(self):
        try:
            await asyncio.wait_for(self.dispatch_wake.wait(), timeout=2)
        except TimeoutError:
            pass

    async def cancel(self, run_id, *, swarm_stop=True):
        if swarm_stop:
            await database(self.swarms.stop, run_id)
        state = self.state(run_id)
        if (not (self.store.run(run_id) or {}).get('deletion_requested_at')
                and not self.is_active(run_id) and state.get('phase') not in {'warm', 'warm_cleanup'}
                and not (state.get('computer_only') and state.get('phase') != 'idle')):
            if self.store.rows("SELECT 1 FROM messages WHERE run_id=? AND status='saving'", (run_id,)):
                self.submit(self.store.run(run_id))
            if self.coordinator:
                await self.coordinator.cancel_children(run_id)
            return
        self.store.update_run(run_id, status='stopping', token_hash='')
        self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
        self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
        self.submit(self.store.run(run_id))
        if self.coordinator:
            await self.coordinator.cancel_children(run_id)

    async def recover(self):
        if not (self.settings.encryption_key or self.settings.session_secret):
            raise RuntimeError('Configure a stable ENCRYPTION_KEY before enabling Temporal')
        if self.settings.moyai_runtime_role in {'standalone', 'worker'}:
            self.prepared_task = asyncio.create_task(self.prepared.serve())
        if self.settings.moyai_runtime_role in {'worker', 'broker', 'api'}:
            # Brokers and APIs need a client for request-scoped tools, but never
            # consume execution activities, dispatch wakes or recover sessions.
            self.dispatch_task = asyncio.create_task(self.serve())
            self.diagnostics_task = asyncio.create_task(watch_event_loop())
            return
        # A legacy in-flight process has no launch journal to reconnect to.
        # Refuse an unsafe live cutover rather than guessing and replaying it.
        legacy = self.store.rows("""SELECT id FROM runs WHERE
            (status NOT IN ('queued','idle','completed','failed','cancelled','interrupted')
             OR EXISTS(SELECT 1 FROM messages WHERE run_id=runs.id AND status='running'))
            AND deletion_requested_at=''
            AND NOT EXISTS(SELECT 1 FROM durable_sessions WHERE run_id=runs.id)""")
        if legacy:
            raise RuntimeError('Finish or stop legacy active sessions before enabling Temporal')
        for row in self.store.rows("SELECT id FROM runs WHERE status NOT IN ('idle','completed','failed','cancelled','interrupted') OR (deletion_requested_at!='' AND deleted_at='') OR EXISTS(SELECT 1 FROM messages WHERE run_id=runs.id AND status IN ('queued','saving'))"):
            self.submit(self.store.run(row['id']))
        for row in self.store.rows('SELECT run_id,state FROM durable_sessions'):
            state = json.loads(row['state'])
            if (state.get('phase') in {'warm', 'warm_cleanup'}
                    or (state.get('computer_only') and state.get('phase') != 'idle')):
                self.submit(self.store.run(row['run_id']))
        for row in self.store.rows("SELECT run_id FROM swarm_missions WHERE status='active'"):
            self.submit(self.store.run(row['run_id']))
        # An external write interrupted during Render shutdown is ambiguous;
        # preserve it for review instead of treating it as an unexecuted action.
        if not self.settings.moyai_separate_broker:
            self.store.execute("UPDATE approvals SET status='uncertain' WHERE status='executing'")
        self.dispatch_task = asyncio.create_task(self.serve())
        self.diagnostics_task = asyncio.create_task(watch_event_loop())

    async def connect_temporal(self):
        return await Client.connect(
            self.settings.temporal_address, namespace=self.settings.temporal_namespace,
            api_key=self.settings.temporal_api_key or None, tls=self.settings.temporal_tls,
            identity=self.identity,
        )

    async def wait_ready(self):
        # Render private services use TCP readiness. Complete this before ASGI
        # startup yields, so Uvicorn cannot bind its port while still connecting.
        try:
            async with asyncio.timeout(self.settings.temporal_startup_timeout_seconds):
                while True:
                    await self.ready.wait()
                    await database(self.store.rows, 'SELECT 1')
                    if self.ready.is_set() and not self.closing:
                        return
        except TimeoutError:
            raise RuntimeError('Temporal did not become ready before the runtime startup deadline.') from None

    def make_worker(self, client):
        return Worker(client, task_queue=self.settings.temporal_task_queue,
                      workflows=[SessionWorkflow, AutomationWorkflow], activities=[self.advance_session, self.launch_automation, self.automation_finished],
                      max_concurrent_activities=self.settings.temporal_worker_activities,
                      max_cached_workflows=self.settings.temporal_workflow_cache_size,
                      graceful_shutdown_timeout=timedelta(seconds=1))

    async def serve(self):
        self.dispatch_loop = asyncio.get_running_loop()
        if self.store.database and self.settings.moyai_runtime_role in {'standalone', 'coordinator'}:
            self.dispatch_listener_task = asyncio.create_task(self.listen_dispatch())
        try:
            await self.serve_dispatch()
        finally:
            if self.dispatch_listener_task:
                self.dispatch_listener_task.cancel()
                await asyncio.gather(self.dispatch_listener_task, return_exceptions=True)
                self.dispatch_listener_task = None
            self.dispatch_loop = None

    async def serve_dispatch(self):
        while not self.closing:
            try:
                await database(self.store.rows, 'SELECT 1')
                self.temporal = await self.connect_temporal()
                if self.settings.moyai_runtime_role in {'standalone', 'worker'}:
                    self.worker = self.make_worker(self.temporal)
                    self.worker_task = asyncio.create_task(self.worker.run())
                self.ready.set()
                while not self.closing:
                    # Clear before reading the outbox, never after: an input
                    # committed during dispatch must interrupt the next wait.
                    self.dispatch_wake.clear()
                    if self.worker_task and self.worker_task.done():
                        await self.worker_task
                        raise RuntimeError('Temporal worker stopped')
                    # A lost database fence must stop activities, including
                    # when the task queue currently has no work to deliver.
                    await database(self.store.rows, 'SELECT 1')
                    backlog = False
                    dispatch_failed = False
                    if self.settings.moyai_runtime_role in {'standalone', 'coordinator'}:
                        try:
                            backlog = await self.dispatch()
                        except Exception as exc:
                            dispatch_failed = True
                            # One failed wake must not tear down a healthy
                            # worker and detach all sandbox observations.
                            log.warning('Temporal wake delivery will retry (%s)', type(exc).__name__)
                    # Drain successful full batches immediately. Waiting two
                    # seconds per 200 wakes adds 28 seconds to a 3,000-row burst.
                    # Empty/partial batches and failures retain bounded polling.
                    if not backlog:
                        if dispatch_failed or self.settings.moyai_runtime_role in {'worker', 'broker', 'api'}:
                            await asyncio.sleep(2)
                        else:
                            await self.wait_for_dispatch()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log exception bodies: provider diagnostics may embed
                # credentials or request data. The inbox remains durable.
                log.warning('Temporal connection paused (%s); retrying', type(exc).__name__)
                self.ready.clear()
                if self.worker and self.worker_task:
                    if not self.worker_task.done():
                        await self.worker.shutdown()
                    await asyncio.gather(self.worker_task, return_exceptions=True)
                await asyncio.sleep(5)

    async def dispatch(self):
        started = time.monotonic()
        slots = asyncio.Semaphore(self.settings.temporal_dispatch_concurrency)
        async def deliver(row):
            async with slots:
                delivery_started = time.monotonic()
                await self.temporal.start_workflow(
                    SessionWorkflow.run, row['run_id'], id='moyai-session-' + row['run_id'],
                    task_queue=self.settings.temporal_task_queue, start_signal='wake',
                    rpc_timeout=timedelta(seconds=10),
                )
                await database(self.store.execute,
                    'UPDATE durable_sessions SET delivered=greatest(delivered,?) WHERE run_id=?',
                    (row['revision'], row['run_id']), write_scope=row['run_id'])
                record('session_wake_delivered', run_id=row['run_id'], revision=row['revision'],
                       duration_ms=elapsed_ms(delivery_started))
        rows = await database(self.store.rows,
            'SELECT run_id,revision FROM durable_sessions WHERE revision>delivered ORDER BY run_id LIMIT ?',
            (self.settings.temporal_dispatch_batch_size,))
        results = await asyncio.gather(*(deliver(row) for row in rows), return_exceptions=True)
        if rows:
            record('session_wake_batch', count=len(rows), failed=sum(isinstance(r, BaseException) for r in results),
                   duration_ms=elapsed_ms(started))
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return len(rows) == self.settings.temporal_dispatch_batch_size

    @activity.defn(name='advance_session')
    async def advance_session(self, run_id: str) -> bool | str | dict:
        started = time.monotonic()
        info = activity.info()
        # Server timestamps avoid conflating provider work with task-queue wait.
        queue_ms = max(0, (info.started_time - info.current_attempt_scheduled_time).total_seconds() * 1000)
        state, before = {}, 'unknown'
        error_type = ''
        async def heartbeat():
            while True:
                activity.heartbeat(run_id)
                await asyncio.sleep(5)
        pulse = asyncio.create_task(heartbeat())
        try:
            state = await database(self.state, run_id)
            before = state.get('phase', 'idle')
            return await self.advance(run_id)
        except asyncio.CancelledError:
            error_type = 'CancelledError'
            # Worker shutdown only detaches observation. The Modal supervisor
            # and persisted capability remain live for the replacement worker.
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            # Exception bodies must not leak into Temporal Cloud history.
            from temporalio.exceptions import ApplicationError
            raise ApplicationError('Session step temporarily unavailable', type=type(exc).__name__) from None
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            # Normal monitor steps wait up to 20 seconds by design. Keep those
            # quiet unless scheduling was slow or the activity failed.
            if before != 'monitor' or queue_ms >= 500 or error_type:
                record('session_activity', run_id=run_id, workflow_run_id=info.workflow_run_id,
                       activity_id=info.activity_id, attempt=info.attempt, phase=before,
                       message_id=state.get('message_id'), segment=state.get('segment'),
                       schedule_to_start_ms=round(queue_ms, 2), duration_ms=elapsed_ms(started),
                       error_type=error_type)
            if self.store.tracing:
                # begin_turn may have claimed a message during this activity.
                # Use its saved identity so the first queue delay is not lost.
                try:
                    current = await database(self.state, run_id)
                    inactive = {'idle', 'warm', 'warm_cleanup', 'unknown'}
                    timed = state if before not in inactive else current
                    if timed.get('phase', 'idle') not in inactive and not timed.get('computer_only'):
                        await database(self.store.tracing.runtime_phase, run_id,
                                       timed.get('message_id'), 'activity_queue',
                                       int(info.current_attempt_scheduled_time.timestamp() * 1e9),
                                       int(info.started_time.timestamp() * 1e9),
                                       segment=timed.get('segment', 0))
                except Exception:
                    log.warning('Activity queue trace capture failed')

    @activity.defn(name='launch_automation')
    async def launch_automation(self, automation_id: str, revision: int, occurrence: str, expires_at: str, trigger_id: str = 'default') -> dict:
        try:
            return await self.automations.launch(automation_id, revision, occurrence, expires_at, trigger_id=trigger_id)
        except Exception as exc:
            from temporalio.exceptions import ApplicationError
            raise ApplicationError('Automation launch temporarily unavailable', type=type(exc).__name__) from None

    @activity.defn(name='automation_finished')
    async def automation_finished(self, run_id: str) -> bool:
        try:
            return self.automations.finished(run_id)
        except Exception as exc:
            from temporalio.exceptions import ApplicationError
            raise ApplicationError('Automation status temporarily unavailable', type=type(exc).__name__) from None

    async def shutdown(self):
        self.closing = True
        self.ready.clear()
        if self.dispatch_listener_task:
            self.dispatch_listener_task.cancel()
            await asyncio.gather(self.dispatch_listener_task, return_exceptions=True)
        if self.prepared_task:
            self.prepared_task.cancel()
            await asyncio.gather(self.prepared_task, return_exceptions=True)
        if self.diagnostics_task:
            self.diagnostics_task.cancel()
            await asyncio.gather(self.diagnostics_task, return_exceptions=True)
        if self.dispatch_task:
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
        if self.worker and self.worker_task:
            if not self.worker_task.done():
                await self.worker.shutdown()
            await asyncio.gather(self.worker_task, return_exceptions=True)
