"""Resolve Slack senders outside webhook acknowledgement and sandbox execution.

Profiles can create an email-labelled local identity; only Google OIDC can
authenticate a web session. This is JIT matching, not SCIM lifecycle management.
"""
import asyncio
import re
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from .db import now

PROFILE_SCOPES = {'users:read', 'users:read.email'}
# Refresh with headroom for worker delays and retries, without extending the
# authorization lifetime used by personal memory, skills and credentials.
PROFILE_MAX_AGE_SECONDS = 3600
PROFILE_REFRESH_SECONDS = PROFILE_MAX_AGE_SECONDS // 2


class SlackIdentities:
    def __init__(self, store, connectors, settings, security, checkpoints):
        self.store, self.connectors, self.settings = store, connectors, settings
        self.security, self.checkpoints = security, checkpoints
        self.wake = asyncio.Event()
        self.worker = None
        self.lock = asyncio.Lock()

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
