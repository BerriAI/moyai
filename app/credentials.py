"""User-scoped provider keys. Raw values never leave the control plane."""
import asyncio
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .db import now


# Curated origins AND routes: a model cannot send a key to a chosen URL, manage
# provider accounts, or follow a redirect to another host.
PROVIDERS = {
    'fireworks': {'name': 'Fireworks', 'base': 'https://api.fireworks.ai/inference/v1',
                  'setup': 'https://app.fireworks.ai/settings/users/api-keys'},
    'openai': {'name': 'OpenAI', 'base': 'https://api.openai.com/v1',
               'setup': 'https://platform.openai.com/api-keys'},
    'anthropic': {'name': 'Anthropic', 'base': 'https://api.anthropic.com/v1',
                  'setup': 'https://platform.claude.com/settings/keys'},
    'together': {'name': 'Together AI', 'base': 'https://api.together.xyz/v1',
                 'setup': 'https://api.together.ai/settings/api-keys'},
    'groq': {'name': 'Groq', 'base': 'https://api.groq.com/openai/v1',
             'setup': 'https://console.groq.com/keys'},
}
Provider = Literal['fireworks', 'openai', 'anthropic', 'together', 'groq']
Scope = Literal['session', 'personal', 'organization']
ACTIVE = {'running', 'awaiting_approval', 'saving', 'waiting_credential'}


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class CredentialRequest(Arguments):
    provider: Provider
    reason: str = Field(min_length=3, max_length=1000)
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')


class Invoke(Arguments):
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    method: Literal['GET', 'POST'] = 'POST'
    path: str = Field(max_length=80)
    body: dict = Field(default_factory=dict)


class SaveSecret(Arguments):
    provider: Provider
    label: str = Field(min_length=1, max_length=80)
    scope: Scope
    value: SecretStr
    client_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')


class Resolve(Arguments):
    decision: Literal['provide', 'decline'] = 'provide'
    secret_id: str = Field(default='', pattern=r'^([0-9a-f]{32})?$')
    scope: Scope | None = None
    label: str = Field(default='', max_length=80)
    value: SecretStr = SecretStr('')

    @model_validator(mode='after')
    def one_source(self):
        if self.decision == 'provide' and bool(self.secret_id) == bool(self.value.get_secret_value()):
            raise ValueError('Choose a saved key or supply a new key.')
        if self.decision == 'provide' and not self.secret_id and self.scope is None:
            raise ValueError('Choose who can use this new key before saving it.')
        return self


TOOLS = {
    'credentials_request': (CredentialRequest, 'Request a provider API key when a benchmark needs one. Never ask for keys in chat. Supply a clear reason and stable request_key. Existing authorized keys may be reused. Otherwise this tool checkpoints and pauses the session for a secure web form; do not poll or launch other work in this tool round. When adding a new key, the secure form asks the user to choose Personal or Organization (admin-only); This session is also available. Never pick a default on their behalf. Personal keys stay owned by the user; organization keys are shared. Keys never enter the sandbox.'),
    'credentials_http_request': (Invoke, 'Use an authorized credential request for a non-streaming inference or model-list API call. The server supplies authentication to the fixed provider origin. Only approved inference paths are allowed. Responses contain no key. For parallel Python benchmarks, use the proxy instructions returned by credentials_request. Provider usage is billed to that separate key, outside Moyai gateway spend.'),
}


