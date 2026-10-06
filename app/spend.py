"""Durable, server-attributed gateway usage; monetary values remain decimal strings."""
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import now
from .security import digest


def money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def period(start=None, end=None):
    today = datetime.now(timezone.utc).date()
    start, end = start or today.replace(day=1), end or today
    if start > end or (end - start).days > 92:
        raise HTTPException(422, 'Choose a date range of up to 93 days, with start before end.')
    return start, end, start.isoformat() + 'T00:00:00+00:00', (end + timedelta(days=1)).isoformat() + 'T00:00:00+00:00'


class UsageCapture:
    """Observe only usage/cost fields; never persist prompts or completions here."""
    def __init__(self, streaming):
        self.streaming = streaming
        self.buffer = b''
        self.usage = {}
        self.cost = None
        self.response_id = ''
        self.done = False

    def consume(self, value):
        if not isinstance(value, dict):
            return
        if isinstance(value.get('id'), str):
            self.response_id = value['id'][:200]
        if isinstance(value.get('usage'), dict):
            self.usage = value['usage']
        # LiteLLM's optional final streaming cost extension. Token prices are
        # never guessed when the gateway does not expose a final cost.
        for source in (value, value.get('usage', {})):
            if isinstance(source, dict):
                cost = money(source.get('x_litellm_response_cost'))
                if cost is not None:
                    self.cost = cost

    def feed(self, chunk):
        self.buffer += chunk
        if self.streaming:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                self.line(line)
        if len(self.buffer) > 8 * 1024 * 1024:
            self.buffer = b''

    def line(self, line):
        if not line.startswith(b'data:'):
            return
        data = line[5:].strip()
        if data == b'[DONE]':
            self.done = True
            return
        try:
            self.consume(json.loads(data))
        except (ValueError, UnicodeDecodeError):
            pass

    def finish(self):
        if self.streaming:
            self.line(self.buffer)
        else:
            try:
                value = json.loads(self.buffer, parse_float=Decimal)
                self.consume(value)
                self.done = isinstance(value, dict) and 'error' not in value
            except (ValueError, UnicodeDecodeError):
                pass
        self.buffer = b''


