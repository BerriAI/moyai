"""Atomic queue edits and cooperative steering, serialized with message claim."""
from fastapi import HTTPException

from .db import now

STEER_NOTE = 'Paused this response to pick up your queued message. The conversation and workspace were saved.'


class MessageQueue:
    def __init__(self, store, mirror=None):
        self.store, self.mirror = store, mirror

    def change(self, run_id, message_id, actor, admin, revision, action, content=None):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            row = conn.execute("SELECT * FROM messages WHERE run_id=? AND id=? AND role='user'", (run_id, message_id)).fetchone()
            if not run or not row:
                raise HTTPException(404, 'Queued message not found.')
            # Accounting email links never grant editing authority.
            if not admin and row['user_id'] != actor:
                raise HTTPException(403, 'You can only change your own queued messages.')
            if row['status'] != 'queued' or row['queue_locked']:
                raise HTTPException(409, 'Moyai already picked up this message. Send a follow-up instead.')
            if row['revision'] != revision:
                raise HTTPException(409, 'This queued message changed. Refresh it before trying again.')
            if run['status'] == 'stopping':
                raise HTTPException(409, 'Wait for this response to finish stopping.')
            if action == 'edit':
                conn.execute('UPDATE messages SET content=?,revision=revision+1 WHERE id=?', (content, message_id))
            elif action == 'delete':
                # Tombstones preserve submission receipts and attachment ownership.
                conn.execute("UPDATE messages SET status='deleted',revision=revision+1 WHERE id=?", (message_id,))
                if run['steer_message_id'] == message_id:
                    conn.execute('UPDATE runs SET steer_message_id=NULL WHERE id=?', (run_id,))
                if not conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running')", (run_id,)).fetchone():
                    conn.execute("UPDATE runs SET status='idle' WHERE id=? AND status='queued'", (run_id,))
            elif action == 'steer':
                if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status='queued' AND queue_locked=1 AND id!=?", (run_id, message_id)).fetchone():
                    raise HTTPException(409, 'Moyai is already picking up another queued message.')
                conn.execute('UPDATE runs SET steer_message_id=? WHERE id=?', (message_id, run_id))
                conn.execute('UPDATE messages SET revision=revision+1 WHERE id=?', (message_id,))
                # A pending approval has no external side effect. Retire it to
                # let its blocked tool return before the safe steering boundary.
                conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
            else:
                raise ValueError('Unknown queue action')
            updated = dict(conn.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone())
            if self.mirror and action in {'edit', 'delete'}:
                self.mirror(conn, run_id, updated, action)
            conn.execute('UPDATE runs SET updated_at=? WHERE id=?', (now(), run_id))
        self.store.event(run_id, 'chat', {'edit':'Queued message edited', 'delete':'Queued message deleted', 'steer':'Queued message prioritized'}[action],
                         {'message_id': message_id})
        return updated

    def accept_steer(self, run_id, active_message_id):
        """Called between complete tool rounds or while durably checkpointed."""
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['active_message_id'] != active_message_id or run['status'] in {'stopping','cancelled','failed','interrupted'}:
                return None
            row = conn.execute("SELECT * FROM messages WHERE run_id=? AND id=? AND status='queued'", (run_id, run['steer_message_id'])).fetchone()
            if not row or row['id'] == active_message_id:
                return None
            conn.execute('UPDATE messages SET queue_locked=1 WHERE id=?', (row['id'],))
        if not row['queue_locked']:
            self.store.event(run_id, 'chat', 'Queued message picked for steering', {'message_id': row['id']})
        return row['id']

    def accepted(self, run_id, message_id):
        return bool(message_id and self.store.rows("SELECT 1 FROM messages m JOIN runs r ON r.id=m.run_id WHERE r.id=? AND r.steer_message_id=m.id AND m.id=? AND m.status='queued' AND m.queue_locked=1", (run_id, message_id)))
