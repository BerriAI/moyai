"""A small, durable budget for public commentary, shared by web and Slack."""
import json
from datetime import datetime

from sandbox.activity import public_text


MAX_UPDATES = 2
MIN_INTERVAL_SECONDS = 300
# Shared by event admission and Slack collection/delivery (aliases r and m).
ACTIVE_TURN_SQL = "m.status='running' AND r.status NOT IN ('stopping','failed','cancelled','interrupted','idle','completed','steered')"


def record_update(store, run_id, message, data, stamp):
    """Keep an opening and one later milestone for a claimed turn.

    The transaction makes the budget survive concurrent callbacks, journal
    replay, steering, and sandbox renewal. Tool activity and final answers use
    their existing paths and never consume this budget.
    """
    message = public_text(message, 1200).strip()
    if not message or message == '[System: Empty message content sanitised to satisfy protocol]':
        return
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute(f"SELECT m.id FROM runs r JOIN messages m ON m.id=r.active_message_id WHERE r.id=? AND {ACTIVE_TURN_SQL}", (run_id,)).fetchone()
        if not active:
            return
        turn_id = active['id']
        previous = conn.execute("SELECT message,data,created_at FROM events WHERE run_id=? AND kind='message' AND json_extract(data,'$.turn_id')=? AND COALESCE(json_extract(data,'$.phase'),'')!='processing' ORDER BY id", (run_id, turn_id)).fetchall()
        previous = [{**dict(row), 'data': json.loads(row['data'])} for row in previous]
        if data.get('activity_id') and any(row['data'].get('activity_id') == data['activity_id'] for row in previous):
            return
        # A direct response to a delivered mid-task message is not unsolicited
        # progress. Allow one such reply, validating the input against the DB;
        # subsequent narration still shares the original turn's update budget.
        reply = conn.execute("SELECT id FROM messages WHERE run_id=? AND id=? AND steering_parent_id=? AND status='injected'",
                             (run_id, data.get('input_id'), turn_id)).fetchone()
        reply_to = reply['id'] if reply and not any(row['data'].get('public_reply_to') == reply['id'] for row in previous) else None
        routine = [row for row in previous if not row['data'].get('public_reply_to')]
        if not reply_to and (len(routine) >= MAX_UPDATES or any(row['message'] == message for row in previous)):
            return
        if not reply_to and routine:
            elapsed = (datetime.fromisoformat(stamp) - datetime.fromisoformat(routine[-1]['created_at'])).total_seconds()
            if elapsed < MIN_INTERVAL_SECONDS:
                return
        data = {**data, 'turn_id': turn_id, 'public_update': True, 'public_reply_to': reply_to}
        conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'message',?,?,?)",
                     (run_id, message, json.dumps(data), stamp))
