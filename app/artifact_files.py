"""Authenticated, bounded access to individual files in saved result archives.

Never extract agent-controlled paths onto the server. A revision pins previews
and downloads to the archive the user selected, rather than a later overwrite.
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
from urllib.parse import quote, urlencode
import zipfile
import zlib

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from . import captures
from .attachments import inspect_file

MAX_ARCHIVE = 20 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
MAX_PREVIEW = 128 * 1024
MAX_ENTRIES = 10000
MAX_LIST = 1000


def safe_member(info):
    name = info.filename
    mode = stat.S_IFMT(info.external_attr >> 16)
    return (bool(name) and len(name) <= 1024 and not info.is_dir()
            and not name.startswith('/') and '\\' not in name and ':' not in name
            and all(ord(c) >= 32 and ord(c) != 127 for c in name)
            and all(part not in {'', '.', '..'} for part in name.split('/'))
            and mode in {0, stat.S_IFREG} and not info.flag_bits & 1
            and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            and 0 <= info.file_size <= MAX_FILE)


@contextmanager
def saved_archive(settings, store, run_id, revision=None):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id) or not store.run(run_id):
        raise HTTPException(404, 'Session not found.')
    path = settings.data_dir / 'artifacts' / (run_id + '.zip')
    try:
        with path.open('rb') as stream:
            stamp = os.fstat(stream.fileno())
            if stamp.st_size > MAX_ARCHIVE:
                raise HTTPException(413, 'This archive is too large to browse.')
            current = hashlib.sha256(f'{stamp.st_mtime_ns}:{stamp.st_size}'.encode()).hexdigest()[:24]
            if revision is not None and revision != current:
                raise HTTPException(409, 'The saved files changed. Close and reopen Files to view the latest version.')
            with zipfile.ZipFile(stream) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_TOTAL:
                    raise HTTPException(413, 'This archive exceeds the file browser limits. Download the ZIP instead.')
                counts = Counter(i.filename for i in entries)
                files = {i.filename: i for i in entries if safe_member(i) and counts[i.filename] == 1}
                yield archive, files, current, len(entries) - len(files)
    except FileNotFoundError:
        raise HTTPException(404, 'No saved files are available yet.') from None
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        raise HTTPException(503, 'The saved archive could not be read. Try again or download the ZIP.') from None


def file_info(run_id, info, revision):
    path = info.filename.removeprefix('new-files/')
    url = f'/api/runs/{run_id}/files/content?' + urlencode({'path': info.filename, 'revision': revision})
    image = PurePosixPath(path).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
    return {'path': path, 'archive_path': info.filename, 'name': PurePosixPath(path).name,
            'workspace_path': path if info.filename.startswith('new-files/') else None,
            'size': info.file_size, 'url': url, 'preview_url': url + '&preview=true',
            **({'kind': 'image', 'inline_url': url + '&inline=true'} if image else {})}


def routes(settings, store, security):
    router = APIRouter()

    @router.get('/api/runs/{run_id}/files')
    def list_files(run_id: str, request: Request):
        security.require(request)
        media = captures.listing(settings, run_id) if store.run(run_id) else []
        if media and not (settings.data_dir / 'artifacts' / (run_id + '.zip')).exists():
            return {'revision': 'captures', 'files': media, 'limited': False, 'note': 'Saved browser captures.'}
        with saved_archive(settings, store, run_id) as (_, files, revision, skipped):
            # Source inventories must not crowd recovery artifacts out of the capped catalog.
            ordered = sorted(files.values(), key=lambda i: (i.filename.startswith('new-files/'), i.filename.casefold()))
            return {'revision': revision, 'files': media + [file_info(run_id, i, revision) for i in ordered[:MAX_LIST]],
                    'limited': len(ordered) > MAX_LIST or bool(skipped),
                    'note': 'Latest saved files. The workspace ZIP contains code and patches; browser captures download separately.'}

    @router.get('/api/runs/{run_id}/files/content')
    def content(run_id: str, request: Request, path: str = Query(max_length=1024),
                revision: str | None = Query(default=None, max_length=64), preview: bool = False, inline: bool = False):
        security.require(request)
        with saved_archive(settings, store, run_id, revision) as (archive, files, _, __):
            info = files.get(path)
            if info is None:
                raise HTTPException(404, 'This file is not available in the saved archive.')
            # Read through ZipExtFile with a hard cap, even if metadata lies.
            limit = MAX_PREVIEW if preview and not inline else MAX_FILE
            with archive.open(info) as stream:
                raw = stream.read(limit + 1)
            if inline:
                if len(raw) > MAX_FILE:
                    raise HTTPException(413, 'This file exceeds the preview size limit.')
                try:
                    _, image, _ = inspect_file(raw)
                except ValueError:
                    raise HTTPException(415, 'This file cannot be previewed as an image.') from None
                if not image:
                    raise HTTPException(415, 'This file cannot be previewed as an image.')
                # Reuse upload decoding/size limits; only re-encoded raster
                # pixels are served inline. Originals keep their download URL.
                return Response(image, media_type='image/jpeg', headers={
                    'Content-Disposition': 'inline', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
                })
            if preview:
                truncated = len(raw) > limit
                try:
                    text = raw[:limit].decode('utf-8', errors='strict')
                except UnicodeDecodeError as exc:
                    # A preview boundary may bisect a UTF-8 character.
                    if truncated and exc.reason == 'unexpected end of data':
                        text = raw[:exc.start].decode('utf-8')
                    else:
                        text = None
                if text is not None and any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                    text = None
                return {'text': text, 'truncated': truncated,
                        'format': 'markdown' if PurePosixPath(path).suffix.lower() in {'.md', '.markdown'} else 'text'}
            if len(raw) > MAX_FILE:
                raise HTTPException(413, 'This file exceeds the download size limit.')
            name = quote(PurePosixPath(path).name, safe='')
            return Response(raw, media_type='application/octet-stream', headers={
                'Content-Disposition': "attachment; filename*=UTF-8''" + name,
                'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
            })

    return router
