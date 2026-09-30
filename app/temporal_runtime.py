"""Temporal client, durable wake outbox, and co-located Python worker."""
import asyncio
from datetime import timedelta
import json
import logging

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from .durable_runner import DurableRunner
from .session_workflow import SessionWorkflow

log = logging.getLogger(__name__)


class TemporalRunManager(DurableRunner):
    def __init__(self, store, settings):
        super().__init__(store, settings)
        self.temporal = None
        self.worker = None
        self.worker_task = None
        self.dispatch_task = None
        self.ready = asyncio.Event()

    def submit(self, run):
        # DB inbox is authoritative. Network errors leave the wake request
        # pending, rather than accepting a message and losing its dispatch.
        self.store.execute('''INSERT INTO durable_sessions(run_id,revision) VALUES(?,1)
            ON CONFLICT(run_id) DO UPDATE SET revision=revision+1''', (run['id'],))

    async def cancel(self, run_id):
        if self.coordinator:
            await self.coordinator.cancel_children(run_id)
        if not self.is_active(run_id) and self.state(run_id).get('phase') not in {'warm', 'warm_cleanup'}:
            return
        self.store.update_run(run_id, status='stopping', token_hash='')
        self.store.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
        self.store.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
        self.submit(self.store.run(run_id))

    async def recover(self):
        if not (self.settings.encryption_key or self.settings.session_secret):
            raise RuntimeError('Configure a stable ENCRYPTION_KEY before enabling Temporal')
        # A legacy in-flight process has no launch journal to reconnect to.
        # Refuse an unsafe live cutover rather than guessing and replaying it.
        legacy = self.store.rows("""SELECT id FROM runs WHERE
            (status NOT IN ('queued','idle','completed','failed','cancelled','interrupted')
             OR EXISTS(SELECT 1 FROM messages WHERE run_id=runs.id AND status='running'))
            AND NOT EXISTS(SELECT 1 FROM durable_sessions WHERE run_id=runs.id)""")
        if legacy:
            raise RuntimeError('Finish or stop legacy active sessions before enabling Temporal')
        for row in self.store.rows("SELECT id FROM runs WHERE status NOT IN ('idle','completed','failed','cancelled','interrupted') OR EXISTS(SELECT 1 FROM messages WHERE run_id=runs.id AND status='queued')"):
            self.submit(self.store.run(row['id']))
        for row in self.store.rows('SELECT run_id,state FROM durable_sessions'):
            if json.loads(row['state']).get('phase') in {'warm', 'warm_cleanup'}:
                self.submit(self.store.run(row['run_id']))
        # An external write interrupted during Render shutdown is ambiguous;
        # preserve it for review instead of treating it as an unexecuted action.
        self.store.execute("UPDATE approvals SET status='uncertain' WHERE status='executing'")
        self.dispatch_task = asyncio.create_task(self.serve())

    async def connect_temporal(self):
        return await Client.connect(
            self.settings.temporal_address, namespace=self.settings.temporal_namespace,
            api_key=self.settings.temporal_api_key or None, tls=self.settings.temporal_tls,
            identity='moyai-render',
        )

    def make_worker(self, client):
        return Worker(client, task_queue=self.settings.temporal_task_queue,
                      workflows=[SessionWorkflow], activities=[self.advance_session],
                      max_concurrent_activities=self.settings.max_concurrent_runs + 20,
                      max_cached_workflows=200,
                      graceful_shutdown_timeout=timedelta(seconds=1))

    async def serve(self):
        while not self.closing:
            try:
                self.temporal = await self.connect_temporal()
                self.worker = self.make_worker(self.temporal)
                self.worker_task = asyncio.create_task(self.worker.run())
                self.ready.set()
                while not self.closing:
                    if self.worker_task.done():
                        await self.worker_task
                        raise RuntimeError('Temporal worker stopped')
                    await self.dispatch()
                    await asyncio.sleep(2)
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
        slots = asyncio.Semaphore(10)
        async def deliver(row):
            async with slots:
                await self.temporal.start_workflow(
                    SessionWorkflow.run, row['run_id'], id='moyai-session-' + row['run_id'],
                    task_queue=self.settings.temporal_task_queue, start_signal='wake',
                    rpc_timeout=timedelta(seconds=10),
                )
                self.store.execute('UPDATE durable_sessions SET delivered=MAX(delivered,?) WHERE run_id=?',
                                   (row['revision'], row['run_id']))
        rows = self.store.rows('SELECT run_id,revision FROM durable_sessions WHERE revision>delivered ORDER BY rowid LIMIT 200')
        results = await asyncio.gather(*(deliver(row) for row in rows), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    @activity.defn(name='advance_session')
    async def advance_session(self, run_id: str) -> bool | str | dict:
        async def heartbeat():
            while True:
                activity.heartbeat(run_id)
                await asyncio.sleep(5)
        pulse = asyncio.create_task(heartbeat())
        try:
            return await self.advance(run_id)
        except asyncio.CancelledError:
            # Worker shutdown only detaches observation. The Modal supervisor
            # and persisted capability remain live for the replacement worker.
            raise
        except Exception as exc:
            # Exception bodies must not leak into Temporal Cloud history.
            from temporalio.exceptions import ApplicationError
            raise ApplicationError('Session step temporarily unavailable', type=type(exc).__name__) from None
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    async def shutdown(self):
        self.closing = True
        if self.dispatch_task:
            self.dispatch_task.cancel()
            await asyncio.gather(self.dispatch_task, return_exceptions=True)
        if self.worker and self.worker_task:
            if not self.worker_task.done():
                await self.worker.shutdown()
            await asyncio.gather(self.worker_task, return_exceptions=True)
