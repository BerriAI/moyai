"""Persist sanitized OTLP spans before sending them; retry with unchanged IDs."""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import logging
import time

import httpx
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest, ExportTraceServiceResponse,
)

log = logging.getLogger(__name__)


TABLES = frozenset({'trace_outbox', 'trace_outbox_raindrop', 'trace_outbox_langfuse',
                   'trace_outbox_langsmith', 'trace_outbox_braintrust', 'trace_outbox_raindrop_events'})
MAX_BATCH_BYTES = 8 * 1024 * 1024


class TraceOutbox:
    # One table per receiver: each keeps its own retries and receipts, so an
    # outage at one destination never delays or duplicates delivery to another.
    batch_row_overhead = 0

    def __init__(self, store, table, endpoint, headers):
        if table not in TABLES:
            raise ValueError('Unknown trace outbox')
        self.store, self.table, self.endpoint = store, table, endpoint
        self.headers = {**headers, 'Content-Type': 'application/x-protobuf'}
        self.client = httpx.AsyncClient(timeout=10, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.task = None
        with store.connect() as conn:
            conn.executescript(f'''
                CREATE TABLE IF NOT EXISTS {table} (
                    trace_id TEXT NOT NULL, span_id TEXT NOT NULL, payload BLOB,
                    created_at REAL NOT NULL, delivered_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(trace_id,span_id)
                );
                CREATE INDEX IF NOT EXISTS {table}_pending
                    ON {table}(next_attempt_at,created_at) WHERE delivered_at IS NULL;
            ''')

    def enqueue(self, span, connection=None):
        payload = encode_spans([span]).SerializeToString()
        self.enqueue_payload(span, payload, connection)

    def enqueue_payload(self, span, payload, connection=None):
        values = (format(span.context.trace_id, '032x'), format(span.context.span_id, '016x'),
                  payload, time.time())
        sql = f'''INSERT INTO {self.table}(trace_id,span_id,payload,created_at)
                  VALUES(?,?,?,?) ON CONFLICT DO NOTHING'''
        if connection is not None:
            connection.execute(sql, values)
        else:
            with self.store.connect() as conn:
                conn.execute(sql, values)
        self.wake.set()

    def encode_batch(self, rows):
        request = ExportTraceServiceRequest()
        for row in rows:
            request.MergeFrom(ExportTraceServiceRequest.FromString(row['payload']))
        return request.SerializeToString()

    def response_error(self, response):
        if response.status_code != 200:
            return 'HTTP ' + str(response.status_code)
        if response.content:
            if 'application/json' in response.headers.get('content-type', ''):
                result = response.json()
                partial = result.get('partialSuccess', result.get('partial_success', {}))
                rejected = int(partial.get('rejectedSpans', partial.get('rejected_spans', 0)))
            else:
                rejected = ExportTraceServiceResponse.FromString(response.content).partial_success.rejected_spans
            if rejected:
                return 'OTLP partial rejection'
        return ''

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            self.wake.clear()
            try:
                if await self.export_once():
                    continue
            except Exception as exc:
                # Diagnostics must not include span contents or credentials.
                log.warning('Trace delivery to %s will retry (%s)', self.table, type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def export_once(self):
        async with self.lock:
            pending = self.store.rows(f'''SELECT trace_id,span_id,length(payload) AS payload_bytes
                FROM {self.table} WHERE delivered_at IS NULL AND next_attempt_at<=?
                ORDER BY created_at,trace_id,span_id LIMIT 64''', (time.time(),))
            queue_ids, size = [], 0
            for row in pending:
                row_size = row['payload_bytes'] + self.batch_row_overhead
                if queue_ids and size + row_size > MAX_BATCH_BYTES:
                    break
                # An existing oversized span is still attempted unchanged by
                # itself. Do not strand it or silently discard trace content.
                queue_ids.append((row['trace_id'], row['span_id']))
                size += row_size
            if not queue_ids:
                return False
            # Read payloads only after selecting a byte-bounded batch; 64 large
            # spans must not all be loaded into memory to send just a few.
            rows = self.store.rows(f'''SELECT * FROM {self.table}
                WHERE (trace_id,span_id) IN (VALUES {','.join('(?,?)' for _ in queue_ids)}) ORDER BY created_at,trace_id,span_id''', tuple(value for pair in queue_ids for value in pair))
            error = ''
            retry_after = 0
            try:
                response = await self.client.post(self.endpoint, headers=self.headers,
                                                  content=self.encode_batch(rows))
                error = self.response_error(response)
                hint = response.headers.get('retry-after', '')
                if error and hint:
                    try:
                        retry_after = float(hint) if hint.isdigit() else (parsedate_to_datetime(hint) - datetime.now(timezone.utc)).total_seconds()
                        retry_after = max(0, min(86400, retry_after))
                    except (ValueError, TypeError, OverflowError):
                        pass
            except Exception as exc:
                error = type(exc).__name__
            stamp = time.time()
            with self.store.connect() as conn:
                for row in rows:
                    if error:
                        delay = max(retry_after, min(300, 2 ** min(row['attempts'] + 1, 9)))
                        conn.execute(f'''UPDATE {self.table} SET attempts=attempts+1,
                            next_attempt_at=?,last_error=? WHERE trace_id=? AND span_id=?''',
                            (stamp + delay, error, row['trace_id'], row['span_id']))
                    else:
                        # Retain a small receipt so journal replay cannot enqueue
                        # an acknowledged span again. Drop its sensitive payload.
                        conn.execute(f'''UPDATE {self.table} SET delivered_at=?,payload=NULL,
                            attempts=attempts+1,last_error='' WHERE trace_id=? AND span_id=?''',
                            (stamp, row['trace_id'], row['span_id']))
            if error:
                log.warning('Trace delivery to %s will retry (%s)', self.table, error)
            return not error

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        # Undelivered spans stay on disk; shutdown does not wait on the gateway.
        await self.client.aclose()


class RaindropEventOutbox(TraceOutbox):
    """Raindrop interactions power Events/Signals; OTLP spans alone do not."""

    # json.dumps adds two brackets and a comma/space between stored JSON
    # objects: exactly two extra bytes per event for every nonempty batch.
    batch_row_overhead = 2

    def __init__(self, store, endpoint, headers):
        super().__init__(store, 'trace_outbox_raindrop_events', endpoint, headers)
        self.headers['Content-Type'] = 'application/json'

    def enqueue(self, span, connection=None):
        # One event per top-level turn. Delegated spans share that event ID.
        if span.parent is not None:
            return
        attrs = span.attributes
        event = {
            'event_id': format(span.context.trace_id, '032x'),
            'user_id': attrs.get('user.id', 'moyai'),
            'event': 'moyai',
            'properties': {'agent': 'moyai', 'run_id': attrs['moyai.run_id'],
                           'turn_id': attrs['moyai.turn_id'], 'session_url': attrs['moyai.session_url'],
                           'status': attrs['moyai.status'], 'environment': attrs['moyai.environment']},
            'ai_data': {'input': attrs['input.value'], 'output': attrs['output.value'],
                        'convo_id': attrs['session.id']},
        }
        self.enqueue_payload(span, json.dumps(event).encode(), connection)

    def encode_batch(self, rows):
        return json.dumps([json.loads(row['payload']) for row in rows]).encode()

    def response_error(self, response):
        # The documented API returns 204; the current hosted API returns 200
        # with the accepted event IDs. Both acknowledge the batch.
        return '' if response.status_code in {200, 204} else 'HTTP ' + str(response.status_code)
