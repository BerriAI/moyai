"""Shared, durable session lifecycle change inboxes and attention events.

State changes and inbox entries commit together. Consumers handle only changed
sessions, coalescing intermediate states so already-resolved requests stay quiet.
Wakeups are hints; the database owns delivery across disconnects and restarts.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import logging

logger = logging.getLogger(__name__)
BATCH_SIZE = 100
RECONCILE_SECONDS = 30


@dataclass(frozen=True)
class SessionAttention:
    key: str
    target_run_id: str
    kind: str
    occurred_at: str


class SessionEvents:
    def __init__(self, store):
        self.store = store
        self.listening = asyncio.Event()
        if store.schema_updates:
            initialize_schema(store)

    @staticmethod
    def subscribe_in(conn, consumer):
        added = conn.execute('INSERT INTO session_event_consumers VALUES(?) ON CONFLICT DO NOTHING RETURNING name',
                             (consumer,)).fetchone()
        if added:
            # One migration-time snapshot preserves events pending in a former
            # polling consumer. Normal starts and drains never scan all runs.
            conn.execute("""INSERT INTO session_event_inbox(consumer,run_id,kind)
                SELECT ?,id,'session.state_changed' FROM runs WHERE parent_run_id='' AND deleted_at=''
                ON CONFLICT DO NOTHING""", (consumer,))

    def consume_in(self, conn, consumer, handle):
        # Lock selected inbox rows too: direct PostgreSQL writers may not use
        # Store's advisory lock, and must not lose an upsert during acknowledgment.
        lock = ' FOR UPDATE' if self.store.database else ''
        changed = conn.execute('SELECT run_id FROM session_event_inbox WHERE consumer=? ORDER BY id LIMIT ?' + lock,
                               (consumer, BATCH_SIZE)).fetchall()
        roots = set()
        for row in changed:
            run = conn.execute("""WITH RECURSIVE ancestors(id,parent_run_id) AS (
                SELECT id,parent_run_id FROM runs WHERE id=? UNION
                SELECT r.id,r.parent_run_id FROM runs r JOIN ancestors a ON r.id=a.parent_run_id)
                SELECT r.* FROM runs r JOIN ancestors a ON a.id=r.id WHERE a.parent_run_id=''""",
                (row['run_id'],)).fetchone()
            if run and run['id'] not in roots:
                roots.add(run['id'])
                attention = self.attention_in(conn, run)
                if attention:
                    handle(run, attention)
            # Queueing and acknowledgment share the caller's write transaction.
            conn.execute('DELETE FROM session_event_inbox WHERE consumer=? AND run_id=?', (consumer, row['run_id']))
        return len(changed)

    def attention_in(self, conn, run):
        if run['deleted_at'] or run['deletion_requested_at'] or run['status'] in {'stopping', 'cancelled'}:
            return None
        # Child requests require the root owner's attention, but child answers
        # are summarized by the parent and must not generate a stream of DMs.
        descendants = """WITH RECURSIVE descendants(id) AS (
            SELECT id FROM runs WHERE id=? UNION
            SELECT r.id FROM runs r JOIN descendants d ON r.parent_run_id=d.id) """
        pending = conn.execute(descendants + """SELECT a.id,a.run_id,a.created_at FROM approvals a
            JOIN runs r ON r.id=a.run_id JOIN descendants d ON d.id=r.id
            WHERE a.status='pending' AND r.deleted_at=''
            AND r.deletion_requested_at='' AND r.status IN ('running','reconnecting','awaiting_approval')
            ORDER BY a.created_at,a.id LIMIT 1""", (run['id'],)).fetchone()
        if pending:
            return SessionAttention('approval:' + pending['id'], pending['run_id'],
                                    'session.approval_required', pending['created_at'])
        pending = conn.execute(descendants + """SELECT q.id,q.run_id,q.generation,q.created_at,r.updated_at FROM credential_requests q
            JOIN runs r ON r.id=q.run_id JOIN descendants d ON d.id=r.id
            WHERE q.status='pending' AND r.deleted_at=''
            AND r.deletion_requested_at='' AND r.status NOT IN ('stopping','cancelled','failed','interrupted','idle','completed')
            ORDER BY q.created_at,q.id LIMIT 1""", (run['id'],)).fetchone()
        if pending:
            return SessionAttention(f"credential:{pending['id']}:{pending['generation']}", pending['run_id'],
                                    'session.access_required',
                                    pending['updated_at'] if pending['generation'] else pending['created_at'])
        if run['status'] not in {'idle', 'completed', 'failed', 'interrupted'}:
            return None
        if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running') LIMIT 1", (run['id'],)).fetchone():
            return None
        answer = conn.execute("SELECT id,status,created_at FROM messages WHERE run_id=? AND role='assistant' AND status!='steered' ORDER BY id DESC LIMIT 1", (run['id'],)).fetchone()
        if answer and answer['status'] == 'saving':
            return None
        turn = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE run_id=? AND role='user'", (run['id'],)).fetchone()[0]
        outcome = run['status'] if run['status'] in {'failed', 'interrupted'} else 'ready'
        key = f"turn:{turn}:{outcome}:{answer['id'] if answer else 0}"
        if run['status'] in {'failed', 'interrupted'} or (answer and answer['status'] == 'save_failed'):
            return SessionAttention(key, run['id'], 'session.interrupted' if run['status'] == 'interrupted'
                                    else 'session.failed', run['updated_at'])
        if answer or run['status'] == 'completed':
            return SessionAttention(key, run['id'], 'session.response_ready',
                                    answer['created_at'] if answer else run['updated_at'])
        return None

    @asynccontextmanager
    async def wake_on_commit(self, wake):
        loop = asyncio.get_running_loop()
        def changed():
            try:
                loop.call_soon_threadsafe(wake.set)
            except RuntimeError:
                # Closing the event loop can race a final committed write.
                # The durable inbox will be recovered by the next worker.
                if not loop.is_closed():
                    raise
        self.store.commit_listeners.add(changed)
        listener = asyncio.create_task(self.listen_postgres(wake)) if self.store.database else None
        if not listener:
            self.listening.set()
        try:
            yield
        finally:
            self.store.commit_listeners.discard(changed)
            if listener:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            self.listening.clear()

    async def listen_postgres(self, wake):
        import psycopg
        from psycopg import sql
        backend = self.store.database
        channel = 'moyai_session_events_' + hashlib.md5(backend.schema.encode()).hexdigest()
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(backend.pool.conninfo,
                        autocommit=True, connect_timeout=10) as connection:
                    await connection.execute(sql.SQL('LISTEN {}').format(sql.Identifier(channel)))
                    self.listening.set()
                    wake.set()  # Replay writes committed before LISTEN became active.
                    async for _ in connection.notifies():
                        wake.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.listening.clear()
                logger.warning('Session event listener reconnecting after %s', type(exc).__name__)
                wake.set()
                await asyncio.sleep(1)


from .schema_initializers import initialize_session_events as initialize_schema
