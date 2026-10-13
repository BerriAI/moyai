"""Durable Slack uploads, using the same attachment pipeline as web inputs."""
import asyncio
import hashlib
import json
import math
from pathlib import PurePath
import re
import tempfile
from urllib.parse import urlsplit

import aiohttp
from agentchat.channels.slack_files import slack_attachments

from .audio import AUDIO_TYPES, AudioTranscriber, audio_type
from .attachments import filename, inspect_file
from .connector_errors import ConnectorError


READ_ATTEMPTS = 2
READ_BUDGET_SECONDS = 90
RETRY_DELAY_SECONDS = 1
INSPECTION_BUDGET_SECONDS = 90
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=45, sock_connect=10, sock_read=15)
TRANSIENT_STATUSES = {408, 500, 502, 503, 504}

# Only application-owned prose/codes enter errors and events. Never copy a
# provider exception, URL, filename, response body or arbitrary header value.
FAILURES = {
    'access_changed': ('Slack access changed.', 'Reconnect Slack and resend the attachment.'),
    'authentication': ('Could not read this Slack attachment because access was denied.', 'Reconnect Slack and verify the bot can access the file.'),
    'missing_scope': ('Reconnect Slack with files:read permission.', 'Reconnect Slack and resend the attachment.'),
    'unavailable': ('This Slack file is unavailable.', 'Upload the attachment again or describe its contents.'),
    'invalid_identifier': ('Expected a Slack file identifier.', 'Resend the attachment from Slack.'),
    'invalid_size': ('Slack returned an invalid file size.', 'Upload the attachment again.'),
    'hard_limit': ('This file exceeds the workspace attachment storage budget.', 'Ask an administrator to increase the budget, or send a smaller file.'),
    'unsupported_location': ('Slack returned an unsupported file location.', 'Upload the attachment directly to Slack again.'),
    'redirect': ('Could not read this Slack attachment without following a redirect.', 'Upload the attachment again; private-file redirects are not followed.'),
    'encoded_response': ('Slack returned an encoded transfer despite the identity request.', 'Retry later or upload the file again; compressed transfer bytes were not accepted as file bytes.'),
    'invalid_headers': ('Slack returned unverifiable download headers.', 'Retry later or upload the file again.'),
    'partial_response': ('Slack returned only a partial file response.', 'Retry later or upload the complete attachment again.'),
    'metadata_size_mismatch': ('Slack file did not match its declared size.', 'Upload the attachment again or describe its contents; the complete file could not be verified.'),
    'truncated_stream': ('Slack file download ended with an incomplete stream.', 'Retry later or upload the attachment again.'),
    'rate_limited': ('Slack rate-limited the attachment read.', 'Wait for Slack’s retry interval, then retry the attachment.'),
    'transient_http': ('Slack temporarily could not serve the attachment.', 'Retry later or describe the missing content.'),
    'timeout': ('Slack attachment read timed out.', 'Retry later or send a smaller attachment.'),
    'read_budget': ('Slack attachment recovery reached its time budget.', 'Retry later or send a smaller attachment.'),
    'transport': ('The Slack attachment connection was interrupted.', 'Retry later or upload the attachment again.'),
    'tls': ('The Slack attachment connection could not be verified.', 'Ask an administrator to check the network connection.'),
    'storage': ('The attachment could not be written to temporary storage.', 'Ask an administrator to check workspace storage.'),
    'inspection_timeout': ('The attachment could not be inspected within its time budget.', 'Send a smaller attachment or describe its contents.'),
}


class SlackFileError(ValueError):
    """An allowlisted explanation plus bounded, non-content read diagnostics."""
    def __init__(self, code, *, retryable=False, retry_after=None):
        self.code = code
        self.retryable, self.retry_after = retryable, retry_after
        self.history = []
        self.retry_blocker = ''
        super().__init__(FAILURES[code][0])

    def diagnostic(self):
        latest = self.history[-1] if self.history else {'stage': 'access'}
        attempts = latest.get('attempt', 0)
        return {'phase': 'slack_attachment_read', 'code': self.code, **latest,
                'attempts': attempts, 'attempt_history': self.history,
                'recovery_tried': ('A read-only retry was attempted; the attempt history identifies the stage reached.'
                                   if attempts > 1 else 'No retry was made.'),
                'remaining_blocker': self.retry_blocker or FAILURES[self.code][0],
                'next_action': FAILURES[self.code][1],
                **({'retry_after_seconds': self.retry_after} if self.retry_after is not None else {})}

    def __str__(self):
        detail = self.diagnostic()
        counts = ', '.join(f'{key}={detail[key]}' for key in ('declared_bytes', 'received_bytes')
                           if detail.get(key) is not None)
        return (f'{FAILURES[self.code][0]} '
                f'[stage={detail["stage"]}, code={self.code}, attempts={detail["attempts"]}'
                f'{", " + counts if counts else ""}] '
                f'{detail["recovery_tried"]} Remaining blocker: {detail["remaining_blocker"]} '
                f'Next action: {detail["next_action"]}')


