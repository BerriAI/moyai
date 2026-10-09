"""Encrypted SDK checkpoints, separate from the public conversation journal."""
import hashlib
import json
import re

from cryptography.fernet import InvalidToken
from fastapi import HTTPException

from sandbox.broker_transport import CONTENT_TYPE


MAX_STATE_BYTES = 2 * 1024 * 1024


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def mark_private_context(store, run):
    """Once private context enters a turn, no native transcript from it is reusable."""
    if not run.get('active_user_id') or not run.get('active_message_id'):
        return
    # This may happen before begin, or after a skill/note was forgotten. Never
    # clear the same turn's taint, and never let an old callback taint a new actor.
    store.execute('''INSERT INTO native_sessions(run_id,turn_id,actor_id,tainted)
        SELECT id,active_message_id,active_user_id,1 FROM runs
        WHERE id=? AND active_message_id=? AND active_user_id=? AND deleted_at=''
        ON CONFLICT(run_id) DO UPDATE SET
        tainted=(CASE WHEN native_sessions.turn_id=excluded.turn_id THEN native_sessions.tainted ELSE 0 END)|1,
        encrypted='',turn_id=excluded.turn_id,actor_id=excluded.actor_id''',
        (run['id'], run['active_message_id'], run['active_user_id']))


class NativeSessions:
    def __init__(self, settings, store, security, checkpoints, require_run, read_body):
        self.settings, self.store, self.security = settings, store, security
        self.checkpoints, self.require_run, self.read_body = checkpoints, require_run, read_body

    def scope(self, run):
        private_owner = dict(run).get('private_owner_id', '')
        if private_owner and private_owner != run['active_user_id']:
            raise HTTPException(404, 'Session not found.')
        settings = self.settings
        policy = {'base': settings.litellm_api_base.rstrip('/'),
            'credential': hashlib.sha256(settings.litellm_api_key.encode()).hexdigest(),
            'models': settings.allowed_models(), 'harness': settings.agent_harness,
            'limits': settings.model_dump(mode='json', include={'model_context_limits'})}
        try:
            model = settings.resolve_model(fallback=run['active_model'] or run['model'])
        except ValueError:
            raise HTTPException(409, 'The selected model is no longer available.') from None
        return {'run': run['id'], 'actor': run['active_user_id'], 'model': model,
            **({'private_owner_id': private_owner} if private_owner else {}),
            'harness': run['harness'], 'gateway': hashlib.sha256(encoded(policy).encode()).hexdigest()}

    def observe_scope(self, run):
        scope = encoded(self.scope(run))
        self.store.execute('''UPDATE native_sessions SET tainted=tainted|2,encrypted=''
            WHERE run_id=? AND turn_id=? AND actor_id=? AND lease!='' AND scope!=?''',
            (run['id'], run['active_message_id'], run['active_user_id'], scope))

    @staticmethod
    def ledger(conn, run_id, excluded):
        # Queued/deleted inputs were never delivered. Steering receipts were:
        # retain their parent and status so a late delivery/settlement fences
        # an SDK checkpoint instead of silently omitting a user correction.
        rows = conn.execute("""SELECT id,status,user_id,model,started_at,steering_parent_id FROM messages
            WHERE run_id=? AND role='user' AND status NOT IN ('queued','running','deleted') ORDER BY id""", (run_id,))
        return hashlib.sha256(encoded([dict(row) for row in rows if row['id'] not in excluded]).encode()).hexdigest()

    @staticmethod
    def taint_reason(tainted):
        return 'private_context' if tainted & 1 else 'scope_changed'

    def validate(self, body):
        if not isinstance(body, dict):
            raise HTTPException(422, 'Invalid native session request.')
        action = body.get('action')
        if not isinstance(action, str):
            raise HTTPException(422, 'Invalid native session action.')
        fields = {'action', 'lease'}
        if action in {'begin', 'restart', 'commit'}:
            fields |= {'compatibility', 'checkpoint'}
        if action == 'commit':
            fields.add('state')
        if action not in {'begin', 'restart', 'commit', 'invalidate'} or set(body) != fields:
            raise HTTPException(422, 'Invalid native session action or fields.')
        if not isinstance(body['lease'], str) or not re.fullmatch(r'[0-9a-f]{32}', body['lease']):
            raise HTTPException(422, 'Invalid native session lease.')
        if action != 'invalidate':
            fingerprint, checkpoint = body['compatibility'], body['checkpoint']
            if not isinstance(fingerprint, str) or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
                raise HTTPException(422, 'Invalid native compatibility fingerprint.')
            if (not isinstance(checkpoint, dict) or set(checkpoint) != {'epoch', 'seq'}
                    or not isinstance(checkpoint['epoch'], str) or not re.fullmatch(r'[0-9a-f]{32}', checkpoint['epoch'])
                    or type(checkpoint['seq']) is not int or not 0 <= checkpoint['seq'] < 2**63):
                raise HTTPException(422, 'Invalid native checkpoint position.')
        if action == 'commit':
            if not isinstance(body['state'], dict):
                raise HTTPException(422, 'Native state must be an opaque JSON object.')
            try:
                size = len(encoded(body).encode())
            except (TypeError, ValueError, RecursionError):
                raise HTTPException(422, 'Invalid native state JSON.') from None
            if size > MAX_STATE_BYTES:
                raise HTTPException(413, 'Native state exceeds its storage limit.')

    def restored(self, conn, row, run, scope, body):
        if run['checkpoint_error']:
            return None, 'workspace_recovery'
        if not row or not row['encrypted']:
            return None, self.taint_reason(row['tainted']) if row and row['tainted'] else 'missing'
        try:
            saved = json.loads(self.security.decrypt(row['encrypted']))
        except (InvalidToken, ValueError, TypeError):
            return None, 'unreadable'
        if not isinstance(saved, dict) or saved.get('version') != 1:
            return None, 'incompatible'
        if saved.get('scope') != scope or saved.get('compatibility') != body['compatibility']:
            return None, 'incompatible'
        if saved.get('checkpoint') != body['checkpoint']:
            return None, 'checkpoint_mismatch'
        if saved.get('ledger') != self.ledger(conn, run['id'], {saved.get('turn'), run['active_message_id']}):
            return None, 'turn_mismatch'
        # Queue IDs are not execution order: a queued next user message can
        # already exist when the previous assistant response is inserted.
        previous = conn.execute("""SELECT * FROM messages WHERE run_id=? AND role='user'
            AND id!=? AND started_at!='' AND steering_parent_id IS NULL
            ORDER BY started_at DESC,id DESC LIMIT 1""", (run['id'], run['active_message_id'])).fetchone()
        current = conn.execute('SELECT started_at FROM messages WHERE id=?', (run['active_message_id'],)).fetchone()
        answer = conn.execute("SELECT * FROM messages WHERE run_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                              (run['id'],)).fetchone()
        if (not previous or previous['id'] != saved.get('turn') or previous['status'] != 'completed'
                or previous['user_id'] != scope['actor'] or not answer or answer['status'] != 'completed'
                or answer['user_id'] != scope['actor'] or answer['model'] != scope['model']
                or not current or not previous['started_at'] <= answer['created_at'] <= current['started_at']):
            return None, 'turn_mismatch'
        if not isinstance(saved.get('state'), dict):
            return None, 'unreadable'
        return saved['state'], 'resumed'

    async def exchange(self, run_id, request):
        admitted = self.require_run(run_id, request)
        if request.headers.get('content-type') != CONTENT_TYPE:
            raise HTTPException(415, 'Use an encrypted broker envelope for native state.')
        body = await self.read_body(request, '/context/native')
        self.validate(body)
        lease, action = body['lease'], body['action']
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if (not run or run['deleted_at'] or run['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                    or not run['chat_enabled'] or not run['active_user_id'] or not run['active_message_id']
                    or (run['active_message_id'], run['active_user_id'], run['token_hash']) !=
                       (admitted['active_message_id'], admitted['active_user_id'], admitted['token_hash'])):
                raise HTTPException(409, 'The native session turn or capability changed.')
            scope = encoded(self.scope(run))
            row = conn.execute('SELECT * FROM native_sessions WHERE run_id=?', (run_id,)).fetchone()
            same_turn = row and (row['turn_id'], row['actor_id']) == (run['active_message_id'], run['active_user_id'])
            tainted = row['tainted'] if same_turn else 0
            if action in {'begin', 'restart'}:
                # A fresh SDK attempt may replace its runtime contract, but it
                # cannot bless unseen canonical inputs or a rolled-back journal.
                # Private-context markers can exist before the first admission.
                prior_admission = same_turn and bool(row['scope'])
                checkpoint = row['checkpoint'] if prior_admission else encoded(body['checkpoint'])
                ledger = row['ledger'] if prior_admission else self.ledger(conn, run_id, {run['active_message_id']})
                if prior_admission and row['scope'] != scope:
                    tainted |= 2
                state, reason = (self.restored(conn, row, run, json.loads(scope), body)
                                 if action == 'begin' else (None, 'fresh'))
                if tainted:
                    state, reason = None, self.taint_reason(tainted)
                # Retain the old encrypted candidate for a lost begin response;
                # restart discards it atomically with renewing the lease.
                encrypted = row['encrypted'] if state is not None else ''
                conn.execute('''INSERT INTO native_sessions
                    (run_id,turn_id,actor_id,lease,capability,scope,compatibility,checkpoint,ledger,encrypted,tainted)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                    turn_id=excluded.turn_id,actor_id=excluded.actor_id,lease=excluded.lease,
                    capability=excluded.capability,scope=excluded.scope,compatibility=excluded.compatibility,
                    checkpoint=excluded.checkpoint,ledger=excluded.ledger,encrypted=excluded.encrypted,tainted=excluded.tainted''',
                    (run_id, run['active_message_id'], run['active_user_id'], lease, run['token_hash'], scope,
                     body['compatibility'], checkpoint, ledger, encrypted, tainted))
                result = {'lease': lease, 'state': state, 'reason': reason}
            else:
                if (not same_turn or row['lease'] != lease or row['capability'] != run['token_hash']
                        or row['scope'] != scope):
                    return {'lease': lease, 'saved': False, 'reason': 'stale_lease'}
                saved = False
                reason, encrypted = 'invalidated', ''
                if action == 'commit':
                    initial = json.loads(row['checkpoint'])
                    if tainted:
                        reason = self.taint_reason(tainted)
                    elif run['checkpoint_error']:
                        reason = 'workspace_recovery'
                    elif row['ledger'] != self.ledger(conn, run_id, {run['active_message_id']}):
                        reason = 'turn_mismatch'
                    elif (body['compatibility'] != row['compatibility']
                            or body['checkpoint']['epoch'] != initial['epoch']
                            or body['checkpoint']['seq'] < initial['seq']):
                        reason = 'incompatible'
                    else:
                        envelope = {'version': 1, 'scope': json.loads(scope), 'turn': run['active_message_id'],
                            'compatibility': body['compatibility'], 'checkpoint': body['checkpoint'],
                            'ledger': row['ledger'], 'state': body['state']}
                        encrypted = self.security.encrypt(encoded(envelope))
                        saved, reason = True, 'saved'
                conn.execute('UPDATE native_sessions SET encrypted=?,lease=? WHERE run_id=? AND lease=?',
                             (encrypted, '' if action == 'invalidate' else lease, run_id, lease))
                result = {'lease': lease, 'saved': saved, 'reason': reason}
        await self.checkpoints.flush()
        return result