class IdentityLink(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slack_user_id: str = Field(max_length=150)
    google_user_id: str = Field(max_length=150)


def completion_events(value):
    """Preserve text, reasoning, tool calls and usage in the Chat Completions SSE shape."""
    base = {key: value[key] for key in ('id', 'created', 'model', 'system_fingerprint') if key in value}
    base['object'] = 'chat.completion.chunk'

    def frame(choices, **extra):
        return 'data: ' + json.dumps({**base, 'choices': choices, **extra}) + '\n\n'

    for index, choice in enumerate(value.get('choices', [])):
        delta = dict(choice.get('message') or {})
        if delta.get('tool_calls'):
            delta['tool_calls'] = [{**tool, 'index': i} for i, tool in enumerate(delta['tool_calls'])]
        position = choice.get('index', index)
        yield frame([{'index': position, 'delta': delta, 'finish_reason': None, 'logprobs': choice.get('logprobs')}])
        yield frame([{'index': position, 'delta': {}, 'finish_reason': choice.get('finish_reason') or 'stop', 'logprobs': None}])
    if isinstance(value.get('usage'), dict):
        yield frame([], usage=value['usage'])
    yield 'data: [DONE]\n\n'


class Spend:
    def __init__(self, store, settings, security, checkpoints):
        self.store, self.settings, self.security, self.checkpoints = store, settings, security, checkpoints
        from .infrastructure_costs import InfrastructureCosts
        self.infrastructure = InfrastructureCosts(store, settings, security, checkpoints)
        with store.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS model_requests (
                    id TEXT PRIMARY KEY, key_hash TEXT NOT NULL, gateway_id TEXT NOT NULL DEFAULT '',
                    run_id TEXT NOT NULL REFERENCES runs(id), message_id INTEGER,
                    user_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL, created_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL DEFAULT 'pending', cost TEXT,
                    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_model_requests_gateway ON model_requests(key_hash,gateway_id);
                CREATE INDEX IF NOT EXISTS idx_model_requests_time ON model_requests(key_hash,created_at);
                CREATE INDEX IF NOT EXISTS idx_model_requests_run ON model_requests(key_hash,run_id);
                CREATE INDEX IF NOT EXISTS idx_model_requests_org_time ON model_requests(created_at);
                CREATE INDEX IF NOT EXISTS idx_model_requests_org_run ON model_requests(run_id);
            ''')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(model_requests)')}
            if 'cost_source' not in columns:
                conn.execute("ALTER TABLE model_requests ADD COLUMN cost_source TEXT NOT NULL DEFAULT ''")

    @property
    def key_hash(self):
        return digest(self.settings.litellm_api_key) if self.settings.litellm_api_key else ''

    def begin(self, run, model):
        request_id = str(uuid4())
        user_id = run['active_user_id'] if run['chat_enabled'] else run['owner_id']
        self.store.execute('INSERT INTO model_requests(id,key_hash,run_id,message_id,user_id,model,created_at) VALUES(?,?,?,?,?,?,?)',
                           (request_id, self.key_hash, run['id'], run['active_message_id'], user_id, model, now()))
        return request_id

    def headers(self, request_id, upstream, streaming):
        # Streaming headers arrive before generation ends and may report zero.
        # Only a final usage field can price a streamed response.
        cost = None if streaming else money(upstream.headers.get('x-litellm-response-cost'))
        gateway_id = upstream.headers.get('x-litellm-call-id', '')[:200]
        self.store.execute('UPDATE model_requests SET gateway_id=?,cost=?,cost_source=? WHERE id=?',
                           (gateway_id, cost, 'response_header' if cost is not None else '', request_id))

    def finish(self, request_id, capture, status):
        usage = capture.usage if capture else {}
        def tokens(field):
            value = usage.get(field)
            return value if type(value) is int and value >= 0 else None
        cost = capture.cost if capture else None
        self.store.execute('UPDATE model_requests SET status=?,finished_at=?,cost_source=CASE WHEN cost IS NULL AND ? IS NOT NULL THEN ? ELSE cost_source END,cost=COALESCE(cost,?),prompt_tokens=?,completion_tokens=?,total_tokens=? WHERE id=?',
                           (status, now(), cost, 'response_usage', cost, tokens('prompt_tokens'), tokens('completion_tokens'), tokens('total_tokens'), request_id))

    def report(self, start=None, end=None):
        start, end, lower, upper = period(start, end)
        users = {u['id']: u for u in self.store.rows('''SELECT id,kind,email,name,linked_user_id,link_method,link_status,profile_checked_at
            FROM users u WHERE kind!='slack' OR linked_user_id IS NOT NULL
            OR EXISTS(SELECT 1 FROM messages WHERE user_id=u.id)
            OR EXISTS(SELECT 1 FROM runs WHERE owner_id=u.id)
            OR EXISTS(SELECT 1 FROM model_requests WHERE user_id=u.id)''')}
        rows = self.store.rows('SELECT * FROM model_requests WHERE created_at>=? AND created_at<?', (lower, upper))
        tracked_since = self.store.rows('SELECT MIN(created_at) AS value FROM model_requests')[0]['value']
        def empty():
            return {'spend': Decimal(0), 'requests': 0, 'pending_costs': 0, 'missing_costs': 0, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0, 'sessions': set()}
        groups = {user['id']: empty() for user in users.values() if user['kind'] == 'google'}
        sessions, models = {}, {}
        def add(bucket, row):
            bucket['requests'] += 1
            bucket['pending_costs'] += row['cost'] is None and row['status'] == 'pending'
            bucket['missing_costs'] += row['cost'] is None and row['status'] != 'pending'
            bucket['spend'] += Decimal(row['cost'] or '0')
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                bucket[key] += row[key] or 0
            if row['run_id']:
                bucket['sessions'].add(row['run_id'])
        total = empty()
        for row in rows:
            actor = users.get(row['user_id'], {})
            user_id = actor.get('linked_user_id') or actor.get('id') or 'unattributed'
            row['user_id'] = user_id
            add(total, row)
            add(groups.setdefault(user_id, empty()), row)
            add(models.setdefault(row['model'], empty()), row)
            if row['run_id']:
                add(sessions.setdefault((user_id, row['run_id']), empty()), row)
        def clean(bucket):
            return {**bucket, 'spend': str(bucket['spend']), 'sessions': len(bucket['sessions'])}
        def identity(user_id):
            return users.get(user_id, {'id': 'unattributed', 'name': 'Unattributed / earlier usage', 'email': '', 'kind': 'unattributed'})
        titles = {r['id']: r['prompt'].split('\n')[0][:150] for r in self.store.rows('SELECT id,prompt FROM runs')}
        result = {'start': str(start), 'end': str(end), 'currency': 'USD', 'timezone': 'UTC', 'total': clean(total),
                'priced_requests': sum(row['cost'] is not None for row in rows),
                'users': [{**identity(key), **clean(value)} for key, value in sorted(groups.items(), key=lambda x: x[1]['spend'], reverse=True)],
                'sessions': [{'user_id': user, 'user_name': identity(user)['name'], 'run_id': run, 'title': titles.get(run, 'Session'), **clean(value)} for (user, run), value in sorted(sessions.items(), key=lambda x: x[1]['spend'], reverse=True)],
                'models': [{'model': key, **clean(value)} for key, value in models.items()],
                'request_details': [{key: row[key] for key in ('id','gateway_id','run_id','message_id','user_id','model','created_at','status','cost','cost_source','prompt_tokens','completion_tokens','total_tokens')} for row in sorted(rows, key=lambda r: r['created_at'], reverse=True)[:500]],
                'identities': list(users.values()), 'tracked_since': tracked_since}

        infrastructure = self.infrastructure.report(start, end)
        result['infrastructure'] = infrastructure
        result['cost_summary'] = {
            'llm': str(total['spend']), 'infrastructure': infrastructure['spend'],
            'total': str(total['spend'] + Decimal(infrastructure['spend'])),
            'estimated': infrastructure['estimated'],
            'incomplete': infrastructure['incomplete'] or bool(total['pending_costs'] or total['missing_costs']),
        }
        return result

    def routes(self):
        router = APIRouter()

        @router.get('/api/admin/spend')
        async def report(request: Request, start: date | None = None, end: date | None = None):
            self.security.require(request, admin=True)
            return self.report(start, end)

        @router.get('/api/admin/adoption')
        async def adoption(request: Request, start: date | None = None, end: date | None = None):
            self.security.require(request, admin=True)
            from .adoption import report as adoption_report
            return adoption_report(self.store, start, end)

        @router.post('/api/admin/spend/link-slack')
        async def link(body: IdentityLink, request: Request):
            self.security.require(request, mutation=True, admin=True)
            actor = self.store.identity(self.security.session_info(request))
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                source = conn.execute("SELECT id,linked_user_id FROM users WHERE id=? AND kind='slack'", (body.slack_user_id,)).fetchone()
                target = conn.execute("SELECT id FROM users WHERE id=? AND kind='google'", (body.google_user_id,)).fetchone()
                if not source or not target:
                    raise HTTPException(422, 'Choose a Slack account and an existing Google sign-in.')
                conn.execute("UPDATE users SET linked_user_id=?,link_method='manual',link_status='manual',updated_at=? WHERE id=?", (body.google_user_id, now(), body.slack_user_id))
                conn.execute('INSERT INTO identity_audit(actor_id,source_id,target_id,created_at,reason,previous_target_id) VALUES(?,?,?,?,?,?)',
                             (actor, body.slack_user_id, body.google_user_id, now(), 'admin_override', source['linked_user_id'] or ''))
            return {'ok': True}

        return router
