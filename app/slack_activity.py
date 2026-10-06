"""Reconcile Slack's transient working indicator with durable session state.

Unlike chat messages, setting a status is idempotent and safe to retry. Slack
expires it after two minutes and clears it whenever the bot posts a reply.
"""
import asyncio
import logging
import time

from .progress import current_focus


logger = logging.getLogger(__name__)
STATUSES = {
    'queued': 'is getting ready…',
    'provisioning': 'is opening the workspace…',
    'reconnecting': 'is reconnecting to the workspace…',
    'running': 'is working…',
    'saving': 'is saving the work…',
    'waiting_children': 'is coordinating parallel agents…',
    'waiting_credential': 'is waiting for a provider key in the web session',
    'awaiting_approval': 'is waiting for approval in the web session',
    'stopping': 'is stopping…',
}


class SlackActivity:
    def __init__(self, owner):
        self.owner = owner
        self.store = owner.store

    def status_for(self, row):
        state = 'queued' if row['queued'] and row['run_status'] in {'idle', 'completed'} else row['run_status']
        if row['paused']:
            return ''
        if state == 'running':
            with self.store.connect() as conn:
                focus = current_focus(conn, row['run_id'])
            if focus:
                return self.owner.chat.scrub(focus)
        return STATUSES.get(state, '')

    def desired_status(self, run_id):
        rows = self.store.rows('''SELECT t.*,r.status AS run_status,
            EXISTS(SELECT 1 FROM messages m WHERE m.run_id=r.id AND m.status='queued') AS queued
            FROM slack_threads t JOIN runs r ON r.id=t.run_id WHERE t.run_id=?''', (run_id,))
        return self.status_for(rows[0]) if rows else ''

    async def sync(self):
        if not self.owner.settings.slack_thread_chat_enabled or not self.owner.status()['enabled']:
            # A disabled/replaced connection cannot send. Slack's TTL clears it.
            return
        team = self.owner.connectors.slack_installation().get('team_id')
        stamp = time.time()
        pending = []
        rows = self.store.rows('''SELECT t.*,r.status AS run_status,a.status AS last_status,
            a.refreshed_at,a.retry_at,
            EXISTS(SELECT 1 FROM messages m WHERE m.run_id=r.id AND m.status='queued') AS queued
            FROM slack_threads t JOIN runs r ON r.id=t.run_id
            LEFT JOIN slack_activity a ON a.run_id=t.run_id
            WHERE t.team_id=? ORDER BY COALESCE(a.retry_at,0),COALESCE(a.refreshed_at,0)''', (team,))
        for row in rows:
            status = self.status_for(row)
            # Coalesce changing focus without delaying lifecycle transitions.
            if status and status not in STATUSES.values() and row['last_status'] not in STATUSES.values():
                if (row['retry_at'] or 0) > stamp or (row['refreshed_at'] or 0) > stamp - 5:
                    continue
            if status == row['last_status'] and (row['retry_at'] or 0) > stamp:
                continue
            if not status and row['last_status'] is None:
                continue  # Never backfill historical idle sessions on deploy.
            if (status == row['last_status'] and row['refreshed_at']
                    and (not status or row['refreshed_at'] > stamp - 60)):
                continue
            pending.append(self.send(row['run_id'], status))
            if len(pending) == 5:
                break
        # Bound status work so a Slack outage cannot hold up saved answers.
        await asyncio.gather(*pending)

    async def send(self, run_id, status):
        stamp = time.time()
        # Record even an ambiguous attempt so completion/restart clears it.
        self.store.execute('''INSERT INTO slack_activity(run_id,status,refreshed_at,retry_at)
            VALUES(?,?,0,?) ON CONFLICT(run_id) DO UPDATE SET
            status=excluded.status,refreshed_at=0,retry_at=excluded.retry_at''', (run_id, status, stamp + 30))
        try:
            async with asyncio.timeout(3):
                await self.owner.agentchat.set_status(self.owner.channel,
                    self.owner.channel.source_for_run(run_id), status)
            self.store.execute('UPDATE slack_activity SET refreshed_at=?,retry_at=0 WHERE run_id=?', (time.time(), run_id))
            if status:
                self.store.execute("UPDATE slack_events SET reply_status='sent' WHERE run_id=?", (run_id,))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Slack working indicator will retry after %s', type(exc).__name__)

    def posted(self, run_id):
        # Slack clears the native indicator on any bot reply. Reconcile again:
        # either restore it for queued/ongoing work or confirm it is cleared.
        self.store.execute('UPDATE slack_activity SET refreshed_at=0,retry_at=0 WHERE run_id=?', (run_id,))
