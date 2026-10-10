"""Persist reply feedback and deliver it to Lens."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import time

import httpx

RETRY_WINDOW = 24 * 60 * 60
logger = logging.getLogger(__name__)


class FeedbackNotConfigured(RuntimeError):
    pass


class LensFeedback:
    def __init__(self, store, settings, client=None):
        self.store, self.settings = store, settings
        self.target = settings.lens_feedback_target()
        self.endpoint, self.headers = self.target or ('', {})
        self.enabled = self.target is not None
        self.client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.task = None
        if store.schema_updates:
            initialize_schema(store)

    @staticmethod
    def trace_for(conn, run_id, assistant_message_id):
        row = conn.execute('''
            SELECT t.trace_id FROM messages a JOIN trace_contexts t
                ON t.run_id=a.run_id AND t.message_id=COALESCE(a.response_to_id,(
                    SELECT id FROM messages WHERE run_id=a.run_id AND role='user'
                        AND id<a.id AND steering_parent_id IS NULL ORDER BY id DESC LIMIT 1))
            WHERE a.run_id=? AND a.id=? AND a.role='assistant'
        ''', (run_id, assistant_message_id)).fetchone()
        return row['trace_id'] if row else None

    def submit(self, run_id, message_id, author, score, comment, source):
        if not self.enabled:
            raise FeedbackNotConfigured('Lens feedback is not configured.')
        if type(score) is not int or not 0 <= score <= 10:
            raise ValueError('Score must be an integer from 0 to 10.')
        if not isinstance(comment, str) or len(comment) > 10000:
            raise ValueError('Comment must be a string of at most 10000 characters.')
        if not isinstance(author, str) or not author.strip() or len(author.strip()) > 256:
            raise ValueError('Author must be a non-empty string of at most 256 characters.')
        if not isinstance(source, str) or source not in {'web', 'slack'}:
            raise ValueError('Source must be web or slack.')
        clean_author = author.strip()
        clean_comment = comment.strip()
        timestamp = time.time()
        with self.store.connect() as conn:
            conn.begin_write()
            message = conn.execute(
                "SELECT id FROM messages WHERE run_id=? AND id=? AND role='assistant'",
                (run_id, message_id),
            ).fetchone()
            if message is None:
                raise LookupError('Assistant reply not found.')
            trace_id = self.trace_for(conn, run_id, message_id)
            if trace_id is None:
                raise LookupError('Assistant reply has no Lens trace.')
            conn.execute('''
                INSERT INTO lens_feedback
                    (run_id,message_id,author,score,comment,source,trace_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id,message_id,author) DO UPDATE SET
                    score=excluded.score,
                    comment=excluded.comment,
                    source=excluded.source,
                    trace_id=excluded.trace_id,
                    updated_at=excluded.updated_at,
                    delivered_at=NULL,
                    attempts=0,
                    next_attempt_at=0,
                    last_error='',
                    failed=0
            ''', (run_id, message_id, clean_author, score, clean_comment, source, trace_id, timestamp, timestamp))
            row = conn.execute(
                'SELECT score,comment,delivered_at,failed FROM lens_feedback '
                'WHERE run_id=? AND message_id=? AND author=?',
                (run_id, message_id, clean_author),
            ).fetchone()
        self.wake.set()
        return {'score': row['score'], 'comment': row['comment'], 'status': self.status(row)}

    @staticmethod
    def status(row):
        if row['delivered_at'] is not None:
            return 'delivered'
        return 'failed' if row['failed'] else 'pending'

    def for_messages(self, run_id, author):
        rows = self.store.rows('''
            SELECT message_id,score,comment,delivered_at,failed
            FROM lens_feedback WHERE run_id=? AND author=?
        ''', (run_id, author))
        return {
            row['message_id']: {
                'score': row['score'],
                'comment': row['comment'],
                'status': self.status(row),
            }
            for row in rows
        }

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
        await self.client.aclose()

    async def run(self):
        while True:
            self.wake.clear()
            try:
                if await self.deliver_once():
                    continue
            except Exception as exc:
                logger.warning('Lens feedback delivery will retry (%s)', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    @staticmethod
    def retry_after(value, current_time):
        if not value:
            return 0
        try:
            return min(86400, max(0, int(float(value))))
        except (ValueError, OverflowError):
            try:
                retry_time = parsedate_to_datetime(value)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return min(86400, max(0, int((retry_time - datetime.fromtimestamp(current_time, timezone.utc)).total_seconds())))
            except (TypeError, ValueError, OverflowError):
                return 0

    async def deliver_once(self):
        if not self.enabled:
            return False
        async with self.lock:
            current_time = time.time()
            self.store.execute('''
                UPDATE lens_feedback SET failed=1,
                    last_error=CASE WHEN last_error='' THEN 'Retry period expired' ELSE last_error END
                WHERE delivered_at IS NULL AND failed=0 AND updated_at<=?
            ''', (current_time - RETRY_WINDOW,))
            rows = self.store.rows('''
                SELECT * FROM lens_feedback
                WHERE delivered_at IS NULL AND failed=0 AND next_attempt_at<=?
                ORDER BY created_at,run_id,message_id LIMIT 64
            ''', (current_time,))
            if not rows:
                return False
            for row in rows:
                await self.deliver_row(row)
            return True

    async def deliver_row(self, row):
        body = {
            'trace_id': row['trace_id'],
            'score': row['score'],
            'comment': row['comment'],
            'user': row['author'],
        }
        try:
            response = await self.client.put(
                self.endpoint,
                json=body,
                headers=self.headers,
                timeout=10,
                follow_redirects=False,
            )
            status_code = response.status_code
            error = f'HTTP {status_code}'
            if status_code == 200:
                self.store.execute('''
                    UPDATE lens_feedback SET delivered_at=?,last_error=''
                    WHERE run_id=? AND message_id=? AND author=? AND updated_at=?
                      AND delivered_at IS NULL AND failed=0
                ''', (time.time(), row['run_id'], row['message_id'], row['author'], row['updated_at']))
                return
            if status_code != 404 and not 500 <= status_code <= 599:
                self.store.execute('''
                    UPDATE lens_feedback SET failed=1,last_error=?
                    WHERE run_id=? AND message_id=? AND author=? AND updated_at=?
                      AND delivered_at IS NULL AND failed=0
                ''', (error, row['run_id'], row['message_id'], row['author'], row['updated_at']))
                return
            retry_after = self.retry_after(response.headers.get('Retry-After'), time.time())
        except Exception as exc:
            error = type(exc).__name__
            retry_after = 0
        current_time = time.time()
        failed = current_time - row['updated_at'] >= RETRY_WINDOW
        attempts = row['attempts'] + 1
        backoff = min(300, 2 ** min(attempts, 9))
        self.store.execute('''
            UPDATE lens_feedback SET attempts=?,next_attempt_at=?,last_error=?,failed=?
            WHERE run_id=? AND message_id=? AND author=? AND updated_at=?
              AND delivered_at IS NULL AND failed=0
        ''', (attempts, current_time + max(backoff, retry_after), error, int(failed),
              row['run_id'], row['message_id'], row['author'], row['updated_at']))


def initialize_schema(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS lens_feedback (
                run_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 10),
                comment TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('web','slack')),
                trace_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                delivered_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                failed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(run_id,message_id,author)
            );
            CREATE INDEX IF NOT EXISTS lens_feedback_pending
                ON lens_feedback(next_attempt_at,created_at)
                WHERE delivered_at IS NULL AND failed=0;
        ''')
