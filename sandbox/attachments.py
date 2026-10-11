"""Restore immutable user inputs before each turn, including on fresh machines."""
import hashlib
import os
from pathlib import Path
import re
import urllib.request

try:
    from .startup import read_with_reconnect
    from .access_transport import broker_headers, open_broker
except ImportError:
    from startup import read_with_reconnect
    from access_transport import broker_headers, open_broker

def prepare_attachments(spec, token, *, root=Path('/workspace/.moyai-attachments'), opener=open_broker, notify=None):
    files = spec.get('attachments', [])
    if not files:
        return
    if root.is_symlink():
        raise ValueError('Attachment directory must not be a symlink')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in files:
        attachment_id, name = item['id'], item['name']
        if (not re.fullmatch(r'[0-9a-f]{32}', attachment_id) or name in {'', '.', '..'}
                or '/' in name or '\\' in name or type(item['size']) is not int or item['size'] <= 0):
            raise ValueError('Invalid attachment metadata')
        directory = root / attachment_id
        if directory.is_symlink():
            raise ValueError('Attachment directory must not be a symlink')
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / name
        if path.is_symlink():
            raise ValueError('Attachment file must not be a symlink')
        if path.is_file() and path.stat().st_size == item['size']:
            with path.open('rb') as source:
                if hashlib.file_digest(source, 'sha256').hexdigest() == item['sha256']:
                    continue
        request = urllib.request.Request(spec['broker_url'].rstrip('/') + '/attachments/' + attachment_id,
                                         headers=broker_headers(spec['broker_url'], token))
        temporary = directory / '.download'
        def download(response):
            checksum, size = hashlib.sha256(), 0
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                while chunk := response.read(min(1024 * 1024, item['size']-size+1)):
                    size += len(chunk)
                    if size > item['size']:
                        raise ValueError('Attachment download exceeded its manifest')
                    checksum.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != item['size'] or checksum.hexdigest() != item['sha256']:
                raise ValueError('Attachment download was incomplete')
        try:
            read_with_reconnect(request, download, stage='attachments', opener=opener, notify=notify)
            temporary.replace(path)
            path.chmod(0o444)
        finally:
            temporary.unlink(missing_ok=True)
