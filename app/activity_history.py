"""Small chat projections and bounded, explicitly requested work history."""
import json

from fastapi import HTTPException


def markers(connection, run_id, cursor):
    # Lifecycle receipts retain durations and completion without reading tool
    # bodies or image previews just to open a conversation.
    rows = connection.execute('''SELECT id,run_id,kind,message,created_at,
        json_number(data,'message_id') AS message_id,
        json_number(data,'response_complete') AS response_complete
        FROM events WHERE run_id=? AND id<=? AND kind='chat' ORDER BY id''', (run_id, cursor))
    receipts = [dict(row) for row in rows]
    for event in receipts:
        event['data'] = {'message_id': event.pop('message_id'), 'response_complete': event.pop('response_complete')}
        if event['data']['response_complete'] is not None:
            event['data']['response_complete'] = bool(event['data']['response_complete'])
    return receipts


def projection(connection, run, messages):
    """Caller owns the read snapshot containing run and message status."""
    cursor = connection.execute('SELECT coalesce(max(id),0) FROM events WHERE run_id=?', (run['id'],)).fetchone()[0]
    receipts = markers(connection, run['id'], cursor)
    roots = {str(message['id']) for message in messages
             if message['role'] == 'user' and not message.get('steering_parent_id')}
    starts = {str(event['data']['message_id']) for event in receipts if event['message'] == 'Response started'} & roots
    active = {str(message['id']) for message in messages
              if message['role'] == 'user' and message['status'] == 'running'
              and not message.get('steering_parent_id')}
    answered, pending = set(), []
    # Match completedHistory in activity.js: an inline session-ID exchange can
    # appear inside a live turn, while queued and steering inputs own no answer.
    for message in messages:
        if message['role'] == 'user':
            if (not message.get('steering_parent_id') and message['status'] not in {'queued', 'steered'}
                    and message.get('started_at') != ''):
                pending.append(str(message['id']))
        elif message['role'] == 'assistant' and message['status'] != 'steered' and pending:
            turn = pending.pop()
            if message['status'] == 'completed':
                answered.add(turn)
    completed = {str(message['id']) for message in messages
                 if message['role'] == 'user' and message['status'] in {'completed', 'idle'}}
    deferred = starts & completed & answered
    loaded = (starts - deferred) | active
    events = {event['id']: event for event in receipts}
    # Failed or unfinished work retains its commentary until a successful
    # answer is present. Every query shares the cursor's SQLite snapshot so SSE
    # can safely resume strictly after that cursor.
    for turn in loaded:
        after = 0
        while True:
            page = _history_page(connection, run['id'], turn, receipts, after, cursor, 200)
            events.update((event['id'], event) for event in page['events'])
            if not page['has_more']:
                break
            after = page['next_after']
    return {'events': sorted(events.values(), key=lambda event: event['id']),
            'activity_cursor': cursor, 'deferred_activity': sorted(deferred),
            'loaded_activity': sorted(loaded)}


def history_page(store, run_id, message_id, *, after=0, until=None, limit=200):
    with store.connect() as connection:
        connection.begin_read()
        message = connection.execute('''SELECT m.id,m.role,m.steering_parent_id FROM messages m
            JOIN runs r ON r.id=m.run_id WHERE m.run_id=? AND m.id=? AND m.status!='deleted'
            AND r.deleted_at='' ''', (run_id, message_id)).fetchone()
        if not message or message['role'] != 'user':
            raise HTTPException(404, 'Work history not found.')
        turn = message['steering_parent_id'] or message_id
        # A steering input belongs to its root turn, always inside this run.
        if not connection.execute('''SELECT 1 FROM messages WHERE run_id=? AND id=?
            AND role='user' AND status!='deleted' AND steering_parent_id IS NULL''', (run_id, turn)).fetchone():
            raise HTTPException(404, 'Work history not found.')
        cursor = connection.execute('SELECT coalesce(max(id),0) FROM events WHERE run_id=?', (run_id,)).fetchone()[0]
        until = min(cursor, until) if until is not None else cursor
        return _history_page(connection, run_id, str(turn), markers(connection, run_id, until), after, until, limit)


def _history_page(connection, run_id, turn, receipts, after, until, limit):
    # Legacy events have no turn_id. Match the UI's lifecycle owner, including
    # repeated starts after recovery and late receipts from an earlier turn.
    ranges, current, start = [], None, None
    for event in receipts:
        target = str(event['data']['message_id'])
        if event['message'] == 'Response started':
            if current == turn:
                ranges.append([start, event['id']])
            current, start = target, event['id']
        elif event['message'] == 'Response saved' and target == current:
            if current == turn:
                ranges.append([start, event['id']])
            current, start = None, None
    if current == turn:
        ranges.append([start, None])
    range_sql = connection.event_ranges_predicate()
    rows = connection.execute(f'''SELECT e.* FROM events e WHERE e.run_id=? AND e.id>? AND e.id<=? AND (
        (e.kind='chat' AND cast(json_number(e.data,'message_id') AS TEXT)=?) OR
        (e.kind!='chat' AND (cast(json_number(e.data,'turn_id') AS TEXT)=? OR
            (coalesce(cast(json_number(e.data,'turn_id') AS TEXT),'0')='0' AND ({range_sql})))))
        ORDER BY e.id LIMIT ?''', (run_id, after, until, turn, turn, json.dumps(ranges), limit + 1)).fetchall()
    more = len(rows) > limit
    events = [dict(row) for row in rows[:limit]]
    for event in events:
        event['data'] = json.loads(event['data'])
    return {'events': events, 'has_more': more, 'next_after': events[-1]['id'] if events else after, 'until': until}
