"""Default-on owner DMs for sessions outside Slack.

Only current, durable attention states are eligible. Preparing a DM is safe to
retry; a message whose Slack receipt is lost is never replayed automatically.
"""
from .private_sinks import private_run
import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone

from .connector_errors import ConnectorError
from .db import database, now
from .session_events import BATCH_SIZE, RECONCILE_SECONDS

logger = logging.getLogger(__name__)

NOTIFICATION_TEXT = {
    'session.approval_required': ('Approval needed', 'This session needs an administrator to review an action in Moyai.'),
    'session.access_required': ('Access needed', 'This session is waiting for access. Provide it securely in Moyai to continue.'),
    'session.response_ready': ('Response ready', 'Moyai finished replying. Open the session to review the result or respond.'),
    'session.failed': ('Session needs attention', 'This session could not finish normally. Open it to review what happened and continue.'),
    'session.interrupted': ('Session needs attention', 'This session could not finish normally. Open it to review what happened and continue.'),
}


class SlackNotifications:
    def __init__(self, owner):
        self.owner, self.store = owner, owner.store
        self.events = self.store.session_events
        self.worker = None
        self.closing = False
        self.wake = asyncio.Event()
        self.delivery_lock = asyncio.Lock()
        if self.store.schema_updates:
            initialize_schema(self.store)

    def preference_owner_in(self, conn, run):
        user = conn.execute('SELECT * FROM users WHERE id=?', (run['owner_id'],)).fetchone()
        if not user:
            return None
        if user['kind'] == 'slack' and user['linked_user_id']:
            # A spend override alone does not transfer notification preferences.
            linked = conn.execute("SELECT * FROM users WHERE id=? AND kind IN ('google','cloudflare')", (user['linked_user_id'],)).fetchone()
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(user['profile_checked_at'])).total_seconds()
            except (ValueError, TypeError):
                age = -1
            if (linked and 0 <= age < 3600 and user['profile_eligible'] and not user['profile_conflict']
                    and user['link_status'] in {'linked', 'manual'} and user['email']
                    and user['email'] == linked['email']):
                return linked['id']
            # Do not fall back to a default-enabled Slack preference while a
            # linked account's opt-out cannot be safely resolved.
            return None
        return user['id'] if user['kind'] in {'google', 'cloudflare', 'slack'} else None

    def queue_in(self, conn, run, signal, user_id, team_id, status):
        preference = conn.execute('SELECT slack_notifications_since FROM user_preferences WHERE user_id=?', (user_id,)).fetchone()
        rollout = conn.execute('SELECT enabled_since FROM slack_notification_state WHERE id=1').fetchone()
        if signal.occurred_at < rollout['enabled_since'] or (preference and signal.occurred_at < preference['slack_notifications_since']):
            status = 'skipped'  # Neither rollout nor re-enabling replays historical events.
        conn.execute('''INSERT INTO slack_notifications
            (run_id,event_key,target_run_id,user_id,owner_id,team_id,status,created_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,event_key) DO NOTHING''',
            (run['id'], signal.key, signal.target_run_id, user_id, run['owner_id'], team_id, status, now()))

    def preferences_changed_in(self, conn, user_id):
        conn.execute("UPDATE slack_notifications SET status='skipped' WHERE user_id=? AND status IN ('pending','preparing')", (user_id,))

    def installation(self):
        bot = self.owner.connectors.slack_installation()
        allowed = (self.owner.settings.slack_bot_enabled and self.owner.settings.slack_dm_enabled
                   and bot.get('installed') and self.owner.connectors.allowed('slack_send')
                   and {'chat:write', 'im:write'} <= set(bot.get('scopes', [])))
        return bot, allowed

    def enabled_in(self, conn, run, user_id):
        if private_run(self.store, run['id'], conn):
            return False
        preference = conn.execute('SELECT slack_notifications FROM user_preferences WHERE user_id=?', (user_id,)).fetchone()
        on_slack = conn.execute('''SELECT 1 FROM slack_threads WHERE run_id=?
            UNION ALL SELECT 1 FROM slack_events WHERE run_id=? LIMIT 1''', (run['id'], run['id'])).fetchone()
        return ((not preference or preference['slack_notifications']) and not on_slack
                and self.preference_owner_in(conn, run) == user_id)

    def collect(self):
        bot, allowed = self.installation()
        with self.store.connect() as conn:
            conn.begin_write()
            def queue(run, signal):
                user_id = self.preference_owner_in(conn, run)
                if user_id and self.enabled_in(conn, run, user_id):
                    self.queue_in(conn, run, signal, user_id, bot.get('team_id') or '',
                                  'pending' if allowed else 'skipped')
            return self.events.consume_in(conn, 'slack_notifications', queue)

    def current(self, row):
        bot, allowed = self.installation()
        with self.store.connect() as conn:
            delivery = conn.execute('SELECT status FROM slack_notifications WHERE id=?', (row['id'],)).fetchone()
            if not delivery or delivery['status'] not in {'pending', 'preparing', 'sending'}:
                return None
            run = conn.execute('SELECT * FROM runs WHERE id=?', (row['run_id'],)).fetchone()
            if (not run or not allowed or bot.get('team_id') != row['team_id']
                    or run['owner_id'] != row['owner_id'] or not self.enabled_in(conn, run, row['user_id'])):
                return None
            signal = self.events.attention_in(conn, run)
            return (dict(run), signal, bot) if signal and signal.key == row['event_key'] else None

    def payload(self, run, signal, channel):
        label, description = NOTIFICATION_TEXT[signal.kind]
        title = self.owner.chat.scrub(run['display_title'] or run['prompt'])[:160].strip() or 'Moyai session'
        url = self.owner.settings.public_url.rstrip('/') + '/#run=' + signal.target_run_id
        return {'channel': channel, 'text': html.escape(f'{label}: {title}', quote=False),
                'parse': 'none', 'unfurl_links': False, 'unfurl_media': False,
                'blocks': [
                    {'type': 'header', 'text': {'type': 'plain_text', 'text': label}},
                    {'type': 'section', 'text': {'type': 'plain_text', 'text': title}},
                    {'type': 'section', 'text': {'type': 'plain_text', 'text': description}},
                    {'type': 'actions', 'elements': [{'type': 'button', 'action_id': 'open_session_notification',
                        'text': {'type': 'plain_text', 'text': 'Open session'}, 'style': 'primary', 'url': url}]},
                    {'type': 'context', 'elements': [{'type': 'plain_text', 'text': 'Manage Slack notifications in Moyai Settings.'}]},
                ]}

    async def deliver_one(self):
        async with self.delivery_lock:
            rows = await database(self.store.rows, "SELECT * FROM slack_notifications WHERE status='pending' AND retry_at<=? ORDER BY id LIMIT 1", (time.time(),))
            if not rows:
                return False
            row = rows[0]
            sending = False
            try:
                claimed = await database(self.store.execute, "UPDATE slack_notifications SET status='preparing' WHERE id=? AND status='pending'", (row['id'],))
                if not claimed:
                    return True
                async with asyncio.timeout(20):
                    current = await database(self.current, row)
                    if not current or not self.owner.identities:
                        await self.set_status(row, 'skipped')
                        return True
                    run, signal, bot = current
                    recipient = await self.owner.identities.requester(run, session_owner=True)
                    audience = {item.strip() for item in self.owner.settings.slack_session_users.split(',')}
                    if recipient['team_id'] != row['team_id'] or ('*' not in audience and recipient['user_id'] not in audience):
                        await self.set_status(row, 'skipped')
                        return True
                    token = await self.owner.connectors.slack_bot_token()
                    headers = {'Authorization': f'Bearer {token}'}
                    opened = await self.owner.connectors.request('POST', 'https://slack.com/api/conversations.open',
                        headers=headers, json={'users': recipient['user_id']})
                    channel = (opened.get('channel') or {}).get('id')
                    if not isinstance(channel, str) or not re.fullmatch(r'D[A-Z0-9]{7,30}', channel):
                        raise ConnectorError('Slack did not return a DM conversation.')
                    await database(self.owner.identities.check_requester, run, bot)
                    if await self.owner.identities.requester(run, session_owner=True) != recipient:
                        raise ConnectorError('The notification recipient changed.')
                    if not await database(self.current, row):
                        await self.set_status(row, 'skipped')
                        return True
                    await self.set_status(row, 'sending')
                    await self.owner.checkpoints.flush()
                    # Recheck after checkpoint awaits, before the only message send.
                    await database(self.owner.identities.check_requester, run, bot)
                    if not await database(self.current, row):
                        await self.set_status(row, 'skipped')
                        return True
                    sending = True
                    result = await self.owner.connectors.request('POST', 'https://slack.com/api/chat.postMessage',
                        headers=headers, json=self.payload(run, signal, channel))
                    if not result.get('ts'):
                        raise ConnectorError('Slack returned no message receipt.')
            except asyncio.CancelledError:
                await self.set_status(row, 'uncertain' if sending else 'pending')
                raise
            except Exception as exc:
                if sending:
                    await self.set_status(row, 'uncertain')
                else:
                    await database(self.store.execute, """UPDATE slack_notifications SET
                        status=CASE WHEN attempts>=2 THEN 'failed' ELSE 'pending' END,
                        attempts=attempts+1,retry_at=? WHERE id=? AND status IN ('preparing','sending')""", (time.time() + 60, row['id']))
                logger.warning('Slack session notification %s: %s', 'uncertain' if sending else 'preparation failed', type(exc).__name__)
            else:
                # Keep confirmed receipt persistence outside the ambiguous-send
                # handler: cancellation drains this write without changing sent.
                await database(self.store.execute, "UPDATE slack_notifications SET status='sent',slack_ts=? WHERE id=?", (result['ts'], row['id']))
            finally:
                await self.owner.checkpoints.flush()
            return True

    async def set_status(self, row, status):
        await database(self.store.execute, "UPDATE slack_notifications SET status=? WHERE id=? AND status IN ('pending','preparing','sending')", (status, row['id']))

    def next_delay(self):
        rows = self.store.rows("SELECT MIN(retry_at) AS ready FROM slack_notifications WHERE status='pending'")
        ready = rows[0]['ready']
        return min(RECONCILE_SECONDS, max(0, ready - time.time())) if ready is not None else RECONCILE_SECONDS

    async def watch(self):
        async with self.events.wake_on_commit(self.wake):
            while not self.closing:
                self.wake.clear()
                try:
                    count = await database(self.collect)
                    if self.closing:
                        break
                    if await self.deliver_one():
                        # Pace message delivery; this is not a session poll.
                        await asyncio.sleep(1)
                        continue
                    if count == BATCH_SIZE:
                        continue
                    delay = await database(self.next_delay)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning('Slack notification worker will retry after %s', type(exc).__name__)
                    delay = RECONCILE_SECONDS
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=delay)
                except TimeoutError:
                    pass

    def recover(self):
        if self.worker and not self.worker.done():
            return
        self.store.execute("UPDATE slack_notifications SET status='uncertain' WHERE status='sending'")
        self.store.execute("UPDATE slack_notifications SET status='pending' WHERE status='preparing'")
        if not self.worker or self.worker.done():
            self.closing = False
            self.worker = asyncio.create_task(self.watch())

    def begin_drain(self):
        self.closing = True
        self.wake.set()

    async def shutdown(self, *, graceful=False):
        if self.worker:
            if graceful:
                self.begin_drain()
            else:
                self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


from .schema_initializers import initialize_slack_notifications as initialize_schema
