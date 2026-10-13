"""A broker dispatch receipt, serialized with steering and capability changes.

The existing event journal records the boundary and outcome. No database lock is
held during a tool, and an ambiguous or duplicate invocation is never replayed.
"""
import hashlib
import json

from fastapi import HTTPException

from .db import now
from .message_queue import MessageQueue


class ToolRejected(HTTPException):
    """A local handler rejected the request before its action could commit."""


class ToolDispatch:
    def __init__(self, store, caller, body):
        self.store, self.caller, self.body = store, caller, body
        self.event_id = None
        self.metadata = {}
        self.confirmed = None

    def claim(self):
        caller, body = self.caller, self.body
        signature = hashlib.sha256(json.dumps(
            [caller['active_message_id'], caller['active_user_id'], caller['token_hash'],
             caller['active_model'], body.input_id, body.name, body.arguments],
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        with self.store.connect(write_scope=caller['id']) as conn:
            conn.begin_write()
            previous = conn.execute("""SELECT data FROM events WHERE run_id=? AND kind='tool_dispatch'
                AND json_text(data,'request_id')=? LIMIT 1""", (caller['id'], body.request_id)).fetchone()
            if previous:
                receipt = json.loads(previous['data'])
                if receipt['signature'] != signature:
                    raise HTTPException(409, 'This tool request identity belongs to another operation.')
                return {'isError': True, 'outcome': receipt['outcome'], 'dispatched': receipt['dispatched'],
                        'outcome_uncertain': receipt['outcome'] == 'unknown',
                        'request_id': body.request_id, 'retryable': False,
                        'error': ('This tool was already dispatched. Inspect its saved outcome and the destination; do not repeat the action.'
                                  if receipt['dispatched'] else
                                  'This tool was not dispatched. Review the rejection and use a new request identity for a corrected action.')}
            run = conn.execute('SELECT * FROM runs WHERE id=?', (caller['id'],)).fetchone()
            if (not run or run['deleted_at'] or run['deletion_requested_at']
                    or run['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                    or any(run[key] != caller[key] for key in
                           ('active_message_id', 'active_user_id', 'active_model', 'token_hash', 'harness'))
                    or not MessageQueue.current_input(conn, caller['id'], caller['active_message_id'], body.input_id)
                    or MessageQueue.pending_input(conn, caller['id'], caller['active_message_id'])):
                return {'isError': True, 'outcome': 'cancelled', 'dispatched': False,
                        'error': 'Cancelled before dispatch because the request or execution changed.'}
            self.metadata = {'request_id': body.request_id, 'signature': signature,
                             'turn_id': caller['active_message_id'], 'input_id': body.input_id,
                             'tool': body.name, 'outcome': 'unknown', 'dispatched': True}
            self.event_id = conn.execute("""INSERT INTO events(run_id,kind,message,data,created_at)
                VALUES(?,'tool_dispatch',?,?,?) RETURNING id""",
                (caller['id'], body.name + ' dispatched; awaiting its outcome.',
                 json.dumps(self.metadata), now())).fetchone()[0]
        return None

    @staticmethod
    def result_outcome(result, outcome=None):
        if outcome is None:
            outcome = 'completed'
            if isinstance(result, dict) and (result.get('error') or result.get('isError')):
                outcome = result.get('outcome', 'failed')
                if outcome == 'completed':
                    outcome = 'failed'
        if outcome not in {'completed', 'failed', 'rejected', 'cancelled', 'unknown'}:
            outcome = 'unknown'
        return outcome, not (isinstance(result, dict) and result.get('dispatched') is False)

    def confirm(self, result):
        # Capture only safe receipt metadata before any logging/checkpoint await.
        # An already received outcome cannot become ambiguous on cancellation.
        if self.confirmed is None:
            self.confirmed = self.result_outcome(result)

    def finish(self, result=None, *, outcome=None):
        if self.event_id is None:
            return
        outcome, dispatched = self.confirmed or self.result_outcome(result, outcome)
        # Save factual outcomes even if Stop or a correction changed the turn.
        # Never store tool arguments, credentials or raw results in this receipt.
        self.store.execute("UPDATE events SET message=?,data=? WHERE id=? AND run_id=?",
            (self.body.name + ': ' + outcome, json.dumps({**self.metadata, 'outcome': outcome, 'dispatched': dispatched}),
             self.event_id, self.caller['id']), write_scope=self.caller['id'])
