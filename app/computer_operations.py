"""Durable identities for tools crossing the agent/computer boundary.

The session's existing execution owner dispatches work. This store supplies
deduplication and receipts; it never owns another scheduler or retries an effect.
"""
import json
import re

from fastapi import HTTPException

from agent.tools.remote_computer import LOCAL_TOOLS
from agent.tools.mcp_bridge import BROWSER_TOOLS

from .db import now
from .tool_dispatch import ToolRejected


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS computer_operations (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        request_id TEXT NOT NULL, turn_id INTEGER NOT NULL, actor_id TEXT,
        capability TEXT NOT NULL, name TEXT NOT NULL, arguments TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', machine_id TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '{}', UNIQUE(run_id,request_id))''')
    store.execute('''CREATE INDEX IF NOT EXISTS idx_computer_operations_pending
        ON computer_operations(run_id,seq) WHERE status IN ('queued','dispatched')''')


READ_TOOL = {'name': 'computer_operation_read',
    'description': 'Inspect a saved computer operation after an uncertain response. Omit request_id to enumerate operations for this session; follow next. This reads receipts and never retries an action.',
    'inputSchema': {'type': 'object', 'properties': {'request_id': {'type': 'string'},
        'after': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'default': 20}},
        'additionalProperties': False},
    'annotations': {'readOnlyHint': True, 'idempotentHint': True}}


