"""Opt-in owner DMs, independent of a session's Slack conversation binding.

Only current, durable attention states are eligible. Preparing a DM is safe to
retry; a message whose Slack receipt is lost is never replayed automatically.
"""
import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone

from .connector_errors import ConnectorError
from .db import database, now

logger = logging.getLogger(__name__)


class SlackNotifications:
    def __init__(self, owner):
        self.owner, self.store = owner, owner.store
        self.worker = None
        self.delivery_lock = asyncio.Lock()
        if self.store.schema_updates:
            initialize_schema(self.store)

    def preference_owner_in(self, conn, run):
        user = conn.execute('SELECT * FROM users WHERE id=?', (run['owner_id'],)).fetchone()
        if not user:
            return None
        if user['kind'] == 'slack' and user['linked_user_id']:
            # A spend override alone does not opt another person into DMs.
            linked = conn.execute("SELECT * FROM users WHERE id=? AND kind IN ('google','cloudflare')", (user['linked_user_id'],)).fetchone()
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(user['profile_checked_at'])).total_seconds()
            except (ValueError, TypeError):
                age = -1
            if (linked and 0 <= age < 3600 and user['profile_eligible'] and not user['profile_conflict']
                    and user['link_status'] in {'linked', 'manual'} and user['email']
                    and user['email'] == linked['email']):
                return linked['id']
        return user['id'] if user['kind'] in {'google', 'cloudflare', 'slack'} else None

    def signal_in(self, conn, run):
        if run['deleted_at'] or run['deletion_requested_at'] or run['status'] in {'stopping', 'cancelled'}:
            return None
        # Child requests require the root owner's attention, but child answers
        # are summarized by the parent and must not generate a stream of DMs.
        pending = conn.execute("""SELECT a.id,a.run_id,a.created_at FROM approvals a
            JOIN runs r ON r.id=a.run_id JOIN run_roots roots ON roots.run_id=r.id
            WHERE roots.root_id=? AND a.status='pending' AND r.deleted_at=''
            AND r.deletion_requested_at='' AND r.status IN ('running','reconnecting','awaiting_approval')
            ORDER BY a.created_at,a.id LIMIT 1""", (run['id'],)).fetchone()
        if pending:
            return ('approval:' + pending['id'], pending['run_id'], 'Approval needed',
                    'This session needs an administrator to review an action in Moyai.', pending['created_at'])
        pending = conn.execute("""SELECT q.id,q.run_id,q.generation,q.created_at,r.updated_at FROM credential_requests q
            JOIN runs r ON r.id=q.run_id JOIN run_roots roots ON roots.run_id=r.id
            WHERE roots.root_id=? AND q.status='pending' AND r.deleted_at=''
            AND r.deletion_requested_at='' AND r.status NOT IN ('stopping','cancelled','failed','interrupted','idle','completed')
            ORDER BY q.created_at,q.id LIMIT 1""", (run['id'],)).fetchone()
        if pending:
            return (f"credential:{pending['id']}:{pending['generation']}", pending['run_id'], 'Access needed',
                    'This session is waiting for access. Provide it securely in Moyai to continue.',
                    pending['updated_at'] if pending['generation'] else pending['created_at'])
        if run['status'] not in {'idle', 'completed', 'failed', 'interrupted'}:
            return None
        if conn.execute("SELECT 1 FROM messages WHERE run_id=? AND status IN ('queued','running') LIMIT 1", (run['id'],)).fetchone():
            return None
        answer = conn.execute("SELECT id,status,created_at FROM messages WHERE run_id=? AND role='assistant' AND status!='steered' ORDER BY id DESC LIMIT 1", (run['id'],)).fetchone()
        if answer and answer['status'] == 'saving':
            return None
        turn = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages WHERE run_id=? AND role='user'", (run['id'],)).fetchone()[0]
        outcome = run['status'] if run['status'] in {'failed', 'interrupted'} else 'ready'
        key = f"turn:{turn}:{outcome}:{answer['id'] if answer else 0}"
        if run['status'] in {'failed', 'interrupted'} or (answer and answer['status'] == 'save_failed'):
            return (key, run['id'], 'Session needs attention',
                    'This session could not finish normally. Open it to review what happened and continue.', run['updated_at'])
        if answer or run['status'] == 'completed':
            return (key, run['id'], 'Response ready',
                    'Moyai finished replying. Open the session to review the result or respond.',
                    answer['created_at'] if answer else run['updated_at'])
        return None

    def queue_in(self, conn, run, signal, user_id, team_id, status):
        preference = conn.execute('SELECT slack_notifications_since FROM user_preferences WHERE user_id=?', (user_id,)).fetchone()
        if preference and signal[4] < preference['slack_notifications_since']:
            status = 'skipped'  # Covers Slack identities first linked after opt-in.
        conn.execute('''INSERT INTO slack_notifications
            (run_id,event_key,target_run_id,user_id,owner_id,team_id,status,created_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,event_key) DO NOTHING''',
            (run['id'], signal[0], signal[1], user_id, run['owner_id'], team_id, status, now()))

    def preferences_changed_in(self, conn, user_id, enabled):
        conn.execute("UPDATE slack_notifications SET status='skipped' WHERE user_id=? AND status IN ('pending','preparing')", (user_id,))
        if enabled:
            # Enabling never replays old completions or existing requests.
            for run in conn.execute("SELECT * FROM runs WHERE parent_run_id='' AND deleted_at='' ").fetchall():
                if self.preference_owner_in(conn, run) == user_id:
                    signal = self.signal_in(conn, run)
                    if signal:
                        self.queue_in(conn, run, signal, user_id, '', 'skipped')

    def installation(self):
        bot = self.owner.connectors.slack_installation()
        allowed = (self.owner.settings.slack_bot_enabled and self.owner.settings.slack_dm_enabled
                   and bot.get('installed') and self.owner.connectors.allowed('slack_send')
                   and {'chat:write', 'im:write'} <= set(bot.get('scopes', [])))
        return bot, allowed

    def enabled_in(self, conn, run, user_id):
        preference = conn.execute('SELECT slack_notifications FROM user_preferences WHERE user_id=?', (user_id,)).fetchone()
        paused = conn.execute('SELECT paused FROM slack_threads WHERE run_id=?', (run['id'],)).fetchone()
        return (preference and preference['slack_notifications']
                and self.preference_owner_in(conn, run) == user_id and not (paused and paused['paused']))

    def collect(self):
        bot, allowed = self.installation()
        with self.store.connect() as conn:
            conn.begin_write()
            # No scan or provider requests when nobody has opted in.
            users = {row['user_id'] for row in conn.execute('SELECT user_id FROM user_preferences WHERE slack_notifications=1')}
            if not users:
                return
            for run in conn.execute("""SELECT r.* FROM runs r WHERE r.parent_run_id=''
                AND r.deleted_at='' AND r.deletion_requested_at='' AND EXISTS(
                    SELECT 1 FROM users u JOIN user_preferences p
                    ON p.user_id=u.id OR p.user_id=u.linked_user_id
                    WHERE u.id=r.owner_id AND p.slack_notifications=1)""").fetchall():
                user_id = self.preference_owner_in(conn, run)
                if user_id not in users:
                    continue
                signal = self.signal_in(conn, run)
                if signal:
                    self.queue_in(conn, run, signal, user_id, bot.get('team_id') or '',
                                  'pending' if allowed and self.enabled_in(conn, run, user_id) else 'skipped')

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
            signal = self.signal_in(conn, run)
            return (dict(run), signal, bot) if signal and signal[0] == row['event_key'] else None

    def payload(self, run, signal, channel):
        title = self.owner.chat.scrub(run['display_title'] or run['prompt'])[:160].strip() or 'Moyai session'
        url = self.owner.settings.public_url.rstrip('/') + '/#run=' + signal[1]
        return {'channel': channel, 'text': html.escape(f'{signal[2]}: {title}', quote=False),
                'parse': 'none', 'unfurl_links': False, 'unfurl_media': False,
                'blocks': [
                    {'type': 'header', 'text': {'type': 'plain_text', 'text': signal[2]}},
                    {'type': 'section', 'text': {'type': 'plain_text', 'text': title}},
                    {'type': 'section', 'text': {'type': 'plain_text', 'text': signal[3]}},
                    {'type': 'actions', 'elements': [{'type': 'button', 'action_id': 'open_session_notification',
                        'text': {'type': 'plain_text', 'text': 'Open session'}, 'style': 'primary', 'url': url}]},
                    {'type': 'context', 'elements': [{'type': 'plain_text', 'text': 'Manage Slack notifications in Moyai Settings.'}]},
                ]}

    async def deliver_one(self):
        async with self.delivery_lock:
            rows = await database(self.store.rows, "SELECT * FROM slack_notifications WHERE status='pending' AND retry_at<=? ORDER BY id LIMIT 1", (time.time(),))
            if not rows:
                return
            row = rows[0]
            sending = False
            try:
                claimed = await database(self.store.execute, "UPDATE slack_notifications SET status='preparing' WHERE id=? AND status='pending'", (row['id'],))
                if not claimed:
                    return
                async with asyncio.timeout(20):
                    current = await database(self.current, row)
                    if not current or not self.owner.identities:
                        await self.set_status(row, 'skipped')
                        return
                    run, signal, bot = current
                    recipient = await self.owner.identities.requester(run, session_owner=True)
                    audience = {item.strip() for item in self.owner.settings.slack_session_users.split(',')}
                    if recipient['team_id'] != row['team_id'] or ('*' not in audience and recipient['user_id'] not in audience):
                        await self.set_status(row, 'skipped')
                        return
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
                        return
                    await self.set_status(row, 'sending')
                    await self.owner.checkpoints.flush()
                    # Recheck after checkpoint awaits, before the only message send.
                    await database(self.owner.identities.check_requester, run, bot)
                    if not await database(self.current, row):
                        await self.set_status(row, 'skipped')
                        return
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

    async def set_status(self, row, status):
        await database(self.store.execute, "UPDATE slack_notifications SET status=? WHERE id=? AND status IN ('pending','preparing','sending')", (status, row['id']))

    async def watch(self):
        while True:
            try:
                await database(self.collect)
                await self.deliver_one()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Slack notification worker will retry after %s', type(exc).__name__)
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            await asyncio.sleep(2)

    def recover(self):
        if self.worker and not self.worker.done():
            return
        self.store.execute("UPDATE slack_notifications SET status='uncertain' WHERE status='sending'")
        self.store.execute("UPDATE slack_notifications SET status='pending' WHERE status='preparing'")
        if not self.worker or self.worker.done():
            self.worker = asyncio.create_task(self.watch())

    async def shutdown(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)


def initialize_schema(store):
    with store.connect() as conn:
        conn.executescript('''CREATE TABLE IF NOT EXISTS slack_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id), event_key TEXT NOT NULL,
            target_run_id TEXT NOT NULL, user_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            team_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            slack_ts TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
            UNIQUE(run_id,event_key)
        );
        CREATE INDEX IF NOT EXISTS idx_slack_notifications_pending ON slack_notifications(status,retry_at,id);''')
