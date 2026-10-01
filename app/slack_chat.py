"""Durable Slack thread routing and a bounded, non-replaying reply outbox."""
import asyncio
import html
import json
import logging
import re
import time
from decimal import Decimal
from uuid import uuid4

from agentchat.models import Message, Sender

from .db import now
from .security import digest
from .slack_activity import SlackActivity, threaded

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'idle'}
COMMANDS = {'stop', 'sleep', 'wake', 'status'}
logger = logging.getLogger(__name__)


def slack_text(text):
    """Format replies without letting model text trigger Slack mentions/unfurls."""
    text = html.escape(text, quote=False)
    def link(match):
        label, url = match.groups()
        if len(url) > 1500:
            return label
        return '<' + url + '|' + label.replace('|', '¦') + '>'
    # Preserve code verbatim; convert only the surrounding Markdown.
    pieces = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    for i in range(0, len(pieces), 2):
        value = re.sub(r'!?\[([^\]\n]+)\]\((https?://[^\s)<>]+)\)', link, pieces[i])
        value = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', value, flags=re.M)
        pieces[i] = re.sub(r'\*\*(.+?)\*\*', r'*\1*', value)
    return ''.join(pieces)


def split_reply(text, limit=2600):
    chunks, opened = [], False
    while text:
        end = len(text) if len(text) <= limit else text.rfind('\n', 0, limit)
        if end < limit // 2 and len(text) > limit:
            end = limit
        part, text = text[:end], text[end:].lstrip('\n')
        in_fence = opened ^ (part.count('```') % 2 == 1)
        chunks.append(('```\n' if opened else '') + part + ('\n```' if in_fence else ''))
        opened = in_fence
    return chunks or ['No response text was returned.']


