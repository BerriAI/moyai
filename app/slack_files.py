"""Moyai access, inspection and retention boundary for AgentChat Slack files."""
import asyncio
from functools import partial
from pathlib import PurePath

from agentchat.channels.slack.files import (
    CanvasFormatError,
    SlackFileError,
    SlackFileReader,
    SlackTransportError,
    file_operation,
    inspect_canvas,
)

from .audio import AUDIO_TYPES, AudioTranscriber, audio_type
from .attachments import inspect_file
from .connector_errors import ConnectorError
from .db import database


INSPECTION_BUDGET_SECONDS = 90


def transport_error(error):
    """Only safe canonical failure metadata crosses the SDK boundary."""
    return SlackTransportError(
        error.code,
        status_code=error.status_code,
        dispatched=error.dispatched,
        rejected=error.rejected,
        retryable=error.retryable,
        retry_after=error.retry_after,
    )


class SlackFiles:
    def __init__(self, owner):
        self.owner = owner
        self.transcriber = AudioTranscriber(owner.settings)
        self.reader = SlackFileReader(
            max_bytes=owner.settings.attachment_storage_limit_mb * 1024 * 1024,
        )

    def check_access(self, team, *, connection=None):
        connectors = self.owner.connectors
        installation = connectors.slack_installation(connection=connection)
        if not self.owner.status(connection=connection)['enabled'] or installation.get('team_id') != team:
            raise SlackFileError('access_changed')

    async def token(self):
        try:
            return await self.owner.connectors.slack_bot_token()
        except ConnectorError as error:
            raise transport_error(error) from None

    async def request(self, method, payload, token):
        try:
            return await self.owner.connectors.request(
                'GET',
                'https://slack.com/api/' + method,
                headers={'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'},
                params=payload,
                allowed_errors=('missing_scope',),
            )
        except ConnectorError as error:
            raise transport_error(error) from None

    async def read(self, file_id, team):
        downloaded = await self.reader.read(
            file_id,
            token=self.token,
            request=self.request,
            authorize=partial(database, self.check_access, team),
        )
        raw = downloaded.raw
        transferred = False
        try:
            await database(self.check_access, team)
            async with asyncio.timeout(INSPECTION_BUDGET_SECONDS):
                if downloaded.representation == 'canvas_html':
                    inspected = await file_operation(inspect_canvas, raw)
                else:
                    inspected = await self.inspect(downloaded.name, raw, downloaded.media_type)
            await database(self.check_access, team)
            raw.seek(0)
            transferred = True
            return downloaded.name, raw, inspected
        except (TimeoutError, CanvasFormatError, SlackFileError) as error:
            stage = 'inspection'
            if isinstance(error, SlackFileError):
                failure = error
                stage = 'access'
            elif isinstance(error, TimeoutError):
                failure = SlackFileError('inspection_timeout')
            else:
                failure = SlackFileError('canvas_representation')
            failure.history = [
                *downloaded.history,
                {**downloaded.state, 'stage': stage, 'code': failure.code},
            ]
            raise failure from None
        finally:
            if not transferred:
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
