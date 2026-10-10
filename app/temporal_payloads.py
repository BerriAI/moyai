"""Encrypted, offloaded Temporal payloads; never part of downloadable artifacts.

Keep these objects and the encryption key for at least Temporal's history
retention (including archived histories). Old ID-only histories remain readable.
"""
import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

from cryptography.fernet import Fernet
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadCodec

from .security import local_secret


class PrivatePayloads:
    def __init__(self, store, settings):
        self.objects = store.objects
        self.directory = Path(settings.data_dir) / 'temporal-payloads'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = settings.encryption_key or local_secret(
            Path(settings.data_dir) / 'encryption.key', lambda: Fernet.generate_key().decode())
        self.cipher = Fernet(key.encode())

    def put(self, raw: bytes) -> dict:
        encrypted = self.cipher.encrypt(raw)
        checksum = hashlib.sha256(encrypted).hexdigest()
        if self.objects.enabled:
            reference = self.objects.put(encrypted)
        else:
            # Publish only a complete file, including after a killed worker.
            fd, name = tempfile.mkstemp(dir=self.directory)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(encrypted)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.directory / checksum)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            reference = 'local:' + checksum
        return {'reference': reference, 'size': len(encrypted), 'sha256': checksum}

    def read(self, pointer: dict) -> bytes:
        reference, size, checksum = pointer['reference'], pointer['size'], pointer['sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', checksum) or type(size) is not int or size < 1:
            raise ValueError('Invalid private payload reference')
        if reference == 'local:' + checksum:
            with (self.directory / checksum).open('rb') as source:
                encrypted = source.read(size + 1)
        else:
            encrypted = self.objects.read(reference, size)
        if len(encrypted) != size or hashlib.sha256(encrypted).hexdigest() != checksum:
            raise ValueError('Private payload failed integrity verification')
        return self.cipher.decrypt(encrypted)


class PrivatePayloadCodec(PayloadCodec):
    encoding = b'moyai/encrypted-reference-v1'

    def __init__(self, payloads: PrivatePayloads):
        self.payloads = payloads

    async def encode(self, payloads: Iterable[Payload]) -> list[Payload]:
        result = []
        for payload in payloads:
            pointer = await asyncio.to_thread(self.payloads.put, payload.SerializeToString())
            result.append(Payload(metadata={'encoding': self.encoding}, data=json.dumps(pointer).encode()))
        return result

    async def decode(self, payloads: Iterable[Payload]) -> list[Payload]:
        result = []
        for payload in payloads:
            if payload.metadata.get('encoding') == self.encoding:
                raw = await asyncio.to_thread(self.payloads.read, json.loads(payload.data))
                result.append(Payload.FromString(raw))
            else:
                result.append(payload)  # Pre-migration workflows carried IDs only.
        return result