class ComputerOperations:
    def __init__(self, store, submit):
        self.store, self.submit = store, submit
        if store.schema_updates:
            initialize_schema(store)

    @staticmethod
    def current(conn, caller):
        run = conn.execute('SELECT * FROM runs WHERE id=?', (caller['id'],)).fetchone()
        if (not run or run['deleted_at'] or run['deletion_requested_at']
                or run['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or any(run[key] != caller[key] for key in ('active_message_id', 'active_user_id', 'token_hash'))):
            raise HTTPException(409, 'The computer operation owner changed.')
        return dict(run)

    @staticmethod
    def result(row):
        return {'request_id': row['request_id'], 'status': row['status'],
                'result': json.loads(row['result'])}

    def exchange(self, caller, body):
        if not isinstance(body, dict) or set(body) - {'input_id'} != {'action', 'request_id', 'name', 'arguments'}:
            raise HTTPException(422, 'Supply a computer_call with its stable request identity, tool name and arguments.')
        identity, name, arguments = body['request_id'], body['name'], body['arguments']
        allowed = LOCAL_TOOLS | {tool['name'] for tool in BROWSER_TOOLS}
        if (body['action'] != 'computer_call' or not isinstance(identity, str)
                or not re.fullmatch(r'[0-9a-f]{32}', identity)
                or not isinstance(name, str) or name not in allowed or not isinstance(arguments, dict)):
            raise HTTPException(422, 'Invalid computer operation identity or tool.')
        try:
            request = encoded(arguments)
        except (TypeError, ValueError):
            raise HTTPException(422, 'Computer arguments must be JSON.') from None
        with self.store.connect(write_scope=caller['id']) as conn:
            conn.begin_write()
            run = self.current(conn, caller)
            session = conn.execute('SELECT state FROM durable_sessions WHERE run_id=?', (run['id'],)).fetchone()
            if not session or not json.loads(session['state']).get('independent_runtime'):
                raise HTTPException(409, 'This session does not have an independent agent runtime.')
            row = conn.execute('SELECT * FROM computer_operations WHERE run_id=? AND request_id=?',
                               (run['id'], identity)).fetchone()
            if row:
                if (row['turn_id'], row['actor_id'], row['capability'], row['name'], row['arguments']) != (
                        run['active_message_id'], run['active_user_id'], run['token_hash'], name, request):
                    raise HTTPException(409, 'This request identity already belongs to another operation.')
                return self.result(row)
            conn.execute('''INSERT INTO computer_operations
                (run_id,request_id,turn_id,actor_id,capability,name,arguments) VALUES(?,?,?,?,?,?,?)''',
                (run['id'], identity, run['active_message_id'], run['active_user_id'], run['token_hash'], name, request))
            if 'input_id' in body:
                from .message_queue import MessageQueue
                if not MessageQueue.current_input(conn, run['id'], run['active_message_id'], body['input_id']):
                    result = {'isError': True, 'error': 'Cancelled before dispatch because the request changed.'}
                    conn.execute("UPDATE computer_operations SET status='cancelled',result=? WHERE run_id=? AND request_id=?",
                                 (encoded(result), run['id'], identity))
                    return {'request_id': identity, 'status': 'cancelled', 'result': result}
                # Keep the originating input for nested broker calls after a
                # worker replacement. Never adopt a later correction's scope.
                conn.execute("""INSERT INTO events(run_id,kind,message,data,created_at)
                    VALUES(?,'computer_input','Computer input scope saved.',?,?)""",
                    (run['id'], encoded({'request_id': identity, 'input_id': body['input_id']}), now()))
            from .performance import computer_requested_in
            computer_requested_in(conn, run, json.loads(session['state']), now())
            self.submit(conn, run)
            return {'request_id': identity, 'status': 'queued', 'result': {}}

    def input_id(self, run_id, request_id):
        rows = self.store.rows("""SELECT data FROM events WHERE run_id=? AND kind='computer_input'
            AND json_text(data,'request_id')=? LIMIT 1""", (run_id, request_id))
        if not rows:
            raise HTTPException(409, 'The computer input scope is unavailable. No nested action was sent.')
        return json.loads(rows[0]['data'])['input_id']

    def pending(self, run_id):
        rows = self.store.rows("SELECT * FROM computer_operations WHERE run_id=? AND status IN ('queued','dispatched') ORDER BY seq LIMIT 1", (run_id,))
        return rows[0] if rows else None

    def dispatch(self, caller, operation, machine_id):
        if not machine_id:
            raise ValueError('A verified computer is required before dispatch.')
        with self.store.connect(write_scope=caller['id']) as conn:
            conn.begin_write()
            self.current(conn, caller)
            row = conn.execute('SELECT * FROM computer_operations WHERE run_id=? AND request_id=?',
                               (caller['id'], operation['request_id'])).fetchone()
            if not row or (row['turn_id'], row['actor_id'], row['capability']) != (
                    caller['active_message_id'], caller['active_user_id'], caller['token_hash']):
                raise HTTPException(409, 'The computer operation owner changed.')
            if row['status'] == 'cancelled':
                return False
            if row['status'] == 'queued':
                conn.execute("UPDATE computer_operations SET status='dispatched',machine_id=? WHERE seq=?",
                             (machine_id, row['seq']))
            elif row['status'] != 'dispatched' or row['machine_id'] != machine_id:
                raise HTTPException(409, 'This operation cannot be dispatched to another computer.')
            return True

    def settle(self, operation, status, result):
        if status not in {'completed', 'unknown', 'cancelled', 'rejected'}:
            raise ValueError('Invalid computer outcome.')
        # A late factual receipt remains valuable after cancellation/revocation.
        # It may settle uncertainty, but can never reopen or replay the action.
        return self.store.execute('''UPDATE computer_operations SET status=?,result=?
            WHERE seq=? AND capability=? AND status IN ('queued','dispatched','unknown')''',
            (status, encoded(result), operation['seq'], operation['capability']), write_scope=operation['run_id'])

    def read(self, caller, arguments):
        if not isinstance(arguments, dict) or set(arguments) - {'request_id', 'after', 'limit'}:
            raise ToolRejected(422, 'Use request_id or after/limit to read computer receipts.')
        after, limit = arguments.get('after', 0), arguments.get('limit', 20)
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ToolRejected(422, 'Supply a nonnegative cursor and a positive page size.')
        if 'request_id' in arguments and (not isinstance(arguments['request_id'], str) or not arguments['request_id']):
            raise ToolRejected(422, 'Supply a nonempty request identity.')
        with self.store.connect() as conn:
            conn.begin_read()
            try:
                self.current(conn, caller)
            except HTTPException as exc:
                # The owner check precedes the read. Storage failures below
                # still propagate without being labeled known local rejections.
                raise ToolRejected(exc.status_code, exc.detail, exc.headers) from None
            if 'request_id' in arguments:
                row = conn.execute('SELECT * FROM computer_operations WHERE run_id=? AND request_id=?',
                                   (caller['id'], arguments['request_id'])).fetchone()
                if not row:
                    raise ToolRejected(404, 'No computer operation has this identity.')
                return self.result(row)
            rows = conn.execute('SELECT * FROM computer_operations WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?',
                                (caller['id'], after, limit+1)).fetchall()
            return {'operations': [{'seq': row['seq'], 'name': row['name'], **self.result(row)} for row in rows[:limit]],
                    'next': rows[limit-1]['seq'] if len(rows) > limit else None}

    def abandon(self, run_id):
        """Cleanup closes receipt polling without claiming unobserved success."""
        for status, outcome in [('queued', 'cancelled'), ('dispatched', 'unknown')]:
            result = {'content': [{'type': 'text', 'text': encoded({
                'outcome': outcome, 'dispatched': status == 'dispatched',
                'message': 'The execution was released. Inspect uncertain effects before retrying.'})}], 'isError': True}
            self.store.execute('UPDATE computer_operations SET status=?,result=? WHERE run_id=? AND status=?',
                               (outcome, encoded(result), run_id, status), write_scope=run_id)
