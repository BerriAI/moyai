"""Ownership for verified PostgreSQL broker overlap.

A live PostgreSQL ownership session, including its backend start time, is the
proof that work is still owned. Age alone is never permission to recover it.
"""
import asyncio
from contextlib import suppress
import logging

from .database import DatabaseBusyError, DatabaseError
from .db import database


logger = logging.getLogger('uvicorn.error.moyai.broker')


class BrokerWork:
    def __init__(self, store):
        self.store = store
        self.database = store.database
        self.pid = self.database.owner_pid
        self.started = self.database.owner.execute('''SELECT extract(epoch FROM backend_start)::text
            FROM pg_stat_activity WHERE pid=pg_backend_pid()''').fetchone()[0]

    def claim_in(self, conn, kind, work_id):
        conn.execute('''INSERT INTO broker_work_owners(kind,work_id,owner_pid,owner_started)
            VALUES(?,?,?,?)''', (kind, work_id, self.pid, self.started))

    def release_in(self, conn, kind, work_id):
        conn.execute('''DELETE FROM broker_work_owners WHERE kind=? AND work_id=?
            AND owner_pid=? AND owner_started=?''', (kind, work_id, self.pid, self.started))

    def recover(self):
        # Backend PID alone can be reused. Both the exact backend lifetime
        # and its runtime ownership lock must still exist to protect a row.
        dead_query = '''SELECT kind,work_id FROM broker_work_owners o
            WHERE NOT EXISTS(SELECT 1 FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid
                WHERE a.pid=o.owner_pid AND extract(epoch FROM a.backend_start)::text=o.owner_started
                AND l.locktype='advisory' AND l.classid=726940
                AND l.objid=?::regnamespace::oid AND l.objsubid=2 AND l.granted)'''
        schema = (self.database.schema,)
        with self.store.connect() as conn:
            if not conn.execute(dead_query + ' LIMIT 1', schema).fetchone():
                return 0
        # A fresh transaction also refreshes pg_stat_activity's snapshot, so an
        # owner that joined after the probe cannot be mistaken for a dead one.
        with self.store.connect() as conn:
            # A queued exclusive lock would stall unrelated session writers
            # behind recovery. Try once; a busy schema is revisited next tick.
            if not conn.execute('SELECT pg_try_advisory_xact_lock(726941, ?::regnamespace::oid::int)', schema).fetchone()[0]:
                return 0
            # Retain all normal admission/lease fences, then re-read ownership
            # under the writer lock: another broker may have recovered already.
            conn.begin_write()
            dead = conn.execute(dead_query, schema).fetchall()
            for row in dead:
                if row['kind'] == 'model':
                    conn.execute("UPDATE model_requests SET status='interrupted' WHERE id=? AND status='pending'", (row['work_id'],))
                elif row['kind'] == 'context':
                    conn.execute("UPDATE context_jobs SET status='interrupted' WHERE operation_id=? AND status='running'", (row['work_id'],))
                elif row['kind'] == 'approval':
                    conn.execute("UPDATE approvals SET status='uncertain' WHERE id=? AND status='executing'", (row['work_id'],))
                else:
                    raise DatabaseError('Unknown broker work ownership kind.')
                conn.execute('DELETE FROM broker_work_owners WHERE kind=? AND work_id=?', (row['kind'], row['work_id']))
            return len(dead)


class BrokerMaintenance:
    """Only one broker runs recoverable, tool-free background maintenance.

    A replacement can serve requests while waiting for this independent lock.
    Release comes after shutdown of the tasks, never before cancellation drains.
    """
    def __init__(self, store, memory_review, spend_recovery, repository_context=None, *, skill_learning=None):
        self.store, self.memory_review, self.spend_recovery = store, memory_review, spend_recovery
        self.repository_context = repository_context
        self.skill_learning = skill_learning
        self.task = None
        self.owned = False

    def acquire(self):
        if self.owned:
            return True
        self.store.database.check_owner()
        self.owned = self.store.database.owner.execute(
            'SELECT pg_try_advisory_lock(726946, %s::regnamespace::oid::int)',
            (self.store.database.schema,)).fetchone()[0]
        return self.owned

    def start(self):
        self.task = asyncio.create_task(self.serve(), name='broker-maintenance-owner')

    async def serve(self):
        try:
            while not await database(self.acquire):
                await asyncio.sleep(.2)
            self.spend_recovery.start()
            if self.memory_review:
                self.memory_review.start()
            if self.skill_learning:
                self.skill_learning.start()
            if self.repository_context:
                self.repository_context.start()
            await asyncio.Event().wait()
        finally:
            errors = []
            closes = [self.spend_recovery.close]
            if self.memory_review:
                closes.insert(0, self.memory_review.close)
            if self.skill_learning:
                closes.insert(1, self.skill_learning.close)
            if self.repository_context:
                closes.insert(0, self.repository_context.close)
            for close in closes:
                try:
                    await close()
                except BaseException as exc:
                    errors.append(exc)
            # An incomplete close retains leadership until process retirement;
            # never let a successor reset a possibly still-running review.
            if errors:
                raise errors[0]
            if self.owned:
                # A dead session already released its lock; never reconnect it.
                owner = self.store.database.owner
                if not owner.closed and not owner.broken:
                    with suppress(Exception):
                        owner.execute('SELECT pg_advisory_unlock(726946, %s::regnamespace::oid::int)',
                                      (self.store.database.schema,))
                self.owned = False

    async def close(self):
        if self.task:
            if not self.task.cancelling():
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


