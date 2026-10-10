"""Encrypted task credentials, their authorization, and durable access requests."""
import asyncio
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

from .database import Connection, DatabaseRow
from .db import now
from .identities import PROFILE_MAX_AGE_SECONDS


# Inference keys retain fixed origins/routes and never enter the sandbox.
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
    'generic': {'name': 'Other service', 'setup': ''},
}
Provider = Literal['fireworks', 'openai', 'anthropic', 'together', 'groq', 'generic']
Scope = Literal['session', 'personal', 'organization']
Lifetime = Literal['session', 'persistent']
Format = Literal['env', 'file']
ACCESSIBLE = {'running', 'awaiting_approval', 'saving', 'waiting_credential', 'queued',
              'provisioning', 'reconnecting', 'waiting_environment', 'waiting_children', 'idle', 'completed'}
ENV_NAME = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}$')
RESERVED_ENV = {'PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'ENV', 'BASH_ENV', 'IFS',
                'PWD', 'OLDPWD', 'TMPDIR', 'NODE_OPTIONS', 'WORKSPACE_RUN_TOKEN',
                'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'MOYAI_CREDENTIAL_PROXY_URL'}


def environment_name(value):
    if (not ENV_NAME.fullmatch(value) or value in RESERVED_ENV
            or value.startswith(('LD_', 'DYLD_', 'PYTHON', 'HERMES_', 'WORKSPACE_', 'MOYAI_'))):
        raise ValueError('Choose a credential environment variable, not a runtime setting.')
    return value


def expiry(value):
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError('Expiry must include a timezone.') from None


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Capability(Arguments):
    provider: Provider
    name: str = Field(default='', max_length=80)
    format: Format = 'env'
    env_var: str = Field(default='', max_length=128)

    @model_validator(mode='after')
    def capability(self):
        self.name = self.name.strip().lower()
        if self.provider == 'generic':
            if not self.name or not re.fullmatch(r'[a-z0-9][a-z0-9 _./:-]{0,79}', self.name):
                raise ValueError('Provide a stable capability name.')
            if self.format == 'file':
                environment_name(self.env_var)
            elif self.env_var:
                raise ValueError('Environment credentials carry variable names in their JSON value.')
        elif self.name or self.format != 'env' or self.env_var:
            raise ValueError('Provider keys use the provider inference connection.')
        return self


class CredentialInput(Arguments):
    name: str = Field(max_length=128, description='Exact environment variable consumed by the authorized command; never a secret value.')
    label: str = Field(min_length=1, max_length=80, description='Human-readable field label, e.g. Access token or AWS access key ID.')
    secret: bool = True
    required: bool = True

    @model_validator(mode='after')
    def valid_name(self):
        environment_name(self.name)
        return self


class CredentialSourceCheck(Arguments):
    secret_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    revision: int = Field(ge=1)
    outcome: Literal['not_found', 'invalid', 'expired', 'permission', 'unavailable']


class CredentialRequest(Capability):
    reason: str = Field(min_length=3, max_length=1000)
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    setup_url: str = Field(default='', max_length=2048, description='Verified service documentation or console URL for obtaining this access. HTTPS only; no secrets. Inference provider setup links remain fixed.')
    setup_instructions: str = Field(default='', max_length=3000, description='Concise task-specific steps to obtain the requested access, including the relevant account/role or administrator action. No secret values.')
    input_fields: list[CredentialInput] = Field(default_factory=list, max_length=32, description='For generic env access, declare the exact required inputs so users never need to write JSON. Single token: [{name: SERVICE_TOKEN, label: Access token}]. AWS: separate AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, and region fields. Mark optional fields required=false. Do not include values.')
    secret_id: str = Field(default='', pattern=r'^([0-9a-f]{32})?$', description='Optional authorized saved credential ID from credentials_list. Select only a matching connection appropriate for the task.')
    source_checks: list[CredentialSourceCheck] = Field(default_factory=list, max_length=32, description='Observed unsuccessful lookup or verification outcomes for available credential_sources, using their secret IDs and revisions. First use each source through credentials_run in this turn. Never invent a check or include secret values. Omit when a source supplies working access; continue using that access instead.')

    @model_validator(mode='after')
    def setup_guidance(self):
        if self.input_fields and (self.provider != 'generic' or self.format != 'env'):
            raise ValueError('Labeled inputs are only supported for generic environment access.')
        if len({field.name for field in self.input_fields}) != len(self.input_fields):
            raise ValueError('Credential input names must be unique.')
        self.setup_url = self.setup_url.strip()
        self.setup_instructions = self.setup_instructions.strip()
        if self.setup_url:
            url = HttpUrl(self.setup_url)
            if (not self.setup_url.startswith('https://') or url.username or url.password
                    or re.search(r'[\s\\\x00-\x1f\x7f]', self.setup_url)):
                raise ValueError('Use an absolute HTTPS setup URL without login details.')
            self.setup_url = str(url)
        return self


class ListCredentials(Arguments):
    provider: Provider | None = None
    name: str = Field(default='', max_length=80)


class ResolveExternal(Arguments):
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    generation: int = Field(ge=0)
    source: Literal['browser_session', 'existing_credentials']


class Materialize(Arguments):
    request_ids: list[str] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def handles(self):
        if (len(set(self.request_ids)) != len(self.request_ids)
                or any(not re.fullmatch(r'[0-9a-f]{32}', item) for item in self.request_ids)):
            raise ValueError('Use distinct credential request IDs.')
        return self


class RunCredential(Materialize):
    command: str = Field(min_length=1, max_length=16000)
    timeout: int = Field(default=120, ge=1, le=600)


class ReportFailure(Arguments):
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    revision: int = Field(ge=1)
    failure: Literal['expired', 'invalid', 'permission', 'unknown']


class Invoke(Arguments):
    request_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    method: Literal['GET', 'POST'] = 'POST'
    path: str = Field(max_length=80)
    body: dict = Field(default_factory=dict)


class SaveSecret(Capability):
    label: str = Field(min_length=1, max_length=80)
    scope: Scope
    lifetime: Lifetime | None = None
    root_id: str = Field(default='', pattern=r'^([0-9a-f]{32})?$')
    expires_at: str = Field(default='', max_length=80)
    value: SecretStr
    client_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,80}$')

    @model_validator(mode='after')
    def choices(self):
        if self.provider == 'generic' and self.scope == 'session':
            raise ValueError('Choose sharing and session use separately for generic access.')
        if self.scope == 'session':
            if self.lifetime not in (None, 'session'):
                raise ValueError('Session scope requires session use.')
            self.scope, self.lifetime = 'personal', 'session'
        if self.lifetime is None:
            if self.provider == 'generic':
                raise ValueError('Choose this session or future sessions.')
            self.lifetime = 'persistent'  # Existing inference-key clients.
        self.expires_at = expiry(self.expires_at)
        if not self.label.strip():
            raise ValueError('Provide a credential name.')
        return self


class Resolve(Arguments):
    decision: Literal['provide', 'decline'] = 'provide'
    generation: int | None = Field(default=None, ge=0)
    secret_id: str = Field(default='', pattern=r'^([0-9a-f]{32})?$')
    scope: Scope | None = None
    lifetime: Lifetime | None = None
    label: str = Field(default='', max_length=80)
    expires_at: str = Field(default='', max_length=80)
    value: SecretStr = SecretStr('')

    @model_validator(mode='after')
    def one_source(self):
        if self.decision == 'provide' and bool(self.secret_id) == bool(self.value.get_secret_value()):
            raise ValueError('Choose saved access or supply new credentials.')
        if self.decision == 'provide' and not self.secret_id and self.scope is None:
            raise ValueError('Choose who can use these credentials before saving them.')
        return self


