"""Select public commentary once, durably, for every conversation surface."""
import json
from sqlite3 import Connection

from sandbox.activity import public_text


MAX_UPDATES = 2
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
    # The sandbox may answer before its next model request acknowledges receipt.
    # Only server-owned, delivered steering children grant a direct-answer slot.
    input_id = data.get('input_id')
    reply = None
    if turn_id and type(input_id) is int:
        reply = conn.execute("""SELECT id FROM messages WHERE run_id=? AND id=? AND steering_parent_id=?
            AND (status='injected' OR (status='queued' AND queue_locked=1))""", (run_id, input_id, turn_id)).fetchone()
    reply_to = reply['id'] if reply and not any(meta.get('public_reply_to') == reply['id'] for _, meta in previous) else None
    if reply_to is None:
        routine = [text for text, meta in previous if not meta.get('public_reply_to')]
        if len(routine) >= MAX_UPDATES or message in routine:
            return
    metadata = {**data, 'turn_id': turn_id, 'public_update': True, 'public_reply_to': reply_to}
    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'message',?,?,?)",
                 (run_id, message, json.dumps(metadata), stamp))
