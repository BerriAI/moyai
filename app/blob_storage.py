"""Private payload storage; the database owns references, never remote credentials."""
import base64
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from datetime import datetime, timezone
from email.utils import formatdate

import boto3
from botocore.config import Config
from fastapi import HTTPException
from fastapi.responses import FileResponse

from .database import Connection


class ObjectStorage:
    """Immutable content-addressed objects bound to one configured destination."""

    def __init__(self, settings=None):
        self.settings = settings
        self.enabled = bool(settings and settings.object_storage_bucket)
        destination = '\n'.join((settings.object_storage_endpoint, settings.object_storage_bucket,
                                 settings.object_storage_prefix)) if self.enabled else ''
        self.identity = hashlib.sha256(destination.encode()).hexdigest()[:32]
        self._client = None
        self._lock = threading.Lock()

    def client(self):
        if not self.enabled:
            raise HTTPException(503, 'Private file storage is not configured.')
        with self._lock:
            if self._client is None:
                settings = self.settings
                self._client = boto3.session.Session().client(
                    's3', endpoint_url=settings.object_storage_endpoint or None,
                    region_name=settings.object_storage_region,
                    aws_access_key_id=settings.object_storage_access_key_id or None,
                    aws_secret_access_key=settings.object_storage_secret_access_key or None,
                    aws_session_token=settings.object_storage_session_token or None,
                    config=Config(connect_timeout=3, read_timeout=10,
                                  retries={'mode': 'standard', 'total_max_attempts': 3},
                                  s3={'addressing_style': 'path'}))
            return self._client

    def key(self, checksum: str) -> str:
        return self.settings.object_storage_prefix + '/blobs/' + checksum

    def put(self, raw: bytes) -> str:
        checksum = hashlib.sha256(raw).hexdigest()
        try:
            # The key derives from the full bytes: retries can only write the
            # same content. MD5 validates transport on S3 and compatible stores.
            self.client().put_object(
                Bucket=self.settings.object_storage_bucket, Key=self.key(checksum), Body=raw,
                ContentType='application/octet-stream',
                ContentMD5=base64.b64encode(hashlib.md5(raw, usedforsecurity=False).digest()).decode())
        except Exception:
            raise HTTPException(503, 'Private file storage is unavailable. Retry the operation.') from None
        return f's3:{self.identity}:{checksum}'

    def put_file(self, path: Path) -> str:
        """Upload a stable operator-owned file without a database-sized buffer."""
        try:
            with Path(path).open('rb') as stream:
                before = os.fstat(stream.fileno())
                sha = hashlib.sha256()
                md5 = hashlib.md5(usedforsecurity=False)
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    sha.update(chunk)
                    md5.update(chunk)
                stream.seek(0)
                self.client().put_object(
                    Bucket=self.settings.object_storage_bucket, Key=self.key(sha.hexdigest()), Body=stream,
                    ContentLength=before.st_size, ContentType='application/octet-stream',
                    ContentMD5=base64.b64encode(md5.digest()).decode())
                after = os.fstat(stream.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError('File changed during upload')
            return f's3:{self.identity}:{sha.hexdigest()}'
        except Exception:
            raise HTTPException(503, 'Private file storage is unavailable. Retry the operation.') from None

    def read(self, reference: str, limit: int) -> bytes:
        return self._consume(reference, limit, collect=True)

    def verify(self, reference: str, expected_size: int) -> None:
        self._consume(reference, expected_size, collect=False, expected_size=expected_size)

    def _consume(self, reference: str, limit: int, *, collect: bool, expected_size: int | None = None) -> bytes:
        match = re.fullmatch(r's3:([0-9a-f]{32}):([0-9a-f]{64})', reference)
        if not self.enabled or not match or match[1] != self.identity:
            raise HTTPException(503, 'This file requires its original private storage configuration.')
        try:
            result = self.client().get_object(Bucket=self.settings.object_storage_bucket, Key=self.key(match[2]))
            stream = result['Body']
            try:
                if result['ContentLength'] > limit or (expected_size is not None and result['ContentLength'] != expected_size):
                    raise ValueError('Stored object exceeds its bound')
                sha, size, chunks = hashlib.sha256(), 0, []
                while chunk := stream.read(min(1024 * 1024, limit - size + 1)):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError('Stored object exceeds its bound')
                    sha.update(chunk)
                    if collect:
                        chunks.append(chunk)
            finally:
                stream.close()
            if size != result['ContentLength'] or sha.hexdigest() != match[2]:
                raise ValueError('Stored object failed integrity verification')
            return b''.join(chunks)
        except Exception:
            # Missing acknowledged objects are storage faults, not empty files.
            raise HTTPException(503, 'This saved file could not be retrieved or verified. Retry later.') from None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


class TemporaryDownload(FileResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            Path(self.path).unlink(missing_ok=True)


class ArtifactStore:
    """Durable archive/capture inventory with legacy local-file compatibility."""

    def __init__(self, store, directory: Path, *, initialize: bool = True):
        self.store = store
        self.root = directory / 'artifacts'
        if initialize:
            if store.schema_updates:
                initialize_schema(store)

    @staticmethod
    def validate_name(name: str) -> tuple[str, ...]:
        parts = PurePosixPath(name).parts
        if not parts or any(part in {'', '.', '..'} for part in parts) or name != '/'.join(parts) or name.startswith('/') or '\\' in name:
            raise ValueError('Invalid saved file name')
        return parts

    def path(self, name: str) -> Path:
        parts = self.validate_name(name)
        path = self.root.joinpath(*parts)
        # Legacy data and local previews must not follow linked directories.
        if self.root.is_symlink() or any(self.root.joinpath(*parts[:index]).is_symlink() for index in range(1, len(parts) + 1)):
            raise FileNotFoundError(name)
        return path

    def info(self, name: str, conn=None) -> dict | None:
        self.validate_name(name)
        if conn is None:
            with self.store.connect() as current:
                return self.info(name, current)
        row = conn.execute('SELECT * FROM artifact_objects WHERE name=?', (name,)).fetchone()
        if row:
            return dict(row) | {'revision': row['sha256']}
        try:
            path = self.path(name)
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        revision = hashlib.sha256(f'{info.st_mtime_ns}:{info.st_size}'.encode()).hexdigest()[:24]
        return {'name': name, 'size': info.st_size, 'revision': revision, 'reference': '', 'sha256': '',
                'modified': info.st_mtime}

    def listing(self, prefix: str, conn=None) -> list[dict]:
        self.validate_name(prefix.rstrip('/'))
        if conn is None:
            with self.store.connect() as current:
                return self.listing(prefix, current)
        names = set()
        try:
            directory = self.path(prefix.rstrip('/'))
            if directory.is_dir():
                names.update(str(path.relative_to(self.root)) for path in directory.iterdir()
                             if not path.name.startswith('.upload-') and not path.is_symlink() and path.is_file())
        except FileNotFoundError:
            pass
        # A bound string predicate avoids SQL wildcard interpretation of names.
        names.update(row['name'] for row in conn.execute(
            'SELECT name FROM artifact_objects WHERE substr(name,1,?)=?', (len(prefix), prefix)))
        return [info for name in sorted(names) if (info := self.info(name, conn)) is not None]

    def save(self, name: str, raw: bytes, *, immutable: bool = False,
             budget: tuple[str, int] | None = None) -> bool:
        self.validate_name(name)
        if budget is not None:
            prefix, maximum = budget
            self.validate_name(prefix.rstrip('/'))
            if not prefix.endswith('/') or not name.startswith(prefix) or maximum < 0:
                raise ValueError('Invalid saved file budget')
        remote = self.store.objects.enabled

        def admitted(conn):
            current = self.info(name, conn)
            if immutable and current is not None:
                return False
            if not remote and current is not None and current['reference']:
                raise HTTPException(503, 'Restore private file storage before replacing this saved file.')
            if budget is not None:
                used = sum(row['size'] for row in self.listing(prefix, conn) if row['name'] != name)
                if used + len(raw) > maximum:
                    raise HTTPException(413, f'This session has reached its {maximum // (1024 * 1024)} MB saved capture budget.')
            return True

        # Avoid uploading known rejections, then recheck at publication: a
        # cancelled to_thread caller cannot stop an upload already in progress.
        with self.store.connect() as conn:
            if not admitted(conn):
                return False
        reference = self.store.objects.put(raw) if remote else ''
        with self.store.connect() as conn:
            conn.begin_write()
            if not admitted(conn):
                return False
            if reference:
                conn.execute('''INSERT INTO artifact_objects(name,reference,size,sha256,created_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(name) DO UPDATE SET reference=excluded.reference,size=excluded.size,
                        sha256=excluded.sha256,created_at=excluded.created_at''',
                    (name, reference, len(raw), hashlib.sha256(raw).hexdigest(), datetime.now(timezone.utc).isoformat()))
            else:
                path = self.path(name)
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                handle, staging = tempfile.mkstemp(prefix='.upload-', dir=path.parent)
                try:
                    # Local writes are bounded and stay under the same writer
                    # lock as their quota check; staging files are not inventory.
                    with os.fdopen(handle, 'wb') as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(staging, path)
                finally:
                    if os.path.exists(staging):
                        os.unlink(staging)
                self.store.generation += 1
        return True

    def read(self, name: str, limit: int, *, revision: str | None = None) -> bytes:
        info = self.info(name)
        if info is None:
            raise FileNotFoundError(name)
        if revision is not None and info['revision'] != revision:
            raise HTTPException(409, 'The saved archive changed. Refresh the file list.')
        if info['size'] > limit:
            raise HTTPException(413, 'Saved file exceeds the size limit.')
        if info['reference']:
            return self.store.objects.read(info['reference'], limit)
        path = self.path(name)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            actual = os.fstat(stream.fileno())
            current = hashlib.sha256(f'{actual.st_mtime_ns}:{actual.st_size}'.encode()).hexdigest()[:24]
            if not stat.S_ISREG(actual.st_mode):
                raise FileNotFoundError(name)
            if current != info['revision']:
                raise HTTPException(409, 'The saved archive changed. Refresh the file list.')
            if actual.st_size > limit:
                raise HTTPException(413, 'Saved file exceeds the size limit.')
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise HTTPException(413, 'Saved file exceeds the size limit.')
        if len(raw) != info['size']:
            raise HTTPException(409, 'The saved archive changed. Refresh the file list.')
        return raw

    def snapshot_in(self, conn: Connection, source: str, destination: str) -> None:
        self.validate_name(destination)
        if self.info(destination, conn) is not None:
            return
        info = self.info(source, conn)
        if info is None:
            return
        if info['reference']:
            conn.execute('''INSERT INTO artifact_objects(name,reference,size,sha256,created_at)
                SELECT ?,reference,size,sha256,created_at FROM artifact_objects WHERE name=? ON CONFLICT DO NOTHING''', (destination, source))
        else:
            original, target = self.path(source), self.path(destination)
            if not target.exists():
                target.hardlink_to(original)

    def download(self, name: str, filename: str) -> FileResponse:
        # FileResponse preserves conditional and multipart range semantics. Its
        # bounded temporary copy uses ephemeral storage, never DATA_DIR.
        if Path(tempfile.gettempdir()).resolve().is_relative_to(self.store.path.parent.resolve()):
            raise HTTPException(503, 'Configure temporary downloads outside the database directory.')
        info = self.info(name)
        if info is None:
            raise FileNotFoundError(name)
        raw = self.read(name, 20 * 1024 * 1024, revision=info['revision'])
        modified = datetime.fromisoformat(info['created_at']).timestamp() if info['reference'] else info['modified']
        headers = {'ETag': '"' + info['revision'] + '"', 'Last-Modified': formatdate(modified, usegmt=True)}
        handle, temporary = tempfile.mkstemp(prefix='moyai-download-', suffix='.zip')
        try:
            with os.fdopen(handle, 'wb') as stream:
                stream.write(raw)
            return TemporaryDownload(temporary, media_type='application/zip', filename=filename, headers=headers)
        except BaseException:
            os.unlink(temporary)
            raise


def initialize_schema(store):
    store.execute('''CREATE TABLE IF NOT EXISTS artifact_objects (
        name TEXT PRIMARY KEY, reference TEXT NOT NULL, size INTEGER NOT NULL,
        sha256 TEXT NOT NULL, created_at TEXT NOT NULL)''')
