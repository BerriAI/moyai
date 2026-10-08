"""Completed browser captures, stored independently of sandbox lifetime."""
from pathlib import Path
import os
import re
import stat
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response

MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024


def valid_name(name):
    return isinstance(name, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,100}\.(png|webm)', name))


def media_type(name, raw):
    if name.endswith('.png') and raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if name.endswith('.webm') and raw.startswith(b'\x1aE\xdf\xa3') and b'webm' in raw[:128]:
        return 'video/webm'
    raise HTTPException(415, 'This capture is not a supported image or video.')


def directory(settings, run_id):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise HTTPException(404, 'Session not found.')
    return settings.data_dir / 'artifacts' / (run_id + '-captures')


def listing(settings, run_id):
    root = directory(settings, run_id)
    if not root.exists():
        return []
    result = []
    for path in sorted(root.iterdir(), key=lambda p: p.name):
        if not valid_name(path.name) or path.is_symlink() or not path.is_file():
            continue
        url = f'/api/runs/{run_id}/computer/captures/{path.name}'
        result.append({'path': 'moyai-captures/' + path.name, 'workspace_path': 'moyai-captures/' + path.name,
                       'archive_path': 'capture:' + path.name, 'name': path.name, 'size': path.stat().st_size,
                       'kind': 'video' if path.suffix == '.webm' else 'image', 'url': url + '?download=true',
                       'inline_url': url})
    return result


def read(path: Path) -> tuple[bytes, str]:
    """Read bounded media without following a replaced file's symlink."""
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404, 'Capture not found.')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise HTTPException(404, 'Capture not found.')
        if info.st_size > MAX_FILE:
            raise HTTPException(413, 'Capture exceeds the size limit.')
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise HTTPException(413, 'Capture exceeds the size limit.')
    return raw, media_type(path.name, raw)


def response(path: Path, request, download=False):
    raw, mime = read(path)
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'bytes',
               'Content-Disposition': ('attachment' if download else 'inline') + "; filename*=UTF-8''" + quote(path.name)}
    status = 200
    ranges = request.headers.get('range')
    if ranges and not download:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', ranges)
        if not match or not any(match.groups()):
            raise HTTPException(416, 'Invalid byte range.', headers={'Content-Range': f'bytes */{len(raw)}'})
        first, last = match.groups()
        start = int(first) if first else max(0, len(raw) - int(last))
        end = min(int(last), len(raw) - 1) if first and last else len(raw) - 1
        if start > end or start >= len(raw):
            raise HTTPException(416, 'Invalid byte range.', headers={'Content-Range': f'bytes */{len(raw)}'})
        headers['Content-Range'] = f'bytes {start}-{end}/{len(raw)}'
        raw = raw[start:end + 1]
        status = 206
    return Response(raw, media_type=mime, status_code=status, headers=headers)
