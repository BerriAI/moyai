"""Completed browser captures, stored independently of sandbox lifetime."""
from .private_sinks import require_owner
from pathlib import Path
import re
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


def listing(settings, run_id, *, store, actor=""):
    require_owner(store, run_id, actor)
    prefix = directory(settings, run_id).name + '/'
    result = []
    for row in store.artifacts.listing(prefix):
        name = row['name'].removeprefix(prefix)
        if not valid_name(name):
            continue
        url = f'/api/runs/{run_id}/computer/captures/{name}'
        result.append({'path': 'moyai-captures/' + name, 'workspace_path': 'moyai-captures/' + name,
                       'archive_path': 'capture:' + name, 'name': name, 'size': row['size'],
                       'kind': 'video' if name.endswith('.webm') else 'image', 'url': url + '?download=true',
                       'inline_url': url})
    require_owner(store, run_id, actor)
    return result


def read(path: Path, *, store, actor="") -> tuple[bytes, str]:
    """Read bounded media without following a replaced file's symlink."""
    name = path.relative_to(store.path.parent / 'artifacts').as_posix()
    require_owner(store, path.parent.name.removesuffix('-captures'), actor)
    try:
        raw = store.artifacts.read(name, MAX_FILE)
    except FileNotFoundError:
        raise HTTPException(404, 'Capture not found.') from None
    require_owner(store, path.parent.name.removesuffix('-captures'), actor)
    return raw, media_type(path.name, raw)


def response(path: Path, request, download=False, *, store, actor=""):
    raw, mime = read(path, store=store, actor=actor)
    require_owner(store, path.parent.name.removesuffix("-captures"), actor)
    return bytes_response(raw, mime, path.name, request, download)


def bytes_response(raw: bytes, mime: str, name: str, request, download=False):
    headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Accept-Ranges': 'bytes',
               'Content-Disposition': ('attachment' if download else 'inline') + "; filename*=UTF-8''" + quote(name)}
    status = 200
    ranges = request.headers.get('range') if not request.headers.get('if-range') else None
    if ranges and not download:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', ranges)
        if not match or not any(match.groups()) or len(ranges) > 100:
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
