"""Resolve Slack senders outside webhook acknowledgement and sandbox execution.

Profiles can create an email-labelled local identity; only Google OIDC can
authenticate a web session. This is JIT matching, not SCIM lifecycle management.
"""
import asyncio
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from .db import now
from .connector_errors import ConnectorError

PROFILE_SCOPES = {'users:read', 'users:read.email'}
# Refresh with headroom for worker delays and retries, without extending the
# authorization lifetime used by personal memory, skills and credentials.
PROFILE_MAX_AGE_SECONDS = 3600
PROFILE_REFRESH_SECONDS = PROFILE_MAX_AGE_SECONDS // 2


class SlackIdentities:
    def __init__(self, store, connectors, settings, security, checkpoints, same_requester: Callable[[str, str], bool]):
        self.store, self.connectors, self.settings = store, connectors, settings
        self.security, self.checkpoints = security, checkpoints
        self.wake = asyncio.Event()
        self.worker = None
        self.lock = asyncio.Lock()
        self.same_requester = same_requester

    def check_requester(self, run: Mapping[str, object], installation: Mapping[str, object]) -> None:
        current = self.store.run(run['id'])
        keys = ('active_user_id', 'active_message_id', 'owner_id', 'chat_enabled', 'token_hash', 'status')
        bot = self.connectors.slack_installation()
        if not current or any(current.get(key) != run.get(key) for key in keys):
            raise ConnectorError('The requester or turn changed. Resolve your Slack identity again.')
        if any(bot.get(key) != installation.get(key) for key in ('team_id', 'user_id')) or not bot.get('installed'):
            raise ConnectorError('The Slack workspace or bot changed. Resolve your Slack identity again.')
        if not self.connectors.allowed('slack_me'):
            raise ConnectorError('The Slack connection is disabled.')

    async def requester(self, run: Mapping[str, object] | None) -> dict[str, str]:
        """Resolve the broker's requester, never a caller-supplied email or accounting override."""
        actor = (run or {}).get('active_user_id')
        if not actor and run and not run.get('chat_enabled'):
            actor = run.get('owner_id')
        rows = self.store.rows('SELECT * FROM users WHERE id=?', (actor,)) if isinstance(actor, str) and actor else []
        if not rows or rows[0]['kind'] not in {'google', 'slack'} or not run:
            raise ConnectorError('Slack identity requires an identified requester. Sign in with Google or message the bot in Slack.')
        user = rows[0]
        installation = self.connectors.slack_installation()
        self.check_requester(run, installation)
        team = installation.get('team_id')
        if not isinstance(team, str) or not re.fullmatch(r'T[A-Z0-9]{7,30}', team):
            raise ConnectorError('Reconnect Slack to identify the bot workspace.')
        if user['kind'] == 'slack':
            recipient = user['id'].removeprefix(f'slack:{team}:')
            if not re.fullmatch(r'[UW][A-Z0-9]{7,30}', recipient):
                raise ConnectorError('Your Slack identity belongs to a different workspace.')
            return {'user_id': recipient, 'team_id': team, 'email': user['email'], 'name': user['name']}
        email = user['email']
        if not email or email.rpartition('@')[2] not in self.settings.google_domains():
            raise ConnectorError('Your verified work email is no longer allowed in this workspace.')
        if not self.status()['ready']:
            raise ConnectorError('Enable Slack identity linking and reconnect the bot with users:read and users:read.email.')

        def candidates():
            return self.store.rows("""SELECT * FROM users WHERE kind='slack' AND id LIKE ?
                AND (linked_user_id=? OR email=?)""", (f'slack:{team}:%', actor, email))

        matches = candidates()
        if not matches:
            # Slack's lookup is scoped to the installed bot and the trusted SSO email.
            token = await self.connectors.slack_bot_token()
            self.check_requester(run, installation)
            result = await self.connectors.request('GET', 'https://slack.com/api/users.lookupByEmail',
                headers={'Authorization': f'Bearer {token}'}, params={'email': email}, allowed_errors=('users_not_found',))
            self.check_requester(run, installation)
            profile = result.get('user') or {}
            recipient = profile.get('id') if isinstance(profile, dict) else None
            if (not result.get('ok') or not isinstance(recipient, str)
                    or not re.fullmatch(r'[UW][A-Z0-9]{7,30}', recipient) or profile.get('team_id') != team):
                raise ConnectorError('No Slack account was verified for your work email in this workspace.')
            with self.store.connect() as conn:
                identity = self.store.slack_identity_in(conn, team, recipient)
            matches = self.store.rows('SELECT * FROM users WHERE id=?', (identity,))
        if len(matches) != 1:
            raise ConnectorError('Your Slack identity is ambiguous. Review the linked accounts before sending a DM.')
        match = matches[0]
        if (match['profile_conflict'] or match['link_status'] == 'email_changed'
                or (match['email'] and match['email'] != email)):
            raise ConnectorError('Your Slack account link needs review before it can be used for a DM.')
        if not self.same_requester(actor, match['id']):
            await self.resolve(match, team)
            await self.checkpoints.flush()
        self.check_requester(run, installation)
        # Re-read after provider awaits; historical/manual spend links alone never select a recipient.
        google = self.store.rows("SELECT id FROM users WHERE kind='google' AND email=?", (email,))
        matches = candidates()
        if len(google) != 1 or google[0]['id'] != actor or len(matches) != 1:
            raise ConnectorError('Your Slack identity is ambiguous. Review the linked accounts before sending a DM.')
        match = matches[0]
        if (match['linked_user_id'] != actor or match['link_status'] not in {'linked', 'manual'}
                or not self.same_requester(actor, match['id'])):
            raise ConnectorError('Your Slack account link could not be verified. Check the profile email or refresh the account link.')
        return {'user_id': match['id'].removeprefix(f'slack:{team}:'), 'team_id': team,
                'email': email, 'name': user['name']}

    def status(self):
        bot = self.connectors.slack_installation()
        missing = sorted(PROFILE_SCOPES - set(bot.get('scopes', [])))
        enabled = self.settings.slack_identity_linking_enabled
        return {'enabled': enabled, 'ready': enabled and bool(bot.get('installed')) and not missing and self.connectors.policy('slack')['enabled'],
                'missing_scopes': missing, 'domains': sorted(self.settings.google_domains()),
                'team_id': bot.get('team_id')}

    def start(self):
        self.worker = asyncio.create_task(self.watch())

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    async def watch(self):
        while True:
            self.wake.clear()
            try:
                await self.sync_due()
            except Exception:
                # Retry persisted identities on the next pass. Never log profile
                # responses or credentials, and never fail Slack session work.
                pass
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except TimeoutError:
                pass

    async def sync_due(self):
        async with self.lock:
            await self.sync_profiles_due()
            if await self.store.slack_mentions.sync_due(self.connectors):
                await self.checkpoints.flush()

    async def sync_profiles_due(self):
        status = self.status()
        if not status['ready']:
            return
        team = status['team_id']
        refresh_before = datetime.fromtimestamp(time.time() - PROFILE_REFRESH_SECONDS, timezone.utc).isoformat()
        # Only senders who actually have a session/message, not unrelated
        # channel participants incidentally seen by the Slack event router.
        rows = self.store.rows("""SELECT u.* FROM users u WHERE kind='slack'
            AND id LIKE ? AND (profile_next_check<=?
                OR (profile_eligible=1 AND profile_checked_at<=?))
            AND (EXISTS(SELECT 1 FROM messages WHERE user_id=u.id)
                 OR EXISTS(SELECT 1 FROM runs WHERE owner_id=u.id)
                 OR EXISTS(SELECT 1 FROM model_requests WHERE user_id=u.id))
            ORDER BY profile_next_check,id LIMIT 10""", (f'slack:{team}:%', int(time.time()), refresh_before))
        for row in rows:
            await self.resolve(row, team)
        if rows:
            await self.checkpoints.flush()
        if len(rows) == 10:
            self.wake.set()

    async def resolve(self, row, team):
        user = row['id'].removeprefix(f'slack:{team}:')
        if not re.fullmatch(r'[UW][A-Z0-9]{7,30}', user):
            self.store.execute("UPDATE users SET profile_next_check=?,link_status='ineligible',profile_eligible=0 WHERE id=?", (int(time.time()) + 86400, row['id']))
            return
        checked = now()
        try:
            async with asyncio.timeout(8):
                token = await self.connectors.slack_bot_token()
                result = await self.connectors.request('GET', 'https://slack.com/api/users.info',
                    headers={'Authorization': f'Bearer {token}'}, params={'user': user},
                    allowed_errors=('user_not_found', 'missing_scope', 'ratelimited'))
            if not self.status()['ready'] or self.status()['team_id'] != team:
                return  # Connection/policy changed while the request was in flight.
            if not result.get('ok'):
                raise ValueError('Profile unavailable')
            profile = result.get('user')
            if not isinstance(profile, dict) or profile.get('id') != user or profile.get('team_id') != team:
                raise ValueError('Profile identity did not match')
            eligible = not any(profile.get(key) for key in ('deleted', 'is_bot', 'is_app_user', 'is_restricted', 'is_ultra_restricted', 'is_stranger'))
            data = profile.get('profile') or {}
            email = data.get('email', '')
            email = email.strip().lower() if isinstance(email, str) else ''
            eligible &= bool(re.fullmatch(r'[^\s@]+@[^\s@]+', email)) and email.rpartition('@')[2] in self.settings.google_domains()
            name = data.get('real_name') or data.get('display_name') or profile.get('real_name') or row['name']
            name = str(name)[:160]
        except Exception:
            self.store.execute("UPDATE users SET link_status=CASE WHEN link_method='manual' THEN 'manual' WHEN linked_user_id IS NOT NULL THEN 'review' ELSE 'unavailable' END,profile_eligible=0,profile_next_check=? WHERE id=?", (int(time.time()) + 300, row['id']))
            return
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
            # Do not move past spend to a different human after a profile email
            # change. Keep the first matched provider IDs and flag a review.
            changed = bool(current['email'] and email and current['email'] != email)
            blocked = changed or bool(current['profile_conflict'])
            status = 'email_changed' if blocked else 'pending_profile' if eligible else 'ineligible'
            if current['link_method'] == 'manual':
                status = 'manual'
            elif current['linked_user_id'] and not eligible:
                status = 'review'
            # Eligible profiles saved by older releases can still carry a daily
            # timer; sync_due also considers their last successful check above.
            refresh_after = PROFILE_REFRESH_SECONDS if eligible and not blocked else 86400
            conn.execute('UPDATE users SET email=?,name=?,profile_eligible=?,profile_conflict=?,profile_checked_at=?,profile_next_check=?,link_status=?,updated_at=? WHERE id=?',
                         (email if eligible else current['email'], name, int(eligible and not blocked), int(blocked), checked,
                          int(time.time()) + refresh_after, status, now(), row['id']))
            for value in {email, current['email']}:
                self.store.reconcile_email_in(conn, value)

    def routes(self):
        router = APIRouter()

        @router.get('/api/admin/identities/status')
        async def status(request: Request):
            self.security.require(request, admin=True)
            return self.status()

        @router.post('/api/admin/identities/refresh')
        async def refresh(request: Request):
            self.security.require(request, admin=True, mutation=True)
            if not self.status()['ready']:
                raise HTTPException(409, 'Reconnect Slack with users:read and users:read.email, and enable the Slack connection.')
            self.store.execute("UPDATE users SET profile_next_check=0 WHERE kind='slack' AND id LIKE ?", (f"slack:{self.status()['team_id']}:%",))
            self.wake.set()
            return {'queued': True}

        return router
