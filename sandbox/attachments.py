"""Restore immutable user inputs before each turn, including on fresh machines."""
import hashlib
import os
from pathlib import Path
import re
import urllib.request

try:
    from .startup import read_with_reconnect
except ImportError:
    from startup import read_with_reconnect

MAX_FILE = 10 * 1024 * 1024


def prepare_attachments(spec, token, *, root=Path('/workspace/.moyai-attachments'), opener=urllib.request.urlopen, notify=None):
    files = spec.get('attachments', [])
    if not files:
        return
    if root.is_symlink():
        raise ValueError('Attachment directory must not be a symlink')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in files:
        attachment_id, name = item['id'], item['name']
        if (not re.fullmatch(r'[0-9a-f]{32}', attachment_id) or name in {'', '.', '..'}
                or '/' in name or '\\' in name or not 0 < item['size'] <= MAX_FILE):
            raise ValueError('Invalid attachment metadata')
        directory = root / attachment_id
        if directory.is_symlink():
            raise ValueError('Attachment directory must not be a symlink')
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / name
        if path.is_symlink():
            raise ValueError('Attachment file must not be a symlink')
        if path.is_file() and path.stat().st_size == item['size'] and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']:
            continue
        request = urllib.request.Request(spec['broker_url'].rstrip('/') + '/attachments/' + attachment_id,
                                         headers={'Authorization': 'Bearer ' + token})
        raw = read_with_reconnect(request, lambda response: response.read(MAX_FILE + 1),
                                  stage='attachments', opener=opener, notify=notify)
        if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Attachment download was incomplete')
        temporary = directory / '.download'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        temporary.replace(path)
        path.chmod(0o444)
