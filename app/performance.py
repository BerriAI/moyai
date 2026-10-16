"""Content-free web-chat milestones and bounded admin latency projections."""
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


from .schema_initializers import initialize_performance as initialize_schema


def submitted_in(conn, run_id, message_id, kind, persisted_at, *, received_at=None,
                 send_now=False, immediate=False):
    received_at = received_at or request_received.get()
    if received_at is None:
        return  # This dashboard measures web submissions, not scheduled/agent inputs.
    conn.execute('''INSERT INTO performance_samples
        (message_id,run_id,kind,received_at,persisted_at,send_now_at,send_now_kind,timing_version)
        VALUES(?,?,?,?,?,?,?,2) ON CONFLICT(message_id) DO NOTHING''',
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


def model_dispatched(store, run, received_at, dispatched_at, request_id):
    """Persist an observed send attempt, not a claim that the gateway received it.

    The timestamp is captured before HTTP send; this write runs independently
    of that transport. A crash can leave a missing observation, never a backfill.
    Keep the ingress and request identity from the earliest dispatched attempt.
    """
    store.execute('''UPDATE performance_samples SET timing_version=2,
        dispatch_at=?,dispatch_broker_at=?,dispatch_request_id=?
        WHERE message_id=? AND run_id=? AND (dispatch_at IS NULL OR dispatch_at>?)''',
        (dispatched_at, received_at, request_id, run['active_message_id'], run['id'], dispatched_at),
        write_scope=run['id'])


def computer_requested_in(conn, run, state, stamp):
    computer = state.get('computer', {})
    kind = ('reuse' if computer.get('phase') == 'ready' else
            'snapshot_restore' if run.get('snapshot_id') else 'cold_start')
    conn.execute('''UPDATE performance_samples SET
        computer_requested_at=?,computer_start_kind=?
        WHERE message_id=? AND run_id=? AND computer_requested_at IS NULL''',
        (stamp, kind, run['active_message_id'], run['id']))


def computer_ready(store, run_id, message_id, stamp):
    store.execute('''UPDATE performance_samples SET computer_ready_at=?
        WHERE run_id=? AND message_id=? AND computer_requested_at IS NOT NULL
        AND computer_ready_at IS NULL''', (stamp, run_id, message_id), write_scope=run_id)


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
    dispatch_samples = [row for row in startup if row['timing_version'] == 2]
    # Use one identical, fully ordered cohort for additive phase statistics.
    dispatch_phases = [row for row in dispatch_samples if all(
        milliseconds(row[a], row[b]) is not None for a, b in (
            ('received_at', 'picked_up_at'), ('picked_up_at', 'dispatch_broker_at'),
            ('dispatch_broker_at', 'dispatch_at')))]
    computers = [row for row in startup if row['computer_requested_at'] is not None]
    metrics = {
        'first_model': metric(startup, 'received_at', 'first_model_at'),
        'send_now': metric(sends, 'send_now_at', 'picked_up_at'),
        'queue_pickup': metric(startup, 'received_at', 'picked_up_at'),
        'after_pickup': metric(picked, 'picked_up_at', 'first_model_at'),
        'model_dispatch': metric(dispatch_samples, 'received_at', 'dispatch_at'),
        'dispatch_queue': metric(dispatch_phases, 'received_at', 'picked_up_at'),
        'worker_to_broker': metric(dispatch_phases, 'picked_up_at', 'dispatch_broker_at'),
        'broker_prepare': metric(dispatch_phases, 'dispatch_broker_at', 'dispatch_at'),
        'computer_ready': metric(computers, 'computer_requested_at', 'computer_ready_at'),
    }
    models = sorted({row['model'] for row in startup + sends})
    breakdown = [{'model': name,
                  'startup': metric([r for r in startup if r['model'] == name], 'received_at', 'first_model_at'),
                  'dispatch': metric([r for r in dispatch_samples if r['model'] == name], 'received_at', 'dispatch_at'),
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
                        'dispatch': metric([r for r in dispatch_samples if inside(r, 'received_at')], 'received_at', 'dispatch_at'),
                        'send_now': metric([r for r in sends if inside(r, 'send_now_at')], 'send_now_at', 'picked_up_at')})
    recent = {row['message_id']: row for row in startup + sends}
    recent = sorted(recent.values(), key=lambda r: r['send_now_at'] or r['received_at'], reverse=True)[:30]
    samples = [{'run_id': row['run_id'], 'message_id': row['message_id'], 'kind': row['kind'],
                'model': row['model'], 'harness': row['harness'], 'status': row['status'],
                'received_at': row['received_at'], 'send_now_at': row['send_now_at'],
                'send_now_kind': row['send_now_kind'], 'pickup_kind': row['pickup_kind'],
                'first_model_ms': milliseconds(row['received_at'], row['first_model_at']),
                'dispatch_ms': milliseconds(row['received_at'], row['dispatch_at']),
                'queue_ms': milliseconds(row['received_at'], row['picked_up_at']),
                'worker_to_broker_ms': milliseconds(row['picked_up_at'], row['dispatch_broker_at']),
                'broker_prepare_ms': milliseconds(row['dispatch_broker_at'], row['dispatch_at']),
                'model_request_id': row['dispatch_request_id'],
                'computer_ready_ms': milliseconds(row['computer_requested_at'], row['computer_ready_at']),
                'computer_start_kind': row['computer_start_kind'],
                'send_now_ms': milliseconds(row['send_now_at'], row['picked_up_at'])} for row in recent]
    return {'start': lower, 'end': upper, 'hours': hours, 'kind': kind, 'model': model,
            'send_kind': send_kind, 'metrics': metrics, 'models': models, 'by_model': breakdown,
            'trend': buckets, 'recent': samples, 'truncated': truncated, 'sample_limit': SAMPLE_LIMIT,
            'dispatch_coverage': {'eligible': len(dispatch_samples), 'legacy': len(startup) - len(dispatch_samples),
                                  'complete_breakdowns': len(dispatch_phases)},
            'computers': {kind: metric([r for r in computers if r['computer_start_kind'] == kind],
                                      'computer_requested_at', 'computer_ready_at')
                          for kind in ('cold_start', 'snapshot_restore', 'reuse')},
            'measurement': 'web_api_handler_to_server_observation', 'generated_at': upper}
