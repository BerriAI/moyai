"""Complete broker payloads across bounded HTTP envelopes.

Parts are encrypted at rest and scoped to one execution and destination. Uploads
never execute actions; the destination still validates ownership and the body.
"""
import base64
import hashlib
import json
import re

from fastapi import HTTPException


ROUTES = frozenset({'/agent-runtime', '/context/native', '/tools/call'})


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS broker_payload_parts (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        scope TEXT NOT NULL, route TEXT NOT NULL, digest TEXT NOT NULL,
        part INTEGER NOT NULL, encrypted TEXT NOT NULL,
        PRIMARY KEY(run_id,scope,route,digest,part))''')


class BrokerPayloads:
    def __init__(self, store, security):
        self.store, self.security = store, security
        if store.schema_updates:
            initialize_schema(store)

    @staticmethod
    def scope(conn, caller):
        row = conn.execute('SELECT * FROM runs WHERE id=?', (caller['id'],)).fetchone()
        keys = ('active_message_id', 'active_user_id', 'token_hash')
        if (not row or row['deleted_at'] or row['deletion_requested_at']
                or row['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or any(row[k] != caller[k] for k in keys)):
            raise HTTPException(409, 'The payload execution owner changed.')
        return hashlib.sha256(json.dumps([row[k] for k in keys]).encode()).hexdigest()

    def upload(self, caller, body):
        if not isinstance(body, dict) or set(body) != {'route', 'digest', 'part', 'data'}:
            raise HTTPException(422, 'Invalid broker payload part.')
        route, digest, part = body['route'], body['digest'], body['part']
        if (not isinstance(route, str) or route not in ROUTES or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest) or type(part) is not int or part < 0):
            raise HTTPException(422, 'Invalid payload destination or part identity.')
        try:
            base64.b64decode(body['data'], validate=True)
        except (TypeError, ValueError):
            raise HTTPException(422, 'Invalid payload part encoding.') from None
        with self.store.connect(write_scope=caller['id']) as conn:
            conn.begin_write()
            scope = self.scope(conn, caller)
            key = (caller['id'], scope, route, digest, part)
            previous = conn.execute('''SELECT encrypted FROM broker_payload_parts
                WHERE run_id=? AND scope=? AND route=? AND digest=? AND part=?''', key).fetchone()
            if previous and self.security.decrypt(previous['encrypted']) != body['data']:
                raise HTTPException(409, 'This payload part already contains different bytes.')
            if not previous:
                conn.execute('INSERT INTO broker_payload_parts VALUES(?,?,?,?,?,?)',
                             (*key, self.security.encrypt(body['data'])))
            return {'uploaded': part}

    def resolve(self, caller, route, body):
        if not isinstance(body, dict) or 'moyai_payload' not in body:
            return body
        if set(body) != ({'moyai_payload', 'name'} if route == '/tools/call' else {'moyai_payload'}):
            raise HTTPException(422, 'Invalid broker payload envelope.')
        manifest = body['moyai_payload']
        if (route not in ROUTES or not isinstance(manifest, dict)
                or set(manifest) != {'digest', 'parts'} or type(manifest['parts']) is not int
                or manifest['parts'] < 1 or not isinstance(manifest['digest'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', manifest['digest'])):
            raise HTTPException(422, 'Invalid broker payload manifest.')
        with self.store.connect() as conn:
            conn.begin_read()
            scope = self.scope(conn, caller)
            rows = conn.execute('''SELECT part,encrypted FROM broker_payload_parts
                WHERE run_id=? AND scope=? AND route=? AND digest=? ORDER BY part''',
                (caller['id'], scope, route, manifest['digest'])).fetchall()
            if len(rows) != manifest['parts'] or any(row['part'] != n for n, row in enumerate(rows)):
                raise HTTPException(409, 'The complete payload is unavailable for this execution. Resend its parts.')
            raw = b''.join(base64.b64decode(self.security.decrypt(row['encrypted']), validate=True) for row in rows)
        if hashlib.sha256(raw).hexdigest() != manifest['digest']:
            raise HTTPException(409, 'Payload integrity verification failed.')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, 'The payload is not valid JSON.') from None
        if route == '/tools/call' and (not isinstance(value, dict) or value.get('name') != body['name']):
            raise HTTPException(422, 'The payload tool identity does not match.')
        return value
