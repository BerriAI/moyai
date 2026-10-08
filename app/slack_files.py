"""Durable Slack uploads, using the same attachment pipeline as web inputs."""
import asyncio
import hashlib
import json
from pathlib import PurePath

from agentchat import Attachment
from agentchat.channels.slack_files import download_slack_file, slack_attachments

from .audio import AUDIO_TYPES, AudioTranscriber, audio_type
from .attachments import MAX_FILE, MAX_FILES, MAX_MESSAGE, filename, inspect_file


def file_ids(files):
    """Persist SDK-normalized references; downloads happen on the claimed turn."""
    return [item.id for item in slack_attachments(files, limit=MAX_FILES)]


class SlackFiles:
    def __init__(self, owner):
        self.owner = owner
        self.transcriber = AudioTranscriber(owner.settings)
        self.slots = asyncio.Semaphore(2)

    def check_access(self, team):
        connectors = self.owner.connectors
        installation = connectors.slack_installation()
        if not self.owner.status()['enabled'] or installation.get('team_id') != team:
            raise ValueError('Slack access changed. Reconnect Slack and resend the attachment.')

    async def read(self, file_id, team):
        connectors = self.owner.connectors
        self.check_access(team)
        token = await connectors.slack_bot_token()
        headers = {'Authorization': 'Bearer ' + token}

        async def request(api, payload):
            # Saved OAuth scopes can be stale after an installation changes.
            # Let Slack authorize the bot token rather than rejecting locally.
            result = await connectors.request('GET', 'https://slack.com/api/' + api,
                                              headers=headers, params=payload,
                                              allowed_errors=('missing_scope',))
            if result.get('ok') is False and result.get('error') == 'missing_scope':
                raise ValueError('Reconnect Slack with files:read permission, then resend the attachment.')
            return result

        async def guard():
            self.check_access(team)

        file = await download_slack_file(Attachment(id=file_id), bot_token=token, request=request,
                                         max_bytes=MAX_FILE, before_download=guard)
        name, raw = filename(file.name), file.content
        extension = PurePath(name).suffix.lower().lstrip('.')
        if extension not in AUDIO_TYPES and (file.media_type or '').startswith('audio/'):
            raise ValueError('Unsupported audio format. Send MP3, WAV, M4A, WebM, OGG or FLAC.')
        if extension in AUDIO_TYPES:
            media_type = audio_type(name, raw)
            transcript = await self.transcriber.transcribe(raw, name, media_type)
            self.check_access(team)
            return name, raw, (media_type, b'', transcript)
        inspected = await asyncio.to_thread(inspect_file, raw)
        self.check_access(team)
        if (file.media_type or '').startswith('image/') and not inspected[1]:
            raise ValueError('This image could not be opened. Try exporting it as PNG or JPEG.')
        return name, raw, inspected

    def include_context(self, run_id):
        """Recover files omitted by app_mention, plus files in the invoked thread.

        Never import unrelated files from nearby channel messages or attach the
        same frozen context to every follow-up. Keep event-provided IDs first.
        """
        store = self.owner.store
        source, run = store.slack_source(run_id), store.run(run_id)
        if not source or source['context_status'] != 'ready':
            return
        with store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            message = conn.execute("SELECT id,status FROM messages WHERE run_id=? AND role='user' ORDER BY id LIMIT 1", (run_id,)).fetchone()
            if not message or message['id'] != run.get('active_message_id') or message['status'] != 'running':
                return
            pending = conn.execute('SELECT * FROM slack_audio_inputs WHERE message_id=?', (message['id'],)).fetchone()
            if pending and pending['status'] != 'pending':
                return
            ids = json.loads(pending['files_json']) if pending else []
            messages = source.get('messages', [])
            # The directly addressed message wins the shared per-message file budget.
            messages = sorted(messages, key=lambda item: item['ts'] != source['mention_ts'])
            for item in messages:
                if item['ts'] == source['mention_ts'] or source.get('kind') == 'thread':
                    ids.extend(item.get('file_ids', []))
            ids = list(dict.fromkeys(ids))[:MAX_FILES]
            if ids:
                conn.execute('INSERT INTO slack_audio_inputs(message_id,files_json) VALUES(?,?) '
                             'ON CONFLICT(message_id) DO UPDATE SET files_json=excluded.files_json',
                             (message['id'], json.dumps(ids)))

    async def prepare(self, run_id):
        store = self.owner.store
        run = store.run(run_id)
        # Only the claimed turn is read. Queued inputs cannot leak into it.
        rows = store.rows("SELECT a.*,m.user_id FROM slack_audio_inputs a JOIN messages m ON m.id=a.message_id "
                          "WHERE m.run_id=? AND m.id=? AND a.status='pending' AND m.status='running'",
                          (run_id, run.get('active_message_id')))
        for row in rows:
            team = row['user_id'].split(':')[1]
            store.event(run_id, 'context', 'Reading the files attached to your Slack message')
            attachments, errors, total = [], [], 0
            for file_id in json.loads(row['files_json']):
                attachment_id = hashlib.sha256(f"slack:{row['message_id']}:{file_id}".encode()).hexdigest()[:32]
                prior = store.rows('SELECT size FROM attachments WHERE id=? AND owner_id=?', (attachment_id, row['user_id']))
                if prior:
                    total += prior[0]['size']
                    attachments.append(attachment_id)
                    continue
                try:
                    async with self.slots, asyncio.timeout(125):
                        item = await self.read(file_id, team)
                    name, raw, inspected = item
                    if total + len(raw) > MAX_MESSAGE:
                        raise ValueError('Attachments must total 20 MB or less per message.')
                    store.attachments.save(attachment_id, row['user_id'], name, raw, inspected,
                                           self.owner.settings.attachment_storage_limit_mb * 1024 * 1024)
                    total += len(raw)
                    attachments.append(attachment_id)
                except ValueError as exc:
                    errors.append(str(exc))
                except Exception:
                    errors.append('Could not read this Slack attachment. Upload it again or describe its contents.')
            with store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT status FROM messages WHERE id=?', (row['message_id'],)).fetchone()
                if not current or current['status'] != 'running':
                    return
                store.attachments.bind_in(conn, attachments, row['message_id'], row['user_id'])
                if errors:
                    explanation = '\n\n[Slack attachments could not be read: ' + ' '.join(dict.fromkeys(errors)) + ' Do not guess the missing content.]'
                    conn.execute('UPDATE messages SET content=content||? WHERE id=?', (explanation, row['message_id']))
                conn.execute("UPDATE slack_audio_inputs SET status='ready' WHERE message_id=?", (row['message_id'],))
            await self.owner.checkpoints.flush()