class SlackChat:
    def __init__(self, owner):
        self.owner = owner
        self.store, self.settings = owner.store, owner.settings
        self.watcher = None
        self.wake = asyncio.Event()
        self.last_post = {}
        self.activity = SlackActivity(owner)


    def link(self, run_id):
        return f"<{self.settings.public_url.rstrip('/')}/#run={run_id}|Open session>"

    def mirroring(self, run_id):
        rows = self.store.rows('SELECT paused,team_id FROM slack_threads WHERE run_id=?', (run_id,))
        if not rows or not self.settings.slack_thread_chat_enabled or not self.owner.status()['enabled']:
            return None
        if rows[0]['team_id'] != self.owner.connectors.slack_installation().get('team_id'):
            return None
        return 'paused' if rows[0]['paused'] else 'active'

    def queue(self, conn, run_id, key, kind, text, metadata=None):
        conn.execute('INSERT OR IGNORE INTO slack_outbox(run_id,dedupe_key,kind,text,created_at,metadata) VALUES(?,?,?,?,?,?)',
                     (run_id, key, kind, text, now(), json.dumps(metadata or {})))

    def enqueue_web(self, run_id, content, client_id, model, user_id, attachment_ids=None, send_now=False):
        """Save a verified web input and its mirror in the same transaction.

        Only new inputs in an enabled, awake binding are eligible. Retrying a
        submission, reconnecting Slack or waking a thread cannot backfill it.
        """
        enabled = self.owner.status()['enabled'] and self.settings.slack_thread_chat_enabled
        team = self.owner.connectors.slack_installation().get('team_id')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            message, created = self.store.enqueue_message_in(conn, run_id, content, client_id, model, user_id, attachment_ids, send_now)
            binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (run_id,)).fetchone()
            if created and binding:
                allowed = enabled and binding['team_id'] == team
                # Preserve chronology when the previous answer has been saved
                # but the background collector has not seen it yet.
                self.collect_answers_in(conn, binding, allowed)
                if allowed and not binding['paused']:
                    user = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                    name = user['name'] if user else 'Web user'
                    if user and user['kind'] == 'google' and user['email']:
                        name = f"{name} ({user['email']})" if name != user['email'] else name
                    value = self.scrub(content)
                    if attachment_ids:
                        names = [row['name'] for row in conn.execute('SELECT name FROM attachments WHERE message_id=? ORDER BY created_at,id', (message['id'],))]
                        value += '\n\nAttachments: ' + ', '.join(names) + '\nOpen files in Moyai: ' + self.owner.settings.public_url.rstrip('/') + '/#run=' + run_id
                    value = self.scrub(value)
                    # Plain-text chunks preserve every character of the input.
                    chunks = [value[i:i + 2600] for i in range(0, len(value), 2600)]
                    for index, chunk in enumerate(chunks):
                        key = f"input:{message['id']}:{index}"
                        self.queue(conn, run_id, key, 'input', chunk, {
                            'message_id': key, 'sender_id': user_id,
                            'sender_name': self.scrub(name),
                        })
        if created:
            self.store.event(run_id, 'chat', 'Message queued', {'message_id': message['id']})
        self.wake.set()
        return message, created

    def change_queued_in(self, conn, run_id, message, action):
        """Retire pending mirrors; post an explicit correction to an active thread."""
        binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (run_id,)).fetchone()
        if not binding:
            return
        prefix = f"input:{message['id']}:%"
        mirrored = conn.execute("SELECT 1 FROM slack_outbox WHERE run_id=? AND dedupe_key LIKE ? AND status IN ('sent','sending','uncertain')", (run_id, prefix)).fetchone()
        inbound = conn.execute('SELECT 1 FROM slack_receipts WHERE run_id=? AND message_id=?', (run_id, message['id'])).fetchone()
        conn.execute("UPDATE slack_outbox SET status='skipped' WHERE run_id=? AND dedupe_key LIKE ? AND status='pending'", (run_id, prefix))
        if (binding['paused'] or not self.settings.slack_thread_chat_enabled or not self.owner.status()['enabled']
                or binding['team_id'] != self.owner.connectors.slack_installation().get('team_id')):
            return
        if action == 'delete' and not (mirrored or inbound):
            return
        text = (f"Queued message #{message['id']} removed in Moyai before the agent picked it up." if action == 'delete' else
                f"Queued message #{message['id']} updated in Moyai. The agent will use this version:\n\n" + message['content'])
        names = [row['name'] for row in conn.execute('SELECT name FROM attachments WHERE message_id=?', (message['id'],))]
        if action == 'edit' and names:
            text += '\n\nAttachments: ' + ', '.join(names)
        for index, chunk in enumerate(split_reply(slack_text(self.scrub(text)))):
            self.queue(conn, run_id, f"input:{message['id']}:revision:{message['revision']}:{index}", 'input_update', chunk + '\n' + self.link(run_id))

    def accept(self, *, team, event_id, channel, ts, root, user, prompt, mentioned, missing_cloud, direct_message=False):
        """Reserve the physical Slack message and queue its turn atomically."""
        original_prompt, selected_model, model_error = prompt, None, ''
        command = prompt.strip().lower().lstrip('/')
        command = command if command in COMMANDS else ''
        directive = re.fullmatch(r'/?model(?:[ \t]+([^\n]+))?(?:\n([\s\S]*))?', prompt.strip(), re.I)
        if directive:
            try:
                selected_model = self.settings.resolve_model(directive[1] or '')
            except ValueError:
                model_error = 'Choose `model astra` or `model opus`, or use the model picker in the web session.'
            prompt = (directive[2] or '').strip()
            if not prompt or model_error:
                command = 'model'
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM slack_receipts WHERE event_id=? OR (team_id=? AND channel=? AND message_ts=?)',
                            (event_id, team, channel, ts)).fetchone():
                return None
            if conn.execute('SELECT 1 FROM slack_events WHERE event_id=? OR (channel=? AND mention_ts=?)',
                            (event_id, channel, ts)).fetchone():
                return None
            actor_id = self.store.slack_identity_in(conn, team, user)
            if direct_message and root == ts:
                existing = conn.execute('SELECT t.*,e.user_id FROM slack_threads t JOIN slack_events e ON e.run_id=t.run_id WHERE t.team_id=? AND t.channel=? AND t.thread_ts=t.started_ts ORDER BY t.started_ts LIMIT 1', (team, channel)).fetchone()
                if existing:
                    if existing['user_id'] != user:
                        return None
                    root = existing['thread_ts']
            binding = conn.execute('SELECT * FROM slack_threads WHERE team_id=? AND channel=? AND thread_ts=?',
                                   (team, channel, root)).fetchone()
            fresh = False
            if not binding:
                if not mentioned and not direct_message:
                    return None
                # Explicit mention may reconnect a pre-upgrade thread. Old
                # messages are never backfilled into Slack on deployment.
                old = conn.execute('SELECT r.id FROM slack_events s JOIN runs r ON r.id=s.run_id WHERE s.channel=? AND s.thread_ts=? AND r.chat_enabled=1 ORDER BY s.created_at DESC LIMIT 1',
                                   (channel, root)).fetchone()
                if command and command != 'model' and not old:
                    return None
                if missing_cloud and not command:
                    raise ValueError('Cloud sessions are not configured.')
                if old:
                    run_id = old['id']
                    # A run cannot be redirected to another thread or team.
                    if conn.execute('SELECT 1 FROM slack_threads WHERE run_id=?', (run_id,)).fetchone():
                        return None
                else:
                    pending = conn.execute("SELECT COUNT(*) FROM runs WHERE status NOT IN ('completed','failed','cancelled','interrupted','idle')").fetchone()[0]
                    if pending >= self.settings.max_pending_runs:
                        raise ValueError('The session queue is full.')
                    run_id, stamp = uuid4().hex, now()
                    plugins = [x['id'] for x in self.owner.connectors.list() if x['connected'] and x['enabled']]
                    conn.execute("INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,created_at,updated_at,chat_enabled,model,owner_id) VALUES(?,?,'','modal',?,?,?,?,1,?,?)",
                                 (run_id, prompt or original_prompt, 'idle' if command else 'queued', json.dumps(plugins), stamp, stamp, selected_model or self.settings.resolve_model(), actor_id))
                    conn.execute("INSERT INTO slack_events(event_id,run_id,channel,thread_ts,user_id,created_at,mention_ts,context_status) VALUES(?,?,?,?,?,?,?,'pending')",
                                 (event_id, run_id, channel, root, user, stamp, ts))
                    fresh = True
                cursor = conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE run_id=?', (run_id,)).fetchone()[0]
                conn.execute('INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts,last_message_id,last_progress) VALUES(?,?,?,?,?,?,?)',
                             (team, channel, root, run_id, ts, cursor, time.time()))
                binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (run_id,)).fetchone()
            run_id = binding['run_id']
            if Decimal(ts) < Decimal(binding['started_ts']):
                return None
            if binding['paused'] and not mentioned and command not in {'wake', 'status'}:
                return None
            if binding['paused'] and (command == 'wake' or (mentioned and not command)):
                # Replies completed while asleep must not be backfilled even
                # when wake arrives before the periodic collector runs.
                conn.execute('UPDATE slack_threads SET last_message_id=(SELECT COALESCE(MAX(id),0) FROM messages WHERE run_id=?) WHERE run_id=?', (run_id, run_id))
            message_id, submit = None, False
            if command:
                if command == 'model' and not model_error:
                    conn.execute('UPDATE runs SET model=?,updated_at=? WHERE id=?', (selected_model, now(), run_id))
                    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'chat','Model changed for new messages',?,?)", (run_id, json.dumps({'model':selected_model}), now()))
                if command == 'sleep':
                    conn.execute('UPDATE slack_threads SET paused=1 WHERE run_id=?', (run_id,))
                    conn.execute("UPDATE slack_outbox SET status='skipped' WHERE run_id=? AND status='pending' AND kind IN ('answer','input','input_update','progress','approval')", (run_id,))
                elif command == 'wake':
                    conn.execute('UPDATE slack_threads SET paused=0 WHERE run_id=?', (run_id,))
                if command in {'stop', 'sleep'}:
                    # Reserve interruption before returning Slack's acknowledgement.
                    # Modal cleanup happens in the background worker.
                    run = conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()
                    if run['status'] not in TERMINAL:
                        conn.execute("UPDATE runs SET status='stopping',token_hash='',updated_at=? WHERE id=?", (now(), run_id))
                    conn.execute("UPDATE messages SET status='cancelled' WHERE run_id=? AND status='queued'", (run_id,))
                    conn.execute("UPDATE approvals SET status='expired' WHERE run_id=? AND status IN ('pending','approved')", (run_id,))
                    conn.execute("UPDATE slack_outbox SET status='skipped' WHERE run_id=? AND status='pending' AND kind='progress'", (run_id,))
                status = conn.execute('SELECT status FROM runs WHERE id=?', (run_id,)).fetchone()[0]
                model_name = next((item['name'] for item in self.settings.model_choices() if item['id'] == selected_model), selected_model)
                response = {'sleep': 'Paused this thread and requested a stop. Say `wake` or mention me to resume.',
                            'stop': 'Stopping the current response and clearing queued follow-ups. You can send another message once it has stopped.',
                            'wake': 'I’m listening again. Send your next message here.',
                            'status': 'This thread is paused.' if binding['paused'] else self.status_text(status),
                            'model': model_error or f'New messages in this session will use *{model_name}*. Running and already queued replies keep their original model.'}[command]
                if fresh and direct_message:
                    response += '\nThis conversation also appears in Moyai, where signed-in BerriAI teammates can view it.'
                self.queue(conn, run_id, 'command:' + event_id, 'control', response + '\n' + self.link(run_id))
            else:
                if missing_cloud:
                    raise ValueError('Cloud sessions are not configured.')
                try:
                    content = prompt if fresh else f'Slack reply from {user}:\n{prompt}'
                    if selected_model is None:
                        current = conn.execute('SELECT model FROM runs WHERE id=?', (run_id,)).fetchone()[0]
                        self.settings.resolve_model(fallback=current)
                    message, submit = self.store.enqueue_message_in(conn, run_id, content, 'slack:' + digest(team + channel + ts), selected_model, actor_id)
                    message_id = message['id']
                    conn.execute('UPDATE slack_threads SET paused=0,last_progress=? WHERE run_id=?', (time.time(), run_id))
                    if not threaded(binding):
                        self.queue(conn, run_id, 'received:' + str(message_id), 'reaction', ts)
                    conn.execute("INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,'chat','Message received from Slack',?,?)",
                                 (run_id, json.dumps({'message_id': message_id, 'user_id': user}), now()))
                except ValueError as exc:
                    self.queue(conn, run_id, 'rejected:' + event_id, 'control', str(exc) + '\n' + self.link(run_id))
            conn.execute('INSERT INTO slack_receipts(event_id,team_id,channel,message_ts,user_id,run_id,message_id,command,handled) VALUES(?,?,?,?,?,?,?,?,?)',
                         (event_id, team, channel, ts, user, run_id, message_id, command, 0 if command in {'stop', 'sleep'} else 1))
        self.wake.set()
        return self.store.run(run_id) if submit else None

    @staticmethod
    def status_text(status):
        return {'queued': 'Your message is queued.', 'provisioning': 'Opening the cloud workspace…',
                'running': 'I’m working through your request…', 'saving': 'Saving the conversation and workspace…',
                'waiting_children': 'The parallel agents are working. I’ll combine their results when they finish.',
                'waiting_credential': 'I need a provider key. Use the secure form in the web session; do not paste it in Slack.',
                'awaiting_approval': 'I need an administrator’s approval in the web app before making that change.',
                'stopping': 'Stopping and cleaning up the cloud workspace…', 'idle': 'Ready for your next message.',
                'completed': 'Finished. Ready for your next message.', 'failed': 'The response failed. Send a follow-up to continue.',
                'cancelled': 'Stopped. Send another message to continue.',
                'interrupted': 'The workspace restarted. Unfinished work was not replayed; send a message to continue.'}.get(status, 'Working…')

    def scrub(self, text):
        for name, value in self.settings.model_dump().items():
            if any(part in name for part in ('password', 'secret', 'api_key', 'token')) and isinstance(value, str) and len(value) >= 8:
                text = text.replace(value, '[redacted]')
        text = re.sub(r'(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[private key redacted]', text)
        text = re.sub(r'\b(?:xox[baprs]-[A-Za-z0-9-]{12,}|sk-[A-Za-z0-9_-]{16,}|GOCSPX-[A-Za-z0-9_-]+)', '[credential redacted]', text)
        return text

    def collect_answers_in(self, conn, binding, allowed):
        run_id = binding['run_id']
        messages = conn.execute("SELECT * FROM messages WHERE run_id=? AND role='assistant' AND id>? ORDER BY id", (run_id, binding['last_message_id'])).fetchall()
        for message in messages:
            if allowed and not binding['paused'] and message['status'] != 'steered':
                value = self.scrub(message['content'])
                if message['status'] in {'failed', 'cancelled', 'interrupted'}:
                    value = 'Response ' + message['status'] + ':\n\n' + value
                if len(value) > 32000:
                    value = value[:32000] + '\n\n[Long response shortened; the full answer is in the web session.]'
                if binding['channel'].startswith('D') and not conn.execute(
                    "SELECT 1 FROM slack_outbox WHERE run_id=? AND kind='answer' LIMIT 1", (run_id,)).fetchone():
                    value += '\n\nThis conversation also appears in Moyai, where signed-in BerriAI teammates can view it.'
                chunks = split_reply(slack_text(value))
                for index, chunk in enumerate(chunks):
                    suffix = '\n\n' + self.link(run_id) if index == len(chunks) - 1 else ''
                    self.queue(conn, run_id, f"answer:{message['id']}:{index}", 'answer', chunk + suffix)
            conn.execute('UPDATE slack_threads SET last_message_id=? WHERE run_id=?', (message['id'], run_id))

    def collect(self):
        enabled = self.owner.status()['enabled'] and self.settings.slack_thread_chat_enabled
        team = self.owner.connectors.slack_installation().get('team_id')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for binding in conn.execute('SELECT t.*,r.status,r.updated_at FROM slack_threads t JOIN runs r ON r.id=t.run_id').fetchall():
                run_id = binding['run_id']
                allowed = enabled and binding['team_id'] == team
                if not allowed:
                    conn.execute("UPDATE slack_outbox SET status='skipped' WHERE run_id=? AND status='pending'", (run_id,))
                self.collect_answers_in(conn, binding, allowed)
                if not allowed or binding['paused']:
                    continue
                for approval in conn.execute("SELECT id FROM approvals WHERE run_id=? AND status='pending'", (run_id,)).fetchall():
                    self.queue(conn, run_id, 'approval:' + approval['id'], 'approval',
                               'I need an administrator to review an external change. Approve or deny the exact action in the web session.\n' + self.link(run_id))
                # A child can ask while the Slack-linked parent waits for it.
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential_requests'").fetchone():
                    for key in conn.execute("SELECT q.id,q.run_id FROM credential_requests q JOIN runs r ON r.id=q.run_id WHERE (r.id=? OR r.parent_run_id=?) AND q.status='pending' AND q.message_id=r.active_message_id AND r.status IN ('running','saving','waiting_credential')",(run_id,run_id)).fetchall():
                        self.queue(conn,run_id,'credential:'+key['id'],'approval',
                                   'A provider API key is needed to continue. Provide a key or open the provider setup link through the secure form. Do not paste keys in Slack.\n' + self.link(key['run_id']))
                if binding['status'] == 'interrupted':
                    self.queue(conn, run_id, f"interrupted:{run_id}:{binding['updated_at']}", 'control', self.status_text('interrupted') + '\n' + self.link(run_id))

    async def deliver_one(self):
        for row in self.store.rows("SELECT o.*,t.team_id,t.channel,t.thread_ts,t.paused FROM slack_outbox o JOIN slack_threads t ON t.run_id=o.run_id WHERE o.status='pending' ORDER BY o.id LIMIT 100"):
            if self.last_post.get(row['channel'], 0) > time.monotonic() - 1.1:
                continue
            if (not self.settings.slack_thread_chat_enabled or not self.owner.status()['enabled']
                    or row['team_id'] != self.owner.connectors.slack_installation().get('team_id')
                    or (row['paused'] and row['kind'] in {'answer', 'input', 'input_update', 'progress', 'approval', 'reaction'})):
                self.store.execute("UPDATE slack_outbox SET status='skipped' WHERE id=?", (row['id'],))
                continue
            if not self.store.execute("UPDATE slack_outbox SET status='sending' WHERE id=? AND status='pending'", (row['id'],)):
                continue
            try:
                # Persist before external side effects. Ambiguous sends are
                # marked uncertain and never replayed automatically.
                await self.owner.checkpoints.flush()
                if row['kind'] == 'reaction':
                    await self.owner.channel.acknowledge(row['run_id'], row['text'])
                    sent_ts = row['text']
                elif row['kind'] == 'input':
                    data = json.loads(row['metadata'])
                    source = self.owner.channel.source_for_run(row['run_id'])
                    message = Message(id=data['message_id'], conversation_id=source.conversation_id,
                        channel='web', sender=Sender(id=data['sender_id'], display_name=data['sender_name']),
                        text=row['text'], role='user')
                    response = await self.owner.agentchat.mirror(self.owner.channel, source, message, origin='Moyai web')
                    sent_ts = response.metadata['slack_ts']
                else:
                    response = await self.owner.agentchat.reply(self.owner.channel,
                        self.owner.channel.source_for_run(row['run_id']), row['text'])
                    sent_ts = response.metadata['slack_ts']
                self.store.execute("UPDATE slack_outbox SET status='sent',slack_ts=? WHERE id=?", (sent_ts, row['id']))
                if row['kind'] != 'reaction':
                    self.activity.posted(row['run_id'])
                if row['kind'] in {'ack', 'reaction'}:
                    self.store.execute("UPDATE slack_events SET reply_status='sent' WHERE run_id=?", (row['run_id'],))
            except (Exception, asyncio.CancelledError) as exc:
                self.store.execute("UPDATE slack_outbox SET status='uncertain' WHERE id=?", (row['id'],))
                if row['kind'] in {'ack', 'reaction'}:
                    self.store.execute("UPDATE slack_events SET reply_status='uncertain' WHERE run_id=?", (row['run_id'],))
                self.store.event(row['run_id'], 'status',
                    'Slack acknowledgment reaction could not be confirmed. Check the bot’s reactions:write permission; the answer will still be delivered.'
                    if row['kind'] == 'reaction' else
                    'Slack message delivery could not be confirmed. The message remains in the web session and will not be sent twice automatically.')
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                self.last_post[row['channel']] = time.monotonic()
                await self.owner.checkpoints.flush()
            return

    async def watch(self):
        while True:
            try:
                self.wake.clear()
                for row in self.store.rows('SELECT event_id,run_id FROM slack_receipts WHERE handled=0'):
                    await self.owner.manager.cancel(row['run_id'])
                    self.store.execute('UPDATE slack_receipts SET handled=1 WHERE event_id=?', (row['event_id'],))
                self.collect()
                await self.deliver_one()
                await self.activity.sync()
                await self.owner.checkpoints.flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A provider outage must not kill the durable delivery worker.
                logger.warning('Slack reply worker will retry after %s', type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=1)
            except TimeoutError:
                pass

    def recover(self):
        self.store.execute("UPDATE slack_outbox SET status='uncertain' WHERE status='sending'")
        self.store.execute("UPDATE slack_outbox SET status='skipped' WHERE status='pending' AND "
                           "(kind IN ('ack','progress') OR (kind='control' AND dedupe_key LIKE 'received:%'))")
        if not self.watcher or self.watcher.done():
            self.watcher = asyncio.create_task(self.watch())

    async def shutdown(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
