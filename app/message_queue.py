"""Atomic queue edits and cooperative steering, serialized with message claim."""
from fastapi import HTTPException
import json

from .db import now


class MessageQueue:
    def __init__(self, store, mirror=None):
        self.store, self.mirror = store, mirror

    def change(self, run_id, message_id, actor, admin, revision, action, content=None):
        with self.store.connect() as conn:
            conn.begin_write()
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
        # Most sandbox polls have no input. A read can proceed during another
        # writer's transaction; do not acquire the backend's writer lock just
        # to poll (SQLite's writer lock or Postgres's schema advisory lock).
        # A newly queued input can wait until the next poll. Positive matches
        # are only a hint: the transaction below rechecks ownership and state.
        if not self.store.rows("""SELECT 1 FROM runs r JOIN messages m ON m.run_id=r.id
                WHERE r.id=? AND r.active_message_id=?
                AND r.status NOT IN ('stopping','cancelled','failed','interrupted')
                AND m.status='queued' AND m.id!=?
                AND (m.id=r.steer_message_id OR m.send_immediately=1 OR EXISTS (
                    SELECT 1 FROM slack_receipts s WHERE s.run_id=m.run_id AND s.message_id=m.id))
                LIMIT 1""", (run_id, active_message_id, active_message_id)):
            return None
        with self.store.connect() as conn:
            conn.begin_write()
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['active_message_id'] != active_message_id or run['status'] in {'stopping','cancelled','failed','interrupted'}:
                return None
            row = conn.execute("SELECT * FROM messages WHERE run_id=? AND id=? AND status='queued'", (run_id, run['steer_message_id'])).fetchone()
            if not row:
                # Opted-in web follow-ups and Slack replies each retain their
                # intent while another message occupies the steering slot.
                row = conn.execute("""SELECT m.* FROM messages m WHERE m.run_id=? AND m.status='queued'
                    AND (m.send_immediately=1 OR EXISTS (
                        SELECT 1 FROM slack_receipts s WHERE s.run_id=m.run_id AND s.message_id=m.id))
                    ORDER BY m.id LIMIT 1""", (run_id,)).fetchone()
                if row and row['id'] != active_message_id:
                    conn.execute('UPDATE runs SET steer_message_id=? WHERE id=?', (row['id'], run_id))
            if not row or row['id'] == active_message_id:
                return None
            conn.execute('UPDATE messages SET queue_locked=1 WHERE id=?', (row['id'],))
        if not row['queue_locked']:
            self.store.event(run_id, 'chat', 'Queued message picked for steering', {'message_id': row['id']})
        return row['id']

    def accepted(self, run_id, message_id):
        return bool(message_id and self.store.rows("SELECT 1 FROM messages m JOIN runs r ON r.id=m.run_id WHERE r.id=? AND r.steer_message_id=m.id AND m.id=? AND m.status='queued' AND m.queue_locked=1", (run_id, message_id)))

    def acknowledge(self, run_id, turn_id, ids):
        """Idempotent receipts from the live sandbox, also carried by model/final events."""
        if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int for i in ids):
            raise HTTPException(422, 'Invalid steering receipts.')
        if not ids:
            return
        with self.store.connect() as conn:
            conn.begin_write()
            for message_id in ids:
                changed = conn.execute("UPDATE messages SET status='injected',started_at=? WHERE id=? AND run_id=? AND steering_parent_id=? AND status='queued' AND queue_locked=1",
                                       (now(), message_id, run_id, turn_id)).rowcount
                if changed:
                    conn.execute('UPDATE runs SET steer_message_id=NULL WHERE id=? AND steer_message_id=?', (run_id, message_id))
                    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'status','Your message is guiding the current task.',?,?)",
                                 (run_id, json.dumps({'message_id': message_id, 'turn_id': turn_id, 'phase': 'steering'}), now()))

    def live_control(self, run_id, turn_id, applied, *, checkpointed=False):
        self.acknowledge(run_id, turn_id, applied)
        target = self.accept_steer(run_id, turn_id)
        if not target:
            return {'steer_message_id': None}
        with self.store.connect() as conn:
            conn.begin_write()
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            message = conn.execute('SELECT * FROM messages WHERE id=?', (target,)).fetchone()
            allowed = {'waiting_children', 'waiting_credential'} if checkpointed else {'running', 'reconnecting', 'awaiting_approval'}
            if run['active_message_id'] != turn_id or run['status'] not in allowed:
                return {'steer_message_id': None}
            # A new requester or model needs a checkpointed capability handoff.
            # Never inject another person's input under the current person's private scope.
            pending_files = conn.execute("SELECT 1 FROM slack_audio_inputs WHERE message_id=? AND status='pending'", (target,)).fetchone()
            if pending_files or message['user_id'] != run['active_user_id'] or message['model'] != run['active_model']:
                return {'steer_message_id': target, 'handoff': True}
            conn.execute('UPDATE messages SET steering_parent_id=? WHERE id=?', (turn_id, target))
        from .attachments import attachment_context
        uploads = [item for item in self.store.attachments.for_run(run_id, target) if item['message_id'] == target]
        return {'steer_message_id': None, 'input': {'id': target, 'content': message['content'] + attachment_context(uploads), 'attachments': uploads}}