class Credentials:
    def __init__(self, store, security, settings, manager, checkpoints):
        self.store, self.security, self.settings = store, security, settings
        self.manager, self.checkpoints = manager, checkpoints
        self.slots = asyncio.Semaphore(settings.max_concurrent_model_requests)
        with store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS provider_secrets (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, label TEXT NOT NULL,
                    scope TEXT NOT NULL, owner_id TEXT NOT NULL, root_id TEXT NOT NULL DEFAULT '',
                    encrypted TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '',
                    client_id TEXT NOT NULL, UNIQUE(owner_id,client_id)
                );
                CREATE TABLE IF NOT EXISTS credential_requests (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    message_id INTEGER NOT NULL, actor_id TEXT NOT NULL,
                    provider TEXT NOT NULL, reason TEXT NOT NULL, request_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', secret_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, resolved_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id,message_id,request_key)
                );
                CREATE TABLE IF NOT EXISTS credential_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id TEXT NOT NULL,
                    secret_id TEXT NOT NULL, action TEXT NOT NULL, run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
            ''')

    def root(self, run):
        return run['parent_run_id'] or run['id']

    def row(self, request_id):
        rows = self.store.rows('SELECT * FROM credential_requests WHERE id=?', (request_id,))
        if not rows:
            raise ValueError('Credential request not found.')
        return rows[0]

    def same_requester(self, owner_id, actor_id):
        if owner_id == actor_id:
            return True
        # Accounting links are not authentication. Only a recent, eligible
        # Slack profile from users.info may match an independently verified SSO.
        rows = self.store.rows('SELECT * FROM users WHERE id IN (?,?)', (owner_id, actor_id))
        owner = next((u for u in rows if u['id'] == owner_id and u['kind'] == 'google'), None)
        actor = next((u for u in rows if u['id'] == actor_id and u['kind'] == 'slack'), None)
        if not owner or not actor or not actor['profile_eligible'] or actor['profile_conflict'] or not actor['email']:
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(actor['profile_checked_at'])).total_seconds()
        except ValueError:
            return False
        return 0 <= age < 3600 and owner['email'] == actor['email']

    def audit_in(self, conn, actor, secret, action, run_id=''):
        conn.execute('INSERT INTO credential_audit(actor_id,secret_id,action,run_id,created_at) VALUES(?,?,?,?,?)',
                     (actor, secret, action, run_id, now()))

    def metadata(self, row, user_id, admin):
        return {**{k: row[k] for k in ('id','provider','label','scope','created_at')},
                'can_manage': admin if row['scope']=='organization' else row['owner_id']==user_id}

    def list_secrets(self, user_id, admin=False, root_id=''):
        rows = self.store.rows("SELECT * FROM provider_secrets WHERE revoked_at='' AND (scope='organization' OR owner_id=?) ORDER BY created_at DESC", (user_id,))
        return [self.metadata(r, user_id, admin) for r in rows
                if r['scope'] != 'session' or not root_id or r['root_id'] == root_id]

    def insert_secret(self, conn, body, user_id, admin, root_id=''):
        if body.scope == 'organization' and not admin:
            raise HTTPException(403, 'Only an administrator can save organization keys.')
        if body.scope != 'organization' and not user_id.startswith('google:') and not self.security.local_preview():
            raise HTTPException(403, 'Use Google sign-in to save a personal or session key.')
        if body.scope == 'session' and not root_id:
            raise HTTPException(422, 'Session keys must be provided through a session request.')
        value = body.value.get_secret_value().strip()
        if not 8 <= len(value) <= 4096 or not re.fullmatch(r'[A-Za-z0-9_./:=+~-]+', value):
            raise HTTPException(422, 'Enter a valid API key without spaces or line breaks.')
        existing = conn.execute('SELECT * FROM provider_secrets WHERE owner_id=? AND client_id=?', (user_id, body.client_id)).fetchone()
        if existing:
            if (existing['revoked_at'] or any(existing[k] != v for k,v in {'provider':body.provider,'scope':body.scope,'label':body.label.strip(),'root_id':root_id}.items())
                    or not hmac.compare_digest(self.security.decrypt(existing['encrypted']),value)):
                raise HTTPException(409, 'This save was already used for a different key. Refresh the form.')
            return existing['id']
        secret_id = uuid4().hex
        conn.execute('INSERT INTO provider_secrets(id,provider,label,scope,owner_id,root_id,encrypted,created_at,client_id) VALUES(?,?,?,?,?,?,?,?,?)',
                     (secret_id, body.provider, body.label.strip(), body.scope, user_id, root_id,
                      self.security.encrypt(value), now(), body.client_id))
        self.audit_in(conn, user_id, secret_id, 'saved ' + body.scope, root_id)
        return secret_id

    def permitted(self, secret, run, actor_id):
        if secret['revoked_at']:
            return False
        if secret['scope'] == 'organization':
            return True
        return (self.same_requester(secret['owner_id'], actor_id)
                and (secret['scope'] != 'session' or secret['root_id'] == self.root(run)))

    def tools(self, run):
        if not self.settings.temporal_enabled or not run['chat_enabled']:
            return []
        return [{'name':name, 'description':description, 'inputSchema':schema.model_json_schema()}
                for name,(schema,description) in TOOLS.items()]

    def ready(self, request):
        if request['status'] == 'declined':
            return {'status':'declined', 'request_id':request['id'], 'provider':request['provider'],
                    'instructions':'The user declined this key request. Continue without it, explain any limitation, and do not request it again unless the user asks.'}
        suffix = '' if request['provider']=='anthropic' else '/v1'
        return {'status':request['status'], 'request_id':request['id'], 'provider':request['provider'],
                'instructions': 'Use credentials_http_request with this request_id. For Python SDKs, base_url=os.environ["MOYAI_CREDENTIAL_PROXY_URL"] + "/' + request['id'] + suffix + '", api_key=os.environ["WORKSPACE_RUN_TOKEN"]. Set max_retries=0 and stream=False. The endpoint is rebuilt each turn; do not hard-code it. No raw provider key is available. Provider charges are separate from Moyai gateway spend.'}

    def request(self, run, args):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            prior = conn.execute('SELECT * FROM credential_requests WHERE run_id=? AND message_id=? AND request_key=?',
                                 (run['id'], run['active_message_id'], args.request_key)).fetchone()
            if prior and (prior['provider'] != args.provider or prior['reason'] != args.reason):
                raise ValueError('Use the original arguments when retrying this credential request.')
            if not prior:
                prior = conn.execute("SELECT * FROM credential_requests WHERE run_id=? AND message_id=? AND status='pending'",
                                     (run['id'],run['active_message_id'])).fetchone()
            if prior:
                row = dict(prior)
                if row['status'] == 'provided':
                    secret = conn.execute('SELECT * FROM provider_secrets WHERE id=?',(row['secret_id'],)).fetchone()
                    if not secret or not self.permitted(secret,run,run['active_user_id']):
                        row.update(status='pending',secret_id='',resolved_at='')
                        conn.execute("UPDATE credential_requests SET status='pending',secret_id='',resolved_at='' WHERE id=?",(row['id'],))
                        self.audit_in(conn,run['active_user_id'],'','requested again',run['id'])
            else:
                actor = run['active_user_id']
                if not actor or not run['active_message_id']:
                    raise ValueError('Start an authenticated chat session before requesting a key.')
                # Reuse a single unambiguous personal/session key before an org
                # default. Ambiguous choices always go through the secure form.
                candidates = [dict(s) for s in conn.execute("SELECT * FROM provider_secrets WHERE provider=? AND revoked_at=''", (args.provider,))
                              if self.permitted(s, run, actor)]
                private = [s for s in candidates if s['scope'] != 'organization']
                choices = private or [s for s in candidates if s['scope'] == 'organization']
                secret = choices[0] if len(choices) == 1 else None
                row = {'id':uuid4().hex, 'run_id':run['id'], 'message_id':run['active_message_id'], 'actor_id':actor,
                       'provider':args.provider,'reason':args.reason,'request_key':args.request_key,
                       'status':'provided' if secret else 'pending','secret_id':secret['id'] if secret else '',
                       'created_at':now(),'resolved_at':now() if secret else ''}
                conn.execute('INSERT INTO credential_requests VALUES(:id,:run_id,:message_id,:actor_id,:provider,:reason,:request_key,:status,:secret_id,:created_at,:resolved_at)',row)
                self.audit_in(conn, actor, row['secret_id'], 'reused' if secret else 'requested', run['id'])
        self.store.event(run['id'], 'credential', 'Provider key ready' if row['status'] == 'provided' else 'Provider key requested', {'request_id':row['id']})
        if row['status'] == 'pending':
            return {'status':'pending','request_id':row['id'],'moyai_wait_credential':row['id'],
                    'message':'Pause for the secure web form. Never ask the user to paste a key in chat.',
                    'session_url':self.settings.public_url.rstrip('/') + '/#run=' + run['id']}
        return self.ready(row)

    def resolution(self, run_id, request_id):
        row = self.row(request_id)
        run = self.store.run(run_id)
        if row['run_id'] != run_id or row['message_id'] != run['active_message_id']:
            raise ValueError('Credential request does not belong to this turn.')
        return self.ready(row)

    def pending(self, run, user_id, admin):
        if run['status'] not in ACTIVE:
            return []
        rows = self.store.rows("SELECT * FROM credential_requests WHERE run_id=? AND message_id=? AND status='pending'", (run['id'],run['active_message_id']))
        return [{**{k:r[k] for k in ('id','provider','reason','status')},
                 'can_personal':self.same_requester(user_id,r['actor_id']), 'can_organization':admin,
                 'setup_url':PROVIDERS[r['provider']]['setup'], 'provider_name':PROVIDERS[r['provider']]['name']}
                for r in rows]

    def resolve(self, request_id, body, user_id, admin):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM credential_requests WHERE id=?',(request_id,)).fetchone()
            if not row:
                raise HTTPException(404,'Credential request not found.')
            run = self.store.run(row['run_id'])
            own = self.same_requester(user_id,row['actor_id'])
            if not own and not admin:
                raise HTTPException(403,'Only the requester or an administrator can resolve this request.')
            if row['status'] != 'pending':
                return row['run_id']  # A lost acknowledgement never saves twice.
            if run['status'] not in ACTIVE or run['active_message_id'] != row['message_id']:
                raise HTTPException(409,'This session is no longer waiting for that key.')
            secret_id = ''
            if body.decision == 'provide':
                if body.secret_id:
                    secret = conn.execute("SELECT * FROM provider_secrets WHERE id=? AND revoked_at=''",(body.secret_id,)).fetchone()
                    if (not secret or secret['provider'] != row['provider'] or
                        (secret['scope'] != 'organization' and (secret['owner_id'] != user_id or not own)) or
                        not self.permitted(secret,run,row['actor_id'])):
                        raise HTTPException(403,'That key is not available to this requester and session.')
                    secret_id = secret['id']
                else:
                    if body.scope != 'organization' and not own:
                        raise HTTPException(403,'Personal keys can only be supplied for your own requests.')
                    data = SaveSecret(provider=row['provider'],scope=body.scope,label=body.label.strip() or PROVIDERS[row['provider']]['name'] + ' key',
                                      value=body.value,client_id='request-' + row['id'])
                    secret_id = self.insert_secret(conn,data,user_id,admin,self.root(run) if body.scope == 'session' else '')
            status = 'provided' if secret_id else 'declined'
            conn.execute('UPDATE credential_requests SET status=?,secret_id=?,resolved_at=? WHERE id=?',(status,secret_id,now(),request_id))
            self.audit_in(conn,user_id,secret_id,status,row['run_id'])
        self.store.event(row['run_id'],'credential','Provider key supplied' if secret_id else 'Provider key request declined',{'request_id':request_id})
        return row['run_id']

    async def invoke(self, run, args):
        request = self.row(args.request_id)
        # A handle from an earlier turn is usable only by that same actor (or
        # with an org key). Children request their own scoped handle.
        secret_rows = self.store.rows('SELECT * FROM provider_secrets WHERE id=?',(request['secret_id'],))
        secret = secret_rows[0] if secret_rows else None
        if (request['run_id'] != run['id'] or request['status'] != 'provided' or not secret
                or not self.permitted(secret,run,run['active_user_id'])):
            raise HTTPException(403,'This provider key is unavailable. Request it again through the secure form.')
        provider = PROVIDERS[secret['provider']]
        paths = {'GET':{'/models'},'POST':{'/messages'} if secret['provider']=='anthropic' else {'/chat/completions','/completions','/embeddings'}}
        if args.path not in paths[args.method] or (args.method == 'GET' and args.body):
            raise HTTPException(422,'Only supported inference and model-list routes are allowed.')
        if args.body.get('stream') or args.body.get('background'):
            raise HTTPException(422,'Use non-streaming, foreground requests through the credential proxy.')
        if self.slots.locked():
            raise HTTPException(429,'Provider request capacity is busy. No provider call was made.')
        async with self.slots:
            value = self.security.decrypt(secret['encrypted'])
            headers = {'x-api-key':value,'anthropic-version':'2023-06-01'} if secret['provider']=='anthropic' else {'Authorization':'Bearer '+value}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300,connect=30),follow_redirects=False) as client:
                    async with client.stream(args.method,provider['base']+args.path,headers=headers,
                                             json=args.body if args.method=='POST' else None) as response:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw)>8*1024*1024:
                                raise HTTPException(502,'Provider response exceeded the size limit.')
                        status = response.status_code
            except httpx.HTTPError:
                raise HTTPException(502,'Provider request could not be confirmed. Do not retry a billed request automatically.') from None
            if not 200 <= status < 300:
                # Provider errors sometimes echo authentication; never forward them.
                return status if 400<=status<600 else 502, {'error':{'message':f'{provider["name"]} rejected the request ({status}). Check the key, quota, model and request.','type':'provider_error'}}
            try:
                # Decode JSON escapes before scrubbing both values and object
                # keys. Provider responses may echo a token as unicode escapes.
                decoded = json.loads(bytes(raw))
                result = json.loads(json.dumps(decoded).replace(value,'[credential redacted]'))
            except (ValueError,UnicodeDecodeError):
                raise HTTPException(502,'Provider returned an invalid JSON response.') from None
            return status, result

    def routes(self):
        router = APIRouter()

        def actor(request, mutation=False):
            self.security.require(request,mutation=mutation)
            return self.store.identity(self.security.session_info(request)), self.security.role(request)=='admin'

        @router.get('/api/credentials')
        async def list_keys(request: Request, run_id: str=''):
            user,admin = actor(request)
            run = self.store.run(run_id) if re.fullmatch(r'[0-9a-f]{32}',run_id) else None
            return {'providers':[{'id':k,'name':v['name'],'setup_url':v['setup']} for k,v in PROVIDERS.items()],
                    'secrets':self.list_secrets(user,admin,self.root(run) if run else '')}

        @router.post('/api/credentials/secrets',status_code=201)
        async def save_key(body: SaveSecret, request: Request):
            user,admin = actor(request,True)
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                identity = self.insert_secret(conn,body,user,admin)
            return {'id':identity,'saved':True}

        @router.delete('/api/credentials/secrets/{secret_id}')
        async def revoke_key(secret_id: str, request: Request):
            user,admin = actor(request,True)
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM provider_secrets WHERE id=?',(secret_id,)).fetchone()
                if not row or not (row['owner_id']==user or (row['scope']=='organization' and admin)):
                    raise HTTPException(404,'Key not found.')
                if row['scope']=='organization' and not admin:
                    raise HTTPException(403,'Only an administrator can revoke organization keys.')
                conn.execute("UPDATE provider_secrets SET encrypted='',revoked_at=? WHERE id=?",(now(),secret_id))
                self.audit_in(conn,user,secret_id,'revoked')
            return {'revoked':True}

        @router.post('/api/credentials/requests/{request_id}')
        async def resolve_request(request_id: str, body: Resolve, request: Request):
            user,admin = actor(request,True)
            run_id = self.resolve(request_id,body,user,admin)
            # Wake is durable before acknowledgement, even if Temporal is down.
            self.manager.submit(self.store.run(run_id))
            await self.checkpoints.flush()
            return {'saved':True}

        return router
