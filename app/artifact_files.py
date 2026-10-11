"""Authenticated, streamed access to individual files in saved result archives.

Never extract agent-controlled paths onto the server. A revision pins previews
and downloads to the archive the user selected, rather than a later overwrite.
"""
from collections import Counter, OrderedDict
from concurrent.futures import Future, TimeoutError
from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import re
import stat
import threading
from typing import Literal
from urllib.parse import quote, urlencode
import zipfile
import zlib

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from . import captures
from .attachments import inspect_file, IMAGE_PREVIEW_BYTES
from .published_artifacts import text_page

PREVIEW_PAGE = 128 * 1024


class ArchiveCatalog:
    """Bounded, per-app metadata cache; never retains private archive bytes.

    A current manifest lookup and authorization precede every use. Revisions
    pin entries, and concurrent readers share one verified archive download.
    Each process owns its cache; no cross-worker coordination is implied.
    """

    def __init__(self, max_bytes=8 * 1024 * 1024, max_entries=32):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.entries, self.pending = OrderedDict(), {}
        self.size = 0
        self.lock = threading.Lock()

    def read(self, key, load):
        with self.lock:
            if key in self.entries:
                encoded = self.entries[key]
                self.entries.move_to_end(key)
                return json.loads(encoded)
            future = self.pending.get(key)
            owner = future is None
            if owner:
                if len(self.pending) >= 16:
                    raise HTTPException(503, 'Saved files are busy. Retry in a moment.')
                future = self.pending[key] = Future()
        if not owner:
            try:
                return json.loads(future.result(timeout=30))
            except TimeoutError:
                raise HTTPException(503, 'The saved workspace is still loading. Retry in a moment.') from None
        try:
            value = load()
            encoded = json.dumps(value).encode()
            with self.lock:
                if len(encoded) <= self.max_bytes:
                    self.entries[key] = encoded
                    self.size += len(encoded)
                    while len(self.entries) > self.max_entries or self.size > self.max_bytes:
                        _, removed = self.entries.popitem(last=False)
                        self.size -= len(removed)
            future.set_result(encoded)
            return value
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)


def safe_member(info):
    name = info.filename
    mode = stat.S_IFMT(info.external_attr >> 16)
    return (bool(name) and not info.is_dir()
            and not name.startswith('/') and '\\' not in name and ':' not in name
            and all(ord(c) >= 32 and ord(c) != 127 for c in name)
            and all(part not in {'', '.', '..'} for part in name.split('/'))
            and mode in {0, stat.S_IFREG} and not info.flag_bits & 1
            and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            and info.file_size >= 0)


@contextmanager
def saved_archive(settings, store, run_id, revision=None):
    if not re.fullmatch(r'[0-9a-f]{32}', run_id) or not store.run(run_id):
        raise HTTPException(404, 'Session not found.')
    name = run_id + '.zip'
    try:
        info = store.artifacts.info(name)
        if info is None:
            raise FileNotFoundError(name)
        current = info['revision']
        if revision is not None and revision != current:
            raise HTTPException(409, 'The saved files changed. Close and reopen Files to view the latest version.')
        with store.artifacts.open(name, revision=current) as source, zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
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


def routes(settings, store, security, published=None):
    router = APIRouter()
    catalogs = ArchiveCatalog()

    @router.get('/api/runs/{run_id}/files')
    def list_files(run_id: str, request: Request, view: Literal['all', 'media'] = 'all'):
        security.require(request)
        if not re.fullmatch(r'[0-9a-f]{32}', run_id) or not store.run(run_id):
            raise HTTPException(404, 'Session not found.')
        media = captures.listing(settings, run_id, store=store)
        outputs = published.listing(run_id) if published else []
        media = outputs + media
        info = store.artifacts.info(run_id + '.zip')
        if view == 'media':
            # Captures and explicitly published outputs have their own manifests;
            # listing them must never open the potentially multi-GB recovery ZIP.
            return {'revision': 'published', 'files': media, 'limited': False,
                    'archive_pending': info is not None,
                    'archive_revision': info['revision'] if info else None,
                    'note': 'Published files and saved captures.'}
        if media and info is None:
            return {'revision': 'published', 'files': media, 'limited': False, 'note': 'Verified artifact versions and saved browser captures.'}
        if info is None:
            raise HTTPException(404, 'No saved files are available yet.')

        def scan():
            with saved_archive(settings, store, run_id, info['revision']) as (_, files, revision, skipped):
                ordered = sorted(files.values(), key=lambda i: (i.filename.startswith('new-files/'), i.filename.casefold()))
                return {'revision': revision, 'files': [file_info(run_id, i, revision) for i in ordered],
                        'limited': bool(skipped)}

        catalog = catalogs.read((run_id, info['revision'], info['reference']), scan)
        explicit_paths = {item['workspace_path'] for item in outputs}
        return {**catalog, 'files': media + [item for item in catalog['files'] if item['workspace_path'] not in explicit_paths],
                'note': 'Latest saved files. The workspace ZIP contains code and patches; browser captures download separately.'}

    @router.get('/api/runs/{run_id}/files/content')
    def content(run_id: str, request: Request, path: str,
                revision: str | None = Query(default=None, max_length=64), preview: bool = False, inline: bool = False, offset: int = 0, length: int = PREVIEW_PAGE):
        security.require(request)
        with saved_archive(settings, store, run_id, revision) as (archive, files, pinned, __):
            info = files.get(path)
            if info is None:
                raise HTTPException(404, 'This file is not available in the saved archive.')
            if offset < 0 or length < 1:
                raise HTTPException(422, 'Use a nonnegative byte offset and positive length.')
            if inline:
                if info.file_size > IMAGE_PREVIEW_BYTES:
                    raise HTTPException(415, 'Download this image to view it at its original size.')
                with archive.open(info) as stream:
                    raw = stream.read()
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
                with archive.open(info) as stream:
                    def read(start, count):
                        stream.seek(start)
                        return stream.read(count)
                    result = text_page(read, offset, length, info.file_size)
                return {**result, 'format': 'markdown' if PurePosixPath(path).suffix.lower() in {'.md', '.markdown'} else 'text'}
            name = quote(PurePosixPath(path).name, safe='')
        # The response owns its archive context for the entire stream.
        def chunks():
            with saved_archive(settings, store, run_id, pinned) as (archive, files, _, __):
                with archive.open(files[path]) as stream:
                    yield from iter(lambda: stream.read(1024 * 1024), b'')
        return StreamingResponse(chunks(), media_type='application/octet-stream', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + name,
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
        })

    return router