class BrokerRequests:
    """Cancel this process's routes on ownership loss; never replay tool writes."""
    def __init__(self, work, slots, maintenance):
        self.work, self.slots, self.maintenance = work, slots, maintenance
        self.tasks = set()
        self.task = None
        self.failed = False
        self.closing = False
        self.recovery_busy = False

    @property
    def draining(self):
        return self.closing or self.failed

    def check(self):
        if self.maintenance.task and self.maintenance.task.done() and not self.closing:
            raise DatabaseError('Broker maintenance ownership stopped.')
        self.work.database.check_owner()
        self.slots.check()

    async def recover(self):
        try:
            await database(self.work.recover)
        except DatabaseBusyError:
            # Only the ownership-aware orphan scan is retried. A busy request
            # pool/row must not retire a healthy owner or replay tool writes.
            if not self.recovery_busy:
                logger.warning('Broker orphan recovery deferred while Postgres is busy; ownership checks continue.')
            self.recovery_busy = True
        else:
            if self.recovery_busy:
                logger.info('Broker orphan recovery resumed after database contention.')
            self.recovery_busy = False

    async def watch(self):
        while True:
            try:
                await database(self.check)
                if not self.closing:
                    await self.recover()
            except Exception as exc:
                self.failed = True
                logger.error('Broker ownership failed; cancelling owned requests: %s',
                             str(exc) if isinstance(exc, DatabaseError) else type(exc).__name__)
                # Leave shared slot locks held until each route has finished
                # closing its provider connection and recording its outcome.
                for task in set(self.tasks) | set(self.slots._maintenance):
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await self.maintenance.close()
                return
            await asyncio.sleep(.5)

    async def start(self):
        await database(self.check)
        await self.recover()
        self.maintenance.start()
        self.task = asyncio.create_task(self.watch(), name='broker-ownership-watch')

    async def close(self):
        self.closing = True
        if self.task:
            # A failed watcher is already draining maintenance. Cancelling it
            # again would interrupt that cleanup during automatic retirement.
            if not self.failed:
                self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        await self.maintenance.close()

    async def drain(self):
        # Uvicorn may start lifespan shutdown immediately after cancelling its
        # overdue HTTP tasks. Wait for their transport/accounting cleanup before
        # the owner or shared-slot sessions can be closed by the lifespan.
        current = asyncio.current_task()
        tasks = (set(self.tasks) | set(self.slots._maintenance)) - {current}
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def quiesce(self):
        """Planned retirement keeps live requests and their cleanup uncancelled."""
        self.closing = True
        current = asyncio.current_task()
        while tasks := {
            task for task in set(self.tasks) | set(self.slots._maintenance)
            if task is not current and not task.done()
        }:
            # wait() observes completion without forwarding cancellation to the
            # accepted HTTP tasks or their accounting/transport cleanup.
            await asyncio.wait(tasks)
        maintenance_task = self.maintenance.task
        await self.maintenance.close()
        if maintenance_task is not None and not maintenance_task.cancelled():
            error = maintenance_task.exception()
            if error is not None:
                raise error
        if self.maintenance.owned or self.failed:
            raise DatabaseError('Broker quiescence did not settle ownership.')
        await database(self.check)


class BrokerOwnershipMiddleware:
    def __init__(self, app, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope.get('path', '').startswith('/broker/'):
            return await self.app(scope, receive, send)
        if self.owner.draining:
            from starlette.responses import JSONResponse
            return await JSONResponse({'detail': 'Broker ownership is unavailable.'}, status_code=503)(scope, receive, send)
        task = asyncio.current_task()
        self.owner.tasks.add(task)
        try:
            await database(self.owner.check)
            await self.app(scope, receive, send)
        finally:
            self.owner.tasks.discard(task)


from .schema_initializers import initialize_broker_ownership as initialize_schema
