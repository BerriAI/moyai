"""Explicit, immutable output versions using the existing database and blob store.

Chunks are transport units, not file-size limits. A manifest becomes visible only
after every byte is verified under the current execution fence.
"""
from .private_sinks import require_request_owner, require_owner
import base64
import codecs
import hashlib
import json
import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import urlencode, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, Response, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import now
from .library_views import page


class Form(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Publish(Form):
    path: str = Field(description='Existing file inside the sandbox workspace. Its exact bytes are published.')
    name: str = Field(default='', description='Optional relative output path; defaults to the workspace-relative source path.')


class Listing(Form):
    run_id: str = Field(default='', description='Current session by default, or a direct child session.')
    cursor: str = ''
    page_size: int = Field(default=50, ge=1)
    revisions: bool = Field(default=False, description='Include every saved revision instead of only the latest file versions.')


class Read(Form):
    run_id: str = ''
    path: str
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    offset: int = Field(default=0, ge=0, description='Byte offset in the exact artifact revision.')
    length: int = Field(default=65536, ge=1, description='Bytes to read; follow next_offset. Binary data is base64 encoded.')


class Fetch(Form):
    run_id: str = ''
    path: str
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    destination: str = Field(description='New file path inside the sandbox workspace.')


class Transfer(Form):
    action: str
    upload_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    path: str = ''
    size: int = Field(default=0, ge=0)
    sha256: str = ''
    part: int = Field(default=0, ge=0)
    data: str = ''
    parts: int = Field(default=0, ge=0)

    @field_validator('path')
    @classmethod
    def safe_path(cls, value):
        if value and (PurePosixPath(value).is_absolute() or '\\' in value
                      or any(p in {'', '.', '..'} for p in value.split('/'))
                      or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError('Use a relative output path without traversal or control characters.')
        return value


SPECS = {
    'artifact_publish': (Publish, False, 'Publish an existing workspace file as an immutable, verified artifact. Returns a durable revision and authenticated download link. Large/binary files use complete chunked transfer. This is session-visible output; publish only intended deliverables, never credentials or private memory/skill files.'),
    'artifacts_list': (Listing, True, 'List explicitly published artifacts in this session or a direct child. Follow next with cursor to enumerate every output.'),
    'artifact_read': (Read, True, 'Read a byte range of an exact published artifact revision. Full bytes remain retrievable; binary/range-boundary data is base64. For local processing use artifact_fetch.'),
    'artifact_fetch': (Fetch, False, 'Download an exact artifact revision from this session or a direct child into a new workspace file. Verifies the complete size and hash. Does not overwrite an existing file.'),
}
TOOLS = [{'name': name, 'description': description, 'inputSchema': schema.model_json_schema(),
          'annotations': {'readOnlyHint': read, 'idempotentHint': read}}
         for name, (schema, read, description) in SPECS.items()]


def text_page(read, offset, length, size):
    """A byte page ending on a UTF-8 boundary; even a one-byte page advances."""
    raw = read(offset, length)
    try:
        decoder = codecs.getincrementaldecoder('utf-8')()
        text = decoder.decode(raw, final=offset+len(raw) >= size)
        pending = decoder.getstate()[0]
        if pending:
            # Include the rest of the character, never consume an invisible tail.
            extra = read(offset+len(raw), 4-len(pending))
            for byte in extra:
                raw += bytes([byte])
                text += decoder.decode(bytes([byte]))
                if not decoder.getstate()[0]:
                    break
        if decoder.getstate()[0] or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
            text = None
    except UnicodeDecodeError:
        text = None
    end = offset+len(raw)
    return {'text': text, 'truncated': end < size, 'next_offset': end if end < size else None}


class PublishedArtifacts:
    def __init__(self, store):
        self.store = store
        if store.schema_updates:
            initialize_schema(store)

    def active(self, conn, admitted):
        run = conn.execute('SELECT * FROM runs WHERE id=?', (admitted['id'],)).fetchone()
        if (not run or run['deleted_at'] or run['deletion_requested_at']
                or run['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or any(run[key] != admitted[key] for key in ('token_hash', 'active_message_id', 'active_user_id'))):
            raise HTTPException(409, 'The artifact execution capability changed.')
        return run

    def readable(self, run_id):
        run = self.store.run(run_id)
        if not run or run['deleted_at'] or run['deletion_requested_at']:
            raise HTTPException(404, 'Session artifacts are unavailable.')
        return run

    def target(self, admitted, run_id):
        require_owner(self.store, run_id or admitted['id'], admitted.get('private_owner_id', ''))
        with self.store.connect() as conn:
            self.active(conn, admitted)
        target = self.readable(run_id or admitted['id'])
        if target['id'] != admitted['id'] and target['parent_run_id'] != admitted['id']:
            raise HTTPException(403, 'Only this session and its direct child artifacts are accessible.')
        return target['id']

    @staticmethod
    def metadata(row):
        url = f"/api/runs/{row['run_id']}/artifacts/content?" + urlencode({'path': row['path'], 'revision': row['revision']})
        image = PurePosixPath(row['path']).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
        return {key: row[key] for key in ('run_id', 'path', 'revision', 'size', 'media_type', 'created_at', 'source_turn')} | {
            'name': PurePosixPath(row['path']).name, 'workspace_path': row['path'], 'url': url,
            'archive_path': 'published:'+row['path']+':'+row['revision'],
            'preview_url': url+'&preview=true', 'kind': 'image' if image else 'artifact',
            **({'inline_url': url+'&inline=true'} if image else {})}

    def listing(self, run_id, *, revisions=False):
        self.readable(run_id)
        rows = self.store.rows('SELECT * FROM published_artifacts WHERE run_id=? ORDER BY created_at,path,revision', (run_id,))
        if revisions:
            return [self.metadata(row) for row in rows]
        latest = {row['path']: row for row in rows}
        return [self.metadata(latest[path]) for path in sorted(latest)]

    def manifest(self, run_id, path, revision):
        self.readable(run_id)
        rows = self.store.rows('SELECT * FROM published_artifacts WHERE run_id=? AND path=? AND revision=?', (run_id, path, revision))
        if not rows:
            raise HTTPException(404, 'Artifact revision not found.')
        return rows[0]

    def raw(self, part, conn=None):
        if part['reference']:
            raw = self.store.objects.read(part['reference'], part['size'])
        else:
            sql = 'SELECT data FROM artifact_parts WHERE run_id=? AND upload_id=? AND part=?'
            args = (part['run_id'], part['upload_id'], part['part'])
            rows = conn.execute(sql, args).fetchall() if conn else self.store.rows(sql, args)
            if not rows:
                raise HTTPException(503, 'Saved artifact part is missing.')
            raw = bytes(rows[0]['data'])
        if len(raw) != part['size'] or hashlib.sha256(raw).hexdigest() != part['sha256']:
            raise HTTPException(503, 'Saved artifact bytes failed integrity verification.')
        return raw

    def chunks(self, manifest, offset=0, length=None):
        end = manifest['size'] if length is None else min(manifest['size'], offset+length)
        rows = self.store.rows('SELECT run_id,upload_id,part,reference,size,sha256 FROM artifact_parts WHERE run_id=? AND upload_id=? ORDER BY part',
                               (manifest['run_id'], manifest['upload_id']))
        position = 0
        for row in rows:
            stop = position + row['size']
            if stop > offset and position < end:
                raw = self.raw(row)
                yield raw[max(0, offset-position):min(row['size'], end-position)]
            position = stop
        if position != manifest['size']:
            raise HTTPException(503, 'Saved artifact is incomplete.')

    def transfer(self, admitted, arguments):
        body = Transfer.model_validate(arguments)
        with self.store.connect() as conn:
            conn.begin_write()
            run = self.active(conn, admitted)
            upload = conn.execute('SELECT * FROM artifact_uploads WHERE run_id=? AND upload_id=?', (run['id'], body.upload_id)).fetchone()
            if body.action == 'begin':
                if not body.path or len(body.sha256) != 64 or any(c not in '0123456789abcdef' for c in body.sha256):
                    raise HTTPException(422, 'Artifact upload needs a relative path, size, and SHA-256.')
                identity = (run['active_user_id'], run['active_message_id'], run['token_hash'], body.path, body.size, body.sha256)
                if upload and tuple(upload[key] for key in ('actor_id', 'turn_id', 'capability', 'path', 'size', 'sha256')) != identity:
                    raise HTTPException(409, 'This upload identity belongs to a different file or execution.')
                conn.execute('INSERT INTO artifact_uploads VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                             (run['id'], body.upload_id, *identity, now()))
                return {'upload_id': body.upload_id, 'accepted': True}
            if not upload or any(upload[key] != run[current] for key, current in
                                 [('actor_id', 'active_user_id'), ('turn_id', 'active_message_id'), ('capability', 'token_hash')]):
                raise HTTPException(409, 'Begin a new artifact upload under the current execution.')
            if body.action == 'part':
                try:
                    raw = base64.b64decode(body.data, validate=True)
                except ValueError:
                    raise HTTPException(422, 'Invalid artifact part encoding.') from None
                checksum = hashlib.sha256(raw).hexdigest()
                old = conn.execute('SELECT sha256 FROM artifact_parts WHERE run_id=? AND upload_id=? AND part=?',
                                   (run['id'], body.upload_id, body.part)).fetchone()
                if old and old['sha256'] != checksum:
                    raise HTTPException(409, 'This upload part already contains different bytes.')
                if not old:
                    if conn.execute('SELECT 1 FROM published_artifacts WHERE run_id=? AND upload_id=?', (run['id'], body.upload_id)).fetchone():
                        raise HTTPException(409, 'Published artifact parts are immutable.')
                    reference = self.store.objects.put(raw) if self.store.objects.enabled else ''
                    conn.execute('INSERT INTO artifact_parts VALUES(?,?,?,?,?,?,?)',
                                 (run['id'], body.upload_id, body.part, b'' if reference else raw, reference, checksum, len(raw)))
                return {'part': body.part, 'sha256': checksum, 'bytes': len(raw)}
            if body.action != 'commit':
                raise HTTPException(422, 'Choose begin, part, or commit.')
            previous = conn.execute('SELECT * FROM published_artifacts WHERE run_id=? AND path=? AND revision=?',
                                    (run['id'], upload['path'], upload['sha256'])).fetchone()
            if previous:
                return {'saved': True, **self.metadata(previous)}
            rows = conn.execute('SELECT run_id,upload_id,part,reference,size,sha256 FROM artifact_parts WHERE run_id=? AND upload_id=? ORDER BY part', (run['id'], body.upload_id)).fetchall()
            if len(rows) != body.parts or any(row['part'] != number for number, row in enumerate(rows)):
                raise HTTPException(409, 'Artifact upload is incomplete. Retry the missing parts.')
            checksum, size = hashlib.sha256(), 0
            for row in rows:
                raw = self.raw(row, conn)
                checksum.update(raw)
                size += len(raw)
            if size != upload['size'] or checksum.hexdigest() != upload['sha256']:
                raise HTTPException(409, 'Artifact size or hash does not match the complete upload.')
            manifest = {'run_id': run['id'], 'path': upload['path'], 'revision': upload['sha256'], 'upload_id': body.upload_id,
                        'size': size, 'media_type': mimetypes.guess_type(upload['path'])[0] or 'application/octet-stream',
                        'created_at': now(), 'source_turn': run['active_message_id']}
            conn.execute('INSERT INTO published_artifacts VALUES(?,?,?,?,?,?,?,?)', tuple(manifest.values()))
            conn.execute('INSERT INTO events(run_id,kind,message,data,created_at) VALUES(?,?,?,?,?)',
                         (run['id'], 'artifact', 'Published '+upload['path'],
                          json.dumps({'turn_id': run['active_message_id'], 'path': upload['path'],
                                      'revision': upload['sha256'], 'size': size}), manifest['created_at']))
            return {'saved': True, **self.metadata(manifest)}

    def call(self, admitted, name, arguments):
        schema = SPECS[name][0]
        body = schema.model_validate(arguments)
        if name in {'artifact_publish', 'artifact_fetch'}:
            raise HTTPException(422, 'Use this tool through the sandbox workspace bridge to transfer local files.')
        target = self.target(admitted, body.run_id)
        if name == 'artifacts_list':
            items = self.listing(target, revisions=body.revisions)
            selected, cursor = page(items, body.cursor, body.page_size, [target, items])
            return {'artifacts': selected, **cursor}
        manifest = self.manifest(target, body.path, body.revision)
        raw = b''.join(self.chunks(manifest, body.offset, body.length))
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            text = None
        end = body.offset + len(raw)
        return {**self.metadata(manifest), 'offset': body.offset, 'bytes': len(raw), 'text': text,
                'data_base64': base64.b64encode(raw).decode(), 'next_offset': end if end < manifest['size'] else None}

    def routes(self, security):
        router = APIRouter()
        @router.get('/api/runs/{run_id}/artifacts/content')
        def content(run_id: str, request: Request, path: str, revision: str, preview: bool = False, inline: bool = False, offset: int = 0, length: int = 65536):
            require_request_owner(self.store, security, request, run_id)
            manifest = self.manifest(run_id, path, revision)
            if offset < 0 or length < 1:
                raise HTTPException(422, 'Use a nonnegative byte offset and positive length.')
            if inline:
                from .attachments import inspect_file, IMAGE_PREVIEW_BYTES
                # This is an image decoder safety bound, not a file retention or
                # download limit. The original remains available at its exact URL.
                if manifest['size'] > IMAGE_PREVIEW_BYTES:
                    raise HTTPException(415, 'Download this image to view it at its original size.')
                try:
                    _, pixels, _ = inspect_file(b''.join(self.chunks(manifest)))
                except ValueError:
                    pixels = None
                if not pixels:
                    raise HTTPException(415, 'This file cannot be previewed as an image.')
                return Response(pixels, media_type='image/jpeg', headers={'Cache-Control': 'no-store',
                                'Content-Disposition': 'inline', 'X-Content-Type-Options': 'nosniff'})
            if preview:
                return JSONResponse({**text_page(lambda start, count: b''.join(self.chunks(manifest, start, count)), offset, length, manifest['size']),
                        'format': 'markdown' if path.endswith(('.md', '.markdown')) else 'text'}, headers={'Cache-Control': 'no-store'})
            headers = {'Content-Disposition': "attachment; filename*=UTF-8''"+quote(PurePosixPath(path).name, safe=''),
                       'Content-Length': str(manifest['size']), 'ETag': '"'+revision+'"', 'X-Content-Type-Options': 'nosniff',
                       'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store'}
            value = request.headers.get('range')
            if value and request.headers.get('if-range', headers['ETag']) == headers['ETag']:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
                size = manifest['size']
                if not match or not any(match.groups()) or not size:
                    raise HTTPException(416, 'Invalid byte range.', headers={'Content-Range': f'bytes */{size}'})
                start = int(match[1]) if match[1] else max(0, size-int(match[2]))
                end = min(size-1, int(match[2])) if match[1] and match[2] else size-1
                if start > end or start >= size:
                    raise HTTPException(416, 'Invalid byte range.', headers={'Content-Range': f'bytes */{size}'})
                headers.update({'Content-Range': f'bytes {start}-{end}/{size}', 'Content-Length': str(end-start+1)})
                return StreamingResponse(self.chunks(manifest, start, end-start+1), status_code=206,
                                         media_type='application/octet-stream', headers=headers)
            return StreamingResponse(self.chunks(manifest), media_type='application/octet-stream', headers=headers)
        return router


def initialize_schema(store):
    with store.connect() as conn:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS artifact_uploads (
                run_id TEXT NOT NULL, upload_id TEXT NOT NULL, actor_id TEXT NOT NULL, turn_id INTEGER,
                capability TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(run_id,upload_id));
            CREATE TABLE IF NOT EXISTS artifact_parts (
                run_id TEXT NOT NULL, upload_id TEXT NOT NULL, part INTEGER NOT NULL, data BLOB NOT NULL,
                reference TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL,
                PRIMARY KEY(run_id,upload_id,part));
            CREATE TABLE IF NOT EXISTS published_artifacts (
                run_id TEXT NOT NULL, path TEXT NOT NULL, revision TEXT NOT NULL, upload_id TEXT NOT NULL,
                size INTEGER NOT NULL, media_type TEXT NOT NULL, created_at TEXT NOT NULL, source_turn INTEGER,
                PRIMARY KEY(run_id,path,revision));
        ''')
