"""Audio transcription through the workspace's existing LiteLLM gateway."""
import asyncio
import json
from pathlib import PurePath

import httpx


AUDIO_TYPES = {'mp3': 'audio/mpeg', 'mpga': 'audio/mpeg', 'mpeg': 'audio/mpeg', 'wav': 'audio/wav', 'm4a': 'audio/mp4',
               'mp4': 'audio/mp4', 'webm': 'audio/webm', 'ogg': 'audio/ogg',
               'oga': 'audio/ogg', 'flac': 'audio/flac'}


def audio_type(name, raw):
    extension = PurePath(name).suffix.lower().lstrip('.')
    if extension not in AUDIO_TYPES:
        return ''
    signatures = {
        'mp3': raw.startswith(b'ID3') or (len(raw) > 1 and raw[0] == 255 and raw[1] & 224 == 224),
        'wav': raw.startswith(b'RIFF') and raw[8:12] == b'WAVE',
        'm4a': raw[4:8] == b'ftyp', 'mp4': raw[4:8] == b'ftyp',
        'webm': raw.startswith(b'\x1aE\xdf\xa3'),
        'ogg': raw.startswith(b'OggS'), 'oga': raw.startswith(b'OggS'),
        'flac': raw.startswith(b'fLaC'),
    }
    signatures['mpeg'] = signatures['mpga'] = signatures['mp3']
    if not signatures[extension]:
        raise ValueError('This audio file could not be read. Export it as MP3, WAV, M4A, WebM, OGG or FLAC.')
    return AUDIO_TYPES[extension]


class AudioTranscriber:
    def __init__(self, settings):
        self.settings = settings
        self.slots = asyncio.Semaphore(2)

    async def transcribe(self, raw, name, media_type):
        settings = self.settings
        if not (settings.litellm_api_base and settings.litellm_api_key and settings.audio_transcription_model):
            raise ValueError('Audio transcription is not configured. Ask an administrator to enable a transcription model on the LiteLLM gateway, or send text.')
        data = {'model': settings.audio_transcription_model, 'response_format': 'json'}
        if settings.audio_transcription_prompt:
            data['prompt'] = settings.audio_transcription_prompt
        async with self.slots:
            try:
                async with asyncio.timeout(90), httpx.AsyncClient(timeout=85, follow_redirects=False) as client:
                    async with client.stream('POST', settings.litellm_api_base.rstrip('/') + '/audio/transcriptions',
                        headers={'Authorization': 'Bearer ' + settings.litellm_api_key},
                        data=data,
                        files={'file': (name, raw, media_type)}) as response:
                        response.raise_for_status()
                        chunks = []
                        async for chunk in response.aiter_bytes():
                            chunks.append(chunk)
                        result = json.loads(b''.join(chunks))
                text = result.get('text') if isinstance(result, dict) else None
                if not isinstance(text, str) or not text.strip():
                    raise ValueError('No speech was detected. Try another recording or type your message.')
                return text.strip()
            except (httpx.HTTPError, TimeoutError):
                # Provider responses may contain credentials or private audio text.
                raise ValueError('Audio transcription failed. Retry, send text, or ask an administrator to check the gateway transcription model.') from None
            except (UnicodeError, json.JSONDecodeError):
                raise ValueError('The transcription service returned an invalid response. Retry or send text.') from None
