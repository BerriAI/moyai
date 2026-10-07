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
from .progress import active_turn
from .pr_delivery import link_captures, select_captures, select_prs
from .security import digest
from .slack_activity import SlackActivity

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted', 'idle'}
COMMANDS = {'stop', 'sleep', 'wake', 'status'}
logger = logging.getLogger(__name__)


USER_MENTION = re.compile(r'<@([UW][A-Z0-9]{7,30})(?:\|[^>\n]*)?>')


def slack_text(text, mentions=frozenset()):
    """Format replies without letting model text trigger Slack mentions/unfurls.

    Only user IDs in ``mentions`` (already mentioned by people in the thread)
    may render as mentions; broadcasts and every other ID stay literal text.
    """
    text = html.escape(text, quote=False)
    def mention(match):
        return f'<@{match[1]}>' if match[1] in mentions else match[0]
    def link(match):
        label, url = match.groups()
        if len(url) > 1500:
            return label
        return '<' + url + '|' + label.replace('|', '¦') + '>'
    # Preserve code verbatim; convert only the surrounding Markdown.
    pieces = re.split(r'(```[\s\S]*?```|`[^`\n]+`)', text)
    for i in range(0, len(pieces), 2):
        value = re.sub(r'!?\[([^\]\n]+)\]\((https?://[^\s)<>]+)\)', link, pieces[i])
        value = re.sub(r'&lt;@([UW][A-Z0-9]{7,30})&gt;', mention, value)
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

    def enqueue_web(self, run_id, content, client_id, model, user_id, attachment_ids=None, send_now=False, *, send_immediately=False):
        """Save a verified web input and its mirror in the same transaction.

        Only new inputs in an enabled, awake binding are eligible. Retrying a
        submission, reconnecting Slack or waking a thread cannot backfill it.
        """
        enabled = self.owner.status()['enabled'] and self.settings.slack_thread_chat_enabled
        team = self.owner.connectors.slack_installation().get('team_id')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            message, created = self.store.enqueue_message_in(conn, run_id, content, client_id, model, user_id, attachment_ids, send_now, send_immediately=send_immediately)
            binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (run_id,)).fetchone()
            if created and binding:
                allowed = enabled and binding['team_id'] == team
                # Preserve chronology when the previous answer has been saved
                # but the background collector has not seen it yet.
                self.collect_answers_in(conn, binding, allowed)
                self.collect_progress_in(conn, binding, allowed)
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

    def cloud_missing(self, missing, provider):
        if provider == self.settings.sandbox_provider:
            return missing
        # The webhook reports readiness for the current default. Follow-ups
        # belong to their session's original provider, even after a switch.
        return [key for key in missing if not key.startswith(('MODAL_', 'SUBSTRATE_'))] + self.settings.missing_sandbox(provider)

    def accept(self, *, team, event_id, channel, ts, root, user, prompt, mentioned, missing_cloud, direct_message=False, file_ids=()):
        """Reserve the physical Slack message and queue its turn atomically."""
        original_prompt, selected_model, model_error = prompt, None, ''
        selected_harness, harness_error = None, ''
        harness_directive = re.fullmatch(r'/?harness(?:[ \t]+([^\n]+))?(?:\n([\s\S]*))?', prompt.strip(), re.I)
        if harness_directive:
            from .harnesses import HARNESSES
            selected_harness = (harness_directive[1] or '').strip().lower()
            if selected_harness not in HARNESSES:
                harness_error = 'Choose a harness in a new thread: ' + ', '.join('`harness ' + name + '`' for name in HARNESSES) + '.'
                selected_harness = None
            prompt = (harness_directive[2] or '').strip()
        command = prompt.strip().lower().lstrip('/')
        command = command if command in COMMANDS else ''
        if harness_directive and (not prompt or harness_error):
            command = 'harness'
        directive = re.fullmatch(r'/?model(?:[ \t]+([^\n]+))?(?:\n([\s\S]*))?', prompt.strip(), re.I)
        if directive:
            try:
                selected_model = self.settings.resolve_model(directive[1] or '')
            except ValueError:
                model_error = 'Choose an enabled model: ' + ', '.join(item['name'] for item in self.settings.model_choices()) + '. Use `model glm-5.3`, for example, or the model picker in the web session.'
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
                # Keep the DM participant guard without adopting an older session.
                existing = conn.execute('SELECT e.user_id FROM slack_threads t JOIN slack_events e ON e.run_id=t.run_id WHERE t.team_id=? AND t.channel=? AND t.thread_ts=t.started_ts ORDER BY t.started_ts LIMIT 1', (team, channel)).fetchone()
                if existing and existing['user_id'] != user:
                    return None
            binding = conn.execute('SELECT * FROM slack_threads WHERE team_id=? AND channel=? AND thread_ts=?',
                                   (team, channel, root)).fetchone()
            fresh = False
            if not binding:
                if not mentioned and not direct_message:
                    return None
                # Explicit mention may reconnect a pre-upgrade thread. Old
                # messages are never backfilled into Slack on deployment.
                old = conn.execute('SELECT r.id,r.sandbox_provider FROM slack_events s JOIN runs r ON r.id=s.run_id WHERE s.channel=? AND s.thread_ts=? AND r.chat_enabled=1 ORDER BY s.created_at DESC LIMIT 1',
                                   (channel, root)).fetchone()
                if command and command not in {'model', 'harness'} and not old:
                    return None
                readiness = self.cloud_missing(missing_cloud, old['sandbox_provider']) if old else missing_cloud
                if readiness and not command:
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
                    harness = selected_harness if not harness_error and selected_harness else self.settings.agent_harness
                    model = self.settings.harness_model(harness)
                    if selected_model:
                        try:
                            model = self.settings.harness_model(harness, selected_model)
                        except ValueError as exc:
                            model_error, selected_model, command = str(exc), None, 'model'
                    conn.execute("INSERT INTO runs(id,prompt,repo_url,mode,status,plugins,created_at,updated_at,chat_enabled,model,owner_id,harness) VALUES(?,?,'','modal',?,?,?,?,1,?,?,?)",
                                 (run_id, prompt or original_prompt, 'idle' if command else 'queued', json.dumps(plugins), stamp, stamp, model, actor_id, harness))
                    conn.execute("INSERT INTO slack_events(event_id,run_id,channel,thread_ts,user_id,created_at,mention_ts,context_status) VALUES(?,?,?,?,?,?,?,'pending')",
                                 (event_id, run_id, channel, root, user, stamp, ts))
                    conn.execute('UPDATE runs SET sandbox_provider=? WHERE id=?', (self.settings.sandbox_provider, run_id))
                    fresh = True
                cursor = conn.execute('SELECT COALESCE(MAX(id),0) FROM messages WHERE run_id=?', (run_id,)).fetchone()[0]
                conn.execute('INSERT INTO slack_threads(team_id,channel,thread_ts,run_id,started_ts,last_message_id,last_progress) VALUES(?,?,?,?,?,?,?)',
                             (team, channel, root, run_id, ts, cursor, time.time()))
                binding = conn.execute('SELECT * FROM slack_threads WHERE run_id=?', (run_id,)).fetchone()
                # An adopted session may already have web-only updates.
                self.collect_progress_in(conn, binding, False)
            run_id = binding['run_id']
            if conn.execute("SELECT 1 FROM runs WHERE id=? AND deleted_at!=''", (run_id,)).fetchone():
                return None  # Retain the binding and receipts; never restart a deleted thread.
            if Decimal(ts) < Decimal(binding['started_ts']):
                return None
            if binding['paused'] and not mentioned and command not in {'wake', 'status'}:
                return None
            if binding['paused'] and (command == 'wake' or (mentioned and not command)):
                # Replies completed while asleep must not be backfilled even
                # when wake arrives before the periodic collector runs.
                conn.execute('UPDATE slack_threads SET last_message_id=(SELECT COALESCE(MAX(id),0) FROM messages WHERE run_id=?) WHERE run_id=?', (run_id, run_id))
                self.collect_progress_in(conn, binding, False)
            message_id, submit = None, False
            current = conn.execute('SELECT harness,model,sandbox_provider FROM runs WHERE id=?', (run_id,)).fetchone()
            if selected_harness and current['harness'] != selected_harness:
                command = 'harness'
                harness_error = 'Harness is fixed for this session. Start a new thread to choose another harness.'
            if selected_model and not harness_error:
                from .harnesses import validate_harness
                try:
                    validate_harness(current['harness'], selected_model)
                except ValueError as exc:
                    command, model_error = 'model', str(exc)
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
                            'model': model_error or f'New messages in this session will use *{model_name}*. Running and already queued replies keep their original model.',
                            'harness': harness_error or f"This session uses *{current['harness']}*. Send your task here; the harness stays fixed for this session."}[command]
                if fresh and direct_message:
                    response += '\nThis conversation also appears in Moyai, where signed-in BerriAI teammates can view it.'
                self.queue(conn, run_id, 'command:' + event_id, 'control', response + '\n' + self.link(run_id))
            else:
                if self.cloud_missing(missing_cloud, current['sandbox_provider']):
                    raise ValueError('Cloud sessions are not configured.')
                try:
                    content = prompt if fresh else f'Slack reply from {user}:\n{prompt}'
                    if selected_model is None:
                        current = conn.execute('SELECT model FROM runs WHERE id=?', (run_id,)).fetchone()[0]
                        self.settings.resolve_model(fallback=current)
                    message, submit = self.store.enqueue_message_in(conn, run_id, content, 'slack:' + digest(team + channel + ts), selected_model, actor_id)
                    message_id = message['id']
                    self.store.slack_mentions.queue_in(conn, message_id, team, prompt)
                    if file_ids:
                        conn.execute('INSERT INTO slack_audio_inputs(message_id,files_json) VALUES(?,?)', (message_id, json.dumps(file_ids)))
                    conn.execute('UPDATE slack_threads SET paused=0,last_progress=? WHERE run_id=?', (time.time(), run_id))
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
                'waiting_credential': 'I need access to continue. Use the secure form in the web session; do not paste credentials in Slack.',
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

    def mentionable_in(self, conn, run_id):
        """Users people already mentioned in this run's Slack text; never the bot."""
        texts = [m.get('text', '') for row in conn.execute('SELECT context_json FROM slack_events WHERE run_id=?', (run_id,))
                 for m in json.loads(row['context_json'] or '{}').get('messages', [])]
        texts += [row['content'] for row in conn.execute(
            'SELECT m.content FROM slack_receipts r JOIN messages m ON m.id=r.message_id WHERE r.run_id=?', (run_id,))]
        found = {match[1] for text in texts if isinstance(text, str) for match in USER_MENTION.finditer(text)}
        return frozenset(found - {self.owner.connectors.slack_installation().get('user_id')})

    def collect_progress_in(self, conn, binding, allowed):
        run_id = binding['run_id']
        turn_id = active_turn(conn, run_id)
        # Completed-turn progress is obsolete; its final answer is authoritative.
        conn.execute("""UPDATE slack_outbox SET status='skipped' WHERE run_id=? AND kind='progress'
            AND status='pending' AND json_extract(metadata,'$.turn_id') IS NOT ?""", (run_id, turn_id))
        events = conn.execute("""SELECT id,message FROM events WHERE run_id=? AND kind='message'
            AND json_extract(data,'$.public_update')=1 AND json_extract(data,'$.turn_id')=? ORDER BY id""",
                              (run_id, turn_id)).fetchall()
        for event in events:
            key = f"progress:{event['id']}"
            text = slack_text(self.scrub(event['message']))
            self.queue(conn, run_id, key, 'progress', text + '\n\n' + self.link(run_id),
                       {'event_id': event['id'], 'turn_id': turn_id})
            if not allowed or binding['paused']:
                conn.execute("UPDATE slack_outbox SET status='skipped' WHERE dedupe_key=? AND status='pending'", (key,))

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
                prs = select_prs(conn, run_id, value)
                for pr in prs:
                    pr.title = self.scrub(pr.title)
                media = select_captures(self.settings, run_id, value) if message['status'] == 'completed' else []
                value = link_captures(self.settings, run_id, value, media)
                if binding['channel'].startswith('D') and not conn.execute(
                    "SELECT 1 FROM slack_outbox WHERE run_id=? AND kind='answer' LIMIT 1", (run_id,)).fetchone():
                    value += '\n\nThis conversation also appears in Moyai, where signed-in BerriAI teammates can view it.'
                if media:
                    self.queue(conn, run_id, f"answer:{message['id']}:media", 'answer',
                               'Saved demo captures.\n\n' + self.link(run_id),
                               {'captures': [item.model_dump() for item in media]})
                chunks = split_reply(slack_text(value, self.mentionable_in(conn, run_id)))
                for index, chunk in enumerate(chunks):
                    suffix = '\n\n' + self.link(run_id) if index == len(chunks) - 1 else ''
                    metadata = {'pull_requests': [pr.model_dump() for pr in prs]} if suffix and prs else None
                    self.queue(conn, run_id, f"answer:{message['id']}:{index}", 'answer', chunk + suffix, metadata)
            conn.execute('UPDATE slack_threads SET last_message_id=? WHERE run_id=?', (message['id'], run_id))

    def collect(self):
        enabled = self.owner.status()['enabled'] and self.settings.slack_thread_chat_enabled
        team = self.owner.connectors.slack_installation().get('team_id')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            for binding in conn.execute('SELECT t.*,r.status,r.updated_at,r.deleted_at FROM slack_threads t JOIN runs r ON r.id=t.run_id').fetchall():
                run_id = binding['run_id']
                allowed = enabled and binding['team_id'] == team and not binding['deleted_at']
                if not allowed:
                    conn.execute("UPDATE slack_outbox SET status='skipped' WHERE run_id=? AND status='pending'", (run_id,))
                self.collect_answers_in(conn, binding, allowed)
                self.collect_progress_in(conn, binding, allowed)
                if not allowed or binding['paused']:
                    continue
                for approval in conn.execute("SELECT id FROM approvals WHERE run_id=? AND status='pending'", (run_id,)).fetchall():
                    self.queue(conn, run_id, 'approval:' + approval['id'], 'approval',
                               'I need an administrator to review an external change. Approve or deny the exact action in the web session.\n' + self.link(run_id))
                # A child can ask while the Slack-linked parent waits for it.
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='credential_requests'").fetchone():
                    self.owner.access.collect_in(conn, binding)
                if binding['status'] == 'interrupted':
                    self.queue(conn, run_id, f"interrupted:{run_id}:{binding['updated_at']}", 'control', self.status_text('interrupted') + '\n' + self.link(run_id))

    async def deliver_one(self):
        for row in self.store.rows("SELECT o.*,t.team_id,t.channel,t.thread_ts,t.paused FROM slack_outbox o JOIN slack_threads t ON t.run_id=o.run_id WHERE o.status='pending' ORDER BY o.id LIMIT 100"):
            if self.skip_stale_progress(row):
                continue
            if self.last_post.get(row['channel'], 0) > time.monotonic() - 1.1:
                continue
            if (not self.settings.slack_thread_chat_enabled or not self.owner.status()['enabled']
                    or row['team_id'] != self.owner.connectors.slack_installation().get('team_id')
                    or (row['paused'] and row['kind'] in {'answer', 'input', 'input_update', 'progress', 'approval', 'reaction'})):
                self.store.execute("UPDATE slack_outbox SET status='skipped' WHERE id=?", (row['id'],))
                continue
            if not self.store.execute("""UPDATE slack_outbox SET status='sending' WHERE id=? AND status='pending'
                    AND EXISTS(SELECT 1 FROM runs WHERE id=slack_outbox.run_id AND deleted_at='')""", (row['id'],)):
                continue
            try:
                # Persist before external side effects. Ambiguous sends are
                # marked uncertain and never replayed automatically.
                await self.owner.checkpoints.flush()
                if self.skip_stale_progress(row):
                    continue
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
                    data = json.loads(row['metadata'])
                    source = self.owner.channel.source_for_run(row['run_id'], {**data, 'kind': row['kind']})
                    if data.get('credential_request_id'):
                        sent_ts, delivered = await self.owner.channel.credential_card(source, row['id'])
                        self.store.execute("UPDATE slack_outbox SET status=CASE WHEN metadata=? THEN 'sent' ELSE 'pending' END,slack_ts=? WHERE id=?", (delivered, sent_ts, row['id']))
                    elif data.get('captures'):
                        sent_ts = await self.owner.channel.deliver_captures(source, data['captures'])
                    else:
                        if data.get('pull_requests'):
                            content = self.owner.channel.rich_reply(source, row['text'], data['pull_requests'])
                            response = await self.owner.agentchat.reply_rich(self.owner.channel, source, content)
                        else:
                            response = await self.owner.agentchat.reply(self.owner.channel, source, row['text'])
                        sent_ts = response.metadata['slack_ts']
                if not json.loads(row['metadata']).get('credential_request_id'):
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
                    'Slack access card update could not be confirmed. It will be retried in the same message.'
                    if row['slack_ts'] and json.loads(row['metadata']).get('credential_request_id') else
                    'Slack message delivery could not be confirmed. The message remains in the web session and will not be sent twice automatically.')
                if isinstance(exc, asyncio.CancelledError):
                    raise
            finally:
                self.last_post[row['channel']] = time.monotonic()
                await self.owner.checkpoints.flush()
            return

    def skip_stale_progress(self, row):
        if row['kind'] != 'progress':
            return False
        with self.store.connect() as conn:
            current = active_turn(conn, row['run_id'])
            if current is not None and json.loads(row['metadata']).get('turn_id') == current:
                return False
            conn.execute("UPDATE slack_outbox SET status='skipped' WHERE id=? AND status IN ('pending','sending')", (row['id'],))
        return True

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
        self.store.execute("UPDATE slack_outbox SET status='pending' WHERE status='uncertain' AND slack_ts!='' AND json_extract(metadata,'$.credential_request_id') IS NOT NULL")
        self.store.execute("UPDATE slack_outbox SET status='skipped' WHERE status='pending' AND "
                           "(kind='ack' OR (kind='progress' AND json_extract(metadata,'$.event_id') IS NULL) "
                           "OR (kind='control' AND dedupe_key LIKE 'received:%'))")
        if not self.watcher or self.watcher.done():
            self.watcher = asyncio.create_task(self.watch())

    async def shutdown(self):
        if self.watcher:
            self.watcher.cancel()
            await asyncio.gather(self.watcher, return_exceptions=True)
