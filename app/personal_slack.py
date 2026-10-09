"""Owner-scoped Slack authorization and read selection. Never expose grant material."""
import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .connector_errors import ConnectorError
from .security import digest

LIMITATION = 'Personal Slack reads require your private web chat. Start a private chat, or disconnect personal Slack in Connections to use organization access.'
FAILURE = 'Personal Slack access failed. Retry, or reconnect personal Slack in Connections. Organization access was not used.'
KEYS = ('active_user_id', 'active_message_id', 'owner_id', 'private_owner_id', 'chat_enabled', 'token_hash', 'status', 'plugins', 'parent_run_id', 'deleted_at')


@dataclass
class Selection:
    owner: str | None
    revision: str | None
    run: dict | None
    capability: str
    credentials: dict = field(repr=False)


class PersonalSlack:
    def __init__(self, store, security, settings, connectors, same_requester=None):
        self.store, self.security, self.settings, self.connectors = store, security, settings, connectors
        self.same_requester = same_requester
        self.locks = {}
        store.execute('''CREATE TABLE IF NOT EXISTS personal_slack_grants (
            owner_id TEXT PRIMARY KEY, revision TEXT NOT NULL, encrypted TEXT,
            label TEXT NOT NULL DEFAULT '', health TEXT NOT NULL DEFAULT 'absent')''')
        store.execute('''CREATE TABLE IF NOT EXISTS personal_slack_states (
            state_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL, owner_id TEXT NOT NULL,
            revision TEXT NOT NULL, expires REAL NOT NULL)''')

    def owner(self, actor):
        rows = self.store.rows('SELECT * FROM users WHERE id=?', (actor,)) if actor else []
        if not rows:
            if actor and self.configured(actor):
                raise ConnectorError('Sign in again with your individual work account.')
            return None
        user = rows[0]
        if user['kind'] in {'google', 'cloudflare'}:
            if (not self.settings.person_login_enabled()
                    or user['email'].rpartition('@')[2] not in self.settings.google_domains()):
                if self.configured(user['id']):
                    raise ConnectorError('Sign in again with your individual work account.')
                return None
            return user['id']
        if user['kind'] == 'slack':
            match = self.same_requester or getattr(self.connectors.slack_identities, 'same_requester', None)
            individuals = self.store.rows("SELECT id,email FROM users WHERE kind IN ('google','cloudflare')")
            candidates = [u['id'] for u in individuals if match and match(u['id'], actor)]
            if len(candidates) != 1:
                # A never-linked Slack sender has no personal owner/grant.
                # A stale or conflicting association is different: do not
                # silently treat it as absence and select shared credentials.
                associated = user.get('linked_user_id') or any(
                    user.get('email') and u['email'] == user['email'] for u in individuals)
                if (not candidates and not associated and not user.get('profile_conflict')
                        and user.get('link_status') not in {'review', 'email_changed'}):
                    return None
                raise ConnectorError('Verify your individual Slack account link before reading Slack.')
            return self.owner(candidates[0])
        return None

    def row(self, owner):
        rows = self.store.rows('SELECT * FROM personal_slack_grants WHERE owner_id=?', (owner,)) if owner else []
        return rows[0] if rows else None

    def configured(self, owner):
        row = self.row(owner)
        return bool(row and row['encrypted'] is not None)

    def revision(self, owner):
        self.store.execute('INSERT OR IGNORE INTO personal_slack_grants(owner_id,revision) VALUES(?,?)', (owner, secrets.token_hex(24)))
        return self.row(owner)['revision']

    def save(self, owner, credentials, label, revision):
        changed = self.store.execute('''UPDATE personal_slack_grants SET encrypted=?,label=?,health='healthy',revision=?
            WHERE owner_id=? AND revision=?''', (self.security.encrypt(json.dumps(credentials)), label,
                secrets.token_hex(24), owner, revision))
        if not changed:
            raise ConnectorError('Personal Slack changed. Retry from Connections.')

    def record_error(self, owner, revision):
        self.store.execute("UPDATE personal_slack_grants SET health='error' WHERE owner_id=? AND revision=?", (owner, revision))

    def disconnect(self, owner):
        self.revision(owner)
        # Disconnect linearizes at this write, including when another process
        # refreshed between ensuring the row exists and revoking it.
        self.store.execute("UPDATE personal_slack_grants SET encrypted=NULL,label='',health='absent',revision=? WHERE owner_id=?",
                           (secrets.token_hex(24), owner))

    def selected_owner(self, run):
        if not run:
            return None
        actor = run.get('active_user_id') or run.get('owner_id')
        return self.owner(actor)

    def eligible(self, run, owner):
        if (not run or run.get('private_owner_id') != owner or not run.get('chat_enabled')
                or run.get('parent_run_id') or self.store.slack_source(run['id'])
                or self.store.rows('SELECT 1 FROM automation_runs WHERE run_id=?', (run['id'],))):
            raise ConnectorError(LIMITATION)

    def recheck(self, selection):
        run = selection.run
        if not self.connectors.policy('slack')['enabled']:
            raise ConnectorError('The Slack connection is disabled.')
        if run:
            current = self.store.run(run['id'])
            if (not current or any(current.get(k) != run.get(k) for k in KEYS)
                    or self.selected_owner(current) != selection.owner):
                raise ConnectorError('The requester or turn changed. Retry your Slack read.')
            if 'plugins' in current and 'slack' not in current['plugins']:
                raise ConnectorError('Select Slack for this chat before reading it.')
        row = self.row(selection.owner)
        revision = row['revision'] if row else None
        if revision != selection.revision:
            raise ConnectorError('Personal Slack changed. Retry your Slack read.')
        if row and row['encrypted'] is not None:
            self.eligible(run, selection.owner)

    async def select(self, run=None, capability='slack_thread'):
        owner = self.selected_owner(run)
        row = self.row(owner)
        selected = Selection(owner, row['revision'] if row else None, dict(run) if run else None, capability, {})
        self.recheck(selected)
        if row and row['encrypted'] is not None:
            selected.credentials, selected.revision = await self.credentials(owner, expected_revision=selected.revision)
        else:
            selected.credentials = await self.connectors.credentials('slack')
        self.recheck(selected)
        return selected

    async def exchange(self, *, code=None, refresh_token=None):
        payload = {'client_id': self.settings.slack_client_id, 'client_secret': self.settings.slack_client_secret}
        if refresh_token:
            payload.update(grant_type='refresh_token', refresh_token=refresh_token)
        else:
            payload.update(code=code, redirect_uri=self.redirect_uri())
        result = await self.connectors.request('POST', 'https://slack.com/api/oauth.v2.access', data=payload)
        user = dict(result.get('authed_user') or result)
        if not user.get('access_token') or user.get('token_type') != 'user':
            raise ConnectorError(FAILURE)
        return {k: v for k, v in {**user, 'team_id': (result.get('team') or {}).get('id'),
                'user_id': user.get('id'), 'expires_at': time.time() + user['expires_in'] if user.get('expires_in') else 0}.items()
                if k in {'access_token', 'refresh_token', 'scope', 'team_id', 'user_id', 'expires_at'}}

    async def verify(self, credentials):
        result = await self.connectors.request('POST', 'https://slack.com/api/auth.test',
            headers={'Authorization': 'Bearer ' + credentials['access_token']})
        if not result.get('user_id') or not result.get('team_id'):
            raise ConnectorError(FAILURE)
        for key in ('team_id', 'user_id'):
            if credentials.get(key) and credentials[key] != result[key]:
                raise ConnectorError(FAILURE)
            credentials[key] = result[key]
        return str(result.get('team', 'Slack')) + ' · ' + str(result.get('user', result['user_id']))

    async def credentials(self, owner, expected_revision=None):
        async with self.locks.setdefault(owner, asyncio.Lock()):
            row = self.row(owner)
            if not row or row['encrypted'] is None or (expected_revision is not None and row['revision'] != expected_revision):
                raise ConnectorError('Personal Slack changed. Retry.')
            try:
                value = json.loads(self.security.decrypt(row['encrypted']))
                if value.get('expires_at') and value['expires_at'] < time.time() + 90:
                    if not value.get('refresh_token'):
                        raise ConnectorError(FAILURE)
                    fresh = await self.exchange(refresh_token=value['refresh_token'])
                    fresh['team_id'], fresh['user_id'] = value['team_id'], value['user_id']
                    label = await self.verify(fresh)
                    self.save(owner, fresh, label, row['revision'])
                    value, row = fresh, self.row(owner)
                if not value.get('access_token'):
                    raise ConnectorError(FAILURE)
                return value, row['revision']
            except Exception:
                self.record_error(owner, row['revision'])
                raise ConnectorError(FAILURE) from None

    def redirect_uri(self):
        return self.settings.public_url.rstrip('/') + '/oauth/slack/personal/callback'

    def status(self, owner):
        row = self.row(owner)
        connected = self.configured(owner)
        return {'available': bool(owner), 'connected': connected,
                'oauth_configured': self.connectors.configured_oauth('slack'),
                'label': row['label'] if connected else '', 'health': row['health'] if connected else 'absent',
                'effective_source': 'personal' if connected else 'organization' if self.store.rows("SELECT 1 FROM connections WHERE provider='slack'") else 'none',
                'limitation': LIMITATION}

    def routes(self, app, checkpoints=None):
        async def flush():
            if checkpoints:
                await checkpoints.flush()

        def actor(request, mutation=False, required=True):
            sid = self.security.require(request, mutation=mutation)
            try:
                owner = self.owner(self.store.identity(self.security.session_info(request)))
            except ConnectorError:
                raise HTTPException(403, 'Sign in with a verified individual work account.') from None
            if required and not owner:
                raise HTTPException(403, 'Sign in with a verified individual work account.')
            return sid, owner

        @app.get('/api/connections/slack/personal')
        async def status(request: Request):
            _, owner = actor(request, required=False)
            return self.status(owner)

        @app.post('/api/connections/slack/personal/oauth')
        async def start(request: Request):
            sid, owner = actor(request, True)
            if not self.connectors.configured_oauth('slack'):
                raise HTTPException(409, 'Configure the Slack OAuth client first.')
            state = secrets.token_urlsafe(32)
            self.store.execute('INSERT INTO personal_slack_states VALUES(?,?,?,?,?)',
                (digest(state), digest(sid), owner, self.revision(owner), time.time() + 600))
            await flush()
            return {'url': 'https://slack.com/oauth/v2/authorize?' + urlencode({
                'client_id': self.settings.slack_client_id, 'redirect_uri': self.redirect_uri(), 'state': state,
                'user_scope': 'search:read,channels:history,groups:history,im:history,mpim:history'})}

        @app.get('/oauth/slack/personal/callback')
        async def callback(request: Request, state: str = '', code: str = '', error: str = ''):
            sid, owner = actor(request)
            with self.store.connect() as conn:
                row = conn.execute('DELETE FROM personal_slack_states WHERE state_hash=? AND session_hash=? AND owner_id=? AND expires>? RETURNING revision',
                    (digest(state), digest(sid), owner, time.time())).fetchone()
            await flush()
            if not row:
                raise HTTPException(400, 'Expired or invalid personal Slack request. Start again.')
            if error or not code:
                return RedirectResponse('/?connection=cancelled#connections', status_code=303)
            try:
                credentials = await self.exchange(code=code)
                label = await self.verify(credentials)
                actor(request)
                self.save(owner, credentials, label, row['revision'])
            except Exception:
                raise HTTPException(409, FAILURE) from None
            await flush()
            return RedirectResponse('/?connection=success#connections', status_code=303)

        @app.delete('/api/connections/slack/personal')
        async def disconnect(request: Request):
            _, owner = actor(request, True)
            self.disconnect(owner)
            await flush()
            return self.status(owner)

        @app.post('/api/connections/slack/personal/check')
        async def check(request: Request):
            _, owner = actor(request, True)
            revision = None
            try:
                credentials, revision = await self.credentials(owner)
                label = await self.verify(credentials)
                actor(request)
                self.save(owner, credentials, label, revision)
            except Exception:
                if revision:
                    self.record_error(owner, revision)
                await flush()
                raise HTTPException(409, FAILURE) from None
            await flush()
            return self.status(owner)
