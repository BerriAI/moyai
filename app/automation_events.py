"""Authenticated event delivery into a bounded, persistent automation inbox."""
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import now

from .automation_sources import EVENT_CHOICES, EventTrigger, IDENTIFIER, normalize, example


class WebhookSetup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=1)
    provider: str = ''
    secret: str = Field(default='', max_length=512)


class TestEvent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: str = ''
    event_header: str = Field(default='', max_length=80)
    payload: dict


def sources(row):
    # Load old single-trigger drafts without discarding them during the upgrade.
    definition = json.loads(row['definition'])
    if 'triggers' in definition:
        return [(t['id'], EventTrigger.model_validate(t['event'])) for t in definition['triggers'] if t.get('event')]
    event = definition.get('event')
    if not event:
        return []
    event = {k:v for k,v in event.items() if k != 'max_runs_per_hour'}
    return [('default', EventTrigger.model_validate(event))]


class AutomationEvents:
    def __init__(self, automations):
        self.automations = automations
        self.store, self.security = automations.store, automations.security
        self.lock = asyncio.Lock()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(automation_webhooks)')}
            if columns and 'provider' not in columns:
                conn.execute('ALTER TABLE automation_webhooks RENAME TO automation_webhooks_legacy')
            conn.execute("""CREATE TABLE IF NOT EXISTS automation_webhooks (
                automation_id TEXT NOT NULL REFERENCES automations(id), provider TEXT NOT NULL,
                encrypted TEXT NOT NULL, PRIMARY KEY(automation_id,provider))""")
            if columns and 'provider' not in columns:
                for key in conn.execute('SELECT k.*,a.definition FROM automation_webhooks_legacy k JOIN automations a ON a.id=k.automation_id').fetchall():
                    providers = {t.provider for _, t in sources(key) if t.provider != 'slack'}
                    if len(providers) == 1:
                        conn.execute('INSERT INTO automation_webhooks VALUES(?,?,?)', (key['automation_id'], providers.pop(), key['encrypted']))
                conn.execute('DROP TABLE automation_webhooks_legacy')
        self.store.execute("""CREATE TABLE IF NOT EXISTS automation_events (
            occurrence TEXT PRIMARY KEY, automation_id TEXT NOT NULL REFERENCES automations(id),
            revision INTEGER NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
        self.store.execute('CREATE INDEX IF NOT EXISTS automation_events_pending ON automation_events(status,received_at)')

    def source_ready(self, row, event):
        if event.provider == 'slack':
            c = self.automations.connectors
            bot = c.slack_installation()
            scopes = {'reactions:read'} if event.event == 'reaction.added' else (
                {'groups:history'} if event.channel_id.startswith('G') else {'channels:history'} if event.channel_id else {'channels:history', 'groups:history'})
            return bool(self.automations.settings.slack_signing_secret and self.automations.settings.slack_bot_enabled
                        and self.automations.settings.slack_session_users and bot.get('installed') and scopes <= set(bot.get('scopes', []))
                        and c.policy('slack')['enabled'])
        return bool(self.store.rows('SELECT 1 FROM automation_webhooks WHERE automation_id=? AND provider=?', (row['id'], event.provider)))

    def ready(self, row):
        return all(self.source_ready(row, event) for _, event in sources(row))

    def public(self, row, actor):
        configured = sources(row)
        providers = []
        for provider in dict.fromkeys(t.provider for _, t in configured):
            matching = [(key,t) for key,t in configured if t.provider == provider]
            providers.append({'provider':provider, 'ready':all(self.source_ready(row,t) for _,t in matching),
                'url': (self.automations.settings.public_url.rstrip('/') + '/hooks/automations/' + row['id'] + '/' + provider)
                    if provider != 'slack' and row['owner_id'] == actor else '',
                'examples':[{'trigger_id':key,'payload':example(t),'event_header':t.event.split('.')[0] if provider == 'github' else ''} for key,t in matching]})
        return {'ready':self.ready(row),'providers':providers,
                'deliveries': self.store.rows("""SELECT e.status,COALESCE(NULLIF(e.detail,''),a.detail,'') AS detail,e.received_at,a.run_id FROM automation_events e
                    LEFT JOIN automation_runs a ON a.occurrence=e.occurrence WHERE e.automation_id=?
                    ORDER BY e.received_at DESC LIMIT 20""", (row['id'],))}

    @staticmethod
    def provider(row, requested):
        providers = {t.provider for _, t in sources(row)}
        if not requested and len(providers) == 1:
            return next(iter(providers))
        if requested not in providers:
            raise HTTPException(422, 'Choose a provider configured in this automation.')
        return requested

    @staticmethod
    def match(row, provider, payload, event_header=''):
        matched = [(key, normalize(t, payload, event_header)) for key,t in sources(row) if t.provider == provider]
        matched = [(key,context) for key,context in matched if context is not None]
        if not matched:
            return None
        return matched[0][1] | {'matched_trigger_ids':[key for key,_ in matched]}

    async def accept(self, row, delivery, context):
        occurrence = 'event:' + row['id'] + ':' + hashlib.sha256(delivery.encode()).hexdigest()
        stamp = now()
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM automation_events WHERE occurrence=?', (occurrence,)).fetchone():
                result = 'duplicate'
            else:
                current = conn.execute('SELECT revision,paused FROM automations WHERE id=?', (row['id'],)).fetchone()
                # Bounded intake, including ignored deliveries, protects the receiver.
                minute = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                if conn.execute('SELECT COUNT(*) FROM automation_events WHERE automation_id=? AND received_at>?', (row['id'], minute)).fetchone()[0] >= 120:
                    raise HTTPException(429, 'Event delivery limit reached. Retry later.', headers={'Retry-After': '60'})
                pending = bool(context and not current['paused'] and current['revision'] == row['revision'])
                if pending and conn.execute("SELECT COUNT(*) FROM automation_events WHERE status='pending'").fetchone()[0] >= 1000:
                    raise HTTPException(503, 'The event inbox is full. Retry later.', headers={'Retry-After': '60'})
                status = 'pending' if pending else 'ignored'
                detail = 'Waiting to run.' if pending else ('Automation paused or changed.' if current['paused'] or current['revision'] != row['revision'] else 'Event did not match the filters.')
                conn.execute('INSERT INTO automation_events VALUES(?,?,?,?,?,?,?,?)',
                             (occurrence, row['id'], row['revision'], json.dumps(context if pending else {}), status, detail, stamp,
                              (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()))
                result = 'accepted' if pending else 'ignored'
        # Do not ACK before the local durable write / optional volume checkpoint.
        await self.automations.checkpoints.flush()
        return {'status': result}

    async def dispatch(self):
        if self.lock.locked():
            return
        async with self.lock:
            # One head item per automation avoids a busy inbox starving others.
            rows = self.store.rows("""SELECT e.* FROM automation_events e WHERE e.status='pending' AND e.rowid IN
                (SELECT MIN(rowid) FROM automation_events WHERE status='pending' GROUP BY automation_id)
                ORDER BY e.received_at LIMIT 200""")
            for event in rows:
                try:
                    result = await self.automations.launch(event['automation_id'], event['revision'], event['occurrence'], event['expires_at'], event=True)
                except HTTPException as exc:
                    if exc.status_code != 429:
                        raise
                    continue
                if result['outcome'] == 'waiting':
                    self.store.execute('UPDATE automation_events SET detail=? WHERE occurrence=?',
                                       (result['detail'], event['occurrence']))
                else:
                    self.store.execute("UPDATE automation_events SET status=?,detail='',context='{}' WHERE occurrence=?",
                                       (result['outcome'], event['occurrence']))
            await self.automations.checkpoints.flush()

    async def slack(self, payload):
        # Called after Slack signature, installed-team, allowlist and own-bot checks.
        delivery = payload.get('event_id', '')
        if not isinstance(delivery, str) or not re.fullmatch(IDENTIFIER, delivery):
            raise HTTPException(400, 'Invalid event identifier.')
        for row in self.store.rows('SELECT * FROM automations'):
            context = self.match(row, 'slack', payload)
            if context:
                await self.accept(row, 'slack:' + delivery, context)

    async def receive(self, automation_id, request, provider=''):
        row = self.automations.row(automation_id)
        provider = self.provider(row, provider)
        if provider == 'slack':
            raise HTTPException(404, 'Use the installed Slack app event endpoint.')
        keys = self.store.rows('SELECT encrypted FROM automation_webhooks WHERE automation_id=? AND provider=?', (automation_id, provider))
        if not keys:
            raise HTTPException(401, 'Webhook is not configured.')
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, 'Webhook payload is too large.')
        secret = self.security.decrypt(keys[0]['encrypted']).encode()
        headers = request.headers
        digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
        def equals(expected, actual):
            return hmac.compare_digest(expected.encode(), actual.encode())
        signed_id = ''
        if provider == 'github':
            valid = equals('sha256=' + digest, headers.get('x-hub-signature-256', ''))
        elif provider == 'linear':
            valid = equals(digest, headers.get('linear-signature', ''))
        elif provider == 'jira':
            valid = equals('sha256=' + digest, headers.get('x-hub-signature', ''))
        elif provider == 'pagerduty':
            valid = any(equals('v1=' + digest, sig.strip()) for sig in headers.get('x-pagerduty-signature', '').split(','))
        elif provider == 'gitlab':
            if secret.startswith(b'whsec_'):
                timestamp, signed_id = headers.get('webhook-timestamp', ''), headers.get('webhook-id', '')
                self.fresh(timestamp)
                try:
                    key = base64.b64decode(secret[6:], validate=True)
                except (ValueError, binascii.Error):
                    raise HTTPException(401, 'Invalid signing configuration.') from None
                expected = 'v1,' + base64.b64encode(hmac.new(key, signed_id.encode() + b'.' + timestamp.encode() + b'.' + body, hashlib.sha256).digest()).decode()
                valid = any(equals(expected, sig) for sig in headers.get('webhook-signature', '').split())
                if not re.fullmatch(IDENTIFIER, signed_id):
                    valid = False
            else:
                valid = hmac.compare_digest(secret, headers.get('x-gitlab-token', '').encode())
        elif headers.get('x-moyai-signature'):
            timestamp, signed_id = headers.get('x-moyai-timestamp', ''), headers.get('x-moyai-event-id', '')
            self.fresh(timestamp)
            expected = 'sha256=' + hmac.new(secret, timestamp.encode() + b'.' + signed_id.encode() + b'.' + body, hashlib.sha256).hexdigest()
            valid = equals(expected, headers.get('x-moyai-signature', '')) and bool(re.fullmatch(IDENTIFIER, signed_id))
        else:
            supplied = headers.get('x-webhook-secret', '') or headers.get('authorization', '').removeprefix('Bearer ')
            valid = hmac.compare_digest(secret, supplied.encode())
            signed_id = headers.get('x-moyai-event-id', '')  # Authenticated sender controls this ID.
            if signed_id and not re.fullmatch(IDENTIFIER, signed_id):
                raise HTTPException(400, 'Invalid event identifier.')
        if not valid:
            raise HTTPException(401, 'Invalid webhook signature or secret.')
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, 'Invalid webhook JSON.') from None
        if not isinstance(payload, dict):
            raise HTTPException(400, 'Webhook must be a JSON object.')
        if provider == 'linear':
            self.fresh(payload.get('webhookTimestamp'), milliseconds=True)
        # Body-only signatures do not authenticate delivery headers. Hash the
        # signed event itself so changing an unsigned ID cannot bypass dedup.
        stable = {k:v for k,v in payload.items() if provider != 'linear' or k != 'webhookTimestamp'}
        delivery = provider + ':' + (signed_id or hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest())
        context = self.match(row, provider, payload, headers.get('x-github-event', ''))
        return await self.accept(row, delivery, context)

    @staticmethod
    def fresh(value, *, milliseconds=False):
        try:
            valid = abs(time.time() - int(value) / (1000 if milliseconds else 1)) <= (60 if milliseconds else 300)
        except (ValueError, TypeError, OverflowError):
            valid = False
        if not valid:
            raise HTTPException(401, 'Webhook timestamp has expired.')

    def routes(self):
        router = APIRouter()

        @router.post('/hooks/automations/{automation_id}/{provider}', status_code=202)
        async def provider_webhook(automation_id: str, provider: str, request: Request):
            return await self.receive(automation_id, request, provider)

        @router.post('/hooks/automations/{automation_id}', status_code=202)
        async def webhook(automation_id: str, request: Request):
            return await self.receive(automation_id, request)

        @router.post('/api/automations/{automation_id}/webhook')
        async def setup(automation_id: str, body: WebhookSetup, request: Request):
            self.security.require(request, mutation=True)
            row = self.automations.row(automation_id)
            self.automations.require_owner(row, request)
            provider = self.provider(row, body.provider)
            if provider == 'slack':
                raise HTTPException(409, 'This automation does not use a webhook secret.')
            if provider in {'linear','pagerduty'} and not body.secret:
                raise HTTPException(422, 'Enter the signing secret from your provider webhook settings.')
            secret = body.secret or secrets.token_urlsafe(32)
            if len(secret) < 16:
                raise HTTPException(422, 'Use a signing secret of at least 16 characters.')
            with self.store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                changed = conn.execute("UPDATE automations SET paused=1,revision=revision+1,updated_at=?,sync_error='' WHERE id=? AND revision=?",
                                       (now(), automation_id, body.revision)).rowcount
                if not changed:
                    raise HTTPException(409, 'Automation changed. Refresh before configuring the webhook.')
                conn.execute('INSERT INTO automation_webhooks VALUES(?,?,?) ON CONFLICT(automation_id,provider) DO UPDATE SET encrypted=excluded.encrypted',
                             (automation_id, provider, self.security.encrypt(secret)))
            self.automations.next_sync = 0
            await self.automations.checkpoints.flush()
            return {'secret': secret if not body.secret else '', 'revision': body.revision + 1}

        @router.post('/api/automations/{automation_id}/test-event')
        async def test_event(automation_id: str, body: TestEvent, request: Request):
            self.security.require(request, mutation=True)
            row = self.automations.row(automation_id)
            self.automations.require_owner(row, request)
            provider = self.provider(row, body.provider)
            if len(json.dumps(body.payload)) > 262144:
                raise HTTPException(413, 'Sample payload is too large.')
            context = self.match(row, provider, body.payload, body.event_header)
            return {'matches': context is not None, 'context': context, 'started': False}


        return router
