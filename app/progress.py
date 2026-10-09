"""Select public commentary once, durably, for every conversation surface."""
import json
from sqlite3 import Connection

from sandbox.activity import focus_text, public_text


MAX_UPDATES = 2
COMPACTION_NOTICES = {
    'Compacting saved context before continuing. Completed tool receipts are preserved.',
    'The agent compacted its context and is continuing. Completed tool receipts remain saved.',
}
INACTIVE = {'stopping', 'completed', 'failed', 'cancelled', 'interrupted', 'idle', 'steered'}


def active_turn(conn: Connection, run_id: str) -> int | None:
    """None is inactive; 0 is a non-chat run with no claimed message."""
    run = conn.execute('SELECT status,chat_enabled,active_message_id FROM runs WHERE id=?', (run_id,)).fetchone()
    if not run or run['status'] in INACTIVE:
        return None
    if not run['chat_enabled']:
        return 0
    message = conn.execute("SELECT id FROM messages WHERE run_id=? AND id=? AND role='user' AND status='running'",
                           (run_id, run['active_message_id'])).fetchone()
    return message['id'] if message else None


def active_input(conn: Connection, run_id: str, turn_id: int) -> int:
    # A locked input has been delivered but may not yet be acknowledged. After
    # acknowledgement use receipt order, not message creation/enqueue order.
    pending = conn.execute("""SELECT id FROM messages WHERE run_id=? AND steering_parent_id=?
        AND status='queued' AND queue_locked=1 LIMIT 1""", (run_id, turn_id)).fetchone()
    if pending:
        return pending['id']
    receipt = conn.execute("""SELECT m.id AS input_id FROM events e JOIN messages m
        ON m.id=json_extract(e.data,'$.message_id') AND m.run_id=e.run_id
        WHERE e.run_id=? AND e.kind='status' AND json_extract(e.data,'$.phase')='steering'
        AND json_extract(e.data,'$.turn_id')=? AND m.steering_parent_id=? AND m.status='injected'
        ORDER BY e.id DESC LIMIT 1""", (run_id, turn_id, turn_id)).fetchone()
    return receipt['input_id'] if receipt else turn_id


def current_focus(conn: Connection, run_id: str) -> str:
    turn_id = active_turn(conn, run_id)
    if turn_id is None:
        return ''
    input_id = active_input(conn, run_id, turn_id)
    row = conn.execute("""SELECT message FROM events WHERE run_id=? AND kind='status'
        AND json_extract(data,'$.live_status')=1 AND json_extract(data,'$.turn_id')=?
        AND json_extract(data,'$.input_id')=? ORDER BY id DESC LIMIT 1""",
        (run_id, turn_id, input_id)).fetchone()
    return row['message'] if row else ''


def record_focus(conn: Connection, run_id: str, message: str, data: dict[str, object], stamp: str) -> None:
    """Admit task focus independently of the durable chat-message allowance."""
    turn_id = active_turn(conn, run_id)
    message = focus_text(message)
    if turn_id is None or not message:
        return
    input_id = active_input(conn, run_id, turn_id)
    if turn_id and (type(data.get('input_id')) is not int or data['input_id'] != input_id):
        return
    identity = data.get('activity_id')
    if not isinstance(identity, str) or not identity or len(identity) > 100:
        return
    if conn.execute("""SELECT 1 FROM events WHERE run_id=? AND kind='status'
        AND json_extract(data,'$.activity_id')=? LIMIT 1""", (run_id, identity)).fetchone():
        return
    metadata = {'phase': 'focus', 'activity_version': 1, 'live_status': True,
                'turn_id': turn_id, 'input_id': input_id, 'activity_id': identity}
    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'status',?,?,?)",
                 (run_id, message, json.dumps(metadata), stamp))


def record(conn: Connection, run_id: str, message: str, data: dict[str, object], stamp: str) -> None:
    """Caller holds the write transaction across scope lookup and admission.

    No timer or process counter: a replay cannot resurrect suppressed chatter,
    and sandbox renewal cannot reset the two-update allowance.
    """
    turn_id = active_turn(conn, run_id)
    message = public_text(message, 1200).strip()
    if turn_id is None or not message or message == '[System: Empty message content sanitised to satisfy protocol]':
        return
    rows = conn.execute("""SELECT message,data FROM events WHERE run_id=? AND kind='message'
        AND COALESCE(json_extract(data,'$.turn_id'),0)=?
        AND COALESCE(json_extract(data,'$.phase'),'')!='processing'""", (run_id, turn_id)).fetchall()
    previous = [(row['message'], json.loads(row['data'])) for row in rows]
    if data.get('activity_id') and any(meta.get('activity_id') == data['activity_id'] for _, meta in previous):
        return
    # These exact runtime notices already render separately from assistant prose.
    # Keep one of each per turn, including across sandbox restarts, without
    # spending either a routine update or a delivered-input reply slot.
    maintenance = message in COMPACTION_NOTICES
    if maintenance and any(text == message for text, _ in previous):
        return
    # The sandbox may answer before its next model request acknowledges receipt.
    # Only server-owned, delivered steering children grant a direct-answer slot.
    input_id = data.get('input_id')
    reply = None
    if not maintenance and turn_id and type(input_id) is int:
        reply = conn.execute("""SELECT id FROM messages WHERE run_id=? AND id=? AND steering_parent_id=?
            AND (status='injected' OR (status='queued' AND queue_locked=1))""", (run_id, input_id, turn_id)).fetchone()
    reply_to = reply['id'] if reply and not any(meta.get('public_reply_to') == reply['id'] for _, meta in previous) else None
    if reply_to is None and not maintenance:
        routine = [text for text, meta in previous
                   if not meta.get('public_reply_to') and text not in COMPACTION_NOTICES]
        if len(routine) >= MAX_UPDATES or message in routine:
            return
    metadata = {**data, 'turn_id': turn_id, 'public_update': True, 'public_reply_to': reply_to}
    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'message',?,?,?)",
                 (run_id, message, json.dumps(metadata), stamp))