class UpdateSecret(Arguments):
    revision: int = Field(ge=1)
    label: str | None = Field(default=None, min_length=1, max_length=80)
    scope: Literal['personal', 'organization'] | None = None
    lifetime: Lifetime | None = None
    root_id: str | None = Field(default=None, pattern=r'^([0-9a-f]{32})?$')
    expires_at: str | None = Field(default=None, max_length=80)
    value: SecretStr | None = None

    @model_validator(mode='after')
    def updates(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError('Omit fields that should stay unchanged.')
        if self.label is not None and not self.label.strip():
            raise ValueError('Provide a credential name.')
        if self.expires_at is not None:
            self.expires_at = expiry(self.expires_at)
        return self


TOOLS = {
    'credentials_list': (ListCredentials, 'Check existing personal and organization access before asking for access. Returns authorized credential metadata, pending request IDs/generations, and available credential_sources including Shared vault access even with a provider filter; never secret values. Inspect an available Shared vault before asking for another provider key.'),
    'credentials_resolve': (ResolveExternal, 'Close one pending request after verifying access through the current browser session or existing authorized credentials, including 1Password. First verify the actual access; a user saying signed in is not verification. Use the exact request_id and generation from credentials_list. This records that the secret form is no longer needed; it does not store, grant, or transfer credentials. Continue using the verified access path. Never resolve unrelated requests or include secret values.'),
    'credentials_request': (CredentialRequest, 'Obtain access needed to complete the task. Check credentials_list first and reuse authorized personal or organization access; secret_id can select a matching saved connection. A lookup_required result means check the returned 1Password credential_sources through credentials_run before asking the user. If lookup or verification fails, retry with source_checks reporting the observed outcomes; working vault access needs no new key or form. Use provider=generic and a stable capability name for any service; format=env accepts a secure environment-variable map. Always declare input_fields with exact environment names and human-readable labels so the form renders a masked Access token box or separate AWS fields instead of asking users to write JSON; format=file accepts a secure file and requires its environment variable name. Explain the needed capability. Include setup_instructions explaining how to obtain access and a verified official service setup_url when known. Use the actual account access method; do not assume a new long-lived key is needed or invent URLs. Never include secrets in setup guidance or ask for credentials in chat. Only a pending result checkpoints and pauses for a secure form. The user chooses personal or organization sharing and this session or future sessions independently. Use this tool alone in its round. Organization sharing is admin-managed.'),
    'credentials_run': (RunCredential, 'Run a foreground sandbox command with approved generic access. Use request_ids from credentials_request; environment values and credential files exist only for this command. Output is bounded and redacted. Never print credentials or copy them to ordinary files. A failed command is not replayed automatically. If authentication fails, report the returned credential revision through credentials_report_failure; distinguish missing permissions from invalid or expired authentication.'),
    'credentials_report_failure': (ReportFailure, 'Report an observed access failure with the request ID and revision returned by credentials_run. Expired or invalid authentication tries alternative saved access, then requires checking available credential_sources before opening a replacement form. No failed operation is replayed. Missing permissions reopen only this request so the user can grant access; they do not invalidate the shared credential. Never guess expiry from a generic command error and never repeat potentially completed writes automatically.'),
    'credentials_http_request': (Invoke, 'Use an authorized inference-key request for a non-streaming inference or model-list API call. The server supplies authentication to fixed provider origins and paths. Responses contain no key. Provider usage is billed to that separate key, outside Moyai gateway spend.'),
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
                CREATE TABLE IF NOT EXISTS credential_source_uses (
                    run_id TEXT NOT NULL REFERENCES runs(id), message_id INTEGER NOT NULL,
                    secret_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    PRIMARY KEY(run_id,message_id,secret_id,revision)
                );
            ''')
            for table, fields in {
                'provider_secrets': {'name': "TEXT NOT NULL DEFAULT ''", 'format': "TEXT NOT NULL DEFAULT 'env'",
                    'env_var': "TEXT NOT NULL DEFAULT ''", 'lifetime': "TEXT NOT NULL DEFAULT 'persistent'",
                    'expires_at': "TEXT NOT NULL DEFAULT ''", 'invalid_reason': "TEXT NOT NULL DEFAULT ''",
                    'revision': 'INTEGER NOT NULL DEFAULT 1'},
                'credential_requests': {'name': "TEXT NOT NULL DEFAULT ''", 'format': "TEXT NOT NULL DEFAULT 'env'",
                    'env_var': "TEXT NOT NULL DEFAULT ''", 'generation': 'INTEGER NOT NULL DEFAULT 0',
                    'failure': "TEXT NOT NULL DEFAULT ''", 'revision': 'INTEGER NOT NULL DEFAULT 1',
                    'secret_revision': 'INTEGER NOT NULL DEFAULT 1', 'setup_url': "TEXT NOT NULL DEFAULT ''",
                    'setup_instructions': "TEXT NOT NULL DEFAULT ''", 'input_fields': "TEXT NOT NULL DEFAULT '[]'",
                    'preferred_scope': "TEXT NOT NULL DEFAULT ''", 'scope_revision': 'INTEGER NOT NULL DEFAULT 0',
                    'resolution_pending': 'INTEGER NOT NULL DEFAULT 0', 'resolution_error': "TEXT NOT NULL DEFAULT ''"},
            }.items():
                columns = conn.column_names(table)
                for field, declaration in fields.items():
                    if field not in columns:
                        conn.execute(f'ALTER TABLE {table} ADD COLUMN {field} {declaration}')
            conn.execute("UPDATE provider_secrets SET scope='personal',lifetime='session' WHERE scope='session'")

    def root(self, run):
        return self.store.root_id(run['id'])

    def row(self, request_id):
        rows = self.store.rows('SELECT * FROM credential_requests WHERE id=?', (request_id,))
        if not rows:
            raise ValueError('Credential request not found.')
        return rows[0]

    def same_requester(self, owner_id, actor_id):
        if owner_id == actor_id:
            return bool(owner_id)
        # Accounting links are not authentication. Require a fresh, eligible
        # Slack profile matching an independently verified Google identity.
        rows = self.store.rows('SELECT * FROM users WHERE id IN (?,?)', (owner_id, actor_id))
        owner = next((u for u in rows if u['id'] == owner_id and u['kind'] in {'google', 'cloudflare'}), None)
        actor = next((u for u in rows if u['id'] == actor_id and u['kind'] == 'slack'), None)
        if not owner or not actor or not actor['profile_eligible'] or actor['profile_conflict'] or not actor['email']:
            return False
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(actor['profile_checked_at'])).total_seconds()
        except (ValueError, TypeError):
            return False
        return 0 <= age < PROFILE_MAX_AGE_SECONDS and owner['email'] == actor['email']

    def audit_in(self, conn, actor, secret, action, run_id=''):
        conn.execute('INSERT INTO credential_audit(actor_id,secret_id,action,run_id,created_at) VALUES(?,?,?,?,?)',
                     (actor, secret, action, run_id, now()))

    def status(self, secret):
        if secret['revoked_at']:
            return 'revoked'
        if secret['invalid_reason']:
            return secret['invalid_reason']
        if secret['expires_at'] and secret['expires_at'] <= now():
            return 'expired'
        return 'active'

    def metadata(self, row, user_id, admin):
        fields = ('id', 'provider', 'label', 'scope', 'created_at', 'name', 'format', 'env_var',
                  'lifetime', 'root_id', 'expires_at', 'invalid_reason', 'revision')
        return {**{k: row[k] for k in fields}, 'status': self.status(row),
                'can_manage': admin if row['scope'] == 'organization' else row['owner_id'] == user_id}

    def permitted(self, secret, run, actor_id):
        if not actor_id or secret['revoked_at']:
            return False
        # Lifetime bounds every source of reachability, including shared access.
        if secret['lifetime'] == 'session' and secret['root_id'] != self.root(run):
            return False
        return secret['scope'] == 'organization' or self.same_requester(secret['owner_id'], actor_id)

    def list_secrets(self, user_id, admin=False, root_id=''):
        rows = self.store.rows("SELECT * FROM provider_secrets WHERE revoked_at='' ORDER BY created_at DESC")
        return [self.metadata(row, user_id, admin) for row in rows
                if ((row['scope'] == 'organization' or self.same_requester(row['owner_id'], user_id))
                    and (not root_id or row['lifetime'] != 'session' or row['root_id'] == root_id))]

    def inventory(self, run, args):
        rows = self.store.rows("SELECT * FROM provider_secrets WHERE revoked_at='' ORDER BY created_at DESC")
        authorized = [row for row in rows if self.permitted(row, run, run['active_user_id'])]
        with self.store.connect() as conn:
            pending = self.pending_rows(conn, run['id'])
        return {'credentials': [self.metadata(row, run['active_user_id'], False) for row in authorized
                if (not args.provider or row['provider'] == args.provider)
                and (not args.name or row['name'] == args.name.strip().lower())],
                'credential_sources': [self.metadata(row, run['active_user_id'], False) for row in authorized
                    if row['provider'] == 'generic' and row['name'] == '1password-shared' and self.status(row) == 'active'],
                'pending_requests': [{'request_id': row['id'], **{key: row[key] for key in ('generation', 'provider', 'name', 'reason')}}
                    for row in pending
                    if (self.same_requester(row['actor_id'], run['active_user_id'])
                        or self.same_requester(run['active_user_id'], row['actor_id']))
                and (not args.provider or row['provider'] == args.provider)
                and (not args.name or row['name'] == args.name.strip().lower())]}

    def validated_value(self, body):
        value = body.value.get_secret_value()
        if body.provider != 'generic':
            value = value.strip()
            if not 8 <= len(value) <= 4096 or not re.fullmatch(r'[A-Za-z0-9_./:=+~-]+', value):
                raise HTTPException(422, 'Enter a valid API key without spaces or line breaks.')
        else:
            try:
                encoded = value.encode()
            except UnicodeError:
                raise HTTPException(422, 'Credentials must be valid UTF-8 text.') from None
            if not value or len(encoded) > 128 * 1024 or '\0' in value:
                raise HTTPException(422, 'Provide credentials up to 128 KiB without null characters.')
            if body.format == 'env':
                try:
                    pairs = json.loads(value)
                    if not isinstance(pairs, dict) or not 1 <= len(pairs) <= 32:
                        raise ValueError()
                    for key, item in pairs.items():
                        environment_name(key)
                        if not isinstance(item, str) or not item or len(item.encode()) > 32768 or '\0' in item:
                            raise ValueError()
                except (ValueError, TypeError, UnicodeError):
                    raise HTTPException(422, 'Use a JSON object of credential environment names and nonempty string values, without runtime settings.') from None
        return value

    def insert_secret(self, conn, body, user_id, admin, root_id=''):
        if body.scope == 'organization' and not admin:
            raise HTTPException(403, 'Only an administrator can save organization credentials.')
        if body.scope != 'organization' and not user_id.startswith(('google:', 'cloudflare:')) and not self.security.local_preview():
            raise HTTPException(403, 'Use Google sign-in to save personal credentials.')
        root_id = root_id or body.root_id if body.lifetime == 'session' else ''
        if body.lifetime == 'session' and not root_id:
            raise HTTPException(422, 'Session credentials must be associated with a session.')
        if root_id and not conn.execute("SELECT 1 FROM runs WHERE id=? AND deleted_at=''", (root_id,)).fetchone():
            raise HTTPException(404, 'Session not found.')
        value = self.validated_value(body)
        fields = {key: getattr(body, key) for key in ('provider', 'scope', 'lifetime', 'name', 'format', 'env_var', 'expires_at')}
        fields.update(label=body.label.strip(), root_id=root_id)
        existing = conn.execute('SELECT * FROM provider_secrets WHERE owner_id=? AND client_id=?', (user_id, body.client_id)).fetchone()
        if existing:
            if (existing['revoked_at'] or any(existing[k] != v for k, v in fields.items())
                    or not hmac.compare_digest(self.security.decrypt(existing['encrypted']).encode(), value.encode())):
                raise HTTPException(409, 'This save was already used for different credentials. Refresh the form.')
            return existing['id']
        secret_id = uuid4().hex
        fields.update(id=secret_id, owner_id=user_id, encrypted=self.security.encrypt(value), created_at=now(), client_id=body.client_id)
        columns = ','.join(fields)
        conn.execute(f'INSERT INTO provider_secrets({columns}) VALUES({",".join("?" for key in fields)})', tuple(fields.values()))
        self.audit_in(conn, user_id, secret_id, 'saved ' + body.scope + ' ' + body.lifetime, root_id)
        return secret_id

    def tools(self, run):
        if not self.settings.temporal_enabled or not run['chat_enabled']:
            return []
        return [{'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
                 'annotations': {'readOnlyHint': name == 'credentials_list', 'idempotentHint': name == 'credentials_list'}}
                for name, (schema, description) in TOOLS.items()]

    def setup(self, row):
        return {'setup_url': PROVIDERS[row['provider']]['setup'] or row['setup_url'],
                'setup_instructions': row['setup_instructions']}

    def pending_result(self, row):
        return {'status': 'pending', 'request_id': row['id'], 'moyai_wait_credential': row['id'],
                'failure': row['failure'], 'generation': row['generation'], **self.setup(row),
                'message': 'Access is needed. Pause for the secure form; never ask for credentials in chat.',
                'session_url': self.settings.public_url.rstrip('/') + '/#run=' + row['run_id']}

    def ready(self, request):
        if request['status'] == 'pending':
            return self.pending_result(request)
        if request['status'] == 'satisfied':
            return {'status': 'satisfied', 'request_id': request['id'], 'generation': request['generation'],
                    'provider': request['provider'],
                    'instructions': 'This request was closed after access was verified through another path. No credential was stored or granted. Continue using that access, rechecking it when needed. If it is no longer usable, create a new request with a new request_key.'}
        if request['status'] == 'declined':
            return {'status': 'declined', 'request_id': request['id'], 'provider': request['provider'],
                    'instructions': 'The user declined this access request. Continue without it, explain any limitation, and do not request it again unless the user asks.'}
        result = {'status': request['status'], 'request_id': request['id'], 'provider': request['provider'], **self.setup(request)}
        if request['provider'] == 'generic':
            return {**result, 'name': request['name'], 'format': request['format'], 'env_var': request['env_var'],
                    'instructions': 'Use credentials_run with this request_id to verify access and continue the task. Credential values are supplied only to that command. Report confirmed authentication failures with the returned revision; never replay potentially completed writes automatically.'}
        suffix = '' if request['provider'] == 'anthropic' else '/v1'
        return {**result, 'instructions': 'Use credentials_http_request with this request_id. For Python SDKs, base_url=os.environ["MOYAI_CREDENTIAL_PROXY_URL"] + "/' + request['id'] + suffix + '", api_key=os.environ["WORKSPACE_RUN_TOKEN"]. Set max_retries=0 and stream=False. The endpoint is rebuilt each turn; do not hard-code it. No raw provider key is available. Provider charges are separate from Moyai gateway spend.'}

    def pending_rows(self, conn: Connection, run_id: str, *, include_children: bool = False) -> list[DatabaseRow]:
        # Pending access belongs to the session and requester, not its latest turn.
        return conn.execute(f"""SELECT q.* FROM credential_requests q JOIN runs r ON r.id=q.run_id
            WHERE (r.id=? OR (?=1 AND r.id IN (SELECT run_id FROM run_ancestry WHERE ancestor_id=?))) AND r.deleted_at=''
            AND r.status IN ({','.join('?' for _ in ACCESSIBLE)}) AND q.status='pending'
            ORDER BY q.created_at,q.id""", (run_id, include_children, run_id, *ACCESSIBLE)).fetchall()

    @staticmethod
    def request_label(row):
        fields = json.loads(row['input_fields'])
        if len(fields) == 1:
            return fields[0]['name']
        return row['name'] or PROVIDERS[row['provider']]['name'] + ' API key'

    def select_scope_in(self, conn, request_id, generation, scope, actor_id):
        """Record a sharing preference, never a grant. Caller owns the transaction."""
        if scope not in {'session', 'personal', 'organization'}:
            raise HTTPException(422, 'Choose Organization, Personal, or Session only.')
        row = conn.execute('SELECT * FROM credential_requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Credential request not found.')
        run = conn.execute('SELECT * FROM runs WHERE id=?', (row['run_id'],)).fetchone()
        if not run or run['deleted_at']:
            raise HTTPException(404, 'Session not found.')
        if (row['status'] != 'pending' or row['generation'] != generation
                or run['status'] not in ACCESSIBLE):
            raise HTTPException(409, 'This access request changed. Open the current session.')
        if not (self.same_requester(actor_id, row['actor_id'])
                or self.same_requester(row['actor_id'], actor_id)):
            raise HTTPException(403, 'Only the requester can choose how to share this access.')
        if row['preferred_scope'] != scope:
            conn.execute('UPDATE credential_requests SET preferred_scope=?,scope_revision=scope_revision+1 WHERE id=?',
                         (scope, request_id))
        return dict(conn.execute('SELECT * FROM credential_requests WHERE id=?', (request_id,)).fetchone())

    def reopen_in(self, conn, row, failure, run=None):
        if row['status'] not in {'provided', 'pending'}:
            return dict(row)
        if row['status'] == 'pending' and (not run or row['message_id'] == run['active_message_id']):
            return dict(row)
        if run and row['message_id'] != run['active_message_id']:
            current = conn.execute('SELECT * FROM credential_requests WHERE run_id=? AND message_id=? AND request_key=?',
                                   (run['id'], run['active_message_id'], row['request_key'])).fetchone()
            if current:
                if self.matching(current, row):
                    current_secret = conn.execute('SELECT * FROM provider_secrets WHERE id=?', (current['secret_id'],)).fetchone()
                    if (current['status'] != 'provided' or (current_secret and self.permitted(current_secret, run, run['active_user_id'])
                            and self.status(current_secret) == 'active')):
                        return dict(current)  # A newer grant/decline wins over this older failure.
                    return self.reopen_in(conn, current, failure, run)
                # The same caller key was used for a different capability in this
                # turn. Preserve that request and move this handle under a new key.
                conn.execute('UPDATE credential_requests SET request_key=? WHERE id=?',
                             (row['request_key'][:60] + '-renew-' + uuid4().hex[:8], row['id']))
            conn.execute('UPDATE credential_requests SET message_id=?,actor_id=? WHERE id=?',
                         (run['active_message_id'], run['active_user_id'], row['id']))
        conn.execute("UPDATE credential_requests SET status='pending',secret_id='',resolved_at='',generation=generation+1,revision=revision+1,preferred_scope='',scope_revision=0,resolution_pending=0,resolution_error='',failure=? WHERE id=?",
                     (failure, row['id']))
        self.audit_in(conn, row['actor_id'], row['secret_id'], 'requested again: ' + failure, row['run_id'])
        return dict(conn.execute('SELECT * FROM credential_requests WHERE id=?', (row['id'],)).fetchone())

    def binding_in(self, conn, row, secret):
        # The handle version changes for both in-place rotation and a different
        # attached secret, whose own version may start again at one.
        if row['secret_revision'] != secret['revision']:
            conn.execute('UPDATE credential_requests SET revision=revision+1,secret_revision=? WHERE id=?',
                         (secret['revision'], row['id']))
            row = dict(conn.execute('SELECT * FROM credential_requests WHERE id=?', (row['id'],)).fetchone())
        return row

    def matching(self, secret, capability):
        return all(secret[key] == capability[key] for key in ('provider', 'name', 'format', 'env_var'))

    def saved_choice(self, conn, run, capability, secret_id=''):
        candidates = [dict(secret) for secret in conn.execute(
            "SELECT * FROM provider_secrets WHERE provider=? AND revoked_at=''", (capability['provider'],))
            if self.matching(secret, capability) and self.permitted(secret, run, run['active_user_id'])
            and self.status(secret) == 'active']
        if secret_id:
            selected = next((secret for secret in candidates if secret['id'] == secret_id), None)
            if not selected:
                raise ValueError('Select an active, authorized saved credential matching this capability.')
            return selected
        private = [secret for secret in candidates if secret['scope'] != 'organization']
        choices = private or candidates
        return choices[0] if len(choices) == 1 else None

    def source_lookup(self, conn, run, args):
        if args.provider == 'generic' and args.name == '1password-shared':
            return None  # Obtaining the source must not recursively require itself.
        sources = [self.metadata(secret, run['active_user_id'], False) for secret in conn.execute(
            "SELECT * FROM provider_secrets WHERE provider='generic' AND name='1password-shared' AND format='env' AND revoked_at=''")
            if self.permitted(secret, run, run['active_user_id']) and self.status(secret) == 'active']
        checked = set()
        for check in args.source_checks:
            used = conn.execute('''SELECT 1 FROM credential_source_uses
                WHERE run_id=? AND message_id=? AND secret_id=? AND revision=?''',
                (run['id'], run['active_message_id'], check.secret_id, check.revision)).fetchone()
            if not used:
                raise ValueError('Use the credential source through credentials_run in this turn before reporting its outcome.')
            checked.add((check.secret_id, check.revision))
        remaining = [source for source in sources if (source['id'], source['revision']) not in checked]
        if not remaining:
            return None
        return {'status': 'lookup_required', 'credential_sources': remaining,
                'request_arguments': args.model_dump(exclude={'secret_id'}),
                'instructions': 'No new credential form was opened. Check these authorized 1Password sources first. '
                    'Request provider=generic, name=1password-shared, format=env with the source secret_id, '
                    'then use credentials_run to inspect Shared and verify task-relevant access. '
                    'Keep vault values out of output; use op run with masking for provider calls. '
                    'If access works, continue with it and close any earlier pending request with credentials_resolve. '
                    'Only if lookup or verification fails, retry request_arguments, preserving earlier source_checks and adding '
                    'each source secret_id, revision and observed outcome (not_found, invalid, expired, permission, unavailable). '
                    'Do not treat network, quota or unrelated command failures as an invalid key. Never replay an uncertain write.'}

    def reuse_in(self, conn, row, secret):
        conn.execute("""UPDATE credential_requests SET status='provided',secret_id=?,secret_revision=?,
            revision=revision+1,resolved_at=?,failure='',resolution_pending=0,resolution_error='' WHERE id=?""",
            (secret['id'], secret['revision'], now(), row['id']))
        self.audit_in(conn, row['actor_id'], secret['id'], 'reused', row['run_id'])
        return dict(conn.execute('SELECT * FROM credential_requests WHERE id=?', (row['id'],)).fetchone())

    def request(self, run, args):
        with self.store.connect() as conn:
            conn.begin_write()
            actor = run['active_user_id']
            if not actor or not run['active_message_id']:
                raise ValueError('Start an authenticated chat session before requesting access.')
            prior = conn.execute('SELECT * FROM credential_requests WHERE run_id=? AND message_id=? AND request_key=?',
                                 (run['id'], run['active_message_id'], args.request_key)).fetchone()
            if prior and any(prior[key] != getattr(args, key) for key in ('provider', 'reason', 'name', 'format', 'env_var')):
                raise ValueError('Use the original arguments when retrying this credential request.')
            input_fields = json.dumps([field.model_dump() for field in args.input_fields])
            if prior and json.loads(prior['input_fields']) != json.loads(input_fields):
                raise ValueError('Use the original credential inputs when retrying this request.')
            if not prior:
                for candidate in self.pending_rows(conn, run['id']):
                    same_actor = (self.same_requester(candidate['actor_id'], run['active_user_id'])
                                  or self.same_requester(run['active_user_id'], candidate['actor_id']))
                    if (same_actor and candidate['request_key'] == args.request_key
                            and self.matching(candidate, args.model_dump()) and candidate['reason'] == args.reason
                            and json.loads(candidate['input_fields']) == json.loads(input_fields)):
                        prior = self.reopen_in(conn, candidate, candidate['failure'], run)
                        break
            # Resolve saved access and require source discovery before a new
            # pending request can checkpoint the agent. A source request can
            # therefore proceed even with an older unrelated pending form.
            secret = None
            valid_prior = False
            if prior and prior['status'] == 'provided':
                bound = conn.execute('SELECT * FROM provider_secrets WHERE id=?', (prior['secret_id'],)).fetchone()
                valid_prior = bool(bound and self.permitted(bound, run, actor) and self.status(bound) == 'active')
                if valid_prior and args.secret_id and args.secret_id != prior['secret_id']:
                    raise ValueError('Use a new request_key to select a different saved connection.')
            terminal_prior = prior and prior['status'] in {'declined', 'satisfied'}
            permission_retry = prior and prior['status'] == 'pending' and prior['failure'] == 'permission'
            if not valid_prior and not terminal_prior and not permission_retry:
                secret = self.saved_choice(conn, run, args.model_dump(), args.secret_id)
                if not secret:
                    lookup = self.source_lookup(conn, run, args)
                    if lookup:
                        return lookup
            if not prior and not secret:
                prior = conn.execute("SELECT * FROM credential_requests WHERE run_id=? AND message_id=? AND status='pending'",
                                     (run['id'], run['active_message_id'])).fetchone()
            if prior:
                row = dict(prior)
                if secret:
                    row = self.reuse_in(conn, row, secret)
                elif row['status'] == 'provided' and not valid_prior:
                    row = self.reopen_in(conn, row, self.status(bound) if bound else 'unavailable')
                # An exact retry may add guidance to an older pending request.
                # Another pending capability must never inherit these details.
                if row['status'] == 'pending' and row['request_key'] == args.request_key:
                    for field in ('setup_url', 'setup_instructions'):
                        value = getattr(args, field)
                        if value and row[field] and row[field] != value:
                            raise ValueError('Use the original setup guidance when retrying this credential request.')
                        if value and not row[field]:
                            conn.execute(f'UPDATE credential_requests SET {field}=? WHERE id=?', (value, row['id']))
                            row[field] = value
            else:
                row = {'id': uuid4().hex, 'run_id': run['id'], 'message_id': run['active_message_id'], 'actor_id': actor,
                       **args.model_dump(exclude={'secret_id', 'source_checks'}), 'input_fields': input_fields, 'status': 'provided' if secret else 'pending', 'secret_id': secret['id'] if secret else '',
                       'created_at': now(), 'resolved_at': now() if secret else '', 'generation': 0, 'failure': '',
                       'revision': 1, 'secret_revision': secret['revision'] if secret else 1}
                conn.execute(f'INSERT INTO credential_requests({",".join(row)}) VALUES({",".join("?" for key in row)})', tuple(row.values()))
                self.audit_in(conn, actor, row['secret_id'], 'reused' if secret else 'requested', run['id'])
            if row['status'] in {'provided', 'declined', 'satisfied'}:
                self.acknowledge_in(conn, run['id'], run['active_message_id'], row['id'], row['generation'])
        message = {'pending': 'Access requested', 'provided': 'Access ready'}.get(row['status'], 'Access request ' + row['status'])
        self.store.event(run['id'], 'credential', message, {'request_id': row['id']})
        return self.ready(row)

    def acknowledge_in(self, conn, run_id, message_id, request_id, generation):
        """A receipt belongs to the exact generation delivered to its original turn."""
        conn.execute("""UPDATE credential_requests SET resolution_pending=0,resolution_error=''
            WHERE id=? AND run_id=? AND message_id=? AND generation=? AND resolution_pending=1 AND status IN ('provided','declined','satisfied')
            AND EXISTS(SELECT 1 FROM messages m JOIN runs r ON r.id=m.run_id
                WHERE m.id=? AND m.run_id=? AND m.status='running' AND r.active_message_id=m.id)""",
            (request_id, run_id, message_id, generation, message_id, run_id))

    def acknowledge(self, run_id, message_id, request_id, generation):
        with self.store.connect() as conn:
            self.acknowledge_in(conn, run_id, message_id, request_id, generation)

    def enqueue_resolution_in(self, conn, run, row):
        content = (f"Secure access update: {row['name'] or row['provider']} access was {row['status']}. "
                   f"Continue the task that requested it: {row['reason']} "
                   "Inspect current state before retrying any external action.")
        enqueue = self.manager.coordinator.enqueue_child_in if run['parent_run_id'] else self.store.enqueue_message_in
        enqueue(conn, row['run_id'], content, f"credential-{row['id']}-{row['generation']}", None, row['actor_id'], restore_archived=False)
        conn.execute("UPDATE credential_requests SET resolution_pending=0,resolution_error='' WHERE id=? AND generation=?",
                     (row['id'], row['generation']))

    def reconcile_resolutions(self, run_id):
        """Finish a delivery abandoned by a handoff, including after worker restart."""
        if not self.store.rows('SELECT 1 FROM credential_requests WHERE run_id=? AND resolution_pending=1 LIMIT 1', (run_id,)):
            return
        with self.store.connect() as conn:
            conn.begin_write()
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run:
                return
            rows = conn.execute("""SELECT q.*,m.status AS message_status FROM credential_requests q
                JOIN messages m ON m.id=q.message_id AND m.run_id=q.run_id
                WHERE q.run_id=? AND q.resolution_pending=1""", (run_id,)).fetchall()
            for row in rows:
                if (run['deleted_at'] or run['status'] not in ACCESSIBLE
                        or row['message_status'] not in {'running', 'completed', 'steered'}):
                    conn.execute("UPDATE credential_requests SET resolution_pending=0,resolution_error='' WHERE id=?", (row['id'],))
                    continue
                if row['message_status'] == 'running':
                    continue
                # Child admission can freeze group results before queue admission.
                # A deferred delivery must not commit half of that operation.
                conn.execute('SAVEPOINT credential_delivery')
                try:
                    self.enqueue_resolution_in(conn, run, row)
                except ValueError as exc:
                    conn.execute('ROLLBACK TO credential_delivery')
                    count = conn.execute("SELECT COUNT(*) FROM messages WHERE run_id=? AND role='user' AND status!='deleted'", (run_id,)).fetchone()[0]
                    detail = ('This session reached its 100-turn limit. Start a new session to continue.'
                              if count >= 100 else str(exc))
                    if detail != row['resolution_error']:
                        conn.execute('UPDATE credential_requests SET resolution_error=? WHERE id=?', (detail, row['id']))
                        conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'credential',?,?,?)",
                                     (run_id, 'Access decision saved, but automatic continuation could not be queued. ' + detail,
                                      json.dumps({'request_id': row['id'], 'turn_id': row['message_id']}), now()))
                finally:
                    conn.execute('RELEASE credential_delivery')

    def resolution(self, run_id, request_id):
        row = self.row(request_id)
        run = self.store.run(run_id)
        if row['run_id'] != run_id or row['message_id'] != run['active_message_id']:
            raise ValueError('Credential request does not belong to this turn.')
        return {**self.ready(row), 'generation': row['generation']}

    def pending(self, run, user_id, admin):
        with self.store.connect() as conn:
            rows = self.pending_rows(conn, run['id'])
        return [{**{key: row[key] for key in ('id', 'provider', 'reason', 'status', 'name', 'format', 'env_var', 'generation', 'failure', 'preferred_scope', 'scope_revision')},
                 'input_fields': json.loads(row['input_fields']),
                 'root_id': self.root(run), 'can_personal': self.same_requester(user_id, row['actor_id']), 'can_organization': admin,
                 **self.setup(row), 'provider_name': row['name'] or PROVIDERS[row['provider']]['name']}
                for row in rows]

    def resolve_external(self, run: dict[str, object], args: ResolveExternal) -> dict[str, object]:
        with self.store.connect() as conn:
            conn.begin_write()
            current = conn.execute('SELECT * FROM runs WHERE id=?', (run['id'],)).fetchone()
            if (not current or current['deleted_at'] or current['status'] != 'running'
                    or not current['active_user_id'] or not current['token_hash']
                    or any(current[key] != run[key] for key in ('active_message_id', 'active_user_id', 'token_hash'))
                    or not conn.execute("SELECT 1 FROM messages WHERE id=? AND run_id=? AND status='running'",
                                        (current['active_message_id'], current['id'])).fetchone()):
                raise HTTPException(409, 'This agent turn is no longer active.')
            row, _ = self.authorized_request(conn, current, args.request_id)
            if row['generation'] != args.generation:
                raise HTTPException(409, 'This access request changed. Check credentials_list again.')
            if row['status'] == 'pending':
                # The active caller consumes this result; never replay the historical requesting turn.
                conn.execute("""UPDATE credential_requests SET status='satisfied',resolved_at=?,
                    resolution_pending=0,resolution_error='' WHERE id=?""", (now(), row['id']))
                self.audit_in(conn, current['active_user_id'], '', 'satisfied via ' + args.source, current['id'])
                conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'credential',?,?,?)",
                             (current['id'], 'Access request satisfied through verified existing access',
                              json.dumps({'request_id': row['id'], 'source': args.source,
                                          'turn_id': current['active_message_id']}), now()))
                row = {**row, 'status': 'satisfied'}
            return {**self.ready(row), 'generation': row['generation']}

    def resolve(self, request_id, body, user_id, admin):
        with self.store.connect() as conn:
            conn.begin_write()
            row = conn.execute('SELECT * FROM credential_requests WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise HTTPException(404, 'Credential request not found.')
            run = conn.execute('SELECT * FROM runs WHERE id=?', (row['run_id'],)).fetchone()
            if not run or run['deleted_at']:
                raise HTTPException(404, 'Session not found.')
            own = self.same_requester(user_id, row['actor_id'])
            if not own and not admin:
                raise HTTPException(403, 'Only the requester or an administrator can resolve this request.')
            if (body.generation is not None and body.generation != row['generation']) or (body.generation is None and row['generation']):
                raise HTTPException(409, 'This access request changed. Reopen the form before continuing.')
            if row['status'] != 'pending':
                return row['run_id']  # Lost acknowledgements never save twice.
            if run['status'] not in ACCESSIBLE:
                raise HTTPException(409, 'This session is no longer waiting for that access.')
            secret_id = ''
            if body.decision == 'provide':
                if body.secret_id:
                    secret = conn.execute('SELECT * FROM provider_secrets WHERE id=?', (body.secret_id,)).fetchone()
                    if (not secret or not self.matching(secret, row) or self.status(secret) != 'active'
                            or (secret['scope'] != 'organization' and (secret['owner_id'] != user_id or not own))
                            or not self.permitted(secret, run, row['actor_id'])):
                        raise HTTPException(403, 'Those credentials are not available to this requester and session.')
                    secret_id = secret['id']
                else:
                    if body.scope != 'organization' and not own:
                        raise HTTPException(403, 'Personal credentials can only be supplied for your own requests.')
                    try:
                        inputs = json.loads(row['input_fields'])
                        if inputs:
                            values = json.loads(body.value.get_secret_value())
                            if not isinstance(values, dict) or any(
                                field['required'] and (not isinstance(values.get(field['name']), str) or not values[field['name']].strip())
                                for field in inputs
                            ):
                                raise ValueError('Missing required credential input.')
                        data = SaveSecret(**{key: row[key] for key in ('provider', 'name', 'format', 'env_var')},
                            scope=body.scope, lifetime=body.lifetime, expires_at=body.expires_at,
                            label=body.label.strip() or row['name'] or PROVIDERS[row['provider']]['name'] + ' key',
                            value=body.value, client_id='request-' + row['id'] + '-' + str(row['generation']))
                    except ValueError:
                        raise HTTPException(422, 'Choose sharing and reuse, and check the credential format and expiry.') from None
                    secret_id = self.insert_secret(conn, data, user_id, admin, self.root(run))
            status = 'provided' if secret_id else 'declined'
            secret_revision = conn.execute('SELECT revision FROM provider_secrets WHERE id=?', (secret_id,)).fetchone()[0] if secret_id else 1
            conn.execute('UPDATE credential_requests SET status=?,secret_id=?,secret_revision=?,resolved_at=?,resolution_pending=1,resolution_error=\'\' WHERE id=?',
                         (status, secret_id, secret_revision, now(), request_id))
            original_running = (run['active_message_id'] == row['message_id'] and conn.execute(
                "SELECT 1 FROM messages WHERE id=? AND run_id=? AND status='running'",
                (row['message_id'], row['run_id'])).fetchone())
            if not original_running:
                # Restore the requester through queue admission, without replaying
                # the completed turn or borrowing the active person's identity.
                try:
                    self.enqueue_resolution_in(conn, run, {**dict(row), 'status': status})
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from None
            self.audit_in(conn, user_id, secret_id, status, row['run_id'])
        self.store.event(row['run_id'], 'credential', 'Access supplied' if secret_id else 'Access request declined', {'request_id': request_id})
        return row['run_id']

    def authorized_request(self, conn, run, request_id):
        row = conn.execute('SELECT * FROM credential_requests WHERE id=?', (request_id,)).fetchone()
        if not row or row['run_id'] != run['id']:
            raise HTTPException(403, 'This credential request is not available in this session.')
        secret = conn.execute('SELECT * FROM provider_secrets WHERE id=?', (row['secret_id'],)).fetchone()
        # Handles from earlier turns may only be used under current permission.
        same_actor = (self.same_requester(row['actor_id'], run['active_user_id'])
                      or self.same_requester(run['active_user_id'], row['actor_id']))
        if not same_actor and (not secret or secret['scope'] != 'organization'):
            raise HTTPException(403, 'Request access for the current requester.')
        return dict(row), dict(secret) if secret else None

    def materialize(self, run, args):
        with self.store.connect() as conn:
            conn.begin_write()
            accepted = []
            for request_id in args.request_ids:
                row, secret = self.authorized_request(conn, run, request_id)
                if row['provider'] != 'generic':
                    raise HTTPException(422, 'Use the inference proxy for provider keys.')
                if row['status'] == 'pending':
                    return self.ready(self.reopen_in(conn, row, row['failure'], run))
                if row['status'] != 'provided':
                    self.acknowledge_in(conn, run['id'], run['active_message_id'], row['id'], row['generation'])
                    return self.ready(row)
                if not secret or not self.permitted(secret, run, run['active_user_id']):
                    return self.ready(self.reopen_in(conn, row, 'unavailable', run))
                if self.status(secret) != 'active':
                    return self.ready(self.reopen_in(conn, row, self.status(secret), run))
                accepted.append((self.binding_in(conn, row, secret), secret))
            # No plaintext is produced until every selected handle is authorized.
            for row, _ in accepted:
                self.acknowledge_in(conn, run['id'], run['active_message_id'], row['id'], row['generation'])
            for _, secret in accepted:
                if secret['name'] == '1password-shared' and secret['format'] == 'env':
                    conn.execute('INSERT INTO credential_source_uses VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                                 (run['id'], run['active_message_id'], secret['id'], secret['revision']))
            return {'status': 'ready', 'bindings': [{'request_id': row['id'], 'revision': row['revision'],
                    'name': secret['name'], 'format': secret['format'], 'env_var': secret['env_var'],
                    'value': self.security.decrypt(secret['encrypted'])} for row, secret in accepted]}

    def report_failure(self, run, args):
        with self.store.connect() as conn:
            conn.begin_write()
            row, secret = self.authorized_request(conn, run, args.request_id)
            if (row['status'] != 'provided' or not secret or row['revision'] != args.revision
                    or row['secret_revision'] != secret['revision']):
                return {'status': 'stale', 'message': 'Access changed since this command. Check the current connection before continuing.'}
            if not self.permitted(secret, run, run['active_user_id']):
                raise HTTPException(403, 'This access is unavailable to the current requester.')
            if args.failure == 'unknown':
                return {'status': 'unknown', 'request_id': row['id'], 'revision': row['revision'],
                        'message': 'The failure does not establish that credentials are invalid. Inspect the command and service; do not automatically replay writes.'}
            if args.failure != 'permission':
                conn.execute('UPDATE provider_secrets SET invalid_reason=? WHERE id=? AND revision=?',
                             (args.failure, secret['id'], secret['revision']))
            replacement = self.saved_choice(conn, run, row) if args.failure != 'permission' else None
            if replacement:
                row = self.reuse_in(conn, row, replacement)
                result = {**self.ready(row), 'retry_required': True,
                          'message': 'Alternative saved access is ready. Verify it with a harmless request; the failed command was not replayed.'}
            else:
                lookup_args = CredentialRequest(**{key: row[key] for key in
                    ('provider', 'name', 'format', 'env_var', 'reason', 'request_key', 'setup_url', 'setup_instructions')},
                    input_fields=json.loads(row['input_fields']))
                lookup = self.source_lookup(conn, run, lookup_args) if args.failure != 'permission' else None
                result = lookup or self.ready(self.reopen_in(conn, row, args.failure, run))
            self.audit_in(conn, run['active_user_id'], secret['id'], 'authentication ' + args.failure, run['id'])
        message = ('Checking alternative access' if result['status'] != 'pending' else
                   'Additional permission needed' if args.failure == 'permission' else 'Saved access needs replacement')
        self.store.event(run['id'], 'credential', message, {'request_id': args.request_id})
        return result

    async def call(self, run, name, arguments):
        args = TOOLS[name][0].model_validate(arguments)
        if name == 'credentials_list':
            return self.inventory(run, args)
        if name == 'credentials_request':
            return self.request(run, args)
        if name == 'credentials_resolve':
            return self.resolve_external(run, args)
        if name == 'credentials_report_failure':
            return self.report_failure(run, args)
        if name == 'credentials_run':
            return {'error': 'Run credential commands through the sandbox credential tool.'}
        status, result = await self.invoke(run, args)
        return {'status_code': status, 'response': result,
                **({'moyai_wait_credential': result['moyai_wait_credential']} if result.get('moyai_wait_credential') else {})}

    async def invoke(self, run, args):
        with self.store.connect() as conn:
            conn.begin_write()
            request, secret = self.authorized_request(conn, run, args.request_id)
            if secret:
                request = self.binding_in(conn, request, secret)
        if (request['run_id'] != run['id'] or request['status'] != 'provided' or not secret
                or not self.permitted(secret, run, run['active_user_id'])):
            raise HTTPException(403, 'This provider key is unavailable. Request it again through the secure form.')
        if secret['provider'] == 'generic':
            raise HTTPException(422, 'Use credentials_run for this connection.')
        if self.status(secret) != 'active':
            failure = 'expired' if self.status(secret) == 'expired' else 'invalid'
            result = self.report_failure(run, ReportFailure(request_id=args.request_id, revision=request['revision'], failure=failure))
            return 401, {'error': {'message': 'Saved authentication is unavailable. Follow the returned access recovery instructions.', 'type': 'credential_' + failure}, **result}
        provider = PROVIDERS[secret['provider']]
        paths = {'GET': {'/models'}, 'POST': {'/messages'} if secret['provider'] == 'anthropic' else {'/chat/completions', '/completions', '/embeddings'}}
        if args.path not in paths[args.method] or (args.method == 'GET' and args.body):
            raise HTTPException(422, 'Only supported inference and model-list routes are allowed.')
        if args.body.get('stream') or args.body.get('background'):
            raise HTTPException(422, 'Use non-streaming, foreground requests through the credential proxy.')
        if self.slots.locked():
            raise HTTPException(429, 'Provider request capacity is busy. No provider call was made.')
        async with self.slots:
            self.acknowledge(run['id'], run['active_message_id'], request['id'], request['generation'])
            value = self.security.decrypt(secret['encrypted'])
            headers = {'x-api-key': value, 'anthropic-version': '2023-06-01'} if secret['provider'] == 'anthropic' else {'Authorization': 'Bearer ' + value}
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30), follow_redirects=False) as client:
                    async with client.stream(args.method, provider['base'] + args.path, headers=headers,
                                             json=args.body if args.method == 'POST' else None) as response:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 8 * 1024 * 1024:
                                raise HTTPException(502, 'Provider response exceeded the size limit.')
                        status = response.status_code
            except httpx.HTTPError:
                raise HTTPException(502, 'Provider request could not be confirmed. Do not retry a billed request automatically.') from None
            if not 200 <= status < 300:
                recovery = {}
                if status in (401, 403):
                    recovery = self.report_failure(run, ReportFailure(request_id=args.request_id, revision=request['revision'], failure='invalid' if status == 401 else 'permission'))
                return status if 400 <= status < 600 else 502, {'error': {'message': f'{provider["name"]} rejected the request ({status}). ' + ('Follow the returned access recovery instructions.' if status == 401 else 'Check permissions, quota, model and request.'), 'type': 'invalid_credentials' if status == 401 else 'permission' if status == 403 else 'provider_error'}, **recovery}
            try:
                decoded = json.loads(bytes(raw))
                result = json.loads(json.dumps(decoded).replace(value, '[credential redacted]'))
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(502, 'Provider returned an invalid JSON response.') from None
            return status, result

    def update_secret(self, secret_id, body, user_id, admin):
        with self.store.connect() as conn:
            conn.begin_write()
            row = conn.execute('SELECT * FROM provider_secrets WHERE id=? AND revoked_at=\'\'', (secret_id,)).fetchone()
            if not row or (row['scope'] == 'organization' and not admin) or (row['scope'] != 'organization' and row['owner_id'] != user_id):
                raise HTTPException(404, 'Credentials not found.')
            if row['revision'] != body.revision:
                raise HTTPException(409, 'These credentials changed. Refresh before saving.')
            values = dict(row)
            for key in body.model_fields_set - {'revision', 'value'}:
                values[key] = getattr(body, key)
            if values['scope'] == 'organization' and not admin:
                raise HTTPException(403, 'Only an administrator can manage organization credentials.')
            if values['scope'] == 'personal' and row['scope'] == 'organization':
                # An administrator can make a shared credential personal only to themselves.
                values['owner_id'] = user_id
                values['client_id'] = 'ownership-' + secret_id
                if not user_id.startswith(('google:', 'cloudflare:')) and not self.security.local_preview():
                    raise HTTPException(403, 'Use Google sign-in to save personal credentials.')
            if values['lifetime'] == 'session':
                root = self.store.run(values['root_id']) if values['root_id'] else None
                if not root or root['deleted_at'] or self.root(root) != values['root_id']:
                    raise HTTPException(422, 'Choose an existing root session for session-only use.')
                if values['scope'] == 'personal' and not self.same_requester(values['owner_id'], root['active_user_id']):
                    raise HTTPException(403, 'Choose your own session for personal credentials.')
            else:
                values['root_id'] = ''
            if body.value is not None:
                candidate = SaveSecret(**{key: values[key] for key in ('provider', 'name', 'format', 'env_var', 'scope', 'lifetime', 'root_id', 'expires_at', 'client_id', 'label')}, value=body.value)
                values['encrypted'] = self.security.encrypt(self.validated_value(candidate))
                values['invalid_reason'] = ''
            values['label'] = values['label'].strip()
            fields = ('label', 'scope', 'lifetime', 'root_id', 'expires_at', 'owner_id', 'client_id', 'encrypted', 'invalid_reason')
            conn.execute('UPDATE provider_secrets SET ' + ','.join(key + '=?' for key in fields) + ',revision=revision+1 WHERE id=?', (*(values[key] for key in fields), secret_id))
            self.audit_in(conn, user_id, secret_id, 'updated', values['root_id'])
            saved = dict(conn.execute('SELECT * FROM provider_secrets WHERE id=?', (secret_id,)).fetchone())
        return self.metadata(saved, user_id, admin)

    def routes(self):
        router = APIRouter()

        def actor(request, mutation=False):
            self.security.require(request, mutation=mutation)
            return self.store.identity(self.security.session_info(request)), self.security.role(request) == 'admin'

        @router.get('/api/credentials')
        async def list_keys(request: Request, run_id: str = ''):
            user, admin = actor(request)
            run = self.store.run(run_id) if re.fullmatch(r'[0-9a-f]{32}', run_id) else None
            if run and run['deleted_at']:
                raise HTTPException(404, 'Session not found.')
            root_id = self.root(run) if run else ''
            return {'providers': [{'id': key, 'name': value['name'], 'setup_url': value['setup']} for key, value in PROVIDERS.items()],
                    'root_id': root_id, 'secrets': self.list_secrets(user, admin, root_id)}

        @router.post('/api/credentials/secrets', status_code=201)
        async def save_key(body: SaveSecret, request: Request):
            user, admin = actor(request, True)
            if body.lifetime == 'session':
                root = self.store.run(body.root_id) if body.root_id else None
                if not root or root['deleted_at'] or self.root(root) != body.root_id:
                    raise HTTPException(422, 'Provide session credentials through a session request.')
                if body.scope == 'personal' and not self.same_requester(user, root['active_user_id']):
                    raise HTTPException(403, 'Choose your own session for personal credentials.')
            with self.store.connect() as conn:
                conn.begin_write()
                identity = self.insert_secret(conn, body, user, admin)
            return {'id': identity, 'saved': True}

        @router.patch('/api/credentials/secrets/{secret_id}')
        async def update_key(secret_id: str, body: UpdateSecret, request: Request):
            user, admin = actor(request, True)
            return self.update_secret(secret_id, body, user, admin)

        @router.delete('/api/credentials/secrets/{secret_id}')
        async def revoke_key(secret_id: str, request: Request):
            user, admin = actor(request, True)
            with self.store.connect() as conn:
                conn.begin_write()
                row = conn.execute('SELECT * FROM provider_secrets WHERE id=?', (secret_id,)).fetchone()
                if not row or not (row['owner_id'] == user or (row['scope'] == 'organization' and admin)):
                    raise HTTPException(404, 'Credentials not found.')
                if row['scope'] == 'organization' and not admin:
                    raise HTTPException(403, 'Only an administrator can revoke organization credentials.')
                conn.execute("UPDATE provider_secrets SET encrypted='',revoked_at=?,revision=revision+1 WHERE id=?", (now(), secret_id))
                self.audit_in(conn, user, secret_id, 'revoked')
            return {'revoked': True}

        @router.post('/api/credentials/requests/{request_id}')
        async def resolve_request(request_id: str, body: Resolve, request: Request):
            user, admin = actor(request, True)
            run_id = self.resolve(request_id, body, user, admin)
            self.manager.submit(self.store.run(run_id))
            await self.checkpoints.flush()
            return {'saved': True}

        return router
