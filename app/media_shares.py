"""Explicit, revocable bearer access to immutable session media snapshots."""
import hashlib
import hmac
from io import BytesIO
import re
import secrets
from urllib.parse import urlsplit
import warnings

from fastapi import APIRouter, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from . import captures
from .db import now

MAX_FILE = captures.MAX_FILE
MAX_SESSION_BYTES = captures.MAX_TOTAL
MAX_SESSION_SHARES = 128
NOTICE = 'Anyone with this link can read this selected media until revoked. Copies and external caches cannot be recalled.'


class ListMedia(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=50, ge=1, le=100)


class ShareMedia(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source: str = Field(pattern=r'^(capture:[A-Za-z0-9_-]{1,100}\.(png|webm)|attachment:[0-9a-f]{32})$')
    revision: str = Field(pattern=r'^[0-9a-f]{24,64}$')
    request_key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,100}$')


class RevokeMedia(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    share_id: str = Field(pattern=r'^[0-9a-f]{32}$')


TOOLS = {
    'media_list': (ListMedia, 'List saved captures, image/video attachments and media share receipts in the current session. Does not share anything. Use the returned source and revision with media_share.'),
    'media_share': (ShareMedia, 'Explicitly share one selected saved capture or attachment from this session as an immutable snapshot. Anyone with the link can read it until revoked. Requires private storage and an externally reachable public origin. Reuse the same request_key for retries; changing selection conflicts. Returns image Markdown or a video link, without publishing it anywhere.'),
    'media_revoke': (RevokeMedia, 'Permanently revoke a media share belonging to this session. Idempotent. Stops future reads at Moyai; cannot recall downloaded copies or external caches.'),
}


def _element(raw, position, end):
    def vint(pos, identifier=False):
        if pos >= end or not raw[pos]:
            raise ValueError
        width = 9 - raw[pos].bit_length()
        if width > (4 if identifier else 8) or pos + width > end:
            raise ValueError
        value = int.from_bytes(raw[pos:pos + width])
        if not identifier:
            value &= (1 << (7 * width)) - 1
            if value == (1 << (7 * width)) - 1:
                value = None
        return value, pos + width
    kind, position = vint(position, True)
    size, start = vint(position)
    stop = end if size is None else start + size
    if stop > end:
        raise ValueError
    return kind, start, stop, size is None


def validated_mime(raw):
    if not raw or len(raw) > MAX_FILE:
        raise HTTPException(413, 'Media must be nonempty and at most 32 MiB.')
    if raw.startswith(b'\x1aE\xdf\xa3'):
        try:
            kind, start, stop, unknown = _element(raw, 0, len(raw))
            if unknown or stop > 4096:
                raise ValueError
            doc_types = []
            pos = start
            while pos < stop:
                kind, a, pos, unknown = _element(raw, pos, stop)
                if unknown:
                    raise ValueError
                if kind == 0x4282:
                    doc_types.append(raw[a:pos])
            if doc_types != [b'webm']:
                raise ValueError
            kind, start, end, _ = _element(raw, stop, len(raw))
            if kind != 0x18538067 or end != len(raw):
                raise ValueError
            kinds, pos, video = set(), start, False
            while pos < end:
                kind, body, pos, unknown = _element(raw, pos, end)
                if body == pos and kind in {0x1549A966, 0x1654AE6B, 0x1F43B675}:
                    raise ValueError
                if kind == 0x1654AE6B:
                    while body < pos:
                        entry, fields, body, indefinite = _element(raw, body, pos)
                        if indefinite:
                            raise ValueError
                        if entry == 0xAE:
                            while fields < body:
                                field, value, fields, indefinite = _element(raw, fields, body)
                                if indefinite:
                                    raise ValueError
                                if field == 0x86 and raw[value:fields] in {b'V_VP8', b'V_VP9', b'V_AV1'}:
                                    video = True
                kinds.add(kind)
                if unknown and kind != 0x1F43B675:
                    raise ValueError
            if not video or not {0x1549A966, 0x1654AE6B, 0x1F43B675} <= kinds:
                raise ValueError
            return 'video/webm'
        except ValueError:
            raise HTTPException(415, 'Media is not a supported WebM container.') from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format not in {'PNG', 'JPEG', 'GIF', 'WEBP'} or image.width * image.height > 25_000_000:
                    raise ValueError
                mime = Image.MIME[image.format]
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
        return mime
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(415, 'Use a valid PNG, JPEG, GIF, WebP image or WebM video under 25 megapixels.') from None


class MediaShares:
    def __init__(self, store, settings, security):
        self.store, self.settings, self.security = store, settings, security
        with store.connect() as conn:
            conn.executescript('''CREATE TABLE IF NOT EXISTS media_shares (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                request_key TEXT NOT NULL, source TEXT NOT NULL, revision TEXT NOT NULL,
                token_hash TEXT NOT NULL, token_ciphertext TEXT NOT NULL, origin TEXT NOT NULL,
                reference TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                mime TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT NOT NULL DEFAULT '',
                UNIQUE(run_id, request_key));
                CREATE INDEX IF NOT EXISTS idx_media_shares_run ON media_shares(run_id);''')

    def tools(self):
        return [{'name': name, 'description': spec[1], 'inputSchema': spec[0].model_json_schema(),
                 'annotations': {'readOnlyHint': name == 'media_list', 'idempotentHint': name == 'media_list'}} for name, spec in TOOLS.items()]

    def call(self, run, name, arguments):
        args = TOOLS[name][0].model_validate(arguments)
        return getattr(self, name.removeprefix('media_'))(run, args)

    def active(self, conn, run):
        current = conn.execute('SELECT deleted_at,status,token_hash,active_message_id,active_user_id FROM runs WHERE id=?', (run['id'],)).fetchone()
        if (not current or current['deleted_at'] or current['status'] not in {'running', 'reconnecting', 'awaiting_approval'}
                or not current['token_hash'] or not hmac.compare_digest(current['token_hash'], run['token_hash'])):
            raise HTTPException(401, 'Run capability expired or invalid.')
        if any(current[field] != run.get(field) for field in ('active_message_id', 'active_user_id')):
            raise HTTPException(409, 'The requester or turn changed. Retry from the current session.')

    def origin(self):
        value = self.settings.public_url.rstrip('/')
        try:
            url = urlsplit(value)
            valid = (url.hostname and url.netloc and not url.username and not url.password
                     and not url.path and not url.query and not url.fragment and url.port != 0
                     and (url.scheme == 'https' or (url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'}))
                     and not re.search(r'[\s<>"\\]', value))
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(503, 'Configure PUBLIC_URL as an externally reachable HTTPS origin before sharing media (HTTP loopback is for local testing only).')
        return value

    def receipt(self, row):
        result = {key: row[key] for key in ('id', 'request_key', 'source', 'revision', 'size', 'sha256', 'mime', 'created_at', 'revoked_at')}
        result['status'] = 'revoked' if row['revoked_at'] else 'shared'
        result['notice'] = NOTICE
        if not row['revoked_at']:
            try:
                token = self.security.decrypt(row['token_ciphertext'])
            except Exception:
                raise HTTPException(503, 'Restore the original workspace encryption key to recover media share links.') from None
            result['url'] = f"{row['origin']}/media/{row['id']}?token={token}"
            result['markdown'] = ('![Shared image]' if row['mime'].startswith('image/') else '[Shared video]') + f"({result['url']})"
        return result

    def list(self, run, args):
        with self.store.connect() as conn:
            self.active(conn, run)
            shares = conn.execute('SELECT * FROM media_shares WHERE run_id=? ORDER BY created_at,id', (run['id'],)).fetchall()
        prefix = captures.directory(self.settings, run['id']).name + '/'
        sources = [{'source': 'capture:' + row['name'][len(prefix):], 'revision': row['revision'], 'size': row['size']}
                   for row in self.store.artifacts.listing(prefix) if captures.valid_name(row['name'][len(prefix):])]
        for row in self.store.attachments.for_run(run['id'], run.get('active_message_id')):
            if row['media_type'].startswith('image/') or row['name'].lower().endswith('.webm'):
                sources.append({'source': 'attachment:' + row['id'], 'revision': row['sha256'], 'size': row['size']})
        page = sources[args.offset:args.offset + args.limit]
        return {'sources': page, 'next_offset': args.offset + args.limit if args.offset + args.limit < len(sources) else None,
                'shares': [self.receipt(row) for row in shares], 'notice': NOTICE}

    def source_bytes(self, run, args):
        kind, name = args.source.split(':', 1)
        if kind == 'capture':
            path = captures.directory(self.settings, run['id']).name + '/' + name
            try:
                raw = self.store.artifacts.read(path, MAX_FILE, revision=args.revision)
                current = self.store.artifacts.info(path)
                if not current or current['revision'] != args.revision:
                    raise HTTPException(409, 'The selected media changed. Refresh media_list.')
                return raw
            except FileNotFoundError:
                raise HTTPException(404, 'Saved capture not found in this session.') from None
        row = self.store.attachments.broker_row(run, name)
        if row['sha256'] != args.revision:
            raise HTTPException(409, 'The selected media changed. Refresh media_list.')
        if row['size'] > MAX_FILE:
            raise HTTPException(413, 'Media must be at most 32 MiB.')
        raw = self.store.attachments.payload(row)
        if hashlib.sha256(raw).hexdigest() != args.revision:
            raise HTTPException(503, 'Attachment storage integrity check failed.')
        return raw

    def prior(self, conn, run, args):
        row = conn.execute('SELECT * FROM media_shares WHERE run_id=? AND request_key=?', (run['id'], args.request_key)).fetchone()
        if row and (row['source'], row['revision']) != (args.source, args.revision):
            raise HTTPException(409, 'This request_key already selected different media. Use its original selection or a new key.')
        return row

    def quota(self, conn, run, size):
        for clause, params, maximum, count in [
            ('WHERE run_id=?', (run['id'],), MAX_SESSION_BYTES, MAX_SESSION_SHARES),
            ('', (), self.settings.media_share_storage_limit_mb * 1024 * 1024, self.settings.media_share_receipt_limit),
        ]:
            used = conn.execute(f'SELECT count(*) AS n,coalesce(sum(size),0) AS bytes FROM media_shares {clause}', params).fetchone()
            if used['n'] >= count or used['bytes'] + size > maximum:
                raise HTTPException(413, 'Media share lifetime quota reached. Revocation does not reset this quota; contact the workspace administrator.')

    def share(self, run, args):
        with self.store.connect() as conn:
            self.active(conn, run)
            previous = self.prior(conn, run, args)
            if previous:
                return self.receipt(previous)
            self.quota(conn, run, 0)
        origin = self.origin()
        if not self.store.objects.enabled:
            raise HTTPException(503, 'Configure private OBJECT_STORAGE_BUCKET and its access before sharing media. The public origin must allow anonymous GET/HEAD to /media/.')
        raw = self.source_bytes(run, args)
        mime = validated_mime(raw)
        with self.store.connect() as conn:
            self.quota(conn, run, len(raw))
        reference = self.store.objects.put(raw)
        token, share_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.active(conn, run)
            previous = self.prior(conn, run, args)
            if previous:
                return self.receipt(previous)
            self.quota(conn, run, len(raw))
            if args.source.startswith('attachment:'):
                self.store.attachments.broker_row(run, args.source.split(':', 1)[1], conn=conn)
            conn.execute('''INSERT INTO media_shares
                (id,run_id,request_key,source,revision,token_hash,token_ciphertext,origin,reference,size,sha256,mime,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (share_id, run['id'], args.request_key, args.source, args.revision,
                 hashlib.sha256(token.encode()).hexdigest(), self.security.encrypt(token), origin, reference,
                 len(raw), hashlib.sha256(raw).hexdigest(), mime, now()))
            return self.receipt(conn.execute('SELECT * FROM media_shares WHERE id=?', (share_id,)).fetchone())

    def revoke(self, run, args):
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            self.active(conn, run)
            row = conn.execute('SELECT * FROM media_shares WHERE id=? AND run_id=?', (args.share_id, run['id'])).fetchone()
            if row is None:
                raise HTTPException(404, 'Media share not found in this session.')
            conn.execute("UPDATE media_shares SET revoked_at=? WHERE id=? AND revoked_at=''", (now(), args.share_id))
            return self.receipt(conn.execute('SELECT * FROM media_shares WHERE id=?', (args.share_id,)).fetchone())

    def public_row(self, share_id, token):
        if not re.fullmatch(r'[0-9a-f]{32}', share_id) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise HTTPException(404, 'Media not found.')
        rows = self.store.rows('''SELECT s.* FROM media_shares s JOIN runs r ON r.id=s.run_id
            WHERE s.id=? AND s.revoked_at='' AND r.deleted_at='' ''', (share_id,))
        if not rows or not hmac.compare_digest(rows[0]['token_hash'], hashlib.sha256(token.encode()).hexdigest()):
            raise HTTPException(404, 'Media not found.')
        return rows[0]

    def routes(self):
        router = APIRouter()

        @router.api_route('/media/{share_id}', methods=['GET', 'HEAD'])
        def content(share_id: str, request: Request):
            token = request.query_params.get('token', '')
            row = self.public_row(share_id, token)
            raw = self.store.objects.read(row['reference'], row['size'])
            if len(raw) != row['size'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise HTTPException(503, 'Media storage integrity check failed.')
            self.public_row(share_id, token)
            response = captures.bytes_response(raw, row['mime'], 'media.' + row['mime'].split('/')[-1], request)
            if request.method == 'HEAD':
                response.body = b''
            return response

        return router