def retry_seconds(value):
    """Slack documents Retry-After as seconds. Invalid/missing means do not retry."""
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


async def file_operation(operation, *args):
    """Drain a file operation before cancellation can close its temporary file."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await task
        except Exception as exc:
            raise cancelled from exc
        raise


def file_ids(files):
    """Persist SDK-normalized references; downloads happen on the claimed turn."""
    if not isinstance(files, list):
        return []
    # The SDK normalizer has a per-call bound. Enumerate every transport batch
    # without turning that implementation detail into a retained-source limit.
    return list(dict.fromkeys(item.id for offset in range(0, len(files), 100)
                              for item in slack_attachments(files[offset:offset+100], limit=100)))


class SlackFiles:
    def __init__(self, owner):
        self.owner = owner
        self.transcriber = AudioTranscriber(owner.settings)
        self.slots = asyncio.Semaphore(2)
        self.rate_limit_until = 0.0

    def check_rate_limit(self):
        remaining = self.rate_limit_until - asyncio.get_running_loop().time()
        if remaining > 0:
            raise SlackFileError('rate_limited', retryable=True, retry_after=remaining)

    def check_access(self, team):
        connectors = self.owner.connectors
        installation = connectors.slack_installation()
        if not self.owner.status()['enabled'] or installation.get('team_id') != team:
            raise SlackFileError('access_changed')

    async def read(self, file_id, team):
        """Retry only verified GETs, never publish partial or unverifiable bytes."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READ_BUDGET_SECONDS
        history, state = [], {'stage': 'access'}
        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(READ_ATTEMPTS):
                    state = {'stage': 'access', 'attempt': attempt + 1}
                    try:
                        name, raw, claimed_type = await self.download(file_id, team, state)
                        break
                    except SlackFileError as exc:
                        if exc.code == 'rate_limited' and exc.retry_after is not None:
                            self.rate_limit_until = max(self.rate_limit_until, loop.time() + exc.retry_after)
                        history.append({**state, 'code': exc.code})
                        exc.history = list(history)
                        if not exc.retryable or attempt + 1 >= READ_ATTEMPTS:
                            raise
                        delay = exc.retry_after if exc.code == 'rate_limited' else RETRY_DELAY_SECONDS
                        if delay is None:
                            exc.retry_blocker = 'Slack did not supply a usable Retry-After interval; no early retry was made.'
                            raise
                        if delay >= deadline - loop.time():
                            exc.retry_blocker = (
                                'Slack’s retry interval exceeds the remaining read budget; no early retry was made.'
                                if exc.code == 'rate_limited' else
                                'The remaining read budget is too short for another attempt.')
                            raise
                        state = {**state, 'stage': 'retry_wait'}
                        await asyncio.sleep(delay)
        except TimeoutError:
            exc = SlackFileError('read_budget')
            exc.history = [*history, {**state, 'code': exc.code}]
            raise exc from None
        transferred = False
        try:
            self.check_access(team)
            async with asyncio.timeout(INSPECTION_BUDGET_SECONDS):
                inspected = await self.inspect(name, raw, claimed_type)
            self.check_access(team)
            raw.seek(0)
            transferred = True
            return name, raw, inspected
        except TimeoutError:
            exc = SlackFileError('inspection_timeout')
            exc.history = [*history, {**state, 'stage': 'inspection', 'code': exc.code}]
            raise exc from None
        except SlackFileError as exc:
            exc.history = [*history, {**state, 'stage': 'access', 'code': exc.code}]
            raise
        finally:
            # Ownership transfers to prepare only on a successful return.
            if not transferred:
                raw.close()

    async def download(self, file_id, team, state):
        connectors = self.owner.connectors
        raw = None
        transferred = False
        try:
            self.check_access(team)
            if not isinstance(file_id, str) or not re.fullmatch(r'F[A-Z0-9]{1,30}', file_id):
                raise SlackFileError('invalid_identifier')
            state['stage'] = 'metadata'
            self.check_rate_limit()
            state['stage'] = 'access'
            token = await connectors.slack_bot_token()
            self.check_access(team)
            headers = {'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}
            state['stage'] = 'metadata'
            # Saved OAuth scopes can be stale after an installation changes.
            info = await connectors.request('GET', 'https://slack.com/api/files.info',
                headers=headers, params={'file': file_id}, allowed_errors=('missing_scope',))
            self.check_access(team)
            if info.get('ok') is False and info.get('error') == 'missing_scope':
                raise SlackFileError('missing_scope')
            file = info.get('file')
            if info.get('ok') is not True or not isinstance(file, dict) or file.get('id') != file_id or file.get('is_external'):
                raise SlackFileError('unavailable')
            size = file.get('size')
            if type(size) is not int or size <= 0:
                raise SlackFileError('invalid_size')
            state['declared_bytes'] = size
            hard_limit = self.owner.settings.attachment_storage_limit_mb * 1024 * 1024
            if size > hard_limit:
                raise SlackFileError('hard_limit')
            url = file.get('url_private_download') or file.get('url_private')
            try:
                target = urlsplit(url) if isinstance(url, str) else None
                if (not target or target.scheme != 'https' or target.hostname != 'files.slack.com'
                        or target.port not in (None, 443) or target.username or target.password
                        or target.fragment or not target.path.startswith('/files-pri/')):
                    raise ValueError
            except ValueError:
                raise SlackFileError('unsupported_location') from None
            name = filename(file.get('name') or 'attachment')
            state['stage'] = 'headers'
            self.check_rate_limit()
            # Compare file bytes, not aiohttp's implicitly decompressed transfer.
            # If Slack ignores identity, reject rather than decode without a bound.
            async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT, auto_decompress=False) as client:
                async with client.get(url, headers=headers, allow_redirects=False) as response:
                    state['http_status'] = response.status
                    if response.status == 429:
                        raise SlackFileError('rate_limited', retryable=True,
                                             retry_after=retry_seconds(response.headers.get('Retry-After')))
                    if response.status in {401, 403}:
                        raise SlackFileError('authentication')
                    if 300 <= response.status < 400:
                        raise SlackFileError('redirect')
                    if response.status in TRANSIENT_STATUSES:
                        raise SlackFileError('transient_http', retryable=True)
                    if response.status == 206 or response.headers.get('Content-Range'):
                        raise SlackFileError('partial_response', retryable=True)
                    if response.status != 200:
                        raise SlackFileError('unavailable')
                    if response.headers.get('Content-Encoding', 'identity').strip().lower() != 'identity':
                        raise SlackFileError('encoded_response', retryable=True)
                    length = response.headers.get('Content-Length')
                    if length is not None:
                        if not re.fullmatch(r'[0-9]{1,20}', length):
                            raise SlackFileError('invalid_headers', retryable=True)
                        state['content_length_bytes'] = int(length)
                        if int(length) > hard_limit:
                            raise SlackFileError('hard_limit')
                        if int(length) != size:
                            raise SlackFileError('metadata_size_mismatch', retryable=True)
                    state.update(stage='download', received_bytes=0)
                    raw = tempfile.TemporaryFile()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        state['received_bytes'] += len(chunk)
                        # Independent actual-byte cap; metadata is not authority
                        # to consume more storage. Check before writing the chunk.
                        if state['received_bytes'] > hard_limit:
                            raise SlackFileError('hard_limit')
                        if state['received_bytes'] > size:
                            raise SlackFileError('metadata_size_mismatch', retryable=True)
                        await file_operation(raw.write, chunk)
            if state['received_bytes'] != size:
                raise SlackFileError('metadata_size_mismatch', retryable=True)
            self.check_access(team)
            raw.seek(0)
            transferred = True
            return name, raw, file.get('mimetype') or ''
        except ConnectorError as exc:
            state['http_status'] = exc.status_code
            if exc.status_code == 429 or exc.code in {'ratelimited', 'rate_limited'}:
                failure = SlackFileError('rate_limited', retryable=True, retry_after=retry_seconds(exc.retry_after))
            elif exc.code == 'missing_scope':
                failure = SlackFileError('missing_scope')
            elif state['stage'] == 'access' or exc.status_code in {401, 403} or exc.code in {'invalid_auth', 'token_revoked', 'not_authed', 'account_inactive'}:
                failure = SlackFileError('authentication')
            elif exc.status_code in TRANSIENT_STATUSES or exc.retryable:
                failure = SlackFileError('transport', retryable=True)
            else:
                failure = SlackFileError('unavailable')
            raise failure from None
        except aiohttp.ClientPayloadError:
            raise SlackFileError('truncated_stream', retryable=True) from None
        except (aiohttp.ClientSSLError, aiohttp.ServerFingerprintMismatch):
            raise SlackFileError('tls') from None
        except TimeoutError:
            raise SlackFileError('timeout', retryable=True) from None
        except aiohttp.ClientError:
            raise SlackFileError('transport', retryable=True) from None
        except OSError:
            raise SlackFileError('storage') from None
        finally:
            # Only a fully verified download transfers ownership to read().
            if raw is not None and not transferred:
                raw.close()

    async def inspect(self, name, raw, claimed_type):
        extension = PurePath(name).suffix.lower().lstrip('.')
        media_type = ''
        try:
            if extension not in AUDIO_TYPES and claimed_type.startswith('audio/'):
                raise ValueError('Unsupported audio preview format. The original file is available.')
            if extension in AUDIO_TYPES:
                media_type = audio_type(name, raw.read(16))
                raw.seek(0)
                transcript = await self.transcriber.transcribe(raw, name, media_type)
                return media_type, b'', transcript
            inspected = await file_operation(inspect_file, raw)
            if claimed_type.startswith('image/') and not inspected[1]:
                raise ValueError('This image could not be opened. The original file is available.')
            return inspected
        except ValueError as exc:
            return media_type or 'application/octet-stream', b'', '', str(exc)

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
            conn.begin_write()
            message = conn.execute("SELECT id,status FROM messages WHERE run_id=? AND role='user' ORDER BY id LIMIT 1", (run_id,)).fetchone()
            if not message or message['id'] != run.get('active_message_id') or message['status'] != 'running':
                return
            pending = conn.execute('SELECT * FROM slack_audio_inputs WHERE message_id=?', (message['id'],)).fetchone()
            if pending and pending['status'] != 'pending':
                return
            ids = json.loads(pending['files_json']) if pending else []
            messages = source.get('messages', [])
            # Keep directly addressed files first without discarding later ones.
            messages = sorted(messages, key=lambda item: item['ts'] != source['mention_ts'])
            for item in messages:
                if item['ts'] == source['mention_ts'] or source.get('kind') == 'thread':
                    ids.extend(item.get('file_ids', []))
            ids = list(dict.fromkeys(ids))
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
            attachments, errors = [], []
            for file_id in json.loads(row['files_json']):
                attachment_id = hashlib.sha256(f"slack:{row['message_id']}:{file_id}".encode()).hexdigest()[:32]
                prior = store.rows("SELECT size FROM attachments WHERE id=? AND owner_id=? AND upload_state='ready'", (attachment_id, row['user_id']))
                if prior:
                    attachments.append(attachment_id)
                    continue
                try:
                    async with self.slots:
                        item = await self.read(file_id, team)
                    name, raw, inspected = item
                    try:
                        await asyncio.to_thread(store.attachments.save, attachment_id, row['user_id'], name, raw, inspected,
                                                self.owner.settings.attachment_storage_limit_mb * 1024 * 1024)
                    finally:
                        if hasattr(raw, 'close'):
                            raw.close()
                    attachments.append(attachment_id)
                except SlackFileError as exc:
                    errors.append(str(exc))
                    store.event(run_id, 'error', str(exc), exc.diagnostic(), turn_id=row['message_id'])
                except ValueError as exc:
                    errors.append(str(exc))
                except Exception:
                    errors.append('Could not read this Slack attachment. Upload it again or describe its contents.')
            with store.connect() as conn:
                conn.begin_write()
                current = conn.execute('SELECT status FROM messages WHERE id=?', (row['message_id'],)).fetchone()
                if not current or current['status'] != 'running':
                    return
                try:
                    # Remote saves can yield after Slack's download guard.
                    # Recheck the entire batch, including recovered draft IDs,
                    # under the same writer lock that publishes their access.
                    self.check_access(team)
                except ValueError as exc:
                    attachments = []
                    errors.append(str(exc))
                store.attachments.bind_in(conn, attachments, row['message_id'], row['user_id'])
                if errors:
                    explanation = '\n\n[Slack attachments could not be read: ' + ' '.join(dict.fromkeys(errors)) + ' Do not guess the missing content.]'
                    conn.execute('UPDATE messages SET content=content||? WHERE id=?', (explanation, row['message_id']))
                conn.execute("UPDATE slack_audio_inputs SET status='ready' WHERE message_id=?", (row['message_id'],))
            await self.owner.checkpoints.flush()
