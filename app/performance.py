"""Content-free web-chat timings, committed with the operation they describe."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import math
from statistics import median


request_received = ContextVar('performance_request_received', default=None)
SAMPLE_LIMIT = 10000
TERMINAL = {'failed', 'cancelled', 'interrupted', 'deleted', 'completed', 'steered'}


@contextmanager
def web_request(received_at):
    # Scope only the synchronous enqueue call, never background execution tasks.
    token = request_received.set(received_at)
    try:
        yield
    finally:
        request_received.reset(token)


def initialize_schema(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS performance_samples (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, received_at TEXT, persisted_at TEXT NOT NULL,
                send_now_at TEXT, send_now_kind TEXT,
                picked_up_at TEXT, pickup_kind TEXT, first_model_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_performance_received ON performance_samples(received_at);
            CREATE INDEX IF NOT EXISTS idx_performance_send_now ON performance_samples(send_now_at);
            CREATE INDEX IF NOT EXISTS idx_performance_run ON performance_samples(run_id);
        ''')


def submitted_in(conn, run_id, message_id, kind, persisted_at, *, received_at=None,
                 send_now=False, immediate=False):
    received_at = received_at or request_received.get()
    if received_at is None:
        return  # This dashboard measures web submissions, not scheduled/agent inputs.
    conn.execute('''INSERT INTO performance_samples
        (message_id,run_id,kind,received_at,persisted_at,send_now_at,send_now_kind)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(message_id) DO NOTHING''',
        (message_id, run_id, kind, received_at, persisted_at,
         received_at if send_now or immediate else None,
         'explicit' if send_now else 'automatic' if immediate else None))


def prioritized_in(conn, run_id, message_id, requested_at):
    # Old queued messages have no trustworthy startup sample. They can still
    # receive a new, accurately timed Send now request after instrumentation.
    conn.execute('''INSERT INTO performance_samples
        (message_id,run_id,kind,persisted_at,send_now_at,send_now_kind)
        SELECT id,run_id,'follow_up',created_at,?,'explicit' FROM messages
        WHERE id=? AND run_id=? ON CONFLICT(message_id) DO UPDATE SET
        send_now_at=COALESCE(performance_samples.send_now_at,excluded.send_now_at),
        send_now_kind=COALESCE(performance_samples.send_now_kind,excluded.send_now_kind)''',
        (requested_at, message_id, run_id))


def picked_up_in(conn, message_id, stamp, kind):
    conn.execute('''UPDATE performance_samples SET picked_up_at=?,pickup_kind=?
        WHERE message_id=? AND picked_up_at IS NULL''', (stamp, kind, message_id))


def model_started_in(conn, message_id, stamp):
    conn.execute('''UPDATE performance_samples SET first_model_at=?
        WHERE message_id=? AND first_model_at IS NULL''', (stamp, message_id))


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (TypeError, ValueError, AttributeError):
        return None


def milliseconds(start, end):
    start, end = timestamp(start), timestamp(end)
    if start is None or end is None or end < start:
        return None
    return round((end - start).total_seconds() * 1000, 2)


def distribution(values):
    values = sorted(values)
    def percentile(p):
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None
    return {'count': len(values), 'average_ms': round(sum(values) / len(values), 2) if values else None,
            'p50_ms': median(values) if values else None, 'p95_ms': percentile(.95), 'max_ms': values[-1] if values else None}


def metric(rows, start, end):
    values, pending, incomplete, invalid = [], 0, 0, 0
    for row in rows:
        if row[end] is None:
            if row['status'] in TERMINAL:
                incomplete += 1
            else:
                pending += 1
            continue
        value = milliseconds(row[start], row[end])
        if start == 'picked_up_at' and milliseconds(row['received_at'], row[start]) is None:
            value = None  # An impossible claim timestamp cannot produce a valid startup segment.
        if value is None:
            invalid += 1
        else:
            values.append(value)
    return {**distribution(values), 'total': len(rows), 'pending': pending,
            'incomplete': incomplete, 'invalid': invalid}


def report(store, *, hours=24, kind='all', model='', send_kind='all', current=None):
    current = current or datetime.now(timezone.utc)
    start = current - timedelta(hours=hours)
    lower, upper = start.isoformat(), current.isoformat()
    filters, args = [], []
    if kind != 'all':
        filters.append('p.kind=?')
        args.append(kind)
    if model:
        filters.append('m.model=?')
        args.append(model)
    where = ''.join(' AND ' + clause for clause in filters)
    base = '''SELECT p.*,m.status,m.model,r.harness FROM performance_samples p
        JOIN messages m ON m.id=p.message_id AND m.run_id=p.run_id
        JOIN runs r ON r.id=p.run_id WHERE r.mode='modal' AND r.parent_run_id=''
        AND r.deleted_at='' AND r.deletion_requested_at='' AND m.input_kind='user'
        AND m.status!='deleted' '''
    with store.connect() as conn:
        conn.begin_read()
        # Both range reads use dedicated time indexes, with bounded materialization.
        # The report never acquires a writer lock or performs a backfill.
        startup = [dict(row) for row in conn.execute(base + where + '''
            AND p.received_at>=? AND p.received_at<? AND COALESCE(p.pickup_kind,'')!='steering'
            ORDER BY p.received_at DESC,p.message_id DESC LIMIT ?''', (*args, lower, upper, SAMPLE_LIMIT + 1))]
        send_filter = ' AND p.send_now_kind=?' if send_kind != 'all' else ''
        send_args = [send_kind] if send_kind != 'all' else []
        sends = [dict(row) for row in conn.execute(base + where + send_filter + '''
            AND p.send_now_at>=? AND p.send_now_at<?
            ORDER BY p.send_now_at DESC,p.message_id DESC LIMIT ?''', (*args, *send_args, lower, upper, SAMPLE_LIMIT + 1))]
    truncated = {'startup': len(startup) > SAMPLE_LIMIT, 'send_now': len(sends) > SAMPLE_LIMIT}
    startup, sends = startup[:SAMPLE_LIMIT], sends[:SAMPLE_LIMIT]
    picked = [row for row in startup if row['picked_up_at'] is not None]
    metrics = {
        'first_model': metric(startup, 'received_at', 'first_model_at'),
        'send_now': metric(sends, 'send_now_at', 'picked_up_at'),
        'queue_pickup': metric(startup, 'received_at', 'picked_up_at'),
        'after_pickup': metric(picked, 'picked_up_at', 'first_model_at'),
    }
    models = sorted({row['model'] for row in startup + sends})
    breakdown = [{'model': name,
                  'startup': metric([r for r in startup if r['model'] == name], 'received_at', 'first_model_at'),
                  'send_now': metric([r for r in sends if r['model'] == name], 'send_now_at', 'picked_up_at')}
                 for name in models]
    buckets = []
    for index in range(24):
        left = start + timedelta(hours=hours * index / 24)
        right = start + timedelta(hours=hours * (index + 1) / 24)
        def inside(row, field):
            stamp = timestamp(row[field])
            return stamp is not None and left <= stamp < right
        buckets.append({'start': left.isoformat(),
                        'startup': metric([r for r in startup if inside(r, 'received_at')], 'received_at', 'first_model_at'),
                        'send_now': metric([r for r in sends if inside(r, 'send_now_at')], 'send_now_at', 'picked_up_at')})
    recent = {row['message_id']: row for row in startup + sends}
    recent = sorted(recent.values(), key=lambda r: r['send_now_at'] or r['received_at'], reverse=True)[:30]
    samples = [{'run_id': row['run_id'], 'message_id': row['message_id'], 'kind': row['kind'],
                'model': row['model'], 'harness': row['harness'], 'status': row['status'],
                'received_at': row['received_at'], 'send_now_at': row['send_now_at'],
                'send_now_kind': row['send_now_kind'], 'pickup_kind': row['pickup_kind'],
                'first_model_ms': milliseconds(row['received_at'], row['first_model_at']),
                'send_now_ms': milliseconds(row['send_now_at'], row['picked_up_at'])} for row in recent]
    return {'start': lower, 'end': upper, 'hours': hours, 'kind': kind, 'model': model,
            'send_kind': send_kind, 'metrics': metrics, 'models': models, 'by_model': breakdown,
            'trend': buckets, 'recent': samples, 'truncated': truncated, 'sample_limit': SAMPLE_LIMIT,
            'measurement': 'web_api_handler_to_server_observation', 'generated_at': upper}
