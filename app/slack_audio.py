"""Durable, message-scoped Slack audio intake outside the webhook deadline."""
import asyncio
import hashlib
import json
from pathlib import PurePath
import re
from urllib.parse import urlsplit

import httpx

from .audio import AUDIO_TYPES, AudioTranscriber, audio_type
from .attachments import MAX_FILE, MAX_MESSAGE, filename


def audio_files(files):
    """Keep only bounded identifiers from signed events, never supplied URLs."""
    if not isinstance(files, list):
        return []
    selected = []
    for file in files[:5]:
        if not isinstance(file, dict) or not re.fullmatch(r'F[A-Z0-9]{7,30}', str(file.get('id', ''))):
            continue
        kind = file.get('mimetype', '')
        extension = PurePath(str(file.get('name', ''))).suffix.lower().lstrip('.')
        if (not kind or str(kind).startswith('audio/') or file.get('subtype') == 'slack_audio'
                or extension in AUDIO_TYPES or file.get('filetype') in AUDIO_TYPES):
            selected.append(file['id'])
    return list(dict.fromkeys(selected))


class SlackAudio:
    def __init__(self, owner):
        self.owner = owner
        self.transcriber = AudioTranscriber(owner.settings)
        self.slots = asyncio.Semaphore(2)

    async def read(self, file_id, team):
        connectors = self.owner.connectors
        installation = connectors.slack_installation()
        if not self.owner.status()['enabled'] or installation.get('team_id') != team:
            raise ValueError('Slack access changed. Reconnect Slack and resend the audio.')
        if 'files:read' not in installation.get('scopes', []):
            raise ValueError('Reconnect Slack with files:read permission, then resend the audio.')
        token = await connectors.slack_bot_token()
        headers = {'Authorization': 'Bearer ' + token}
        info = await connectors.request('GET', 'https://slack.com/api/files.info', headers=headers, params={'file': file_id})
        file = info.get('file', {})
        if file.get('id') != file_id or file.get('is_external'):
            raise ValueError('This Slack audio file is unavailable. Upload the recording again.')
        name = filename(file.get('name') or 'recording.' + file.get('filetype', 'webm'))
        extension = PurePath(name).suffix.lower().lstrip('.')
        if extension not in AUDIO_TYPES:
            if not str(file.get('mimetype', '')).startswith('audio/'):
                return None
            raise ValueError('Unsupported audio format. Send MP3, WAV, M4A, WebM, OGG or FLAC.')
        if not isinstance(file.get('size'), int) or not 0 < file['size'] <= MAX_FILE:
            raise ValueError('Each audio recording must be 10 MB or smaller.')
        url = file.get('url_private_download') or file.get('url_private', '')
        parsed = urlsplit(url)
        # Do not send a bot token to event-provided URLs or redirected hosts.
        if (parsed.scheme != 'https' or parsed.hostname != 'files.slack.com' or parsed.port not in (None, 443)
                or parsed.username or parsed.password or not parsed.path.startswith('/files-pri/')):
            raise ValueError('Slack returned an unsupported file location. Upload the recording again.')
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            async with client.stream('GET', url, headers=headers) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise ValueError('Each audio recording must be 10 MB or smaller.')
                    chunks.append(chunk)
        raw = b''.join(chunks)
        media_type = audio_type(name, raw)
        transcript = await self.transcriber.transcribe(raw, name, media_type)
        return name, raw, (media_type, b'', transcript)

    async def prepare(self, run_id):
        store = self.owner.store
        run = store.run(run_id)
        # Only the claimed turn is read. Queued inputs cannot leak into it.
        rows = store.rows("SELECT a.*,m.user_id FROM slack_audio_inputs a JOIN messages m ON m.id=a.message_id "
                          "WHERE m.run_id=? AND m.id=? AND a.status='pending' AND m.status='running'",
                          (run_id, run.get('active_message_id')))
        for row in rows:
            source = store.rows('SELECT team_id FROM slack_receipts WHERE message_id=?', (row['message_id'],))
            team = source[0]['team_id'] if source else self.owner.connectors.slack_installation().get('team_id')
            store.event(run_id, 'context', 'Transcribing the audio attached to your Slack message')
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
                    if item is None:
                        continue
                    name, raw, inspected = item
                    total += len(raw)
                    if total > MAX_MESSAGE:
                        raise ValueError('Audio attachments must total 20 MB or less per message.')
                    store.attachments.save(attachment_id, row['user_id'], name, raw, inspected,
                                           self.owner.settings.attachment_storage_limit_mb * 1024 * 1024)
                    attachments.append(attachment_id)
                except ValueError as exc:
                    errors.append(str(exc))
                except Exception:
                    errors.append('Could not download or transcribe this Slack audio. Upload it again or send text.')
            with store.connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                current = conn.execute('SELECT status FROM messages WHERE id=?', (row['message_id'],)).fetchone()
                if not current or current['status'] != 'running':
                    return
                store.attachments.bind_in(conn, attachments, row['message_id'], row['user_id'])
                if errors:
                    explanation = '\n\n[Audio could not be understood: ' + ' '.join(dict.fromkeys(errors)) + ' Do not guess the missing speech.]'
                    conn.execute('UPDATE messages SET content=content||? WHERE id=?', (explanation, row['message_id']))
                conn.execute("UPDATE slack_audio_inputs SET status='ready' WHERE message_id=?", (row['message_id'],))
            await self.owner.checkpoints.flush()
